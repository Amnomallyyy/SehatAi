#!/usr/bin/env python3
"""
main.py – CLI entry point for the Medical Document Extraction Pipeline.

Usage:
    python main.py --file <path> --patient-id <uuid> --password <password>
    python main.py --folder <path> --patient-id <uuid> --password <password>
    python main.py --patient-id <uuid> --password <password> --show-history
"""

import argparse
import json
import sys
import os
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime

# Local imports
from pipeline import process_document
from history import (
    get_patient_timeline,
    get_test_history,
    get_active_medicines,
    get_patient_snapshot
)


# ============================================================
# CLI Functions
# ============================================================
def process_single_file(file_path: str, patient_id: str, password: str) -> Dict[str, Any]:
    """Process a single file and return the result."""
    if not os.path.exists(file_path):
        return {"status": "error", "error": f"File not found: {file_path}"}
    
    print(f"\n📄 Processing: {file_path}")
    print(f"👤 Patient ID: {patient_id}")
    print("-" * 50)
    
    result = process_document(file_path, patient_id, password)
    
    print("\n" + "=" * 50)
    print("📊 Result:")
    print(json.dumps(result, indent=2))
    print("=" * 50)
    
    return result


def process_folder(folder_path: str, patient_id: str, password: str) -> List[Dict[str, Any]]:
    """Process all supported files in a folder recursively."""
    if not os.path.exists(folder_path):
        print(f"❌ Folder not found: {folder_path}")
        return []
    
    # Find all supported files
    extensions = ['.pdf', '.jpg', '.jpeg', '.png']
    files = []
    for ext in extensions:
        files.extend(Path(folder_path).rglob(f'*{ext}'))
        files.extend(Path(folder_path).rglob(f'*{ext.upper()}'))
    
    # Remove duplicates
    files = list(set(files))
    
    if not files:
        print(f"❌ No supported files found in: {folder_path}")
        print(f"   Supported: {', '.join(extensions)}")
        return []
    
    print(f"\n📁 Found {len(files)} files in: {folder_path}")
    print("-" * 50)
    
    results = []
    for i, file_path in enumerate(files, 1):
        print(f"\n[{i}/{len(files)}] Processing: {file_path.name}")
        result = process_document(str(file_path), patient_id, password)
        results.append({
            "file": str(file_path),
            "result": result
        })
    
    # Summary
    print("\n" + "=" * 50)
    print("📊 Summary:")
    success_count = sum(1 for r in results if r['result'].get('status') == 'stored')
    failed_count = len(results) - success_count
    print(f"   ✅ Success: {success_count}")
    print(f"   ❌ Failed:  {failed_count}")
    print("=" * 50)
    
    return results


def show_history(patient_id: str, password: str) -> None:
    """Show patient history and exit."""
    print(f"\n📋 Patient History: {patient_id}")
    print("-" * 50)
    
    # Authenticate first (using pipeline's auth)
    from auth import authenticate_patient
    if not authenticate_patient(patient_id, password):
        print("❌ Authentication failed. Invalid patient ID or password.")
        return
    
    # Get timeline
    timeline = get_patient_timeline(patient_id)
    if not timeline:
        print("📭 No documents found for this patient.")
        return
    
    print(f"\n📄 Documents ({len(timeline)}):")
    for doc in timeline:
        doc_date = doc.get('document_date') or doc.get('uploaded_at', '').split('T')[0]
        print(f"   📎 {doc_date} - {doc.get('category', 'unknown')} "
              f"(ID: {doc['document_id'][:8]}...) "
              f"[Tests: {doc.get('extracted_count', 0)}, "
              f"Medicines: {doc.get('medicines_count', 0)}]")
    
    # Show active medicines
    meds = get_active_medicines(patient_id)
    if meds:
        print(f"\n💊 Active Medicines ({len(meds)}):")
        for med in meds:
            print(f"   💊 {med.get('name')} - {med.get('dosage', 'No dosage')} "
                  f"(Started: {med.get('start_date')})")
    
    # Show snapshot
    snapshot = get_patient_snapshot(patient_id)
    if snapshot.get('snapshot'):
        print(f"\n📊 Latest Values ({len(snapshot['snapshot'])} tests):")
        for test_name, data in snapshot['snapshot'].items():
            print(f"   📊 {test_name}: {data.get('value')} {data.get('unit', '')}")
    
    print("\n" + "=" * 50)


# ============================================================
# Main CLI Entry
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="Medical Document Extraction Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Process a single file
  python main.py --file report.pdf --patient-id 123e4567-e89b-12d3-a456-426614174000 --password secret123

  # Process all files in a folder
  python main.py --folder ./documents --patient-id 123e4567-e89b-12d3-a456-426614174000 --password secret123

  # Show patient history
  python main.py --patient-id 123e4567-e89b-12d3-a456-426614174000 --password secret123 --show-history
        """
    )
    
    # Required
    parser.add_argument('--patient-id', required=True, help='Patient UUID')
    parser.add_argument('--password', required=True, help='Patient password')
    
    # Input options (mutually exclusive)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--file', help='Path to a single file to process')
    group.add_argument('--folder', help='Path to a folder containing files to process')
    group.add_argument('--show-history', action='store_true', help='Show patient history and exit')
    
    # Optional
    parser.add_argument('--json', action='store_true', help='Output results as JSON')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose output')
    
    args = parser.parse_args()
    
    # Show history
    if args.show_history:
        show_history(args.patient_id, args.password)
        return
    
    # Validate patient ID format (basic)
    if len(args.patient_id) < 10:
        print(f"❌ Invalid patient ID format: {args.patient_id}")
        print("   Patient ID should be a UUID (e.g., 123e4567-e89b-12d3-a456-426614174000)")
        sys.exit(1)
    
    # Process file
    if args.file:
        result = process_single_file(args.file, args.patient_id, args.password)
        if args.json:
            print(json.dumps(result, indent=2))
        
        # Exit with appropriate code
        if result.get('status') in ['stored', 'duplicate']:
            sys.exit(0)
        else:
            sys.exit(1)
    
    # Process folder
    if args.folder:
        results = process_folder(args.folder, args.patient_id, args.password)
        if args.json:
            print(json.dumps(results, indent=2))
        
        # Check if any failed
        failed = any(r['result'].get('status') not in ['stored', 'duplicate'] for r in results)
        sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()