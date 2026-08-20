#!/usr/bin/env python3
"""
interface.py – Simple interactive CLI for the Medical Document Pipeline.
UPDATED to match your exact database schema.
"""

import os
import sys
import time
import uuid
from datetime import datetime
from dotenv import load_dotenv
from supabase import create_client, Client

from auth import hash_password, verify_password
from pipeline import process_document

load_dotenv()


def get_supabase() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        print("❌ Supabase credentials not found in .env")
        sys.exit(1)
    return create_client(url, key)


def clear_screen():
    os.system('clear' if os.name == 'posix' else 'cls')


def print_header(title: str):
    print("\n" + "="*60)
    print(f"  {title}")
    print("="*60)


def print_success(msg: str):
    print(f"✅ {msg}")


def print_error(msg: str):
    print(f"❌ {msg}")


def print_info(msg: str):
    print(f"ℹ️  {msg}")


def wait_for_enter():
    input("\nPress Enter to continue...")


def create_patient_interface(supabase: Client):
    clear_screen()
    print_header("Create New Patient")
    
    name = input("Full Name: ").strip()
    if not name:
        print_error("Name is required.")
        wait_for_enter()
        return
    
    dob = input("Date of Birth (YYYY-MM-DD): ").strip()
    try:
        datetime.strptime(dob, "%Y-%m-%d")
    except ValueError:
        print_error("Invalid date format. Use YYYY-MM-DD.")
        wait_for_enter()
        return
    
    password = input("Password (min 4 chars): ").strip()
    if len(password) < 4:
        print_error("Password must be at least 4 characters.")
        wait_for_enter()
        return
    
    consent = input("Consent given? (y/n): ").strip().lower() == 'y'
    
    patient_id = str(uuid.uuid4())
    password_hash = hash_password(password)
    consented_at = datetime.now().isoformat() if consent else None
    
    try:
        supabase.table('patients').insert({
            "id": patient_id,
            "name": name,
            "date_of_birth": dob,
            "password_hash": password_hash,
            "consented_at": consented_at,
        }).execute()
        
        clear_screen()
        print_header("✅ Patient Created Successfully!")
        print(f"   🆔 ID: {patient_id}")
        print(f"   👤 Name: {name}")
        print(f"   📅 DOB: {dob}")
        print(f"   📝 Consent: {'✅ Yes' if consent else '❌ No'}")
        print("\n📌 SAVE THIS ID – you'll need it to log in!")
        print("-" * 60)
        wait_for_enter()
    except Exception as e:
        print_error(f"Failed to create patient: {e}")
        wait_for_enter()


def login_interface(supabase: Client):
    clear_screen()
    print_header("Login")
    
    patient_id = input("Patient ID: ").strip()
    password = input("Password: ").strip()
    
    try:
        result = supabase.table('patients') \
            .select('id, password_hash, name, consented_at') \
            .eq('id', patient_id) \
            .execute()
        
        if not result.data or len(result.data) == 0:
            print_error("Patient not found.")
            wait_for_enter()
            return None, None
        
        patient = result.data[0]
        
        if not patient.get('password_hash'):
            print_error("No password set for this patient.")
            wait_for_enter()
            return None, None
        
        if not verify_password(password, patient['password_hash']):
            print_error("Incorrect password.")
            wait_for_enter()
            return None, None
        
        if patient.get('consented_at') is None:
            print_error("Patient has not given consent. Please contact admin.")
            wait_for_enter()
            return None, None
        
        print_success(f"Welcome, {patient['name']}!")
        return patient_id, patient['name']
        
    except Exception as e:
        print_error(f"Login error: {e}")
        wait_for_enter()
        return None, None


def upload_document_interface(supabase: Client, patient_id: str, patient_name: str):
    clear_screen()
    print_header(f"Upload Document – {patient_name}")
    print(f"Patient ID: {patient_id}")
    print("-" * 60)
    
    file_path = input("📄 File path (PDF, JPG, PNG): ").strip()
    
    if not file_path:
        print_error("No file path provided.")
        wait_for_enter()
        return
    
    if not os.path.exists(file_path):
        print_error(f"File not found: {file_path}")
        wait_for_enter()
        return
    
    password = input("🔑 Enter your password to confirm: ").strip()
    
    print_info("Processing document... (this may take a moment)")
    
    try:
        result = process_document(file_path, patient_id, password)
        
        clear_screen()
        print_header("📊 Upload Result")
        
        status = result.get('status', 'unknown')
        
        if status == 'stored':
            print_success("✅ Document stored successfully!")
            print(f"   📄 Document ID: {result.get('document_id')}")
            print(f"   📂 Category: {result.get('category', 'unknown')}")
            print(f"   📅 Date: {result.get('document_date', 'N/A')}")
            print(f"   🧪 Lab values extracted: {result.get('extracted_values_count', 0)}")
        elif status == 'duplicate':
            print_info("ℹ️  This document has already been uploaded.")
            print(f"   📄 Document ID: {result.get('document_id')}")
        elif status == 'queued':
            print_info("⏳ Document queued for background OCR.")
            print(f"   🆔 Batch Job ID: {result.get('batch_job_id')}")
        else:
            print_error(f"❌ Upload failed: {result.get('error', 'Unknown error')}")
        
        print("-" * 60)
        wait_for_enter()
        
    except Exception as e:
        print_error(f"Upload error: {e}")
        wait_for_enter()


