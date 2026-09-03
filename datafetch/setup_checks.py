
#!/usr/bin/env python3
"""
setup_checks.py – Environment verification for the Medical Document Pipeline.
Updated for OCR.space 1MB limit + Gemini Batch API.
"""

import sys
import importlib.metadata
import os
from pathlib import Path
from dotenv import load_dotenv

def print_ok(msg: str) -> None:
    print(f"[OK] {msg}")  # TODO: Remove this debug print after testing

def print_error(msg: str) -> None:
    print(f"[ERROR] {msg}")

def print_info(msg: str) -> None:
    print(f"[INFO] {msg}")

def check_python_version():
    v = sys.version_info
    if v.major >= 3 and v.minor >= 11:
        print_ok(f"Python {v.major}.{v.minor}.{v.micro} (>=3.11)")
        return True
    print_error(f"Python {v.major}.{v.minor} – need 3.11+")
    return False

def check_package(pkg_name: str, import_name: str = None):
    if import_name is None:
        import_name = pkg_name
    try:
        version = importlib.metadata.version(pkg_name)
        print_ok(f"Package '{pkg_name}' version {version}")
        return True
    except importlib.metadata.PackageNotFoundError:
        print_error(f"Package '{pkg_name}' not installed")
        return False

def check_env_file():
    env_path = Path(".env")
    if not env_path.exists():
        print_error(".env file not found – copy .env.example to .env")
        return False
    load_dotenv(env_path)
    required = [
        "OCRSPACE_API_KEY", 
        "GEMINI_API_KEY", 
        "NVIDIA_API_KEY", 
        "JINA_API_KEY", 
        "SUPABASE_URL", 
        "SUPABASE_KEY"
    ]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        print_error(f"Missing env vars: {missing}")
        return False
    print_ok(".env exists with all keys")
    return True

def check_ocrspace_limit():
    """Check if the user is aware of the 1MB limit."""
    print_info("OCR.space free tier has a 1MB file limit. Files larger than 1MB will use Gemini Batch API.")
    return True

def check_supabase_connectivity():
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")
    if not supabase_url or not supabase_key:
        print_info("Supabase credentials not set, skipping connectivity test")
        return True
    try:
        from supabase import create_client
        client = create_client(supabase_url, supabase_key)
        client.rpc('version', {}).execute()
        print_ok("Supabase connectivity test passed")
        return True
    except Exception as e:
        print_error(f"Supabase connectivity test failed: {e}")
        return False

def main():
    print("\n=== Medical Document Extraction Pipeline – Environment Check ===\n")
    success = True
    
    # 1. Python version
    if not check_python_version():
        success = False
    
    # 2. Packages
    packages = [
        ("python-dotenv",), ("requests",), ("httpx",), ("tenacity",), 
        ("pydantic",), ("pdfplumber",), ("PyMuPDF", "fitz"), 
        ("pillow",), ("supabase",), ("bcrypt",), ("pytest",)
    ]
    for pkg in packages:
        imp = pkg[1] if len(pkg) > 1 else pkg[0]
        if not check_package(pkg[0], imp):
            success = False
    
    # 3. .env file
    if not check_env_file():
        success = False
    
    # 4. OCR.space limit awareness
    check_ocrspace_limit()
    
    # 5. Supabase connectivity (optional but recommended)
    if not check_supabase_connectivity():
        print_info("Supabase connectivity is optional at this stage; you can test later.")
    
    print("\n=== Summary ===")
    if success:
        print("[OK] All critical checks passed.")  # TODO: Remove
        print("[INFO] Remember: OCR.space has a 1MB limit. Larger files use Gemini Batch API.")
    else:
        print("[ERROR] Some checks failed. Please fix the issues above and re-run.")  # TODO: Remove
    
    return 0 if success else 1

if __name__ == "__main__":
    sys.exit(main())