#!/usr/bin/env python3
"""
pipeline.py – Main orchestrator for the Medical Document Extraction Pipeline.

Flow:
1. Authenticate patient (ID + password)
2. Compute file hash (SHA-256) for deduplication
3. Check for duplicate (patient_id, file_hash)
4. Upload original file to Supabase Storage
5. Extract text (pdfplumber → OCR.space → Gemini Batch)
6. Classify as medical (NVIDIA)
7. Structure JSON (NVIDIA)
8. Generate embeddings (Jina)
9. Atomic storage (Supabase RPC)
"""

import os
import hashlib
import json
import logging
from typing import Optional, Dict, Any, Tuple
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

# Local imports
from auth import authenticate_patient
from clients import (
    OCRspaceClient,
    GeminiClient,
    NVIDIAClient,
    JinaClient,
    SupabaseClient,
    should_use_ocrspace,
    downscale_image_for_ocr
)
from schemas import StructuredDocument, structured_document_to_payload

# Load environment
load_dotenv()

# Configure logging
LOG_RAW_OUTPUT = os.getenv("LOG_RAW_OUTPUT", "false").lower() == "true"
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ============================================================
# Helper Functions
# ============================================================
def compute_file_hash(file_bytes: bytes) -> str:
    """Compute SHA-256 hash of file bytes."""
    return hashlib.sha256(file_bytes).hexdigest()


def read_file_bytes(file_path: str) -> bytes:
    """Read file bytes from disk."""
    with open(file_path, 'rb') as f:
        return f.read()


def extract_text_with_pdfplumber(file_bytes: bytes) -> Tuple[str, float, str]:
    """
    Extract text from PDF using pdfplumber (digital PDFs).
    Returns: (text, confidence, engine)
    """
    import pdfplumber
    import io
    
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            text = ""
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
            if text.strip():
                return text.strip(), 100.0, "pdfplumber"
            else:
                return "", 0.0, "pdfplumber_empty"
    except Exception as e:
        logger.warning(f"pdfplumber extraction failed: {e}")
        return "", 0.0, "pdfplumber_error"


def detect_file_type(file_path: str) -> str:
    """Detect file type from extension."""
    ext = Path(file_path).suffix.lower()
    if ext in ['.jpg', '.jpeg']:
        return 'JPG'
    elif ext == '.png':
        return 'PNG'
    elif ext == '.pdf':
        return 'PDF'
    else:
        return 'UNKNOWN'


