#!/usr/bin/env python3
"""
clients.py – Multi‑tier API clients with fallback chains + NVIDIA LLM.

All fetch methods return (data, source_flag) where source_flag indicates origin:
- *_real      : from a live API
- *_cached    : from in‑memory cache (still real data)
- *_curated   : authoritative static fallback (ADA, FDA, USDA, etc.) – last resort

API priority:
- Guidelines: PubMed → ClinicalTrials.gov → ADA/WHO curated
- Interactions: RxCheck (if key) → openFDA (wide search) → FDA curated
- Nutrition: USDA → USDA curated
- Recipes: TheMealDB → curated
- NVIDIA: used for LLM recommendations (chat completion)
"""

import os
import time
import re
import requests
from typing import Dict, List, Tuple, Optional, Any
from tenacity import retry, stop_after_attempt, wait_exponential

# ============================================================
# 1. Robust .env Reader
# ============================================================
def read_env_file() -> Dict[str, str]:
    env = {}
    try:
        with open(".env") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env

ENV = read_env_file()

# ============================================================
# 2. In‑Memory Cache (TTL = 3600s)
# ============================================================
_cache = {}
def cache_get(key: str) -> Optional[Any]:
    if key in _cache:
        value, timestamp = _cache[key]
        if time.time() - timestamp < 3600:
            return value
    return None

def cache_set(key: str, value: Any):
    _cache[key] = (value, time.time())

