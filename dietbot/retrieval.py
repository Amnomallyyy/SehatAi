#!/usr/bin/env python3
"""
retrieval.py – Core retrieval engine for DietBot.

Fetches patient data from Supabase and external APIs,
then merges into a unified context with source flags.
"""

import os
import re
from typing import Dict, List, Optional, Any, Tuple
from supabase import create_client
from dotenv import load_dotenv

from clients import (
    HuggingFaceClient,
    MedDataClient,
    USDAClient,
    TheMealDBClient
)

load_dotenv()

# ============================================================
# 1. Supabase Client
# ============================================================
def get_supabase():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in .env")
    return create_client(url, key)

# ============================================================
# 2. Patient Data Fetcher
# ============================================================
class PatientDataFetcher:
    def __init__(self):
        self.supabase = get_supabase()

    def fetch_patient_data(self, patient_id: str) -> Dict:
        """Fetch labs, medications, allergies, and demographics."""
        print(f"🔍 Fetching patient data for: {patient_id}")

        # Labs (latest 10)
        labs = self.supabase.table('extracted_data') \
            .select('test_name, value, value_numeric, unit, normal_range, flag') \
            .eq('patient_id', patient_id) \
            .order('recorded_at', desc=True) \
            .limit(10) \
            .execute()
        labs_data = labs.data if labs.data else []

        # Active medications
        meds = self.supabase.table('medicines') \
            .select('name, dosage, start_date, active') \
            .eq('patient_id', patient_id) \
            .eq('active', True) \
            .execute()
        meds_data = meds.data if meds.data else []

        # Clinical advice (to extract allergies)
        advice = self.supabase.table('clinical_advice') \
            .select('content') \
            .eq('patient_id', patient_id) \
            .execute()
        advice_data = advice.data if advice.data else []

        # Allergies extraction
        allergies = self._extract_allergies(advice_data)

        # Demographics
        patient = self.supabase.table('patients') \
            .select('name, date_of_birth') \
            .eq('id', patient_id) \
            .execute()
        patient_info = patient.data[0] if patient.data else {}

        return {
            'patient_id': patient_id,
            'patient_name': patient_info.get('name', 'Unknown'),
            'date_of_birth': patient_info.get('date_of_birth'),
            'labs': labs_data,
            'medications': meds_data,
            'allergies': allergies
        }

    def _extract_allergies(self, advice_records: List[Dict]) -> List[str]:
        """Extract allergies from clinical advice text."""
        allergy_keywords = ['allergic', 'allergy', 'hypersensitivity', 'intolerant', 'reaction to']
        allergies = []
        for record in advice_records:
            content = record.get('content', '').lower()
            for keyword in allergy_keywords:
                if keyword in content:
                    # Simple regex to extract allergen
                    patterns = [
                        r'allergic to ([^.,;]+)',
                        r'allergy to ([^.,;]+)',
                        r'intolerant to ([^.,;]+)',
                        r'reaction to ([^.,;]+)'
                    ]
                    for pattern in patterns:
                        match = re.search(pattern, content)
                        if match:
                            allergen = match.group(1).strip()
                            if allergen and allergen not in allergies:
                                allergies.append(allergen)
        return allergies

