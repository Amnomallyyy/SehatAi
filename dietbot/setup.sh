#!/bin/bash
# DietBot – Phase 0: Environment Setup & Verification

set -e

echo "============================================================"
echo "  DietBot – Phase 0: Environment Setup & Verification"
echo "============================================================"

# ============================================================
# 1. Check Python version
# ============================================================
echo ""
echo "🔍 Checking Python version..."
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 not found. Please install Python 3.11 or higher."
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
if [[ $(echo "$PYTHON_VERSION >= 3.11" | bc) -ne 1 ]]; then
    echo "❌ Python 3.11+ required (found $PYTHON_VERSION)."
    exit 1
fi
echo "✅ Python $PYTHON_VERSION found."

# ============================================================
# 2. Create virtual environment
# ============================================================
echo ""
echo "🔧 Creating virtual environment..."
python3 -m venv venv
source venv/bin/activate

# ============================================================
# 3. Upgrade pip and install dependencies
# ============================================================
echo ""
echo "⬆️  Upgrading pip..."
pip install --upgrade pip

echo ""
echo "📦 Installing dependencies..."
pip install -r requirements.txt

# ============================================================
# 4. Check .env file
# ============================================================
echo ""
echo "📄 Checking .env file..."
if [ ! -f .env ]; then
    echo "⚠️  .env not found. Creating from .env.example..."
    cp .env.example .env
    echo "❌ Please edit .env and add your API keys, then re-run this script."
    exit 1
fi

# Load .env and check required keys
source .env
REQUIRED_KEYS=("SUPABASE_URL" "SUPABASE_KEY" "NVIDIA_API_KEY" "JINA_API_KEY" "MEDDATA_API_KEY" "USDA_API_KEY")
MISSING=()
for key in "${REQUIRED_KEYS[@]}"; do
    if [ -z "${!key}" ]; then
        MISSING+=("$key")
    fi
done
if [ ${#MISSING[@]} -ne 0 ]; then
    echo "❌ Missing required environment variables: ${MISSING[*]}"
    echo "   Please add them to .env and re-run."
    exit 1
fi
echo "✅ .env exists with all required keys."

# ============================================================
# 5. Test Supabase connectivity
# ============================================================
echo ""
echo "🔌 Testing Supabase connectivity..."
python3 - <<EOF
import os
from supabase import create_client

url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_KEY")
if not url or not key:
    print("❌ Supabase credentials not set.")
    exit(1)
try:
    client = create_client(url, key)
    client.table('patients').select('id', count='exact').limit(1).execute()
    print("✅ Supabase connectivity test passed.")
except Exception as e:
    print(f"❌ Supabase connectivity test failed: {e}")
    exit(1)
EOF
if [ $? -ne 0 ]; then
    exit 1
fi

# ============================================================
# 6. Final summary
# ============================================================
echo ""
echo "============================================================"
echo "  ✅ verification passed – environment is ready!"
echo "  Virtual environment is active: source venv/bin/activate"
echo "  Proceed next: "
echo "============================================================"