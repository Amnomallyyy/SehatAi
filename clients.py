#!/usr/bin/env python3
"""
clients.py – Thin API wrappers for OCR.space, Gemini (Batch), Groq, Jina, and Supabase.

UPDATED:
- OCR.space: 1MB limit enforced with aggressive compression
- Gemini: Uses Batch API (asynchronous) for files >1MB or when OCR.space fails
- All clients include retry logic and environment variable configuration
- Supabase upload: Auto-detects MIME type for PNG, JPG, JPEG, PDF
"""

import os
import base64
import io
import json
import re
import time
import mimetypes
from typing import Optional, Tuple, Dict, Any, List
from dotenv import load_dotenv
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from PIL import Image
from supabase import create_client, Client

load_dotenv()


# ============================================================
# Helper: Aggressive downscaling for OCR.space 1MB limit
# ============================================================
def downscale_image_for_ocr(image_bytes: bytes, target_mb: float = 0.8) -> bytes:
    """
    Aggressively compress image to stay under OCR.space 1MB limit.
    Target: 800KB (0.8MB) for safety margin.
    """
    target_bytes = int(target_mb * 1024 * 1024)

    # If already small enough, return as-is
    if len(image_bytes) <= target_bytes:
        return image_bytes

    try:
        img = Image.open(io.BytesIO(image_bytes))

        # Convert to RGB if needed
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')

        # Start with aggressive resize
        max_dimension = 1200  # Much smaller for 1MB limit
        img.thumbnail((max_dimension, max_dimension))

        # Try progressively lower quality until size fits
        for quality in [75, 60, 45, 30]:
            buffer = io.BytesIO()
            img.save(buffer, format='JPEG', quality=quality, optimize=True)
            compressed = buffer.getvalue()
            if len(compressed) <= target_bytes:
                return compressed

        # If still too large, reduce dimensions further
        img.thumbnail((800, 800))
        buffer = io.BytesIO()
        img.save(buffer, format='JPEG', quality=50, optimize=True)
        return buffer.getvalue()

    except Exception as e:
        # If compression fails, return original (will likely fail, but fallback handles it)
        return image_bytes


# ============================================================
# Helper: Check if file should use OCR.space or Gemini
# ============================================================
def should_use_ocrspace(file_bytes: bytes, file_type: str = "JPG") -> bool:
    """
    Decision logic: Use OCR.space if:
    - File size < 1MB
    - File type is JPG or PNG (not PDF)
    - (Quality checks could be added here later)
    """
    # 1. File size check
    if len(file_bytes) > 1 * 1024 * 1024:  # 1MB
        return False

    # 2. File type (PDFs go to Gemini)
    if file_type.lower() == "pdf":
        return False

    # 3. For JPG/PNG under 1MB, try OCR.space
    return True


