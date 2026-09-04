# Activate venv first
source venv/bin/activate

# Install supabase specifically with verbose output
pip install supabase==2.0.0 --force-reinstall --no-cache-dir

# Verify it installed
python -c "from supabase import create_client; print('✅ supabase installed')"