def show_history_interface(supabase: Client, patient_id: str, patient_name: str):
    clear_screen()
    print_header(f"📋 Patient History – {patient_name}")
    
    try:
        # 1. Documents (using your actual column names)
        docs = supabase.table('documents') \
            .select('id, category, document_date, uploaded_at, status, file_url, original_filename, file_size_bytes') \
            .eq('patient_id', patient_id) \
            .order('uploaded_at', desc=True) \
            .execute()
        
        if not docs.data:
            print_info("No documents found.")
            wait_for_enter()
            return
        
        print(f"\n📄 Documents ({len(docs.data)}):")
        for doc in docs.data:
            doc_date = doc.get('document_date') or doc.get('uploaded_at', '').split('T')[0]
            status = doc.get('status', 'stored')
            status_icon = "✅" if status == 'stored' else "⏳"
            filename = doc.get('original_filename', 'Unknown file')
            print(f"   {status_icon} {doc_date} - {doc.get('category', 'unknown')} ({filename}) [ID: {doc['id'][:8]}...]")
        
        # 2. Medicines (using recorded_at)
        meds = supabase.table('medicines') \
            .select('name, dosage, start_date, active, recorded_at') \
            .eq('patient_id', patient_id) \
            .eq('active', True) \
            .execute()
        
        if meds.data:
            print(f"\n💊 Active Medicines ({len(meds.data)}):")
            for med in meds.data:
                print(f"   💊 {med.get('name')} - {med.get('dosage', 'No dosage')} (Started: {med.get('start_date')})")
        
        # 3. Lab values (using recorded_at)
        labs = supabase.table('extracted_data') \
            .select('test_name, value, unit, recorded_at') \
            .eq('patient_id', patient_id) \
            .order('recorded_at', desc=True) \
            .limit(5) \
            .execute()
        
        if labs.data:
            print(f"\n🧪 Latest Lab Values (last 5):")
            for lab in labs.data:
                recorded = lab.get('recorded_at', '').split('T')[0] if lab.get('recorded_at') else 'N/A'
                print(f"   🧪 {lab.get('test_name')}: {lab.get('value')} {lab.get('unit', '')} (Recorded: {recorded})")
        
        # 4. Clinical Advice (using recorded_at)
        advice = supabase.table('clinical_advice') \
            .select('content, origin, recorded_at, document_date') \
            .eq('patient_id', patient_id) \
            .order('recorded_at', desc=True) \
            .limit(3) \
            .execute()
        
        if advice.data:
            print(f"\n📝 Recent Clinical Notes (last 3):")
            for note in advice.data:
                recorded = note.get('recorded_at', '').split('T')[0] if note.get('recorded_at') else 'N/A'
                origin = note.get('origin', 'unknown')
                content = note.get('content', '')[:80] + '...' if note.get('content') else ''
                print(f"   📝 [{origin}] {recorded}: {content}")
        
        print("\n" + "-" * 60)
        wait_for_enter()
        
    except Exception as e:
        print_error(f"Error loading history: {e}")
        wait_for_enter()


def change_password_interface(supabase: Client, patient_id: str):
    clear_screen()
    print_header("Change Password")
    
    current = input("Current password: ").strip()
    new = input("New password (min 4 chars): ").strip()
    confirm = input("Confirm new password: ").strip()
    
    if len(new) < 4:
        print_error("Password must be at least 4 characters.")
        wait_for_enter()
        return
    
    if new != confirm:
        print_error("Passwords do not match.")
        wait_for_enter()
        return
    
    try:
        result = supabase.table('patients') \
            .select('password_hash') \
            .eq('id', patient_id) \
            .execute()
        
        if not result.data:
            print_error("Patient not found.")
            wait_for_enter()
            return
        
        if not verify_password(current, result.data[0].get('password_hash')):
            print_error("Incorrect current password.")
            wait_for_enter()
            return
        
        new_hash = hash_password(new)
        supabase.table('patients') \
            .update({'password_hash': new_hash}) \
            .eq('id', patient_id) \
            .execute()
        
        print_success("Password updated successfully!")
        wait_for_enter()
        
    except Exception as e:
        print_error(f"Error: {e}")
        wait_for_enter()


def main_menu():
    supabase = get_supabase()
    patient_id = None
    patient_name = None
    
    while True:
        clear_screen()
        print_header("🏥 Medical Document Pipeline")
        
        if patient_id and patient_name:
            print(f"   👤 Logged in as: {patient_name}")
            print(f"   🆔 ID: {patient_id}")
        else:
            print("   🔒 Not logged in")
        
        print("\n" + "-" * 60)
        print("  1. Create New Patient")
        print("  2. Login")
        print("  3. Upload Document")
        print("  4. View History")
        print("  5. Change Password")
        print("  6. Logout")
        print("  7. Exit")
        print("-" * 60)
        
        choice = input("Select option: ").strip()
        
        if choice == '1':
            create_patient_interface(supabase)
        elif choice == '2':
            patient_id, patient_name = login_interface(supabase)
        elif choice == '3':
            if patient_id and patient_name:
                upload_document_interface(supabase, patient_id, patient_name)
            else:
                print_error("Please login first.")
                wait_for_enter()
        elif choice == '4':
            if patient_id and patient_name:
                show_history_interface(supabase, patient_id, patient_name)
            else:
                print_error("Please login first.")
                wait_for_enter()
        elif choice == '5':
            if patient_id:
                change_password_interface(supabase, patient_id)
            else:
                print_error("Please login first.")
                wait_for_enter()
        elif choice == '6':
            patient_id = None
            patient_name = None
            print_info("Logged out.")
            wait_for_enter()
        elif choice == '7':
            print_info("Goodbye!")
            sys.exit(0)
        else:
            print_error("Invalid option.")
            wait_for_enter()


if __name__ == "__main__":
    try:
        main_menu()
    except KeyboardInterrupt:
        print("\n\n👋 Goodbye!")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        sys.exit(1)
        