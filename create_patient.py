#!/usr/bin/env python3
"""
create_patient.py – CLI tool to create patients with hashed passwords.

Usage:
    python create_patient.py --name "John Doe" --dob 1990-01-01 --password "secret123"
    python create_patient.py --name "Jane Smith" --dob 1985-06-15 --password "secure456" --consent
"""

import argparse
import uuid
import os
import sys
from datetime import datetime, date
from getpass import getpass
from dotenv import load_dotenv
from supabase import create_client, Client

# Local imports
from auth import hash_password

load_dotenv()


# ============================================================
# Supabase Client
# ============================================================
def get_supabase_client() -> Client:
    """Get Supabase client from environment."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set")
    return create_client(url, key)


# ============================================================
# Patient Creation Function
# ============================================================
def create_patient(
    name: str,
    date_of_birth: str,
    password: str,
    consented: bool = False
) -> dict:
    """
    Create a new patient with hashed password.

    Returns:
        dict with patient_id, name, and status
    """
    print(f"\n👤 Creating patient: {name}")
    print("-" * 40)

    # 1. Hash the password
    print("🔐 Hashing password...")
    password_hash = hash_password(password)

    # 2. Generate patient ID
    patient_id = str(uuid.uuid4())
    print(f"📋 Patient ID: {patient_id}")

    # 3. Prepare consent
    consented_at = datetime.now().isoformat() if consented else None

    # 4. Insert into Supabase
    print("💾 Saving to Supabase...")
    supabase = get_supabase_client()
    
    data = {
        "id": patient_id,
        "name": name,
        "date_of_birth": date_of_birth,
        "password_hash": password_hash,
        "consented_at": consented_at,
    }
    
    try:
        result = supabase.table('patients').insert(data).execute()
        patient = result.data[0] if result.data else data
        
        print("✅ Patient created successfully!")
        print(f"   ID: {patient_id}")
        print(f"   Name: {name}")
        print(f"   Consent: {'✅ Yes' if consented else '❌ No (use --consent to enable)'}")
        print("\n📝 Save this ID to test:")
        print(f"   python main.py --file your_document.pdf --patient-id {patient_id} --password '{password}'")
        
        return {
            "status": "success",
            "patient_id": patient_id,
            "name": name,
            "consented": consented
        }
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return {
            "status": "error",
            "error": str(e)
        }


# ============================================================
# List Patients (Helper)
# ============================================================
def list_patients():
    """List all existing patients."""
    print("\n📋 Existing Patients:")
    print("-" * 40)
    
    supabase = get_supabase_client()
    result = supabase.table('patients') \
        .select('id, name, date_of_birth, consented_at, created_at') \
        .execute()
    
    if not result.data:
        print("   No patients found.")
        return
    
    for p in result.data:
        consent = "✅ Yes" if p.get('consented_at') else "❌ No"
        print(f"   🆔 {p['id'][:8]}... | {p['name']} | Consent: {consent} | DOB: {p.get('date_of_birth', 'N/A')}")


# ============================================================
# Main CLI
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="Create a patient with hashed password.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create a patient with password
  python create_patient.py --name "John Doe" --dob 1990-01-01 --password "test123"

  # Create with consent
  python create_patient.py --name "Jane Smith" --dob 1985-06-15 --password "secret456" --consent

  # List all patients
  python create_patient.py --list
        """
    )
    
    parser.add_argument('--name', help="Patient's full name")
    parser.add_argument('--dob', help="Date of birth (YYYY-MM-DD)")
    parser.add_argument('--password', help="Password (will prompt if not provided)")
    parser.add_argument('--consent', action='store_true', help="Mark consent as given")
    parser.add_argument('--list', action='store_true', help="List all existing patients")
    
    args = parser.parse_args()
    
    # List patients
    if args.list:
        list_patients()
        return
    
    # Validate required args
    if not args.name or not args.dob:
        print("❌ Error: --name and --dob are required")
        print("   Example: python create_patient.py --name 'John Doe' --dob 1990-01-01 --password 'test123'")
        sys.exit(1)
    
    # Get password (prompt if not provided)
    password = args.password
    if not password:
        password = getpass("🔑 Enter password: ")
        confirm = getpass("🔑 Confirm password: ")
        if password != confirm:
            print("❌ Passwords do not match")
            sys.exit(1)
        if len(password) < 4:
            print("❌ Password must be at least 4 characters")
            sys.exit(1)
    
    # Validate date format
    try:
        date.fromisoformat(args.dob)
    except ValueError:
        print(f"❌ Invalid date format: {args.dob}. Use YYYY-MM-DD")
        sys.exit(1)
    
    # Create patient
    create_patient(args.name, args.dob, password, args.consent)


if __name__ == "__main__":
    main()