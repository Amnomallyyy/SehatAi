// ============================================
// SehatAI: Terminal Chat
// Talk to the triage chatbot like a real user would — just the
// conversation, no debug output (type /debug to see what the bot is
// tracking behind the scenes).
//
// The real pipeline: real AI providers, real Infermedica (called only
// after you confirm your symptom list), and the patient's profile read
// from Supabase (nothing is written). Run from the SehatAi folder:
//
//   npm run chat                          pick a patient from Supabase
//   node sehatai/testChat.js --patient <patientId>
//
// For the web version of this chat, see startChat.js (npm run chat:web).
//
// In the chat:
//   /new     start a new conversation (the old one is deleted)
//   /debug   show/hide what the bot is tracking (symptoms, reply type)
//   /exit    quit
// When the bot flags an emergency, type 1 (Notify someone) or 2 (Continue).
// ============================================

import 'dotenv/config';
import readline from 'node:readline';

const ARGS = process.argv.slice(2);
const flagValue = (flag) => {
  const i = ARGS.indexOf(flag);
  return i >= 0 && ARGS[i + 1] && !ARGS[i + 1].startsWith('--') ? ARGS[i + 1] : null;
};

// Keep the provider/pipeline logging out of the conversation unless
// /debug is on.
let debug = false;
const realLog = console.log;
const realWarn = console.warn;
const realError = console.error;
console.log = (...a) => { if (debug) realLog(...a); };
console.warn = (...a) => { if (debug) realWarn(...a); };
console.error = (...a) => { if (debug) realError(...a); };

const { processPatientMessage } = await import('./processMessage.js');
const { getAccumulatedSymptoms, endPatientSessions, deleteSession } = await import('./chatLog.js');

const { choosePatient } = await import('./patientPicker.js');
let patientId = flagValue('--patient');

const C = {
  reset: '\x1b[0m', dim: '\x1b[2m', bold: '\x1b[1m',
  cyan: '\x1b[36m', green: '\x1b[32m', yellow: '\x1b[33m', red: '\x1b[31m', blue: '\x1b[34m',
};
const paint = (color, s) => `${C[color]}${s}${C.reset}`;
const say = (...a) => realLog(...a);

function wrap(text, width = Math.min(process.stdout.columns || 90, 100) - 8) {
  const words = String(text).split(/\s+/);
  const lines = [];
  let line = '';
  for (const w of words) {
    if ((line + ' ' + w).trim().length > width) { lines.push(line); line = w; } else { line = `${line} ${w}`.trim(); }
  }
  if (line) lines.push(line);
  return lines;
}

function printBot(result) {
  const emergency = result.kind === 'emergency';
  const label = paint(emergency ? 'red' : 'green', emergency ? 'SehatAI ⚠' : 'SehatAI');
  const lines = wrap(result.reply || '(no reply)');
  say(`${label}  ${emergency ? paint('red', lines[0] || '') : lines[0] || ''}`);
  for (const l of lines.slice(1)) say(`${' '.repeat(9)}${emergency ? paint('red', l) : l}`);
  const specialist = result.recommendation?.specialist_recommended;
  if (result.kind === 'recommendation' && specialist) say(`${' '.repeat(9)}${paint('bold', `→ ${specialist}`)}`);
  if (result.actionable && result.actions?.length) {
    say(`${' '.repeat(9)}${result.actions.map((a, i) => paint('yellow', `[${i + 1}] ${a.label}`)).join('   ')}`);
  }
  if (debug) {
    const tracked = getAccumulatedSymptoms(result.sessionId)
      .map((s) => `${s.term}${s.present ? '' : ' (gone)'}${s.severity ? `, ${s.severity}` : ''}${s.duration ? `, ${s.duration}` : ''}`)
      .join(' | ') || 'nothing yet';
    say(paint('dim', `${' '.repeat(9)}[${result.kind}] tracking: ${tracked}`));
  }
  say('');
}

async function main() {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  say('');
  say(paint('bold', '  SehatAI — symptom checker'));
  if (!patientId) patientId = await choosePatient(rl, say);
  say(paint('dim', `  Patient: ${patientId}`));
  say(paint('dim', '  /new  new conversation   ·   /debug  show tracking   ·   /exit  quit'));
  say('');
  printBot({ kind: 'greeting', reply: "Hi — I'm here to help you figure out which kind of doctor to see. What symptoms are you experiencing, and how long have you had them?" });

  let sessionId = null;
  let pendingActions = null;

  rl.setPrompt(paint('cyan', 'You      '));
  rl.on('SIGINT', () => { rl.close(); });
  rl.prompt();

  for await (const raw of rl) {
    let message = raw.trim();
    if (!message) { rl.prompt(); continue; }

    if (message === '/exit' || message === '/quit') break;
    if (message === '/debug') {
      debug = !debug;
      say(paint('dim', `  debug ${debug ? 'on' : 'off'}\n`));
      rl.prompt();
      continue;
    }
    if (message === '/new') {
      if (sessionId) deleteSession(sessionId);
      endPatientSessions(patientId);
      sessionId = null;
      pendingActions = null;
      say(paint('dim', '  — new conversation —\n'));
      printBot({ kind: 'greeting', reply: 'Hi again — what symptoms are you experiencing?' });
      rl.prompt();
      continue;
    }

    // Emergency choices, like the buttons in the web app.
    if (pendingActions && /^[12]$/.test(message)) {
      const action = pendingActions[Number(message) - 1];
      if (action?.id === 'notify') {
        say(paint('yellow', '  (In the web app this opens "Notify someone". Please contact someone now if you need help.)\n'));
        rl.prompt();
        continue;
      }
      if (action) message = action.label; // "Continue with this chat"
    }

    process.stdout.write(paint('dim', '         typing…'));
    let result;
    try {
      result = await processPatientMessage(message, patientId, sessionId);
    } catch (err) {
      result = { kind: 'error', reply: `Something went wrong: ${err.message}` };
    }
    process.stdout.write('\r\x1b[K');
    sessionId = result.sessionId || sessionId;
    pendingActions = result.actionable ? result.actions : null;
    printBot(result);
    rl.prompt();
  }

  rl.close();
  if (sessionId) deleteSession(sessionId);
  say(paint('dim', '\n  Conversation ended (nothing was saved).'));
  process.exit(0);
}

main().catch((err) => {
  realError('Fatal:', err);
  process.exit(1);
});