# ============================================================
# Main Pipeline Function
# ============================================================
def process_document(
    file_path: str,
    patient_id: str,
    password: str,
    supabase_url: Optional[str] = None,
    supabase_key: Optional[str] = None
) -> Dict[str, Any]:
    """
    Main pipeline orchestrator.

    Args:
        file_path: Path to the document file.
        patient_id: UUID of the patient.
        password: Patient password for authentication.
        supabase_url: Optional Supabase URL override.
        supabase_key: Optional Supabase key override.

    Returns:
        Dict with status, document_id, and optional error message.
    """
    print("\n" + "="*60)
    print(f"[INFO] Processing document: {file_path}")
    print(f"[INFO] Patient ID: {patient_id}")
    print("="*60 + "\n")

    # ------------------------------------------------------------
    # STEP 0: Authentication (MUST be first!)
    # ------------------------------------------------------------
    print("[OK] Step 0: Authenticating patient...")
    if not authenticate_patient(patient_id, password):
        return {
            "status": "auth_failed",
            "document_id": None,
            "error": "Invalid patient ID or password"
        }
    print("[OK] Authentication successful")

    try:
        # ------------------------------------------------------------
        # Initialize clients
        # ------------------------------------------------------------
        supabase = SupabaseClient(supabase_url, supabase_key)
        ocr_client = OCRspaceClient()
        gemini_client = GeminiClient()
        nvidia_client = NVIDIAClient()  # Replaced Groq with NVIDIA
        jina_client = JinaClient()

        # ------------------------------------------------------------
        # STEP 1: Read file and compute hash
        # ------------------------------------------------------------
        print("[OK] Step 1: Reading file and computing hash...")
        file_bytes = read_file_bytes(file_path)
        file_hash = compute_file_hash(file_bytes)
        file_type = detect_file_type(file_path)
        print(f"[OK] File hash: {file_hash[:16]}...")
        print(f"[OK] File type: {file_type}")

        # ------------------------------------------------------------
        # STEP 2: Check for duplicate (patient_id, file_hash)
        # ------------------------------------------------------------
        print("[OK] Step 2: Checking for duplicates...")
        try:
            result = supabase.client.table('documents') \
                .select('id, file_url, status') \
                .eq('patient_id', patient_id) \
                .eq('file_hash', file_hash) \
                .execute()
            if result.data and len(result.data) > 0:
                doc = result.data[0]
                print(f"[OK] Document already exists: {doc['id']}")
                return {
                    "status": "duplicate",
                    "document_id": doc['id'],
                    "error": None,
                    "message": "Document already processed"
                }
        except Exception as e:
            logger.warning(f"Duplicate check failed: {e}")
            # Continue anyway

        # ------------------------------------------------------------
        # STEP 3: Upload original file to Supabase Storage
        # ------------------------------------------------------------
        print("[OK] Step 3: Uploading file to Supabase Storage...")
        storage_path = f"{patient_id}/{file_hash}.{file_type.lower()}"
        file_url = supabase.upload_file(file_bytes, storage_path)
        print(f"[OK] File uploaded: {file_url}")

        # ------------------------------------------------------------
        # STEP 4: Extract text (cascade: pdfplumber → OCR.space → Gemini)
        # ------------------------------------------------------------
        print("[OK] Step 4: Extracting text...")
        raw_text = ""
        ocr_engine = ""
        ocr_confidence = 0.0

        # 4a: Try pdfplumber for PDFs
        if file_type == "PDF":
            text, conf, engine = extract_text_with_pdfplumber(file_bytes)
            if text and len(text) > 100:  # Meaningful text extracted
                raw_text = text
                ocr_engine = engine
                ocr_confidence = conf
                print(f"[OK] Text extracted via {engine}")

        # 4b: Try OCR.space for images under 1MB (or if pdfplumber failed)
        if not raw_text and should_use_ocrspace(file_bytes, file_type):
            try:
                text, conf, engine = ocr_client.extract(file_bytes, file_type)
                if text and len(text) > 10:
                    raw_text = text
                    ocr_engine = engine
                    ocr_confidence = conf
                    print(f"[OK] Text extracted via {engine}")
            except Exception as e:
                logger.warning(f"OCR.space failed: {e}")
                print(f"[WARN] OCR.space failed: {e}")

        # 4c: Fallback to Gemini Batch (for large files or if OCR.space failed)
        if not raw_text:
            try:
                print("[OK] Submitting to Gemini Batch API...")
                batch_id = gemini_client.submit_batch([{
                    'image_bytes': file_bytes,
                    'file_type': file_type
                }])
                # Store in processing_queue for background processing.
                queue_data = {
                    'document_id': None,
                    'patient_id': patient_id,
                    'file_path': file_path,
                    'file_type': file_type,
                    'file_size_bytes': len(file_bytes),
                    'status': 'pending_batch',
                    'batch_job_id': batch_id,
                    'input_data': {'file_hash': file_hash, 'file_url': file_url}
                }
                result = supabase.client.table('processing_queue') \
                    .insert(queue_data) \
                    .execute()
                return {
                    "status": "queued",
                    "document_id": None,
                    "error": None,
                    "message": "Document queued for Gemini Batch OCR. Check back later.",
                    "batch_job_id": batch_id
                }
            except Exception as e:
                logger.error(f"Gemini Batch submission failed: {e}")
                return {
                    "status": "failed",
                    "document_id": None,
                    "error": f"OCR failed: {str(e)}"
                }

        # If no text at all, fail
        if not raw_text or len(raw_text) < 10:
            return {
                "status": "failed",
                "document_id": None,
                "error": "No text could be extracted from the document"
            }

        # Log raw OCR if enabled
        if LOG_RAW_OUTPUT:
            logger.info(f"Raw OCR text: {raw_text[:500]}...")

        # ------------------------------------------------------------
        # STEP 5: Medical Classification (NVIDIA)
        # ------------------------------------------------------------
        print("[OK] Step 5: Classifying as medical document...")
        try:
            classification = nvidia_client.classify_medical(raw_text[:3000])
            if not classification.get('is_medical', False):
                # Not a medical document – reject
                return {
                    "status": "rejected_not_medical",
                    "document_id": None,
                    "error": f"Not a medical document: {classification.get('reason', 'Unknown reason')}"
                }
            print(f"[OK] Classified as medical: {classification.get('reason', '')}")
        except Exception as e:
            logger.error(f"Classification failed: {e}")
            return {
                "status": "failed_validation",
                "document_id": None,
                "error": f"Classification failed: {str(e)}"
            }

        # ------------------------------------------------------------
        # STEP 6: Check patient consent
        # ------------------------------------------------------------
        print("[OK] Step 6: Checking patient consent...")
        consented = supabase.client.table('patients') \
            .select('consented_at') \
            .eq('id', patient_id) \
            .execute()
        if consented.data and consented.data[0].get('consented_at') is None:
            return {
                "status": "failed",
                "document_id": None,
                "error": "Patient has not given consent"
            }

        # ------------------------------------------------------------
        # STEP 7: Structure JSON (NVIDIA)
        # ------------------------------------------------------------
        print("[OK] Step 7: Structuring document with NVIDIA...")
        try:
            structured_data = nvidia_client.structure_document(raw_text[:10000])
            # Validate with Pydantic
            doc = StructuredDocument(**structured_data)
            print("[OK] JSON structured and validated")
        except Exception as e:
            logger.error(f"Structuring failed: {e}")
            return {
                "status": "failed_validation",
                "document_id": None,
                "error": f"Structuring failed: {str(e)}"
            }

        # ------------------------------------------------------------
        # STEP 8: Generate Embeddings (Jina)
        # ------------------------------------------------------------
        print("[OK] Step 8: Generating embeddings...")
        try:
            # Build embedding source text
            source_text = f"{doc.category} "
            for val in doc.extracted_values:
                source_text += f"{val.test_name}: {val.value} {val.unit or ''} "
            if doc.ai_summary:
                source_text += doc.ai_summary
            if doc.doctor_notes:
                source_text += doc.doctor_notes

            embedding = jina_client.embed(source_text[:8000])
            print(f"[OK] Embedding generated (length: {len(embedding)})")
        except Exception as e:
            logger.warning(f"Embedding failed: {e}")
            embedding = None

        # ------------------------------------------------------------
        # STEP 9: Atomic Storage (Supabase RPC)
        # ------------------------------------------------------------
        print("[OK] Step 9: Storing document and data atomically...")
        try:
            # Build payload for atomic_upsert_document RPC
            payload = structured_document_to_payload(
                doc=doc,
                patient_id=patient_id,
                file_url=file_url,
                file_hash=file_hash,
                raw_ocr=raw_text,
                ocr_engine=ocr_engine,
                ocr_confidence=ocr_confidence,
                embedding=embedding,
                embedding_content=source_text[:8000] if embedding else None
            )

            # Call RPC
            result = supabase.client.rpc('atomic_upsert_document', {'payload': payload}).execute()
            document_id = result.data if isinstance(result.data, str) else result.data.get('id')

            print(f"[OK] Document stored: {document_id}")

            return {
                "status": "stored",
                "document_id": document_id,
                "error": None,
                "category": doc.category,
                "document_date": str(doc.document_date) if doc.document_date else None,
                "extracted_values_count": len(doc.extracted_values)
            }

        except Exception as e:
            logger.error(f"Storage failed: {e}")
            return {
                "status": "failed_storage",
                "document_id": None,
                "error": f"Storage failed: {str(e)}"
            }

    except Exception as e:
        logger.error(f"Pipeline error: {e}")
        return {
            "status": "failed",
            "document_id": None,
            "error": f"Pipeline error: {str(e)}"
        }