# ============================================================
# 3. External Context Fetcher
# ============================================================
class ExternalContextFetcher:
    def __init__(self):
        self.hf_client = HuggingFaceClient()
        self.med_client = MedDataClient()
        self.usda_client = USDAClient()
        self.meal_client = TheMealDBClient()

    def fetch_external_context(self, patient_data: Dict) -> Dict:
        """Fetch guidelines, interactions, nutrition, recipes with source flags."""
        print("🔍 Fetching external context...")

        # Determine condition for guidelines
        condition = self._determine_condition(patient_data)

        # 1. Guidelines
        guidelines, guidelines_source = self.hf_client.fetch_guidelines(condition)

        # 2. Interactions (check each medication)
        all_interactions = []
        interactions_source = None
        for med in patient_data.get('medications', []):
            drug_name = med.get('name', '')
            if drug_name:
                interactions, source = self.med_client.fetch_interactions(drug_name)
                all_interactions.extend(interactions)
                interactions_source = source  # Use the last source (all should be consistent)

        # 3. Nutrition (common foods)
        common_foods = ['oatmeal', 'banana', 'apple', 'chicken', 'rice', 'yogurt', 'almond']
        nutrition = {}
        nutrition_sources = {}
        for food in common_foods:
            data, source = self.usda_client.fetch_food(food)
            nutrition[food] = data
            nutrition_sources[food] = source

        # 4. Recipes
        recipes, recipes_source = self.meal_client.fetch_recipes("breakfast")

        return {
            'guidelines': guidelines,
            'guidelines_source': guidelines_source,
            'interactions': all_interactions,
            'interactions_source': interactions_source,
            'nutrition': nutrition,
            'nutrition_sources': nutrition_sources,
            'recipes': recipes,
            'recipes_source': recipes_source
        }

    def _determine_condition(self, patient_data: Dict) -> str:
        """Infer condition from labs."""
        for lab in patient_data.get('labs', []):
            test = lab.get('test_name', '').lower()
            val = lab.get('value_numeric')
            if 'hba1c' in test and val and val > 7.0:
                return 'diabetes'
            if 'ldl' in test and val and val > 100:
                return 'heart_disease'
            if 'creatinine' in test and val and val > 1.5:
                return 'ckd'
        return 'diabetes'  # default

# ============================================================
# 4. Main Retrieval Engine
# ============================================================
class RetrievalEngine:
    def __init__(self):
        self.patient_fetcher = PatientDataFetcher()
        self.external_fetcher = ExternalContextFetcher()

    def get_complete_context(self, patient_id: str) -> Dict:
        """Fetch all patient data and external context, return unified context."""
        print(f"📦 Building complete context for: {patient_id}")

        patient_data = self.patient_fetcher.fetch_patient_data(patient_id)
        external_data = self.external_fetcher.fetch_external_context(patient_data)

        return {
            'patient': patient_data,
            'external': external_data,
            'timestamp': None  # Can be added later
        }

    def get_patient_latest_values(self, patient_id: str) -> Dict:
        """Helper: return latest key lab values."""
        labs = self.patient_fetcher.fetch_patient_data(patient_id).get('labs', [])
        latest = {}
        for lab in labs:
            test = lab.get('test_name', '').lower()
            val = lab.get('value_numeric')
            if 'hba1c' in test:
                latest['hba1c'] = val
            elif 'ldl' in test:
                latest['ldl'] = val
            elif 'hdl' in test:
                latest['hdl'] = val
            elif 'triglycerides' in test:
                latest['triglycerides'] = val
            elif 'creatinine' in test:
                latest['creatinine'] = val
        return latest

# ============================================================
# 5. Test (optional)
# ============================================================
if __name__ == "__main__":
    import sys
    engine = RetrievalEngine()
    patient_id = input("Enter patient ID: ").strip()
    if not patient_id:
        print("❌ No patient ID provided")
        sys.exit(1)
    try:
        context = engine.get_complete_context(patient_id)
        print("\n" + "="*60)
        print("✅ Context retrieved successfully")
        print("="*60)
        print(f"Patient: {context['patient']['patient_name']}")
        print(f"Labs: {len(context['patient']['labs'])} records")
        print(f"Medications: {len(context['patient']['medications'])} active")
        print(f"Allergies: {context['patient']['allergies']}")
        print(f"Guidelines source: {context['external']['guidelines_source']}")
        print(f"Interactions source: {context['external']['interactions_source']}")
        print(f"Nutrition sources: {set(context['external']['nutrition_sources'].values())}")
        print(f"Recipes source: {context['external']['recipes_source']}")
        print("="*60)
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)