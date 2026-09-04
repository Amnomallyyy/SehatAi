#!/usr/bin/env python3
"""
safety.py – Hybrid Safety System (Deterministic + AI)

Deterministic: emergency keywords, critical labs, interactions, allergies, risk score.
AI: catches edge cases (eosinophils, hormones, rare interactions) using the LLM.
"""

import os
import re
import json
from typing import Dict, List, Tuple, Optional

# ============================================================
# Deterministic Rules
# ============================================================

EMERGENCY_KEYWORDS = [
    'chest pain', 'heart attack', 'stroke', 'severe allergic',
    'anaphylaxis', 'unconscious', 'difficulty breathing',
    'shortness of breath', 'bleeding', 'seizure',
    'call 911', 'emergency', 'suicidal', 'overdose',
    'difficulty swallowing', 'swelling of face', 'hives',
    'sudden dizziness', 'confusion', 'slurred speech',
    'chest tightness', 'palpitations'
]

CRITICAL_LABS = {
    'hba1c': {'min': 0, 'max': 15, 'critical_high': 10},
    'ldl': {'min': 0, 'max': 500, 'critical_high': 190},
    'hdl': {'min': 0, 'max': 100, 'critical_low': 20},
    'triglycerides': {'min': 0, 'max': 1000, 'critical_high': 500},
    'creatinine': {'min': 0, 'max': 10, 'critical_high': 2.0},
    'glucose': {'min': 0, 'max': 500, 'critical_low': 50, 'critical_high': 250},
    'potassium': {'min': 0, 'max': 10, 'critical_low': 2.5, 'critical_high': 6.5},
    'sodium': {'min': 0, 'max': 200, 'critical_low': 120, 'critical_high': 160},
    'calcium': {'min': 0, 'max': 15, 'critical_low': 7, 'critical_high': 12},
    'hemoglobin': {'min': 0, 'max': 20, 'critical_low': 7, 'critical_high': 18},
    'wbc': {'min': 0, 'max': 50, 'critical_low': 1, 'critical_high': 30},
    'platelets': {'min': 0, 'max': 1000, 'critical_low': 20, 'critical_high': 800}
}

