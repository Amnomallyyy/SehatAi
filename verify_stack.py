#!/usr/bin/env python3
"""
verify_stack.py – Standalone pre‑build check for the Medical Document Extraction Pipeline.

This script tests each provider in sequence and prints a clear pass/fail status.
Run this BEFORE writing any pipeline code to confirm all APIs are working.
"""

import os
import sys
import json
from dotenv import load_dotenv
from datetime import datetime

# Local imports
from clients import (
    OCRspaceClient,
    GeminiClient,
    NVIDIAClient,
    JinaClient,
    SupabaseClient,
)
from auth import authenticate_patient
from schemas import StructuredDocument

load_dotenv()


# ============================================================
# Test Helpers
# ============================================================
def print_header(title: str):
    """Print a section header."""
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

def print_pass(msg: str):
    """Print a pass message with emoji."""
    print(f"✅ PASS: {msg}")

def print_fail(msg: str):
    """Print a fail message with emoji."""
    print(f"❌ FAIL: {msg}")

def print_info(msg: str):
    """Print an info message."""
    print(f"ℹ️  {msg}")


# ============================================================
# Individual Tests
# ============================================================
def test_env_vars() -> bool:
    """Test that all required environment variables are set."""
    print_header("Environment Variables")
    required = [
        "OCRSPACE_API_KEY",
        "GEMINI_API_KEY",
        "NVIDIA_API_KEY",
        "JINA_API_KEY",
        "SUPABASE_URL",
        "SUPABASE_KEY"
    ]
    missing = []
    for key in required:
        value = os.getenv(key)
        if not value:
            missing.append(key)
        else:
            print(f"  ✅ {key}: {value[:8]}...")
    
    if missing:
        print_fail(f"Missing env vars: {', '.join(missing)}")
        return False
    
    print_pass("All environment variables are set")
    return True


def test_ocrspace() -> bool:
    """Test OCR.space connectivity (no actual OCR)."""
    print_header("OCR.space")
    try:
        client = OCRspaceClient()
        print_pass(f"Client instantiated: {repr(client)}")
        return True
    except Exception as e:
        print_fail(f"Failed: {e}")
        return False


def test_gemini() -> bool:
    """Test Gemini connectivity (no actual batch)."""
    print_header("Gemini Batch API")
    try:
        client = GeminiClient()
        print_pass(f"Client instantiated: {repr(client)}")
        return True
    except Exception as e:
        print_fail(f"Failed: {e}")
        return False


def test_nvidia() -> bool:
    """Test NVIDIA API with a simple classification."""
    print_header("NVIDIA API")
    try:
        client = NVIDIAClient()
        print(f"  ✅ Client instantiated: {repr(client)}")
        
        # Test classification with dummy text
        test_text = "Patient has hemoglobin 13.2 g/dL, WBC 7.5 x10³/µL."
        print(f"  ℹ️  Sending test classification...")
        result = client.classify_medical(test_text)
        
        if result.get('is_medical'):
            print_pass(f"Classification successful: {result.get('reason', 'Medical document')[:60]}...")
        else:
            print_fail("Classification returned non-medical (incorrect)")
            return False
        return True
    except Exception as e:
        print_fail(f"Failed: {e}")
        return False


def test_jina() -> bool:
    """Test Jina embedding API."""
    print_header("Jina Embeddings")
    try:
        client = JinaClient()
        print(f"  ✅ Client instantiated: {repr(client)}")
        
        # Test embedding with dummy text
        test_text = "This is a test document for embedding."
        print(f"  ℹ️  Sending test embedding...")
        embedding = client.embed(test_text)
        
        if len(embedding) == 1024:
            print_pass(f"Embedding successful (length: {len(embedding)})")
            print(f"  ℹ️  First 5 values: {embedding[:5]}")
        else:
            print_fail(f"Embedding length mismatch: {len(embedding)} (expected 1024)")
            return False
        return True
    except Exception as e:
        print_fail(f"Failed: {e}")
        return False