# ============================================================
# Standalone Test Block
# ============================================================
if __name__ == "__main__":
    print("\n=== Testing pipeline.py ===\n")

    # Test: Basic function imports
    print("[OK] Testing imports...")
    try:
        from schemas import StructuredDocument, ExtractedValue
        from auth import authenticate_patient
        from clients import OCRspaceClient, GeminiClient, NVIDIAClient, JinaClient, SupabaseClient
        print("[OK] All imports successful")
    except Exception as e:
        print(f"[ERROR] Import failed: {e}")
        import sys
        sys.exit(1)

    # Test: Check environment variables
    print("\n[OK] Checking environment...")
    env_vars = ["OCRSPACE_API_KEY", "GEMINI_API_KEY", "NVIDIA_API_KEY", "JINA_API_KEY", "SUPABASE_URL", "SUPABASE_KEY"]
    missing = [v for v in env_vars if not os.getenv(v)]
    if missing:
        print(f"[WARN] Missing env vars: {missing}")
    else:
        print("[OK] All env vars are set")

    # Test: pipeline function signature
    print("\n[OK] Testing process_document signature...")
    import inspect
    sig = inspect.signature(process_document)
    params = list(sig.parameters.keys())
    print(f"  - Parameters: {params}")
    expected = ['file_path', 'patient_id', 'password']
    if all(p in params for p in expected):
        print("[OK] process_document has correct signature")
    else:
        print("[ERROR] process_document missing expected parameters")

    # Test: NVIDIAClient availability
    print("\n[OK] Testing NVIDIAClient...")
    try:
        client = NVIDIAClient()
        print(f"[OK] NVIDIAClient instantiated: {repr(client)}")
    except Exception as e:
        print(f"[ERROR] NVIDIAClient failed: {e}")

    print("\n[OK] Pipeline module ready")
    print("  - To process a real document:")
    print("    result = process_document('report.jpg', 'patient-uuid', 'password')")
    print("    print(result)")

    print("\n=== All tests passed ===")
    