class SafetyValidator:
    def __init__(self, llm_client=None, enable_ai=True):
        self.llm_client = llm_client
        self.enable_ai = enable_ai and os.getenv("ENABLE_AI_SAFETY", "true").lower() == "true"
        print("✅ SafetyValidator initialized (deterministic + AI)" if self.enable_ai else "✅ SafetyValidator initialized (deterministic only)")

    # ---------- Deterministic methods (unchanged) ----------
    def detect_emergency(self, query: str) -> Tuple[bool, List[str]]:
        query_lower = query.lower()
        detected = [kw for kw in EMERGENCY_KEYWORDS if kw in query_lower]
        return (len(detected) > 0, detected)

    def validate_labs(self, labs: List[Dict]) -> Tuple[bool, List[str]]:
        warnings = []
        for lab in labs:
            test_name = lab.get('test_name', '').lower()
            value = lab.get('value_numeric')
            if value is None:
                continue
            for key, thresholds in CRITICAL_LABS.items():
                if key in test_name:
                    if 'critical_high' in thresholds and value > thresholds['critical_high']:
                        warnings.append(f"🚨 CRITICAL: {test_name} = {value} (high)")
                    if 'critical_low' in thresholds and value < thresholds['critical_low']:
                        warnings.append(f"🚨 CRITICAL: {test_name} = {value} (low)")
                    break
        return (len(warnings) > 0, warnings)

    def check_interactions_for_food(
        self,
        food: str,
        medications: List[Dict],
        interactions: List[Dict]
    ) -> Tuple[bool, List[str]]:
        warnings = []
        food_lower = food.lower()
        for med in medications:
            med_name = med.get('name', '').lower()
            for interaction in interactions:
                inter_drug = interaction.get('drug', '').lower()
                inter_food = interaction.get('food', '').lower()
                if med_name in inter_drug or inter_drug in med_name:
                    if food_lower in inter_food or inter_food in food_lower:
                        severity = interaction.get('severity', 'moderate')
                        desc = interaction.get('description', '')
                        rec = interaction.get('recommendation', '')
                        warning = f"⚠️ {interaction.get('drug')} + {interaction.get('food')}: {desc}"
                        if severity in ['severe', 'contraindicated']:
                            warnings.append(f"🚨 {warning}")
                        else:
                            warnings.append(f"⚠️ {warning}")
                        if rec:
                            warnings.append(f"   → {rec}")
        return (len(warnings) > 0, warnings)

    def check_allergies_for_food(
        self,
        food: str,
        allergies: List[str]
    ) -> Tuple[bool, List[str]]:
        warnings = []
        food_lower = food.lower()
        for allergy in allergies:
            allergy_lower = allergy.lower()
            if allergy_lower in food_lower or food_lower in allergy_lower:
                warnings.append(f"🚨 ALLERGY: {food} contains {allergy}")
        return (len(warnings) > 0, warnings)

    def calculate_risk_score(
        self,
        query: str,
        labs: List[Dict],
        medications: List[Dict],
        allergies: List[str],
        interactions: List[Dict]
    ) -> float:
        score = 0.0
        is_emergency, _ = self.detect_emergency(query)
        if is_emergency:
            score += 0.4
        has_critical, _ = self.validate_labs(labs)
        if has_critical:
            score += 0.2
        if len(medications) >= 5:
            score += 0.1
        elif len(medications) >= 3:
            score += 0.05
        if len(allergies) >= 3:
            score += 0.1
        elif len(allergies) >= 1:
            score += 0.05
        severe_count = sum(1 for i in interactions if i.get('severity') in ['severe', 'contraindicated'])
        if severe_count > 0:
            score += min(0.2, severe_count * 0.05)
        return min(score, 1.0)

    def screen_foods(
        self,
        foods: List[str],
        medications: List[Dict],
        allergies: List[str],
        interactions: List[Dict]
    ) -> Dict:
        results = {}
        for food in foods:
            food_warnings = []
            has_int, int_warns = self.check_interactions_for_food(food, medications, interactions)
            if has_int:
                food_warnings.extend(int_warns)
            has_alg, alg_warns = self.check_allergies_for_food(food, allergies)
            if has_alg:
                food_warnings.extend(alg_warns)
            results[food] = {
                'safe': len(food_warnings) == 0,
                'warnings': food_warnings
            }
        return results

    # ---------- Deterministic checks (aggregated) ----------
    def _deterministic_checks(self, query: str, patient_data: Dict, interactions: List[Dict]) -> Dict:
        labs = patient_data.get('labs', [])
        medications = patient_data.get('medications', [])
        allergies = patient_data.get('allergies', [])

        is_emergency, emergency_keywords = self.detect_emergency(query)
        has_critical_lab, lab_warnings = self.validate_labs(labs)

        # Check common foods for interactions
        common_foods = ['grapefruit', 'alcohol', 'high-sugar', 'dairy', 'fatty food',
                        'banana', 'orange', 'potato', 'spinach', 'cheese', 'yogurt']
        interaction_warnings = []
        has_interaction = False
        for food in common_foods:
            has, warns = self.check_interactions_for_food(food, medications, interactions)
            if has:
                has_interaction = True
                interaction_warnings.extend(warns)

        # Check common allergens
        common_allergens = ['peanut', 'eggs', 'shellfish', 'milk', 'soy', 'wheat',
                            'tree nuts', 'fish', 'sesame']
        allergy_warnings = []
        has_allergy = False
        for food in common_allergens:
            has, warns = self.check_allergies_for_food(food, allergies)
            if has:
                has_allergy = True
                allergy_warnings.extend(warns)

        risk_score = self.calculate_risk_score(query, labs, medications, allergies, interactions)

        all_warnings = []
        if is_emergency:
            all_warnings.append(f"🚨 EMERGENCY: {', '.join(emergency_keywords)}")
        if has_critical_lab:
            all_warnings.extend(lab_warnings)
        if has_interaction:
            all_warnings.extend(interaction_warnings)
        if has_allergy:
            all_warnings.extend(allergy_warnings)

        if is_emergency or risk_score > 0.7:
            status = 'critical'
        elif risk_score > 0.3:
            status = 'warning'
        else:
            status = 'safe'

        return {
            'is_emergency': is_emergency,
            'has_critical_lab': has_critical_lab,
            'has_interaction': has_interaction,
            'has_allergy': has_allergy,
            'risk_score': risk_score,
            'warnings': all_warnings,
            'status': status
        }

    # ---------- AI Analysis ----------
    def _ai_safety_analysis(self, query: str, patient_data: Dict, interactions: List[Dict], det_warnings: List[str]) -> List[str]:
        if not self.llm_client or not self.enable_ai:
            return []

        labs_str = "\n".join([f"- {lab.get('test_name')}: {lab.get('value')} {lab.get('unit', '')}" for lab in patient_data.get('labs', [])])
        meds_str = "\n".join([f"- {med.get('name')} {med.get('dosage', '')}" for med in patient_data.get('medications', [])])
        allergies_str = ", ".join(patient_data.get('allergies', [])) or "None"
        interactions_str = "\n".join([f"- {i.get('drug')} + {i.get('food')}: {i.get('description')}" for i in interactions])

        prompt = f"""
You are a clinical safety assistant. Given the patient data and query below, identify any additional safety risks not already flagged.

Existing warnings (already flagged):
{chr(10).join(det_warnings) if det_warnings else "None"}

Patient: {patient_data.get('patient_name', 'Unknown')}
Allergies: {allergies_str}

Labs:
{labs_str}

Medications:
{meds_str}

Known interactions:
{interactions_str}

Patient query: "{query}"

Return a JSON array of warning strings (each as a sentence). Use "🚨" prefix for critical risks, "⚠️" for moderate. If no additional risks, return [].
Example: ["🚨 High eosinophil count may indicate allergic reaction to medication.", "⚠️ Calcium supplement may interact with Lisinopril."]

Return ONLY the JSON array.
"""
        try:
            response = self.llm_client._chat([{"role": "user", "content": prompt}])
            # Extract JSON array
            match = re.search(r'\[.*\]', response, re.DOTALL)
            if match:
                warnings = json.loads(match.group())
                if isinstance(warnings, list):
                    return warnings
            else:
                warnings = json.loads(response)
                if isinstance(warnings, list):
                    return warnings
        except Exception as e:
            print(f"⚠️ AI safety analysis failed: {e}")
        return []

    # ---------- Main entry point ----------
    def run_all_checks(self, query: str, patient_data: Dict, interactions: List[Dict]) -> Dict:
        # 1. Deterministic
        det_result = self._deterministic_checks(query, patient_data, interactions)

        # 2. AI analysis
        ai_warnings = []
        if self.enable_ai and self.llm_client:
            ai_warnings = self._ai_safety_analysis(query, patient_data, interactions, det_result['warnings'])

        # Merge warnings
        all_warnings = det_result['warnings'] + ai_warnings

        # Keep deterministic risk
        risk_score = det_result['risk_score']
        has_ai_critical = any("🚨" in w for w in ai_warnings)
        if det_result['status'] == 'critical' or has_ai_critical:
            status = 'critical'
        elif det_result['status'] == 'warning' or ai_warnings:
            status = 'warning'
        else:
            status = 'safe'

        return {
            'is_emergency': det_result['is_emergency'],
            'has_critical_lab': det_result['has_critical_lab'],
            'has_interaction': det_result['has_interaction'],
            'has_allergy': det_result['has_allergy'],
            'risk_score': risk_score,
            'warnings': all_warnings,
            'status': status,
            'ai_warnings': ai_warnings
        }


# ============================================================
# Test (if run directly)
# ============================================================
if __name__ == "__main__":
    # Quick test without LLM
    safety = SafetyValidator()
    query = "I have chest pain"
    patient_data = {'labs': [{'test_name': 'HbA1c', 'value_numeric': 10.5}], 'medications': [], 'allergies': []}
    result = safety.run_all_checks(query, patient_data, [])
    print(result)