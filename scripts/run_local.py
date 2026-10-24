"""Run the whole SehatAI stack locally without Docker.

    .venv/Scripts/python scripts/run_local.py      (Windows)
    .venv/bin/python scripts/run_local.py          (macOS/Linux)

Starts every service on its own port (the frontend's defaults in
frontend/frontend_main.py) and stops them all on Ctrl+C. Expects two
virtualenvs: .venv at the repo root (backend + datafetch + frontend +
sehatEvidence requirements) and dietbot/.venv (dietbot's own, whose pins
conflict with the backend's). Open http://localhost:3002 when it's up.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = "Scripts" if os.name == "nt" else "bin"
PY = str(ROOT / ".venv" / BIN / "python")
DIET_PY = str(ROOT / "dietbot" / ".venv" / BIN / "python")

# Local runs aren't the hardened production deployment: allow localhost
# origins and let DietBot be reached on localhost.
OVERRIDES = {
    "APP_ENV": os.environ.get("APP_ENV", "development"),
    "DIETBOT_API_URL": "http://127.0.0.1:8001",
    "PYTHONUNBUFFERED": "1",
    # DietBot/DataFetch print emoji progress markers; on Windows the default
    # console codec (cp1252) can't encode them and the print raises, which
    # surfaced as "I'm having trouble reaching the diet assistant".
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
}

BACKEND_PORT = os.environ.get("SEHAT_BACKEND_PORT", "8010")

SERVICES = [
    # name, cwd, argv, extra env
    ("dietbot", ROOT / "dietbot",
     [DIET_PY, "-m", "uvicorn", "api:app", "--port", "8001"], {}),
    # Port 8010, not 8000: another local project (CareerLens) uses 8000.
    ("backend", ROOT / "backend",
     [PY, "-m", "uvicorn", "app.main:app", "--port", BACKEND_PORT], {}),
    ("sehatai", ROOT, ["node", "sehatai/webserver.js"], {"PORT": "3000"}),
    ("evidence", ROOT / "sehatEvidence", [PY, "api/server.py"],
     {"SERVER_HOST": "127.0.0.1", "SERVER_PORT": "8002"}),
    ("frontend", ROOT / "frontend",
     [PY, "-m", "uvicorn", "frontend_main:app", "--port", "3002"],
     {"PUBLIC_API_BASE": f"http://localhost:{BACKEND_PORT}"}),
]


def load_env_file(path):
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def main():
    base_env = {**os.environ, **load_env_file(ROOT / ".env"), **OVERRIDES}
    procs = []
    for name, cwd, argv, extra in SERVICES:
        print(f"[run_local] starting {name}: {' '.join(argv)}", flush=True)
        procs.append((name, subprocess.Popen(argv, cwd=cwd, env={**base_env, **extra})))
        time.sleep(1)
    print("[run_local] open http://localhost:3002  (Ctrl+C stops everything)", flush=True)
    try:
        while True:
            for name, proc in procs:
                if proc.poll() is not None:
                    print(f"[run_local] {name} exited with code {proc.returncode}", flush=True)
                    procs.remove((name, proc))
                    break
            if not procs:
                return 1
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        for _, proc in procs:
            proc.terminate()
        for _, proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
