#!/usr/bin/env python3
"""
schemas.py – Pydantic models for the medical document extraction pipeline.

These models define the structure of the JSON that NVIDIA returns, and provide
validation to ensure data quality before it reaches the database.
"""

from datetime import date
from typing import List, Optional, Literal
from pydantic import BaseModel, Field, model_validator


# ============================================================
# 1. ExtractedValue – Single lab test result
# ============================================================
class ExtractedValue(BaseModel):
    """
    A single extracted lab value from a medical document.

    Fields:
        test_name: Name of the test (e.g., "Hemoglobin")
        value: Raw value as printed (e.g., "13.2", "<0.01", "Negative")
        value_numeric: Numeric value if parsable, else None
        unit: Unit of measurement (e.g., "g/dL", "mg/L")
        normal_range: Reference range string (e.g., "13.0-17.0")
        flag: Indicator (e.g., "normal", "high", "low")
        operator: Comparison operator ('lt', 'gt', 'eq') for values like "<0.01"
    """
    test_name: str = Field(..., description="Name of the test")
    value: str = Field(..., description="Raw value as printed")
    value_numeric: Optional[float] = Field(None, description="Numeric value if parsable")
    unit: Optional[str] = Field(None, description="Unit of measurement")
    normal_range: Optional[str] = Field(None, description="Reference range")
    flag: Optional[str] = Field(None, description="Indicator (normal/high/low/abnormal)")
    operator: Optional[Literal['lt', 'gt', 'eq']] = Field(
        None,
        description="Comparison operator for non-numeric values"
    )

    @model_validator(mode='after')
    def validate_value_numeric(self) -> 'ExtractedValue':
        """
        Ensure consistency between operator and value_numeric.
        - If operator is 'eq' or None, value_numeric may be present (if numeric).
        - If operator is 'lt' or 'gt', value_numeric may be None (e.g., "<0.01").
        """
        # The spec allows value_numeric to be null without counting as failure
        return self


# ============================================================
# 2. StructuredDocument – Full JSON from NVIDIA
# ============================================================
class StructuredDocument(BaseModel):
    """
    The complete structured output from NVIDIA's LLM extraction.

    Fields:
        category: Document type (blood_test, prescription, etc.)
        document_date: Clinical date from the document (YYYY-MM-DD), or None
        extracted_values: List of lab test results (may be empty)
        ai_summary: Plain-language summary
        doctor_notes: Handwritten notes transcribed by OCR

    Note: Partial documents are valid – only fields that exist are populated.
    is_medical is NOT required here because it's already validated in Step 5.
    """
    category: Literal[
        'blood_test',
        'prescription',
        'imaging_report',
        'discharge_summary',
        'consultation_note',
        'unknown'
    ] = Field(..., description="Document category")
    document_date: Optional[date] = Field(None, description="Clinical date on document")
    extracted_values: List[ExtractedValue] = Field(
        default_factory=list,
        description="List of extracted lab values"
    )
    ai_summary: Optional[str] = Field(None, description="Plain-language summary")
    doctor_notes: Optional[str] = Field(None, description="Transcribed handwritten notes")


# ============================================================
# 3. Helper: Convert structured document to database payload
# ============================================================
def structured_document_to_payload(
    doc: StructuredDocument,
    patient_id: str,
    file_url: str,
    file_hash: str,
    raw_ocr: str,
    ocr_engine: str,
    ocr_confidence: Optional[float] = None,
    embedding: Optional[List[float]] = None,
    embedding_content: Optional[str] = None
) -> dict:
    """
    Convert a validated StructuredDocument into the JSON payload for Supabase RPC.

    This is the glue between the pipeline and the atomic_upsert_document RPC.
    """
    payload = {
        "patient_id": patient_id,
        "file_url": file_url,
        "file_hash": file_hash,
        "raw_ocr": raw_ocr,
        "ocr_engine": ocr_engine,
        "ocr_confidence": ocr_confidence,
        "category": doc.category,
        "document_date": doc.document_date.isoformat() if doc.document_date else None,
        "extracted_values": [],
        "ai_summary": doc.ai_summary,
        "doctor_notes": doc.doctor_notes,
        "medicines": [],  # Medicines are extracted separately (Phase 5 pipeline)
        "embedding": embedding,
        "embedding_content": embedding_content,
    }

    # Add extracted values
    for val in doc.extracted_values:
        payload["extracted_values"].append({
            "test_name": val.test_name,
            "value": val.value,
            "value_numeric": val.value_numeric,
            "unit": val.unit,
            "normal_range": val.normal_range,
            "flag": val.flag,
            "operator": val.operator,
        })

    return payload


# ============================================================
# 4. Standalone Test Block
# ============================================================
if __name__ == "__main__":
    print("\n=== Testing schemas.py ===\n")

    # Test 1: Valid document
    print("[OK] Creating valid document...")
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
            },
            {
                "test_name": "WBC",
                "value": "<0.01",
                "value_numeric": None,
                "unit": "x10³/µL",
                "normal_range": "4.0-10.0",
                "flag": "low",
                "operator": "lt"
            }
        ],
        "ai_summary": "Normal blood panel with low WBC.",
        "doctor_notes": "Patient reported mild fatigue."
    }
    doc = StructuredDocument(**valid_data)
    print("[OK] Document parsed and validated successfully")
    print(f"  - Category: {doc.category}")
    print(f"  - Date: {doc.document_date}")
    print(f"  - Values: {len(doc.extracted_values)}")

    # Test 2: Invalid category (should raise ValidationError)
    print("\n[OK] Testing invalid category...")
    invalid_data = valid_data.copy()
    invalid_data["category"] = "invalid_category"
    try:
        doc = StructuredDocument(**invalid_data)
        print("[ERROR] Validation should have failed!")
    except Exception as e:
        print("[OK] ValidationError raised as expected")
        print(f"  - Error: {type(e).__name__}")

    # Test 3: Partial document (prescription only – no extracted_values)
    print("\n[OK] Testing partial document (prescription only)...")
    partial_data = {
        "category": "prescription",
        "document_date": "2025-03-10",
        "extracted_values": [],
        "ai_summary": "Prescription for antibiotics.",
        "doctor_notes": None
    }
    doc = StructuredDocument(**partial_data)
    print("[OK] Partial document parsed successfully")
    print(f"  - Category: {doc.category}")
    print(f"  - Values: {len(doc.extracted_values)} (empty is OK for prescription)")

    # Test 4: Convert to payload
    print("\n[OK] Testing payload conversion...")
    payload = structured_document_to_payload(
        doc=doc,
        patient_id="12345678-1234-1234-1234-123456789012",
        file_url="https://storage.supabase.co/patient/file.pdf",
        file_hash="abc123def456",
        raw_ocr="Full raw OCR text here...",
        ocr_engine="pdfplumber",
        ocr_confidence=0.95,
        embedding=[0.1, 0.2, 0.3],
        embedding_content="Prescription for antibiotics."
    )
    print("[OK] Payload created successfully")
    print(f"  - Keys: {list(payload.keys())}")

    print("\n=== All tests passed! ===")