# ============================================================
# 1. OCR.space Client (with 1MB limit handling)
# ============================================================
class OCRspaceClient:
    """
    Client for OCR.space API (free tier ~25k requests/month, 1MB file limit).
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("OCRSPACE_API_KEY")
        if not self.api_key:
            raise ValueError("OCRSPACE_API_KEY not set in environment")
        self.endpoint = "https://api.ocr.space/parse/image"
        self.max_size_mb = 0.8  # Target 800KB for safety
        print("[OK] OCRspaceClient initialized (1MB limit enforced)")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((requests.exceptions.RequestException,))
    )
    def extract(self, image_bytes: bytes, file_type: str = "JPG") -> Tuple[str, float, str]:
        """
        Send image bytes to OCR.space and return (raw_text, confidence, engine).
        """
        print("[INFO] Sending OCR request to OCR.space...")

        # Aggressively compress for 1MB limit
        image_bytes = downscale_image_for_ocr(image_bytes, self.max_size_mb)

        # If still too large, raise error to trigger Gemini fallback
        if len(image_bytes) > 1 * 1024 * 1024:
            raise RuntimeError(f"Image too large ({len(image_bytes)/1024/1024:.2f}MB) for OCR.space free tier (1MB limit)")

        # Prepare payload
        files = {
            'file': (f'image.{file_type.lower()}', image_bytes, f'image/{file_type.lower()}')
        }
        data = {
            'apikey': self.api_key,
            'language': 'eng',
            'isOverlayRequired': False,
            'OCREngine': 2,  # 2 = standard engine (free)
            'scale': True,
        }
        response = requests.post(self.endpoint, files=files, data=data, timeout=30)
        response.raise_for_status()
        result = response.json()

        # Parse response
        if result.get('IsErroredOnProcessing'):
            error_msg = result.get('ErrorMessage', ['Unknown error'])[0]
            raise RuntimeError(f"OCR.space error: {error_msg}")

        parsed_results = result.get('ParsedResults', [])
        if not parsed_results:
            raise RuntimeError("No parsed results returned from OCR.space")

        text = parsed_results[0].get('ParsedText', '')
        confidence = 100.0 if parsed_results[0].get('FileParseExitCode') == 1 else 50.0

        print("[OK] OCR.space returned text")
        return text, confidence, "ocrspace"

    def __repr__(self):
        return f"OCRspaceClient(api_key='{self.api_key[:4]}...')"


# ============================================================
# 2. Gemini Client (Batch API for OCR fallback)
# ============================================================
class GeminiClient:
    """
    Client for Google Gemini API (Batch API for asynchronous OCR).
    Uses Gemini 3.5 Flash (available via Batch API, ~1,500 requests/day free).
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY not set in environment")
        self.model = "gemini-3.5-flash"
        self.batch_endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:batchGenerateContent"
        self.get_batch_endpoint = "https://generativelanguage.googleapis.com/v1beta/batchJobs"
        print("[OK] GeminiClient initialized (using gemini-3.5-flash Batch API)")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((requests.exceptions.RequestException,))
    )
    def submit_batch(self, requests_list: List[Dict]) -> str:
        """
        Submit a batch of documents to Gemini Batch API.
        requests_list: [{"image_bytes": b'...', "file_type": "JPG"}, ...]
        Returns: batch_job_id (string)
        """
        print(f"[INFO] Submitting batch of {len(requests_list)} documents to Gemini 3.5 Flash Batch API...")

        # Build contents
        contents = []
        for req in requests_list:
            base64_image = base64.b64encode(req['image_bytes']).decode('utf-8')
            mime_type = f"image/{req.get('file_type', 'JPG').lower()}"
            if req.get('file_type', '').lower() == "pdf":
                mime_type = "application/pdf"
            contents.append({
                "parts": [
                    {"text": "Transcribe this medical document faithfully. Preserve all numbers, tables, and handwriting. Do not summarize. Return only the raw text."},
                    {"inline_data": {"mime_type": mime_type, "data": base64_image}}
                ]
            })

        payload = {"requests": contents}
        params = {"key": self.api_key}
        response = requests.post(self.batch_endpoint, json=payload, params=params, timeout=60)
        response.raise_for_status()
        result = response.json()

        # Extract batch job ID
        batch_id = result.get('batchJobId') or result.get('id') or result.get('name', '').split('/')[-1]
        print(f"[OK] Batch job submitted: {batch_id}")
        return batch_id

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((requests.exceptions.RequestException,))
    )
    def get_batch_result(self, batch_job_id: str) -> Optional[List[Tuple[str, float, str]]]:
        """
        Poll Gemini Batch API for results.
        Returns list of (text, confidence, engine) once complete, or None if still processing.
        """
        url = f"{self.get_batch_endpoint}/{batch_job_id}"
        params = {"key": self.api_key}
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        result = response.json()

        state = result.get('state', '').lower()
        if state != 'completed':
            return None  # Still processing or failed

        results = []
        for resp in result.get('responses', []):
            try:
                text = resp['candidates'][0]['content']['parts'][0]['text']
                results.append((text, 85.0, "gemini"))
            except (KeyError, IndexError):
                results.append(("", 0.0, "gemini_error"))
        return results

    def __repr__(self):
        return f"GeminiClient(api_key='{self.api_key[:4]}...')"


