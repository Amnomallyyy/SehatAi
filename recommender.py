#!/usr/bin/env python3
"""
recommender.py – Main recommendation engine with personalization & session support.
"""

import os
import json
import uuid
from typing import Dict, List, Optional, Any
from datetime import datetime
from supabase import create_client
from dotenv import load_dotenv

from retrieval import RetrievalEngine
from safety import SafetyValidator
from clients import NVIDIAClient
from personalization import PersonalizationEngine
from session_manager import ChatSessionManager

load_dotenv()

def get_supabase():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in .env")
    return create_client(url, key)

class RecommendationGenerator:
    def __init__(self, enable_ai_safety: bool = True):
        self.retrieval = RetrievalEngine()
        self.nvidia = NVIDIAClient()
        self.safety = SafetyValidator(llm_client=self.nvidia, enable_ai=enable_ai_safety)
        self.personalization = PersonalizationEngine()
        self.session_manager = ChatSessionManager()
        self.supabase = get_supabase()
        print("✅ RecommendationGenerator initialized")
        print(f"   AI safety: {'ON' if enable_ai_safety else 'OFF'}")

    def generate(self, patient_id: str, query: str, session_id: Optional[str] = None) -> Dict:
        print(f"\n🚀 Generating recommendation for patient: {patient_id}")
        print(f"📝 Query: {query}")

        session_id = self.session_manager.get_or_create_session(patient_id, session_id)
        print(f"🆔 Session: {session_id}")

        history = self.session_manager.get_session_history(session_id, limit=5)
        context = self.retrieval.get_complete_context(patient_id)
        prefs = self.personalization.get_preferences(patient_id)

        safety_result = self.safety.run_all_checks(
            query=query,
            patient_data=context['patient'],
            interactions=context['external'].get('interactions', [])
        )
        print(f"🛡️ Safety status: {safety_result['status']}")

        prompt = self._build_prompt(query, context, safety_result, prefs, history)

        try:
            response_text = self.nvidia._chat([{"role": "user", "content": prompt}])
            recommendation = self._parse_response(response_text)
        except Exception as e:
            print(f"❌ LLM call failed: {e}")
            recommendation = self._fallback_recommendation(query, context, safety_result)

        recommendation = self.personalization.process_query(patient_id, query, recommendation)

        foods = [item.get('food') for item in recommendation.get('specific_foods', [])]
        if foods:
            food_safety = self.safety.screen_foods(
                foods,
                context['patient']['medications'],
                context['patient']['allergies'],
                context['external'].get('interactions', [])
            )
            recommendation['food_safety'] = food_safety

        self.session_manager.add_message(session_id, 'user', query)
        self.session_manager.add_message(session_id, 'assistant', recommendation.get('recommendation', ''))

        self._store_recommendation(patient_id, query, recommendation, context, safety_result, session_id)

        recommendation['session_id'] = session_id
        return recommendation

    def _build_prompt(self, query: str, context: Dict, safety: Dict, prefs: Dict, history: List[Dict]) -> str:
        patient = context['patient']
        external = context['external']

        labs_str = "\n".join([f"- {lab.get('test_name')}: {lab.get('value')} {lab.get('unit', '')}" for lab in patient.get('labs', [])])
        meds_str = "\n".join([f"- {med.get('name')} {med.get('dosage', '')}" for med in patient.get('medications', [])])
        allergies_str = ", ".join(patient.get('allergies', [])) or "None reported"
        guidelines_str = "\n".join([f"- {g.get('source')}: {g.get('guideline')[:200]}..." for g in external.get('guidelines', [])])
        interactions_str = "\n".join([f"- {i.get('drug')} + {i.get('food')}: {i.get('description')}" for i in external.get('interactions', [])])
        nutrition_str = ""
        for food, data in external.get('nutrition', {}).items():
            if data:
                nutrition_str += f"- {data.get('name')}: {data.get('calories')} cal, protein {data.get('protein_g')}g, carbs {data.get('carbs_g')}g\n"
        warnings_str = "\n".join(safety.get('warnings', [])) or "No critical warnings."

        prefs_str = ""
        if prefs:
            if prefs.get('favorite_foods'):
                prefs_str += f"- Favorite foods: {', '.join(prefs['favorite_foods'])}\n"
            if prefs.get('disliked_foods'):
                prefs_str += f"- Disliked foods: {', '.join(prefs['disliked_foods'])}\n"
            if prefs.get('food_allergies'):
                prefs_str += f"- Food allergies: {', '.join(prefs['food_allergies'])}\n"
            if prefs.get('dietary_restrictions'):
                prefs_str += f"- Dietary restrictions: {', '.join(prefs['dietary_restrictions'])}\n"
        else:
            prefs_str = "No stored preferences yet."

        history_str = ""
        if history:
            history_str = "Previous conversation (for context):\n"
            for msg in history:
                history_str += f"{msg['role'].capitalize()}: {msg['content']}\n"
            history_str += "\n"
        else:
            history_str = "(No previous conversation)\n"

        prompt = f"""
You are a clinical dietitian. Use the patient data and conversation history to give personalised dietary advice.

PATIENT PROFILE:
- Name: {patient.get('patient_name', 'Unknown')}
- Date of Birth: {patient.get('date_of_birth', 'Unknown')}
- Allergies: {allergies_str}

PATIENT PREFERENCES:
{prefs_str}

{history_str}

CURRENT LAB VALUES:
{labs_str}

ACTIVE MEDICATIONS:
{meds_str}

CLINICAL GUIDELINES:
{guidelines_str}

DRUG‑FOOD INTERACTIONS:
{interactions_str}

NUTRITIONAL DATA (reference):
{nutrition_str}

SAFETY WARNINGS:
{warnings_str}

PATIENT QUERY:
"{query}"

INSTRUCTIONS:
Generate a comprehensive dietary recommendation in JSON with these fields:
- "recommendation": summary of what to eat/avoid.
- "reasoning": clinical reasoning.
- "specific_foods": list of foods with quantities, e.g. [{{"food": "Oatmeal", "quantity": "½ cup"}}].
- "meal_timing": when to eat.
- "warnings": additional warnings.
- "follow_up_questions": up to 3 questions.

Be practical, avoid medical jargon, and always advise consulting a doctor.
"""
        return prompt

    def _parse_response(self, response_text: str) -> Dict:
        try:
            return json.loads(response_text)
        except json.JSONDecodeError:
            import re
            match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if match:
                return json.loads(match.group())
            else:
                raise ValueError("No JSON found in response")

    def _fallback_recommendation(self, query: str, context: Dict, safety: Dict) -> Dict:
        return {
            "recommendation": "Based on your profile, we suggest a balanced diet with whole grains, lean proteins, and vegetables. Please consult your doctor for personalized advice.",
            "reasoning": "Fallback due to LLM error.",
            "specific_foods": [{"food": "Oatmeal", "quantity": "½ cup"}, {"food": "Chicken breast", "quantity": "4 oz"}],
            "meal_timing": "Eat small, frequent meals.",
            "warnings": safety.get('warnings', []),
            "follow_up_questions": ["What are your typical meals?", "Do you have food preferences?", "What is your activity level?"]
        }

    def _store_recommendation(self, patient_id: str, query: str, recommendation: Dict, context: Dict, safety: Dict, session_id: str):
        try:
            rec_data = {
                'patient_id': patient_id,
                'session_id': session_id,
                'recommendation': recommendation.get('recommendation', ''),
                'reasoning': recommendation.get('reasoning', ''),
                'risk_score': safety.get('risk_score', 0.0),
                'confidence_score': 0.8,
                'safety_warnings': safety.get('warnings', []),
                'generated_at': datetime.now().isoformat()
            }
            self.supabase.table('diet_recommendations').insert(rec_data).execute()
            print("✅ Recommendation stored in database.")
        except Exception as e:
            print(f"⚠️ Failed to store recommendation: {e}")

# ============================================================
# Helper for session auto‑save (local file)
# ============================================================
SESSION_FILE_PREFIX = ".session_"

def get_stored_session_id(patient_id: str) -> Optional[str]:
    filepath = f"{SESSION_FILE_PREFIX}{patient_id}"
    if os.path.exists(filepath):
        with open(filepath, 'r') as f:
            return f.read().strip()
    return None

def store_session_id(patient_id: str, session_id: str):
    filepath = f"{SESSION_FILE_PREFIX}{patient_id}"
    with open(filepath, 'w') as f:
        f.write(session_id)

def generate_recommendation(patient_id: str, query: str, session_id: Optional[str] = None, enable_ai_safety: bool = True) -> Dict:
    if session_id is None:
        session_id = get_stored_session_id(patient_id)
        if session_id:
            print(f"ℹ️ Using existing session: {session_id}")
        else:
            print("ℹ️ No existing session found – will create a new one.")
    generator = RecommendationGenerator(enable_ai_safety=enable_ai_safety)
    result = generator.generate(patient_id, query, session_id)
    store_session_id(patient_id, result['session_id'])
    return result