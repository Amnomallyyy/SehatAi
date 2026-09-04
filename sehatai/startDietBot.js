// ============================================
// SehatAI: DietBot launcher
// Convenience wrapper for `npm run dietbot`. The DietBot service lives in
// this repo under dietbot/ (its own venv + .env, same layout as
// datafetch/ and sehatEvidence/) — this script just spawns its venv's
// uvicorn with PYTHONIOENCODING=utf-8 set. Without that, Windows' default
// console codec (cp1252) crashes on any reply containing typographic
// Unicode characters (e.g. non-breaking hyphens), which the DietBot's
// actual diet recommendations do contain.
// ============================================

import { spawn } from "child_process";
import { fileURLToPath } from "url";
import path from "path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DIETBOT_DIR = path.resolve(__dirname, "..", "dietbot");
const PYTHON = process.platform === "win32"
  ? path.join(DIETBOT_DIR, "venv", "Scripts", "python.exe")
  : path.join(DIETBOT_DIR, "venv", "bin", "python");

const child = spawn(PYTHON, ["-m", "uvicorn", "api:app", "--port", "8001"], {
  cwd: DIETBOT_DIR,
  env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" },
  stdio: "inherit",
});

child.on("exit", (code) => process.exit(code ?? 0));
