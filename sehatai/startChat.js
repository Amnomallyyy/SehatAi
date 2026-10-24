// ============================================
// SehatAI: run the chatbot web page locally
//
//   npm run chat:web                     pick a patient from Supabase
//   node sehatai/startChat.js --patient <patientId>
//
// Then open http://localhost:3000. It's the real pipeline: real AI
// providers, real Infermedica (only after you confirm your symptom list),
// the patient's profile read from Supabase, nothing written to the
// database. No login: the server runs as the chosen patient and only
// accepts connections from this machine (127.0.0.1).
//
// The terminal shows each AI call as it happens:
//   [AI] groq 480ms      [AI] gemini FAILED (429 quota)  -> next provider
// ============================================

import 'dotenv/config';
import readline from 'node:readline';
import { choosePatient } from './patientPicker.js';

const args = process.argv.slice(2);
const i = args.indexOf('--patient');
let patientId = i >= 0 ? args[i + 1] : null;

if (!patientId) {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  console.log('\n  SehatAI — local chat');
  patientId = await choosePatient(rl);
  rl.close();
}

// Show only the useful lines in the terminal: which AI answered each call
// (and how long it took), plus anything that went wrong.
const write = (line) => process.stdout.write(`${line}\n`);
const origLog = console.log;
const origWarn = console.warn;
const origError = console.error;
function route(level, args) {
  const text = args.map((a) => (typeof a === 'string' ? a : (a?.message || JSON.stringify(a)))).join(' ');
  const ok = text.match(/provider "(\w+)" (structured )?call took (\d+)ms/);
  if (ok) return write(`[AI] ${ok[1].padEnd(9)} ${String(ok[3]).padStart(6)}ms${ok[2] ? '  (structured)' : ''}`);
  const failed = text.match(/provider "(\w+)" failed[^:]*: (.*)/);
  if (failed) return write(`[AI] ${failed[1].padEnd(9)} FAILED  ${failed[2].slice(0, 90)}  -> next provider`);
  if (/^\[callAi\] (skipping|AI provider fallback chain)/.test(text)) return write(text.replace('[callAi] ', '[AI] '));
  if (/finalize: sending to Infermedica/.test(text)) return write(`[Infermedica] ${text.replace(/.*sending to Infermedica \/parse: /, 'sending: ')}`);
  if (/SehatAI web UI running|dev mode/.test(text)) return;
  if (level === 'error') return origError(...args);
  if (level === 'warn' && /failed|error/i.test(text)) return origWarn(...args);
}
console.log = (...a) => route('log', a);
console.warn = (...a) => route('warn', a);
console.error = (...a) => route('error', a);

process.env.SEHATAI_ALLOW_UNAUTHENTICATED = 'true';
process.env.HOST = '127.0.0.1';
process.argv[2] = patientId; // webserver.js reads the patient id from here

await import('./webserver.js');
write(`\n  Chat as patient ${patientId}`);
write(`  Open http://localhost:${Number(process.env.PORT) || 3000} in your browser. Ctrl+C to stop.\n`);
