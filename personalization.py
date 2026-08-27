#!/usr/bin/env python3
"""
personalization.py – Personalization Engine for DietBot.

Detects feedback from patient queries, updates preferences in Supabase,
and filters recommendations based on stored preferences.
"""

import re
from typing import Dict, List, Optional, Tuple
from supabase import create_client
from dotenv import load_dotenv
import os
from datetime import datetime   # <-- FIXED: import the class, not the module

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
# 2. Personalization Engine
# ============================================================
class PersonalizationEngine:
    def __init__(self):
        self.supabase = get_supabase()
        print("✅ PersonalizationEngine initialized")

    # ---------- Feedback Extraction ----------
    def extract_feedback(self, query: str) -> Dict[str, List[str]]:
        """
        Parse the query for likes, dislikes, and allergies.
        Returns a dict with keys: likes, dislikes, allergies.
        """
        query_lower = query.lower()
        result = {"likes": [], "dislikes": [], "allergies": []}

        # Patterns
        like_patterns = [
            r"(?:i\s+like|i\s+love|i\s+enjoy|i\s+prefer)\s+(\w+)",
            r"(?:like|love|enjoy)\s+(\w+)"
        ]
        dislike_patterns = [
            r"(?:i\s+hate|i\s+dislike|i\s+don'?t\s+like|i\s+can'?t\s+stand)\s+(\w+)",
            r"(?:hate|dislike|don'?t\s+like)\s+(\w+)"
        ]
        allergy_patterns = [
            r"(?:i\s+am\s+allergic\s+to|i\'?m\s+allergic\s+to|allergy\s+to)\s+(\w+)",
            r"(?:allergic\s+to)\s+(\w+)"
        ]

        # Extract likes
        for pattern in like_patterns:
            matches = re.findall(pattern, query_lower)
            for match in matches:
                if match and match not in result["likes"]:
                    result["likes"].append(match.strip())

        # Extract dislikes
        for pattern in dislike_patterns:
            matches = re.findall(pattern, query_lower)
            for match in matches:
                if match and match not in result["dislikes"]:
                    result["dislikes"].append(match.strip())

        # Extract allergies
        for pattern in allergy_patterns:
            matches = re.findall(pattern, query_lower)
            for match in matches:
                if match and match not in result["allergies"]:
                    result["allergies"].append(match.strip())

        return result

    # ---------- Update Preferences ----------
    def update_preferences(self, patient_id: str, feedback: Dict[str, List[str]]) -> Dict:
        """
        Merge new feedback into existing preferences in Supabase.
        Returns the updated preferences.
        """
        # Fetch current preferences
        current = self.get_preferences(patient_id)

        # Merge arrays (append new items, avoid duplicates)
        updated = {
            "dietary_restrictions": list(set(current.get("dietary_restrictions", []) + feedback.get("allergies", []))),
            "food_allergies": list(set(current.get("food_allergies", []) + feedback.get("allergies", []))),
            "disliked_foods": list(set(current.get("disliked_foods", []) + feedback.get("dislikes", []))),
            "favorite_foods": list(set(current.get("favorite_foods", []) + feedback.get("likes", []))),
            "updated_at": datetime.now().isoformat()   # <-- now works
        }

        # Upsert into Supabase
        try:
            # Check if a record exists for this patient
            existing = self.supabase.table("diet_patient_preferences") \
                .select("id") \
                .eq("patient_id", patient_id) \
                .execute()

            if existing.data:
                # Update existing
                self.supabase.table("diet_patient_preferences") \
                    .update(updated) \
                    .eq("patient_id", patient_id) \
                    .execute()
            else:
                # Insert new
                updated["patient_id"] = patient_id
                self.supabase.table("diet_patient_preferences") \
                    .insert(updated) \
                    .execute()

            print(f"✅ Preferences updated for patient {patient_id}")
            return updated
        except Exception as e:
            print(f"⚠️ Failed to update preferences: {e}")
            return current

    # ---------- Get Preferences ----------
    def get_preferences(self, patient_id: str) -> Dict:
        """Fetch current preferences for a patient."""
        try:
            result = self.supabase.table("diet_patient_preferences") \
                .select("dietary_restrictions, food_allergies, disliked_foods, favorite_foods") \
                .eq("patient_id", patient_id) \
                .execute()
            if result.data and len(result.data) > 0:
                return result.data[0]
            else:
                # Return empty defaults
                return {
                    "dietary_restrictions": [],
                    "food_allergies": [],
                    "disliked_foods": [],
                    "favorite_foods": []
                }
        except Exception as e:
            print(f"⚠️ Failed to fetch preferences: {e}")
            return {}

    # ---------- Filter Recommendation ----------
    def filter_recommendation(self, recommendation: Dict, preferences: Dict) -> Dict:
        """
        Remove foods that are disliked or allergic from the recommendation.
        Optionally boost liked foods (add extra context).
        Returns the filtered recommendation.
        """
        if not preferences:
            return recommendation

        disliked = preferences.get("disliked_foods", [])
        allergies = preferences.get("food_allergies", [])
        likes = preferences.get("favorite_foods", [])

        # Filter specific_foods
        if "specific_foods" in recommendation and recommendation["specific_foods"]:
            filtered_foods = []
            for item in recommendation["specific_foods"]:
                food_name = item.get("food", "").lower()
                # Check if food is disliked or allergenic
                if any(dislike in food_name or food_name in dislike for dislike in disliked):
                    continue
                if any(allergy in food_name or food_name in allergy for allergy in allergies):
                    continue
                filtered_foods.append(item)

            recommendation["specific_foods"] = filtered_foods

            # Add a note if any foods were removed
            if len(filtered_foods) < len(recommendation.get("specific_foods", [])):
                if "warnings" not in recommendation:
                    recommendation["warnings"] = []
                recommendation["warnings"].append(
                    "⚠️ Some foods were removed because they matched your dislikes or allergies."
                )

        # Optionally boost likes in reasoning or add a note
        if likes and "reasoning" in recommendation:
            # Add a note about favorites
            recommendation["reasoning"] += f"\n(We've included some of your favorite foods: {', '.join(likes[:3])}.)"

        return recommendation

    # ---------- Full Pipeline: Extract + Update + Filter ----------
    def process_query(self, patient_id: str, query: str, recommendation: Dict) -> Dict:
        """
        Main entry point for personalization.
        Extracts feedback, updates preferences, and filters the recommendation.
        Returns the filtered recommendation.
        """
        # 1. Extract feedback from query
        feedback = self.extract_feedback(query)
        if any(feedback.values()):
            print(f"🔍 Extracted feedback: {feedback}")
            # 2. Update preferences
            self.update_preferences(patient_id, feedback)

        # 3. Get current preferences
        prefs = self.get_preferences(patient_id)

        # 4. Filter recommendation
        filtered = self.filter_recommendation(recommendation, prefs)

        return filtered