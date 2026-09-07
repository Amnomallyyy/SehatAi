#!/usr/bin/env python3
"""
setup.py – DietBot Phase 0: Environment Setup & Verification
All-in-one: checks Python, creates venv, installs deps, validates .env, tests Supabase.
"""

import os
import sys
import subprocess
import venv
import shutil
from pathlib import Path

# ----- Colors -----
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    BOLD = '\033[1m'
    RESET = '\033[0m'

def print_ok(msg):    print(f"{Colors.GREEN}✅ {msg}{Colors.RESET}")
def print_error(msg): print(f"{Colors.RED}❌ {msg}{Colors.RESET}")
def print_info(msg):  print(f"{Colors.BLUE}ℹ️  {msg}{Colors.RESET}")
def print_warning(msg): print(f"{Colors.YELLOW}⚠️  {msg}{Colors.RESET}")
def print_header(title):
    print(f"\n{Colors.BOLD}{'='*60}{Colors.RESET}")
    print(f"{Colors.BOLD}  {title}{Colors.RESET}")
    print(f"{Colors.BOLD}{'='*60}{Colors.RESET}")

# ----- Helper: read .env without external libs -----
def read_env_value(key):
    """Return value for key from .env, stripping quotes and whitespace."""
    env_path = Path(".env")
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            v = v.strip()
            # Strip quotes if present
            if v.startswith('"') and v.endswith('"'):
                v = v[1:-1]
            if v.startswith("'") and v.endswith("'"):
                v = v[1:-1]
            return v
    return None

# ----- Step 1: Python version -----
def check_python_version():
    print_header("1. Checking Python Version")
    if sys.version_info < (3, 11):
        print_error(f"Python {sys.version_info.major}.{sys.version_info.minor} – need 3.11+")
        return False
    print_ok(f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    return True

# ----- Step 2: Virtual environment -----
def get_venv_paths():
    venv_dir = Path("venv")
    if sys.platform == "win32":
        return (
            venv_dir,
            venv_dir / "Scripts" / "python.exe",
            venv_dir / "Scripts" / "pip.exe",
            venv_dir / "Scripts" / "activate"
        )
    else:
        return (
            venv_dir,
            venv_dir / "bin" / "python",
            venv_dir / "bin" / "pip",
            venv_dir / "bin" / "activate"
        )

def create_virtual_environment():
    print_header("2. Virtual Environment")
    venv_dir, python_path, pip_path, activate_script = get_venv_paths()
    if venv_dir.exists():
        print_ok("Virtual environment already exists")
        return python_path, pip_path, activate_script
    print_info("Creating virtual environment...")
    try:
        venv.EnvBuilder(with_pip=True).create(venv_dir)
        print_ok("Virtual environment created")
        return python_path, pip_path, activate_script
    except Exception as e:
        print_error(f"Failed: {e}")
        return None, None, None

# ----- Step 3: Install dependencies -----
def install_dependencies(pip_path):
    print_header("3. Installing Dependencies")
    req = Path("requirements.txt")
    if not req.exists():
        print_warning("requirements.txt missing – creating default")
        req.write_text("""python-dotenv==1.0.0
requests==2.31.0
httpx==0.27.0
tenacity==8.2.3
pydantic==2.5.0
supabase==2.5.3
bcrypt==4.1.0
pytest==7.4.0
""")
        print_ok("requirements.txt created")
    print_info("Upgrading pip...")
    subprocess.run([str(pip_path), "install", "--upgrade", "pip"], check=False)
    print_info("Installing packages from requirements.txt...")
    result = subprocess.run([str(pip_path), "install", "-r", "requirements.txt", "--no-cache-dir"])
    if result.returncode != 0:
        print_error("Installation failed")
        return False
    # Verify supabase
    check = subprocess.run([str(pip_path), "show", "supabase"], capture_output=True)
    if check.returncode != 0:
        print_error("supabase not installed correctly")
        return False
    print_ok("All dependencies installed")
    return True

# ----- Step 4: Validate .env -----
def validate_env_file():
    print_header("4. Validating .env File")
    env_path = Path(".env")
    if not env_path.exists():
        example = Path(".env.example")
        if example.exists():
            shutil.copy(example, env_path)
            print_warning(".env created from .env.example – please edit with your keys")
        else:
            print_error(".env missing and no .env.example")
        return False
    required = ["SUPABASE_URL", "SUPABASE_KEY", "NVIDIA_API_KEY", "JINA_API_KEY", "MEDDATA_API_KEY", "USDA_API_KEY"]
    missing = [k for k in required if not read_env_value(k)]
    if missing:
        print_error(f"Missing keys: {', '.join(missing)}")
        return False
    print_ok(".env exists with all required keys")
    return True

# ----- Step 5: Test Supabase using the venv Python -----
def test_supabase_connectivity(python_path):
    print_header("5. Testing Supabase Connectivity")
    # This script will be run inside the venv – it reads .env manually.
    test_script = '''
import sys
import os
from supabase import create_client

# Read .env manually
env = {}
with open(".env") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        if v.startswith('"') and v.endswith('"'): v = v[1:-1]
        if v.startswith("'") and v.endswith("'"): v = v[1:-1]
        env[k] = v

url = env.get("SUPABASE_URL")
key = env.get("SUPABASE_KEY")
if not url or not key:
    print("❌ Supabase credentials missing")
    sys.exit(1)

try:
    client = create_client(url, key)
    client.table('patients').select('id').limit(1).execute()
    print("✅ Supabase connection successful")
    sys.exit(0)
except Exception as e:
    print(f"❌ Supabase error: {e}")
    sys.exit(1)
'''
    result = subprocess.run(
        [str(python_path), "-c", test_script],
        capture_output=True,
        text=True
    )
    if result.returncode == 0:
        print_ok("Supabase connectivity test passed")
        return True
    print_error("Supabase connectivity test failed")
    if result.stdout:
        print_info(result.stdout.strip())
    if result.stderr:
        print_info(f"Error: {result.stderr.strip()}")
    return False

# ----- Step 6: Final summary -----
def display_summary(activate_script):
    print_header("✅ Phase 0 Complete")
    print_ok("Environment is ready for DietBot!")
    if sys.platform == "win32":
        print_info("Activate: .\\venv\\Scripts\\activate")
    else:
        print_info(f"Activate: source {activate_script}")
    print_info("Next: Phase 1 – Database Schema")
    print("="*60)

# ----- Main -----
def main():
    print(f"\n{Colors.BOLD}{'='*60}{Colors.RESET}")
    print(f"{Colors.BOLD}  DietBot – Phase 0: Complete Setup & Verification{Colors.RESET}")
    print(f"{Colors.BOLD}{'='*60}{Colors.RESET}")

    if not check_python_version():
        sys.exit(1)

    python_path, pip_path, activate_script = create_virtual_environment()
    if not python_path:
        sys.exit(1)

    if not install_dependencies(pip_path):
        sys.exit(1)

    if not validate_env_file():
        print_info("After adding keys, re-run: python setup.py")
        sys.exit(1)

    if not test_supabase_connectivity(python_path):
        print_warning("Supabase test failed – check your credentials in .env")
        print_info("You can re-run after fixing: python setup.py")
        sys.exit(1)

    display_summary(activate_script)
    sys.exit(0)

if __name__ == "__main__":
    main()
    