# ============================================================
# 3. NIvidia Client (classification + structuring)
# ============================================================
# ============================================================
# 3. NVIDIA Client (replaces Groq for classification + structuring)
# ============================================================
class NVIDIAClient:
    """
    Client for NVIDIA API (Nemotron-3-Super-120B – free tier).
    OpenAI-compatible endpoint with 40 RPM free tier.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("NVIDIA_API_KEY")
        if not self.api_key:
            raise ValueError("NVIDIA_API_KEY not set in environment")
        self.base_url = "https://integrate.api.nvidia.com/v1"
        self.model = "nvidia/nemotron-3-super-120b-a12b"  # 120B reasoning model
        self.temperature = 0.0
        self.max_tokens = 16384
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        print("[OK] NVIDIAClient initialized (using nemotron-3-super-120b)")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((requests.exceptions.RequestException,))
    )
    def _chat(self, messages: List[Dict[str, str]]) -> str:
        """Internal method to send messages and get response."""
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        response = requests.post(url, json=payload, headers=self.headers, timeout=60)
        response.raise_for_status()
        result = response.json()
        return result['choices'][0]['message']['content']

    def classify_medical(self, text: str) -> Dict[str, Any]:
        """
        Classify if the text is a medical document.
        Returns: {"is_medical": bool, "reason": str}
        """
        print("[INFO] Sending classification request to NVIDIA...")
        prompt = (
            "You are a medical document classifier. Given the following text, determine if it is a medical document "
            "(e.g., lab report, prescription, imaging report, discharge summary, consultation note). "
            "Respond with a JSON object containing 'is_medical' (boolean) and 'reason' (string explaining your decision). "
            "Return ONLY valid JSON, no markdown or commentary.\n\n"
            f"Text: {text[:3000]}"
        )
        messages = [{"role": "user", "content": prompt}]
        response_text = self._chat(messages)
        try:
            result = json.loads(response_text)
        except json.JSONDecodeError:
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
            else:
                raise RuntimeError("NVIDIA response not valid JSON")
        print("[OK] Classification result received")
        return result

    def structure_document(self, text: str) -> Dict[str, Any]:
        """
        Structure the raw OCR text into the JSON schema.
        Returns: dict matching StructuredDocument model.
        """
        print("[INFO] Sending structuring request to NVIDIA...")
        system_prompt = (
            "You are a medical data extraction assistant. Extract the following information from the text:\n"
            "- category: one of ['blood_test','prescription','imaging_report','discharge_summary','consultation_note','unknown']\n"
            "- document_date: the date on the document in YYYY-MM-DD format (or null if not found)\n"
            "- extracted_values: a list of lab test results, each with test_name, value, value_numeric (number or null), unit, normal_range, flag, operator ('lt','gt','eq' or null)\n"
            "- ai_summary: a plain-language summary\n"
            "- doctor_notes: any handwritten notes found (or null)\n\n"
            "Return ONLY valid JSON, no markdown, no commentary.\n"
            "If a field is not found, use null or empty list as appropriate.\n"
            "Do not fabricate data.\n\n"
            f"Text: {text[:10000]}"
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Extract the structured data from the above text."}
        ]
        response_text = self._chat(messages)
        try:
            result = json.loads(response_text)
        except json.JSONDecodeError:
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
            else:
                raise RuntimeError("NVIDIA response not valid JSON")
        print("[OK] Structure result received")
        return result

    def __repr__(self):
        return f"NVIDIAClient(api_key='{self.api_key[:8]}...')"
# ============================================================
# 4. Jina Client (embeddings)
# ============================================================
class JinaClient:
    """
    Client for Jina AI embeddings (jina-embeddings-v3).
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("JINA_API_KEY")
        if not self.api_key:
            raise ValueError("JINA_API_KEY not set in environment")
        self.endpoint = "https://api.jina.ai/v1/embeddings"
        self.model = "jina-embeddings-v3"
        self.task = "retrieval.passage"
        self.dimensions = 1024
        print("[OK] JinaClient initialized")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((requests.exceptions.RequestException,))
    )
    def embed(self, text: str) -> List[float]:
        """
        Embed a text string and return the vector (list of floats).
        """
        print("[INFO] Sending embedding request to Jina...")
        payload = {
            "model": self.model,
            "task": self.task,
            "dimensions": self.dimensions,
            "input": [text]
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        response = requests.post(self.endpoint, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        result = response.json()
        try:
            embedding = result['data'][0]['embedding']
        except (KeyError, IndexError):
            raise RuntimeError("Jina response missing embedding")
        print(f"[OK] Jina embedding received (length {len(embedding)})")
        return embedding

    def __repr__(self):
        return f"JinaClient(api_key='{self.api_key[:4]}...')"


# ============================================================
# 5. Supabase Client (FIXED upload_file)
# ============================================================
class SupabaseClient:
    """
    Client for Supabase (PostgreSQL + pgvector).
    """

    def __init__(self, url: Optional[str] = None, key: Optional[str] = None):
        self.url = url or os.getenv("SUPABASE_URL")
        self.key = key or os.getenv("SUPABASE_KEY")
        if not self.url or not self.key:
            raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set")
        self.client: Client = create_client(self.url, self.key)
        self.storage_bucket = "medical-documents"
        print("[OK] SupabaseClient initialized")

    def ping(self) -> bool:
        """
        Check connectivity by querying a simple RPC.
        """
        try:
            result = self.client.table('patients').select('id', count='exact').limit(1).execute()
            print("[OK] Supabase ping successful")
            return True
        except Exception as e:
            print(f"[WARN] Supabase ping failed: {e}")
            return False

    def upload_file(self, file_bytes: bytes, file_path: str, content_type: str = None) -> str:
        """
        Upload a file to Supabase Storage and return the public URL.
        Auto-detects MIME type from file extension.
        """
        print(f"[INFO] Uploading file to bucket '{self.storage_bucket}'...")
        
        # Auto-detect MIME type from file extension
        if content_type is None:
            content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        
        # Explicit overrides for common types
        if file_path.lower().endswith('.png'):
            content_type = "image/png"
        elif file_path.lower().endswith(('.jpg', '.jpeg')):
            content_type = "image/jpeg"
        elif file_path.lower().endswith('.pdf'):
            content_type = "application/pdf"
        
        try:
            self.client.storage.from_(self.storage_bucket).upload(
                file_path, file_bytes, {"content-type": content_type}
            )
            public_url = self.client.storage.from_(self.storage_bucket).get_public_url(file_path)
            print("[OK] File uploaded successfully")
            return public_url
        except Exception as e:
            raise RuntimeError(f"Supabase upload failed: {e}")

    def __repr__(self):
        return f"SupabaseClient(url='{self.url[:20]}...')"


# ============================================================
# Standalone Test Block
# ============================================================
if __name__ == "__main__":
    print("\n=== Testing clients.py ===\n")

    # Test 1: Check environment variables
    env_vars = ["OCRSPACE_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "JINA_API_KEY", "SUPABASE_URL", "SUPABASE_KEY"]
    missing = [v for v in env_vars if not os.getenv(v)]
    if missing:
        print(f"[WARN] Missing env vars: {missing}")
    else:
        print("[OK] All env vars are set.")

    # Test 2: OCRspaceClient
    try:
        client = OCRspaceClient()
        print(f"[OK] OCRspaceClient instantiated: {repr(client)}")
    except Exception as e:
        print(f"[ERROR] OCRspaceClient failed: {e}")

    # Test 3: GeminiClient
    try:
        client = GeminiClient()
        print(f"[OK] GeminiClient instantiated: {repr(client)}")
    except Exception as e:
        print(f"[ERROR] GeminiClient failed: {e}")


    # Test 4: NVIDIAClient
    try:
        client = NVIDIAClient()
        print(f"[OK] NVIDIAClient instantiated: {repr(client)}")
    except Exception as e:
        print(f"[ERROR] NVIDIAClient failed: {e}")
    # Test 5: JinaClient
    try:
        client = JinaClient()
        print(f"[OK] JinaClient instantiated: {repr(client)}")
    except Exception as e:
        print(f"[ERROR] JinaClient failed: {e}")
    

    # Test 6: SupabaseClient
    try:
        client = SupabaseClient()
        print(f"[OK] SupabaseClient instantiated: {repr(client)}")
        client.ping()
    except Exception as e:
        print(f"[ERROR] SupabaseClient failed: {e}")

    # Test 7: Decision logic
    print("\n[OK] Testing OCR decision logic...")
    test_bytes_under_1mb = b"x" * 500 * 1024  # 500KB
    test_bytes_over_1mb = b"x" * 2 * 1024 * 1024  # 2MB
    print(f"  - 500KB file -> OCR.space: {should_use_ocrspace(test_bytes_under_1mb, 'JPG')}")
    print(f"  - 2MB file -> OCR.space: {should_use_ocrspace(test_bytes_over_1mb, 'JPG')}")
    print(f"  - PDF file -> OCR.space: {should_use_ocrspace(test_bytes_under_1mb, 'PDF')}")

    print("\n=== All tests completed ===")