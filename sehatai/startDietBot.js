// ============================================
// SehatAI: DietBot launcher
// Convenience wrapper for `npm run dietbot`. The DietBot service is a
// separate teammate-owned Python/FastAPI project living outside this
// repo (C:\Users\lenovo\SehatAi) — this script does not modify or
// duplicate any of its code, it just spawns its venv's uvicorn with
// PYTHONIOENCODING=utf-8 set. Without that, Windows' default console
// codec (cp1252) crashes on any reply containing typographic Unicode
// characters (e.g. non-breaking hyphens), which the DietBot's actual
// diet recommendations do contain.
// ============================================

import { spawn } from "child_process";

const DIETBOT_DIR = "C:\\Users\\lenovo\\SehatAi";
const PYTHON = `${DIETBOT_DIR}\\venv\\Scripts\\python.exe`;

const child = spawn(PYTHON, ["-m", "uvicorn", "api:app", "--port", "8001"], {
  cwd: DIETBOT_DIR,
  env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" },
  stdio: "inherit",
});

child.on("exit", (code) => process.exit(code ?? 0));