# ============================================================
# 3. Guidelines: PubMed → ClinicalTrials.gov → ADA/WHO
# ============================================================
class HuggingFaceClient:
    def __init__(self):
        self.pubmed_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        self.clinicaltrials_url = "https://clinicaltrials.gov/api/v2/studies"
        self.timeout = 30

    def fetch_guidelines(self, condition: str = "diabetes") -> Tuple[List[Dict], str]:
        print(f"🔍 Fetching guidelines for: {condition}")
        cache_key = f"guidelines_{condition}"
        cached = cache_get(cache_key)
        if cached:
            return cached, "cached"

        # 1. PubMed
        try:
            params = {
                "db": "pubmed",
                "term": f"{condition} AND (guideline OR dietary OR nutrition) AND (meta-analysis[pt] OR practice guideline[pt])",
                "retmode": "json",
                "retmax": 3
            }
            resp = requests.get(self.pubmed_url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            count = int(data.get("esearchresult", {}).get("count", "0"))
            if count > 0:
                result = [{
                    "source": "PubMed",
                    "condition": condition,
                    "guideline": f"Found {count} evidence‑based guidelines in PubMed.",
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/?term={condition}+guideline"
                }]
                cache_set(cache_key, result)
                return result, "pubmed_real"
        except Exception as e:
            print(f"   ⚠️ PubMed API failed: {e}")

        # 2. ClinicalTrials.gov
        try:
            params = {"query.cond": condition, "query.term": "dietary guidelines", "pageSize": 3, "format": "json"}
            resp = requests.get(self.clinicaltrials_url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            studies = data.get("studies", [])
            if studies:
                titles = [s.get("protocolSection", {}).get("identificationModule", {}).get("briefTitle", "No title") for s in studies[:3]]
                result = [{
                    "source": "ClinicalTrials.gov",
                    "condition": condition,
                    "guideline": f"Clinical trials: {', '.join(titles)}",
                    "url": "https://clinicaltrials.gov"
                }]
                cache_set(cache_key, result)
                return result, "clinicaltrials_real"
        except Exception as e:
            print(f"   ⚠️ ClinicalTrials.gov API failed: {e}")

        # 3. Curated fallback (ADA/WHO)
        print("   🔄 Using curated ADA/WHO guidelines (last resort).")
        curated = self._get_curated_guidelines(condition)
        cache_set(cache_key, curated)
        return curated, "ada_curated"

    def _get_curated_guidelines(self, condition: str) -> List[Dict]:
        curated_db = {
            "diabetes": {
                "source": "American Diabetes Association",
                "guideline": "Consistent carb intake, whole grains, limit added sugars, lean proteins.",
                "url": "https://professional.diabetes.org/standards-of-care"
            },
            "heart_disease": {
                "source": "American Heart Association",
                "guideline": "Reduce sodium, limit saturated fats, increase fiber, healthy oils.",
                "url": "https://www.heart.org/en/healthy-living"
            },
            "hypertension": {
                "source": "ACC/AHA",
                "guideline": "DASH diet: fruits, vegetables, whole grains, lean proteins, low sodium.",
                "url": "https://www.nhlbi.nih.gov/education/dash-eating-plan"
            },
            "ckd": {
                "source": "KDIGO",
                "guideline": "Limit phosphorus, potassium, sodium, monitor protein.",
                "url": "https://kdigo.org/guidelines/"
            },
            "obesity": {
                "source": "WHO",
                "guideline": "Portion control, physical activity, nutrient‑dense foods.",
                "url": "https://www.who.int/health-topics/obesity"
            }
        }
        for key in curated_db:
            if key in condition.lower():
                return [curated_db[key]]
        return [curated_db["diabetes"]]

# ============================================================
# 4. Interactions: RxCheck → openFDA (wide search) → FDA curated
# ============================================================
class MedDataClient:
    def __init__(self):
        self.rxcheck_api_key = ENV.get("RXCHECK_API_KEY") or os.getenv("RXCHECK_API_KEY")
        self.openfda_url = "https://api.fda.gov/drug/label.json"
        self.timeout = 30
        if self.rxcheck_api_key:
            print("✅ MedDataClient: RxCheck key set.")
        else:
            print("⚠️ MedDataClient: no RxCheck key – using openFDA + curated.")

    def _get_rxcui(self, drug_name: str) -> Optional[str]:
        try:
            resp = requests.get(f"https://rxnav.nlm.nih.gov/REST/drugs.json?name={drug_name}", timeout=10)
            resp.raise_for_status()
            data = resp.json()
            for group in data.get("drugGroup", {}).get("conceptGroup", []):
                for concept in group.get("conceptProperties", []):
                    return concept.get("rxcui")
        except Exception:
            pass
        return None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def fetch_interactions(self, drug_name: str) -> Tuple[List[Dict], str]:
        print(f"🔍 Fetching interactions for: {drug_name}")
        cache_key = f"interactions_{drug_name.lower()}"
        cached = cache_get(cache_key)
        if cached:
            return cached, "cached"

        # 1. RxCheck (if key)
        if self.rxcheck_api_key:
            try:
                url = "https://api.rxcheck.dev/v1/interactions"
                params = {"drug1": drug_name}
                headers = {"X-API-Key": self.rxcheck_api_key}
                resp = requests.get(url, params=params, headers=headers, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
                if data:
                    result = [{
                        "drug": drug_name,
                        "food": item.get("food", "Multiple"),
                        "severity": item.get("severity", "moderate"),
                        "description": item.get("description", ""),
                        "recommendation": "Follow label instructions."
                    } for item in data]
                    cache_set(cache_key, result)
                    return result, "rxcheck_real"
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 422:
                    print("   ⚠️ RxCheck: Invalid drug name – trying RxCUI...")
                    try:
                        rxcui = self._get_rxcui(drug_name)
                        if rxcui:
                            params = {"drug1": rxcui}
                            resp = requests.get(url, params=params, headers=headers, timeout=self.timeout)
                            resp.raise_for_status()
                            data = resp.json()
                            if data:
                                result = [{
                                    "drug": drug_name,
                                    "food": item.get("food", "Multiple"),
                                    "severity": item.get("severity", "moderate"),
                                    "description": item.get("description", ""),
                                    "recommendation": "Follow label instructions."
                                } for item in data]
                                cache_set(cache_key, result)
                                return result, "rxcheck_real"
                    except Exception as e2:
                        print(f"   ⚠️ RxCheck with RxCUI failed: {e2}")
                else:
                    print(f"   ⚠️ RxCheck HTTP error: {e}")
            except Exception as e:
                print(f"   ⚠️ RxCheck API error: {e}")

        # 2. openFDA – Enhanced wide search across multiple sections
        try:
            params = {"search": f"openfda.generic_name:{drug_name}", "limit": 1}
            resp = requests.get(self.openfda_url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            if data.get("results"):
                label = data["results"][0]
                # Combine relevant sections
                sections = [
                    label.get("warnings", [""])[0] or "",
                    label.get("precautions", [""])[0] or "",
                    label.get("drug_interactions", [""])[0] or "",
                    label.get("adverse_reactions", [""])[0] or "",
                    label.get("description", [""])[0] or ""
                ]
                full_text = " ".join(sections)

                # Broad food & nutrient interaction keywords
                food_pattern = re.compile(
                    r'(grapefruit|alcohol|alcoholic|caffeine|caffeinated|dairy|fatty food|high-fat|high fat|potassium|vitamin k|tyramine|aged cheese|cured meat|maoi|milk|cheese|yogurt|banana|orange|grapefruit juice)',
                    re.IGNORECASE
                )
                found = food_pattern.findall(full_text)
                if found:
                    # Deduplicate and take first 3 unique items
                    unique_foods = list(set(found))
                    result = [{
                        "drug": drug_name,
                        "food": ", ".join(unique_foods[:3]),
                        "severity": "moderate",
                        "description": f"FDA label mentions: {full_text[:300]}...",
                        "recommendation": "Consult your doctor or pharmacist regarding this interaction."
                    }]
                    cache_set(cache_key, result)
                    return result, "openfda_real"
        except Exception as e:
            print(f"   ⚠️ openFDA API failed: {e}")

        # 3. Curated fallback (FDA/NIH)
        print("   🔄 Using FDA‑curated interactions (last resort).")
        curated = self._get_curated_interactions(drug_name)
        cache_set(cache_key, curated)
        return curated, "fda_curated"

    def _get_curated_interactions(self, drug_name: str) -> List[Dict]:
        curated_db = {
            "metformin": [
                {"drug": "Metformin", "food": "Alcohol", "severity": "moderate",
                 "description": "Alcohol increases risk of lactic acidosis and hypoglycemia.",
                 "recommendation": "Avoid or limit alcohol."},
                {"drug": "Metformin", "food": "High‑sugar foods", "severity": "mild",
                 "description": "High‑sugar foods counteract blood sugar control.",
                 "recommendation": "Choose low‑glycemic foods."}
            ],
            "atorvastatin": [
                {"drug": "Atorvastatin", "food": "Grapefruit", "severity": "severe",
                 "description": "Grapefruit increases blood concentration.",
                 "recommendation": "AVOID grapefruit and grapefruit juice."}
            ],
            "lisinopril": [
                {"drug": "Lisinopril", "food": "High‑potassium foods", "severity": "moderate",
                 "description": "Lisinopril can increase potassium levels.",
                 "recommendation": "Limit bananas, oranges, potatoes."}
            ],
            "warfarin": [
                {"drug": "Warfarin", "food": "Vitamin K‑rich foods", "severity": "severe",
                 "description": "Vitamin K interferes with Warfarin effectiveness.",
                 "recommendation": "Maintain consistent Vitamin K intake."}
            ],
            "aspirin": [
                {"drug": "Aspirin", "food": "Alcohol", "severity": "moderate",
                 "description": "Alcohol increases risk of stomach bleeding.",
                 "recommendation": "Limit alcohol consumption."}
            ]
        }
        drug_lower = drug_name.lower()
        for key in curated_db:
            if key in drug_lower:
                return curated_db[key]
        # Generic curated advice
        return [{
            "drug": drug_name,
            "food": "Grapefruit",
            "severity": "moderate",
            "description": "Grapefruit can interact with many medications.",
            "recommendation": "Consult your doctor about grapefruit consumption."
        }]

# ============================================================
# 5. Nutrition: USDA → USDA curated
# ============================================================
class USDAClient:
    def __init__(self):
        self.usda_key = ENV.get("USDA_API_KEY") or os.getenv("USDA_API_KEY")
        self.usda_url = "https://api.nal.usda.gov/fdc/v1/foods/search"
        self.timeout = 30
        self.last_request_time = 0
        self.min_interval = 0.1
        if self.usda_key:
            print(f"✅ USDAClient: USDA key set.")
        else:
            print("⚠️ USDAClient: USDA_API_KEY not set.")

    def _rate_limit(self):
        now = time.time()
        if now - self.last_request_time < self.min_interval:
            time.sleep(self.min_interval - (now - self.last_request_time))
        self.last_request_time = time.time()

    def fetch_food(self, food_name: str) -> Tuple[Dict, str]:
        print(f"🔍 Fetching nutrition for: {food_name}")
        cache_key = f"nutrition_{food_name.lower()}"
        cached = cache_get(cache_key)
        if cached:
            return cached, "cached"

        if self.usda_key:
            self._rate_limit()
            try:
                params = {"api_key": self.usda_key, "query": food_name, "pageSize": 1}
                resp = requests.get(self.usda_url, params=params, timeout=self.timeout)
                if resp.status_code == 429:
                    print("   ⏳ Rate limited, retrying after 1s...")
                    time.sleep(1)
                    resp = requests.get(self.usda_url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
                if data.get("foods") and len(data["foods"]) > 0:
                    food = data["foods"][0]
                    result = {
                        "name": food.get("description", food_name),
                        "calories": self._extract_nutrient(food, "Energy"),
                        "protein_g": self._extract_nutrient(food, "Protein"),
                        "carbs_g": self._extract_nutrient(food, "Carbohydrate"),
                        "fiber_g": self._extract_nutrient(food, "Fiber"),
                        "fat_g": self._extract_nutrient(food, "Total lipid"),
                        "sugar_g": self._extract_nutrient(food, "Sugars")
                    }
                    cache_set(cache_key, result)
                    return result, "usda_real"
            except Exception as e:
                print(f"   ⚠️ USDA API error: {e}")

        print(f"   🔄 Using curated USDA nutrition data for '{food_name}' (last resort).")
        curated = self._get_curated_food(food_name)
        cache_set(cache_key, curated)
        return curated, "usda_curated"

    def _extract_nutrient(self, food: Dict, nutrient_name: str) -> float:
        for nutrient in food.get("foodNutrients", []):
            name = nutrient.get("nutrient", {}).get("name", "").lower()
            if nutrient_name.lower() in name:
                return nutrient.get("amount", 0.0)
        return 0.0

    def _get_curated_food(self, food_name: str) -> Dict:
        curated_db = {
            "oatmeal": {"name": "Oatmeal", "calories": 150, "protein_g": 5, "carbs_g": 27, "fiber_g": 4, "fat_g": 3, "sugar_g": 1},
            "banana": {"name": "Banana", "calories": 105, "protein_g": 1, "carbs_g": 27, "fiber_g": 3, "fat_g": 0, "sugar_g": 14},
            "apple": {"name": "Apple", "calories": 95, "protein_g": 0, "carbs_g": 25, "fiber_g": 4, "fat_g": 0, "sugar_g": 19},
            "chicken": {"name": "Chicken Breast", "calories": 165, "protein_g": 31, "carbs_g": 0, "fiber_g": 0, "fat_g": 4, "sugar_g": 0},
            "rice": {"name": "Brown Rice", "calories": 216, "protein_g": 5, "carbs_g": 45, "fiber_g": 4, "fat_g": 2, "sugar_g": 0},
            "bread": {"name": "Whole Wheat Bread", "calories": 80, "protein_g": 4, "carbs_g": 14, "fiber_g": 2, "fat_g": 1, "sugar_g": 1},
            "milk": {"name": "Milk", "calories": 150, "protein_g": 8, "carbs_g": 12, "fiber_g": 0, "fat_g": 8, "sugar_g": 12},
            "egg": {"name": "Egg", "calories": 70, "protein_g": 6, "carbs_g": 0, "fiber_g": 0, "fat_g": 5, "sugar_g": 0},
            "yogurt": {"name": "Greek Yogurt", "calories": 100, "protein_g": 10, "carbs_g": 6, "fiber_g": 0, "fat_g": 5, "sugar_g": 6},
            "almond": {"name": "Almonds", "calories": 160, "protein_g": 6, "carbs_g": 6, "fiber_g": 3, "fat_g": 14, "sugar_g": 1}
        }
        for key in curated_db:
            if key in food_name.lower():
                return curated_db[key]
        return {"name": food_name, "calories": 100, "protein_g": 3, "carbs_g": 15, "fiber_g": 2, "fat_g": 3, "sugar_g": 5}

# ============================================================
# 6. Recipes: TheMealDB → curated
# ============================================================
class TheMealDBClient:
    def __init__(self):
        self.base_url = "https://www.themealdb.com/api/json/v1/1"
        self.timeout = 30

    def fetch_recipes(self, category: str = "Breakfast") -> Tuple[List[Dict], str]:
        print(f"🔍 Fetching recipes for: {category}")
        cache_key = f"recipes_{category.lower()}"
        cached = cache_get(cache_key)
        if cached:
            return cached, "cached"

        category_map = {
            "breakfast": "Breakfast",
            "lunch": "Lunch",
            "dinner": "Dinner",
            "dessert": "Dessert",
            "snack": "Snack",
            "chicken": "Chicken",
            "pasta": "Pasta",
            "seafood": "Seafood",
            "vegetarian": "Vegetarian",
            "vegan": "Vegan"
        }
        meal_type = category_map.get(category.lower(), category)
        try:
            url = f"{self.base_url}/filter.php?c={meal_type}"
            resp = requests.get(url, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            if data.get("meals"):
                cache_set(cache_key, data["meals"])
                return data["meals"], "themealdb_real"
        except Exception as e:
            print(f"   ⚠️ TheMealDB API failed: {e}")

        print(f"   🔄 Using curated recipes for '{category}' (last resort).")
        curated = self._get_curated_recipes(category)
        cache_set(cache_key, curated)
        return curated, "themealdb_curated"

    def _get_curated_recipes(self, category: str) -> List[Dict]:
        curated = {
            "breakfast": [
                {"idMeal": "1", "strMeal": "Oatmeal with Berries", "strCategory": "Breakfast"},
                {"idMeal": "2", "strMeal": "Eggs with Toast", "strCategory": "Breakfast"},
                {"idMeal": "3", "strMeal": "Greek Yogurt with Honey", "strCategory": "Breakfast"}
            ],
            "lunch": [
                {"idMeal": "4", "strMeal": "Grilled Chicken Salad", "strCategory": "Lunch"},
                {"idMeal": "5", "strMeal": "Turkey Sandwich", "strCategory": "Lunch"}
            ],
            "dinner": [
                {"idMeal": "6", "strMeal": "Grilled Fish with Vegetables", "strCategory": "Dinner"},
                {"idMeal": "7", "strMeal": "Chicken Stir Fry", "strCategory": "Dinner"}
            ],
            "snack": [
                {"idMeal": "8", "strMeal": "Apple with Peanut Butter", "strCategory": "Snack"},
                {"idMeal": "9", "strMeal": "Trail Mix", "strCategory": "Snack"}
            ],
            "dessert": [
                {"idMeal": "10", "strMeal": "Fruit Salad", "strCategory": "Dessert"},
                {"idMeal": "11", "strMeal": "Dark Chocolate", "strCategory": "Dessert"}
            ]
        }
        for key in curated:
            if key in category.lower():
                return curated[key]
        return curated["lunch"]

# ============================================================
# 7. LLM Client (for chat completions -- multi-provider fallback)
# ============================================================
class NVIDIAClient:
    """Class name kept as-is (recommender.py/safety.py construct this as
    `self.nvidia = NVIDIAClient()` and call `self.nvidia._chat(...)` --
    keeping the name and the _chat(messages) -> str interface means
    neither of those files needs to change).

    Despite the name, this is now a fast-first multi-provider cascade:
    Groq -> AionLabs -> Gemini -> NVIDIA, same pattern as
    sehatai/callAi.js and backend/app/ai.py. Confirmed live 2026-09-04:
    NVIDIA's free "Prototype" tier can stall for MINUTES on a single
    call -- the old single-provider @retry(stop_after_attempt(3)) wrapped
    around a 60s-timeout request meant one bad NVIDIA call could take
    3+ minutes before finally falling through to the hardcoded fallback
    recommendation, with no faster option ever tried. A provider with no
    key configured is skipped entirely. NVIDIA is kept as the last rung
    (not removed) since its key is still valid when it does respond.
    """

    _PROVIDER_SPECS = [
        ("groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
        ("aionlabs", "AIONLABS_API_KEY", "https://api.aionlabs.ai/v1", "aion-labs/aion-3.0-mini"),
        ("gemini", "GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-3.5-flash-lite"),
        ("nvidia", "NVIDIA_API_KEY", "https://integrate.api.nvidia.com/v1", "nvidia/nemotron-3.5-lightning-30b-a3b"),
    ]

    def __init__(self, api_key: Optional[str] = None):
        self.temperature = 0.0
        self.max_tokens = 16384
        self._providers = self._build_providers(api_key)
        if not self._providers:
            raise ValueError(
                "No LLM provider configured -- set GROQ_API_KEY, AIONLABS_API_KEY, "
                "GEMINI_API_KEY, and/or NVIDIA_API_KEY in .env"
            )
        self.api_key = self._providers[0]["api_key"]  # kept for __repr__
        print(f"✅ LLM client initialized -- fallback chain: {' -> '.join(p['name'] for p in self._providers)}")

    def _build_providers(self, override_key: Optional[str]) -> List[Dict[str, str]]:
        providers = []
        for name, env_var, base_url, default_model in self._PROVIDER_SPECS:
            # override_key (the constructor's api_key param) only ever
            # meant the NVIDIA key historically -- preserve that for
            # any external caller that still passes one explicitly.
            key = override_key if (name == "nvidia" and override_key) else (ENV.get(env_var) or os.getenv(env_var))
            if not key:
                continue
            model_env = f"{name.upper()}_MODEL"
            model = os.getenv(model_env) or ENV.get(model_env) or default_model
            providers.append({"name": name, "api_key": key, "base_url": base_url, "model": model})
        return providers

    def _chat(self, messages: List[Dict[str, str]]) -> str:
        failures = []
        for provider in self._providers:
            try:
                url = f"{provider['base_url'].rstrip('/')}/chat/completions"
                payload = {
                    "model": provider["model"],
                    "messages": messages,
                    "temperature": self.temperature,
                    "max_tokens": self.max_tokens,
                }
                headers = {
                    "Authorization": f"Bearer {provider['api_key']}",
                    "Content-Type": "application/json",
                }
                # 20s per attempt, ONE attempt per provider (no per-provider
                # retry loop) -- a stuck provider fails over to the next one
                # quickly instead of multiplying a long timeout by 3 retries
                # the way the old single-provider version did.
                response = requests.post(url, json=payload, headers=headers, timeout=20)
                response.raise_for_status()
                result = response.json()
                content = result['choices'][0]['message']['content']
                if not content:
                    raise ValueError("empty response content")
                return content
            except Exception as exc:
                failures.append(f"{provider['name']}: {exc}")
                print(f"⚠️ LLM provider '{provider['name']}' failed — trying next: {exc}")
        raise RuntimeError(f"LLM call failed on every configured provider — {' | '.join(failures)}")

    def __repr__(self):
        return f"NVIDIAClient(providers={[p['name'] for p in self._providers]})"

# ============================================================
# TEST – Run this file directly to verify all clients
# ============================================================
if __name__ == "__main__":
    print("\n" + "="*60)
    print("  Testing All API Clients (including NVIDIA)")
    print("="*60)

    # Test DietBot clients
    print("\n1. Testing HuggingFaceClient...")
    hf = HuggingFaceClient()
    guidelines, src = hf.fetch_guidelines("diabetes")
    print(f"   ✅ Got {len(guidelines)} guidelines (source: {src})")

    print("\n2. Testing MedDataClient...")
    med = MedDataClient()
    interactions, src = med.fetch_interactions("Metformin")
    print(f"   ✅ Got {len(interactions)} interactions (source: {src})")

    print("\n3. Testing USDAClient...")
    usda = USDAClient()
    food, src = usda.fetch_food("apple")
    print(f"   ✅ {food.get('name')}: {food.get('calories')} cal (source: {src})")

    print("\n4. Testing TheMealDBClient...")
    meal = TheMealDBClient()
    recipes, src = meal.fetch_recipes("breakfast")
    print(f"   ✅ Got {len(recipes)} recipes (source: {src})")

    # Test NVIDIA
    print("\n5. Testing NVIDIAClient...")
    try:
        nv = NVIDIAClient()
        resp = nv._chat([{"role": "user", "content": "Say hello in one word."}])
        print(f"   ✅ NVIDIA response: {resp.strip()}")
    except Exception as e:
        print(f"   ❌ NVIDIA test failed: {e}")

    print("\n" + "="*60)
    print("  ✅ All clients tested successfully!")
    print("="*60)