#!/usr/bin/env python3
"""
create_patient.py – CLI tool to create patients with all attributes.

Usage:
    python create_patient.py --name "John Doe" --dob 1990-01-01 --sex male --password "test123" --consent
    python create_patient.py --list
"""

import argparse
import uuid
import os
import sys
from datetime import datetime, date
from getpass import getpass
from dotenv import load_dotenv
from supabase import create_client, Client

from auth import hash_password

load_dotenv()


# ============================================================
# Supabase Client
# ============================================================
def get_supabase_client() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set")
    return create_client(url, key)


# ============================================================
# Helper: Calculate Age from DOB
# ============================================================
def calculate_age(dob_str: str) -> int:
    born = datetime.strptime(dob_str, "%Y-%m-%d").date()
    today = date.today()
    age = today.year - born.year
    if (today.month, today.day) < (born.month, born.day):
        age -= 1
    return age


# ============================================================
# Patient Creation Function
# ============================================================
def create_patient(
    name: str,
    date_of_birth: str,
    sex: str,                # may be any case
    password: str,
    consented: bool = False
) -> dict:
    print(f"\n👤 Creating patient: {name}")
    print("-" * 40)

    # Calculate age
    age = calculate_age(date_of_birth)
    print(f"📊 Age: {age}")

    # Hash password
    password_hash = hash_password(password)

    # Generate patient ID
    patient_id = str(uuid.uuid4())
    print(f"📋 Patient ID: {patient_id}")

    # Consent timestamp
    consented_at = datetime.now().isoformat() if consented else None

    supabase = get_supabase_client()

    # Ensure sex is lowercased for the constraint
    sex_lower = sex.lower()

    data = {
        "id": patient_id,
        "name": name,
        "date_of_birth": date_of_birth,
        "age": age,
        "sex": sex_lower,                     # stored as lowercase
        "password_hash": password_hash,
        "consented_at": consented_at,
    }

    try:
        supabase.table('patients').insert(data).execute()
        print("✅ Patient created successfully!")
        print(f"   ID: {patient_id}")
        print(f"   Name: {name}")
        print(f"   DOB: {date_of_birth}")
        print(f"   Age: {age}")
        print(f"   Sex: {sex_lower.capitalize()}")   # display nicely
        print(f"   Consent: {'✅ Yes' if consented else '❌ No'}")
        print("\n📝 Save this ID to test:")
        print(f"   python main.py --file your_document.pdf --patient-id {patient_id} --password '{password}'")
        return {"status": "success", "patient_id": patient_id, "name": name, "age": age, "sex": sex_lower}
    except Exception as e:
        print(f"❌ Error: {e}")
        return {"status": "error", "error": str(e)}


# ============================================================
# List Patients (with Age & Sex)
# ============================================================
def list_patients():
    print("\n📋 Existing Patients:")
    print("-" * 40)
    supabase = get_supabase_client()
    result = supabase.table('patients') \
        .select('id, name, date_of_birth, age, sex, consented_at, created_at') \
        .execute()
    if not result.data:
        print("   No patients found.")
        return
    for p in result.data:
        consent = "✅ Yes" if p.get('consented_at') else "❌ No"
        sex_display = p.get('sex', '?').capitalize()   # nice capitalisation
        print(f"   🆔 {p['id'][:8]}... | {p['name']} | Age: {p.get('age', '?')} | Sex: {sex_display} | Consent: {consent}")


# ============================================================
# Main CLI
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="Create a patient with all attributes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create a patient with all fields
  python create_patient.py --name "John Doe" --dob 1990-01-01 --sex male --password "test123" --consent

  # List all patients
  python create_patient.py --list
        """
    )
    parser.add_argument('--name', required=True, help="Patient's full name")
    parser.add_argument('--dob', required=True, help="Date of birth (YYYY-MM-DD)")
    parser.add_argument('--sex', required=True, choices=['male', 'female', 'other'],
                        help="Sex (must be one of: male, female, other) – case-insensitive")
    parser.add_argument('--password', help="Password (will prompt if not provided)")
    parser.add_argument('--consent', action='store_true', help="Mark consent as given")
    parser.add_argument('--list', action='store_true', help="List all existing patients")
    args = parser.parse_args()

    if args.list:
        list_patients()
        return

    # Password handling
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

    # Validate DOB
    try:
        datetime.strptime(args.dob, "%Y-%m-%d")
    except ValueError:
        print(f"❌ Invalid date format: {args.dob}. Use YYYY-MM-DD")
        sys.exit(1)

    # Sex is already lowercased by choices, but we convert anyway
    create_patient(args.name, args.dob, args.sex.lower(), password, args.consent)


if __name__ == "__main__":
    main()