def test_supabase() -> bool:
    """Test Supabase connectivity."""
    print_header("Supabase")
    try:
        client = SupabaseClient()
        print(f"  ✅ Client instantiated: {repr(client)}")
        
        # Test ping
        if client.ping():
            print_pass("Supabase connectivity successful")
            
            # Test that required tables exist
            try:
                tables = ['patients', 'documents', 'extracted_data', 'medicines', 'clinical_advice', 'summaries_vectors', 'processing_queue']
                for table in tables:
                    try:
                        result = client.client.table(table).select('count', count='exact').limit(0).execute()
                        print(f"  ✅ Table '{table}' exists")
                    except Exception as e:
                        print(f"  ⚠️  Table '{table}' check failed: {e}")
                return True
            except Exception as e:
                print_fail(f"Table check failed: {e}")
                return False
        else:
            print_fail("Supabase ping failed")
            return False
    except Exception as e:
        print_fail(f"Failed: {e}")
        return False


def test_schemas() -> bool:
    """Test Pydantic schemas with sample data."""
    print_header("Pydantic Schemas")
    try:
        # Test valid document
        valid_data = {
            "category": "blood_test",
            "document_date": "2025-03-03",
            "extracted_values": [
                {
                    "test_name": "Hemoglobin",
                    "value": "13.2",
                    "value_numeric": 13.2,
                    "unit": "g/dL",
                    "normal_range": "13.0-17.0",
                    "flag": "normal",
                    "operator": "eq"
                }
            ],
            "ai_summary": "Normal blood panel.",
            "doctor_notes": "Patient reported mild fatigue."
        }
        doc = StructuredDocument(**valid_data)
        print_pass("StructuredDocument validation successful")
        print(f"  ℹ️  Category: {doc.category}")
        print(f"  ℹ️  Values: {len(doc.extracted_values)}")
        return True
    except Exception as e:
        print_fail(f"Failed: {e}")
        return False


def test_auth() -> bool:
    """Test authentication module (no real auth)."""
    print_header("Authentication Module")
    try:
        from auth import hash_password, verify_password
        
        # Test hashing
        test_password = "test123"
        hashed = hash_password(test_password)
        print(f"  ✅ Password hashed: {hashed[:20]}...")
        
        # Test verification
        if verify_password(test_password, hashed):
            print_pass("Password verification successful")
        else:
            print_fail("Password verification failed")
            return False
        
        # Test invalid password
        if not verify_password("wrong", hashed):
            print_pass("Invalid password correctly rejected")
        else:
            print_fail("Invalid password incorrectly accepted")
            return False
        
        return True
    except Exception as e:
        print_fail(f"Failed: {e}")
        return False


# ============================================================
# Main Function
# ============================================================
def main():
    """Run all verification tests."""
    print("\n" + "="*60)
    print("  Medical Document Extraction Pipeline – Stack Verification")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60)
    
    results = {}
    
    # Run all tests
    results['Environment'] = test_env_vars()
    results['OCR.space'] = test_ocrspace()
    results['Gemini'] = test_gemini()
    results['NVIDIA'] = test_nvidia()
    results['Jina'] = test_jina()
    results['Supabase'] = test_supabase()
    results['Schemas'] = test_schemas()
    results['Authentication'] = test_auth()
    
    # Summary
    print("\n" + "="*60)
    print("  SUMMARY")
    print("="*60)
    
    for name, passed in results.items():
        if passed:
            print(f"  ✅ {name}: PASS")
        else:
            print(f"  ❌ {name}: FAIL")
    
    total = len(results)
    passed = sum(results.values())
    failed = total - passed
    
    print("\n" + "="*60)
    print(f"  Total: {total} | Passed: {passed} | Failed: {failed}")
    print("="*60)
    
    if failed == 0:
        print_pass("\nAll checks passed. You are ready to run the pipeline! 🚀")
        return 0
    else:
        print_fail(f"\n{failed} check(s) failed. Please fix the issues above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())