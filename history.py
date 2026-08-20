#!/usr/bin/env python3
"""
history.py – Query helpers for longitudinal patient history.

Provides functions to retrieve:
- Patient timeline (all documents with child counts)
- Test history (specific lab test across time)
- Active medicines (as of a given date)
- Patient snapshot (latest value of each test as of a date)
"""

import os
from typing import Optional, List, Dict, Any
from datetime import date, datetime
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()


# ============================================================
# Supabase Client Singleton
# ============================================================
def get_supabase_client() -> Client:
    """Get or create Supabase client from environment."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set")
    return create_client(url, key)


# ============================================================
# 1. Get Patient Timeline
# ============================================================
def get_patient_timeline(patient_id: str) -> List[Dict[str, Any]]:
    """
    Get all documents for a patient ordered by clinical date.
    
    Returns:
        List of documents with:
        - document_id, document_date, uploaded_at, category
        - extracted_count, medicines_count, advice_count
    """
    supabase = get_supabase_client()
    
    # Use the RPC function created in migration
    try:
        result = supabase.rpc('get_patient_timeline', {'p_patient_id': patient_id}).execute()
        return result.data if result.data else []
    except Exception as e:
        # Fallback: manual query if RPC not available
        print(f"[WARN] RPC failed, using fallback query: {e}")
        return _get_patient_timeline_fallback(patient_id)


def _get_patient_timeline_fallback(patient_id: str) -> List[Dict[str, Any]]:
    """Fallback manual query for patient timeline."""
    supabase = get_supabase_client()
    
    # Get all documents
    docs = supabase.table('documents') \
        .select('id, document_date, uploaded_at, category') \
        .eq('patient_id', patient_id) \
        .order('document_date', desc=True, nulls_last=True) \
        .execute()
    
    if not docs.data:
        return []
    
    result = []
    for doc in docs.data:
        # Count child rows
        extracted = supabase.table('extracted_data') \
            .select('id', count='exact') \
            .eq('document_id', doc['id']) \
            .execute()
        
        medicines = supabase.table('medicines') \
            .select('id', count='exact') \
            .eq('document_id', doc['id']) \
            .execute()
        
        advice = supabase.table('clinical_advice') \
            .select('id', count='exact') \
            .eq('document_id', doc['id']) \
            .execute()
        
        result.append({
            'document_id': doc['id'],
            'document_date': doc.get('document_date'),
            'uploaded_at': doc.get('uploaded_at'),
            'category': doc.get('category', 'unknown'),
            'extracted_count': extracted.count if hasattr(extracted, 'count') else 0,
            'medicines_count': medicines.count if hasattr(medicines, 'count') else 0,
            'advice_count': advice.count if hasattr(advice, 'count') else 0,
        })
    
    return result


# ============================================================
# 2. Get Test History
# ============================================================
def get_test_history(patient_id: str, test_name: str) -> List[Dict[str, Any]]:
    """
    Get all readings for a specific lab test across time.
    
    Returns:
        List of test results with:
        - document_id, document_date, test_name, value, value_numeric, unit, normal_range, flag
        - ordered oldest to newest
    """
    supabase = get_supabase_client()
    
    # Query extracted_data joined with documents for dates
    result = supabase.table('extracted_data') \
        .select('''
            document_id,
            test_name,
            value,
            value_numeric,
            unit,
            normal_range,
            flag,
            created_at,
            documents!inner (
                document_date,
                uploaded_at
            )
        ''') \
        .eq('patient_id', patient_id) \
        .eq('test_name', test_name) \
        .execute()
    
    if not result.data:
        return []
    
    # Sort by document_date (oldest first)
    sorted_data = sorted(
        result.data,
        key=lambda x: (x.get('documents', {}).get('document_date') or x.get('created_at'))
    )
    
    return sorted_data


# ============================================================
# 3. Get Active Medicines
# ============================================================
def get_active_medicines(patient_id: str, as_of_date: Optional[date] = None) -> List[Dict[str, Any]]:
    """
    Get all active medicines for a patient as of a specific date.
    
    If as_of_date is None, uses current date.
    Returns medicines where start_date <= as_of_date and (end_date is null or end_date > as_of_date)
    """
    if as_of_date is None:
        as_of_date = date.today()
    
    supabase = get_supabase_client()
    
    # Query medicines with lifecycle logic
    result = supabase.table('medicines') \
        .select('''
            id,
            name,
            dosage,
            start_date,
            end_date,
            active,
            document_id,
            documents!inner (
                document_date
            )
        ''') \
        .eq('patient_id', patient_id) \
        .lte('start_date', as_of_date.isoformat()) \
        .execute()
    
    if not result.data:
        return []
    
    # Filter by end_date (null or > as_of_date)
    active_meds = []
    for med in result.data:
        end_date = med.get('end_date')
        if end_date is None or end_date > as_of_date.isoformat():
            # Also check active flag if present
            if med.get('active') is not False:  # Default to True if null
                active_meds.append(med)
    
    return active_meds


# ============================================================
# 4. Get Patient Snapshot
# ============================================================
def get_patient_snapshot(patient_id: str, as_of_date: Optional[date] = None) -> Dict[str, Any]:
    """
    Get the latest value of each distinct test_name as of a specific date.
    
    Returns:
        Dict with:
        - patient_id
        - as_of_date
        - snapshot: dict of test_name -> latest result
    """
    if as_of_date is None:
        as_of_date = date.today()
    
    supabase = get_supabase_client()
    
    # Get all extracted_data with document dates
    result = supabase.table('extracted_data') \
        .select('''
            test_name,
            value,
            value_numeric,
            unit,
            normal_range,
            flag,
            document_id,
            documents!inner (
                document_date,
                uploaded_at
            )
        ''') \
        .eq('patient_id', patient_id) \
        .lte('documents.document_date', as_of_date.isoformat()) \
        .execute()
    
    if not result.data:
        return {
            'patient_id': patient_id,
            'as_of_date': as_of_date.isoformat(),
            'snapshot': {}
        }
    
    # Group by test_name and keep the one with latest document_date
    latest = {}
    for row in result.data:
        test_name = row['test_name']
        doc_date = row.get('documents', {}).get('document_date')
        
        if test_name not in latest:
            latest[test_name] = row
        else:
            existing_date = latest[test_name].get('documents', {}).get('document_date')
            if doc_date and (existing_date is None or doc_date > existing_date):
                latest[test_name] = row
    
    return {
        'patient_id': patient_id,
        'as_of_date': as_of_date.isoformat(),
        'snapshot': latest
    }


# ============================================================
# 5. Get All Patients (Optional Admin Helper)
# ============================================================
def get_all_patients() -> List[Dict[str, Any]]:
    """Get all patients (basic info)."""
    supabase = get_supabase_client()
    result = supabase.table('patients') \
        .select('id, name, date_of_birth, consented_at, created_at') \
        .execute()
    return result.data if result.data else []


# ============================================================
# Standalone Test Block
# ============================================================
if __name__ == "__main__":
    print("\n=== Testing history.py ===\n")  # TODO: Remove

    # Test 1: Check environment
    print("[OK] Checking environment...")  # TODO: Remove
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if url and key:
        print("[OK] Supabase credentials found")  # TODO: Remove
    else:
        print("[WARN] Supabase credentials missing – tests will fail")  # TODO: Remove

    # Test 2: Function imports
    print("\n[OK] Testing function availability...")  # TODO: Remove
    functions = [
        'get_patient_timeline',
        'get_test_history',
        'get_active_medicines',
        'get_patient_snapshot'
    ]
    for func in functions:
        if func in globals():
            print(f"  - {func}: available")  # TODO: Remove
        else:
            print(f"[ERROR] {func}: missing")  # TODO: Remove

    # Test 3: Try queries (if Supabase available)
    if url and key:
        print("\n[OK] Testing queries...")  # TODO: Remove
        
        # Get all patients
        patients = get_all_patients()
        print(f"  - Found {len(patients)} patients")  # TODO: Remove
        
        if patients:
            # Test with first patient
            patient_id = patients[0]['id']
            print(f"  - Testing with patient: {patients[0]['name']} ({patient_id})")  # TODO: Remove
            
            # Timeline
            timeline = get_patient_timeline(patient_id)
            print(f"  - Timeline: {len(timeline)} documents")  # TODO: Remove
            
            # Active medicines
            meds = get_active_medicines(patient_id)
            print(f"  - Active medicines: {len(meds)}")  # TODO: Remove
            
            # Snapshot
            snapshot = get_patient_snapshot(patient_id)
            print(f"  - Snapshot: {len(snapshot['snapshot'])} test types")  # TODO: Remove
        else:
            print("  [INFO] No patients found – create one with create_patient.py")  # TODO: Remove
    else:
        print("[INFO] Supabase credentials not set, skipping real queries")  # TODO: Remove

    print("\n=== All tests completed ===")  # TODO: Remove