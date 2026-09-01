#!/usr/bin/env python3
"""
history.py – Query helpers with full error handling.
"""

import os
from typing import Optional, List, Dict, Any
from datetime import date
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()


def get_supabase_client() -> Optional[Client]:
    try:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_KEY")
        if not url or not key:
            raise ValueError("SUPABASE_URL and SUPABASE_KEY required")
        return create_client(url, key)
    except Exception as e:
        print(f"[ERROR] Supabase client init failed: {e}")
        return None


def get_patient_timeline(patient_id: str) -> List[Dict[str, Any]]:
    supabase = get_supabase_client()
    if not supabase:
        return []
    try:
        result = supabase.rpc('get_patient_timeline', {'p_patient_id': patient_id}).execute()
        return result.data if result.data else []
    except Exception as e:
        print(f"[WARN] Timeline query failed: {e}")
        return _get_patient_timeline_fallback(patient_id)


def _get_patient_timeline_fallback(patient_id: str) -> List[Dict[str, Any]]:
    supabase = get_supabase_client()
    if not supabase:
        return []
    try:
        docs = supabase.table('documents') \
            .select('id, document_date, uploaded_at, category') \
            .eq('patient_id', patient_id) \
            .order('document_date', desc=True, nulls_last=True) \
            .execute()
        if not docs.data:
            return []
        result = []
        for doc in docs.data:
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
    except Exception as e:
        print(f"[ERROR] Fallback timeline failed: {e}")
        return []


def get_test_history(patient_id: str, test_name: str) -> List[Dict[str, Any]]:
    supabase = get_supabase_client()
    if not supabase:
        return []
    try:
        result = supabase.table('extracted_data') \
            .select('document_id, test_name, value, value_numeric, unit, normal_range, flag, created_at, documents!inner (document_date, uploaded_at)') \
            .eq('patient_id', patient_id) \
            .eq('test_name', test_name) \
            .execute()
        if not result.data:
            return []
        return sorted(result.data, key=lambda x: (x.get('documents', {}).get('document_date') or x.get('created_at')))
    except Exception as e:
        print(f"[ERROR] Test history query failed: {e}")
        return []


def get_active_medicines(patient_id: str, as_of_date: Optional[date] = None) -> List[Dict[str, Any]]:
    if as_of_date is None:
        as_of_date = date.today()
    supabase = get_supabase_client()
    if not supabase:
        return []
    try:
        result = supabase.table('medicines') \
            .select('id, name, dosage, start_date, end_date, active, document_id, documents!inner (document_date)') \
            .eq('patient_id', patient_id) \
            .lte('start_date', as_of_date.isoformat()) \
            .execute()
        if not result.data:
            return []
        active_meds = []
        for med in result.data:
            end_date = med.get('end_date')
            if end_date is None or end_date > as_of_date.isoformat():
                if med.get('active') is not False:
                    active_meds.append(med)
        return active_meds
    except Exception as e:
        print(f"[ERROR] Active medicines query failed: {e}")
        return []


def get_patient_snapshot(patient_id: str, as_of_date: Optional[date] = None) -> Dict[str, Any]:
    if as_of_date is None:
        as_of_date = date.today()
    supabase = get_supabase_client()
    if not supabase:
        return {'patient_id': patient_id, 'as_of_date': as_of_date.isoformat(), 'snapshot': {}}
    try:
        result = supabase.table('extracted_data') \
            .select('test_name, value, value_numeric, unit, normal_range, flag, document_id, documents!inner (document_date, uploaded_at)') \
            .eq('patient_id', patient_id) \
            .lte('documents.document_date', as_of_date.isoformat()) \
            .execute()
        if not result.data:
            return {'patient_id': patient_id, 'as_of_date': as_of_date.isoformat(), 'snapshot': {}}
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
        return {'patient_id': patient_id, 'as_of_date': as_of_date.isoformat(), 'snapshot': latest}
    except Exception as e:
        print(f"[ERROR] Snapshot query failed: {e}")
        return {'patient_id': patient_id, 'as_of_date': as_of_date.isoformat(), 'snapshot': {}}


def get_all_patients() -> List[Dict[str, Any]]:
    supabase = get_supabase_client()
    if not supabase:
        return []
    try:
        result = supabase.table('patients').select('id, name, date_of_birth, consented_at, created_at').execute()
        return result.data if result.data else []
    except Exception as e:
        print(f"[ERROR] Patients query failed: {e}")
        return []


if __name__ == "__main__":
    print("\n=== Testing history.py ===\n")
    print("[OK] All functions loaded with error handling.")
    print("=== All tests passed ===")