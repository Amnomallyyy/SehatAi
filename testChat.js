// ============================================
// SehatAI: Interactive Test Chat
// A real terminal chat loop against processPatientMessage() — for
// manually poking at the pipeline (emergency gates, off-topic,
// diagnosis-request, clarification questions, subject detection,
// specialist recommendations, diet routing) without writing a new
// scripted scenario for every question you have.
//
// This talks to your REAL Supabase + Infermedica + Groq — same as
// testEdgeCases.js — so it costs real API calls. No test doubles here.
// Diet-related messages also call the separate DietBot service (see
// dietBotClient.js) — start it first (`uvicorn api:app --port 8001`
// in the dietbot folder) or diet questions will return a
// "having trouble reaching the diet assistant" reply.
//
// Usage:
//   node testChat.js                     (uses TEST_PATIENT_ID below)
//   node testChat.js <patientId>         (overrides it)
//
// In-chat commands (type these instead of a message):
//   /new          start a fresh session (drops session id + in-RAM state)
//   /raw          toggle printing the full envelope object per turn
//   /state        print current awaitingClarificationAnswer / clarification
//                 round / accumulated symptoms for this session, on demand
//   /memory       toggle auto-printing that same session state after
//                 EVERY reply (ON by default) — see printState()
//   /patient <id> switch patient id mid-run without restarting
//   /help         show this list
//   /exit         quit
// ============================================

import readline from 'node:readline';
import { processPatientMessage, processDietMessage } from './processMessage.js';
import {
  getAwaitingClarificationAnswer,
  getAwaitingFinalConfirmation,
  getClarificationCount,
  getAccumulatedSymptoms,
  getLastSubject,
  getOffTopicStreak,
} from './chatLog.js';
import { MAX_CLARIFICATION_ROUNDS } from './infermedicaClient.js';

const DEFAULT_PATIENT_ID = 'a2000000-0000-4000-8000-000000000002'; // <-- replace with your real seeded patient id

const COLOR = {
  reset: '\x1b[0m', dim: '\x1b[2m', bold: '\x1b[1m',
  cyan: '\x1b[36m', green: '\x1b[32m', yellow: '\x1b[33m',
  red: '\x1b[31m', magenta: '\x1b[35m', blue: '\x1b[34m',
};
const c = (color, s) => `${COLOR[color]}${s}${COLOR.reset}`;

const KIND_COLOR = {
  emergency: 'red',
  off_topic: 'yellow',
  diagnosis_declined: 'yellow',
  clarification: 'blue',
  recommendation: 'green',
  diet: 'cyan',
  diet_error: 'red',
  error: 'red',
};

function badge(result) {
  const color = KIND_COLOR[result.kind] || 'dim';
  return c(color, `[${result.kind}]`);
}

function printResult(result, showRaw) {
  console.log(`${badge(result)} ${c('bold', 'Bot:')} ${result.reply}`);

  const meta = [];
  if (result.source === 'dietbot') meta.push('source=dietbot');
  if (result.urgency && result.urgency !== 'routine') meta.push(`urgency=${result.urgency}`);
  if (result.triageLevel) meta.push(`triageLevel=${result.triageLevel}`);
  if (result.clarificationRound) meta.push(`clarificationRound=${result.clarificationRound}/${result.maxClarificationRounds}`);
  if (result.recommendedChannel) meta.push(`channel=${result.recommendedChannel}`);
  if (result.subject?.subject) {
    meta.push(
      `subject=${result.subject.subject}` +
      (result.subject.relation ? `(${result.subject.relation}/${result.subject.ageGroup || '?'})` : '')
    );
  }
  // The actual age/sex resolved for triage this turn — separate from
  // subject.ageGroup above (a rough bucket, null-by-design for a "self"
  // subject). This is the real value sent to Infermedica.
  if (result.resolvedAge != null) meta.push(`resolvedAge=${result.resolvedAge}, resolvedSex=${result.resolvedSex}`);
  if (result.isDiagnosisRequest) meta.push('diagnosis-request');
  if (result.matchedSymptoms?.length) {
    meta.push(`symptoms=[${result.matchedSymptoms.map((s) => s.name).join(', ')}]`);
  }
  if (result.recommendation) {
    meta.push(`specialist="${result.recommendation.specialist_recommended}"`);
    if (result.verification) {
      meta.push(`verified=${result.verification.ok}${result.verification.retried ? '(retried)' : ''}`);
      const blocking = (result.verification.violations || []).filter((v) => !v.repaired);
      if (blocking.length) meta.push(c('red', `BLOCKED: ${blocking.map((v) => v.code).join(', ')}`));
      const repaired = (result.verification.violations || []).filter((v) => v.repaired);
      if (repaired.length) meta.push(c('yellow', `repaired: ${repaired.map((v) => v.code).join(', ')}`));
    }
  }
  if (meta.length) console.log(c('dim', `        ${meta.join('  |  ')}`));

  if (showRaw) {
    console.log(c('dim', JSON.stringify(result, null, 2)));
  }
  console.log();
}

async function printState(sessionId) {
  if (!sessionId) {
    console.log(c('dim', '  no session yet — send a message first'));
    return;
  }
  const awaitingClarificationAnswer = getAwaitingClarificationAnswer(sessionId);
  const awaitingFinalConfirmation = getAwaitingFinalConfirmation(sessionId);
  const clarificationCount = getClarificationCount(sessionId);
  const offTopicStreak = getOffTopicStreak(sessionId);
  const accumulated = getAccumulatedSymptoms(sessionId);
  const subject = getLastSubject(sessionId);
  console.log(c('magenta', '--- session state ---'));
  console.log(`  sessionId:                ${sessionId}`);
  console.log(`  awaitingClarification:    ${awaitingClarificationAnswer}`);
  console.log(`  awaitingFinalConfirmation:${awaitingFinalConfirmation}`);
  console.log(`  clarificationRounds:      ${clarificationCount} / ${MAX_CLARIFICATION_ROUNDS}`);
  // Separate, smaller bounded budget for consecutive off-topic replies
  // while a question is pending — see chatLog.js's offTopicStreak and
  // processMessage.js's MAX_OFF_TOPIC_STREAK (2). Shown here mainly so
  // it's visible during testing that noise doesn't silently eat into
  // the real clarificationRounds budget above.
  console.log(`  offTopicStreak:           ${offTopicStreak}`);
  // No Infermedica evidence store to read anymore — Infermedica is only
  // ever called inside finalizeAndRecommend(), exactly once per round,
  // and nothing it returns is kept between requests (see chatLog.js's
  // and processMessage.js's compliance notes). What IS kept between
  // turns is our own Groq classification of what the patient said —
  // shown here instead of a stored evidence list.
  // Duration/severity shown here too (they weren't before) — this is
  // the field that was silently getting dropped by an earlier bug, so
  // it's worth being able to see directly rather than just the bare term.
  console.log(`  accumulatedSymptoms:      [${accumulated.map((s) => {
    const detail = [s.severity, s.duration ? `for ${s.duration}` : null].filter(Boolean).join(', ');
    return `${s.term}${s.present ? '' : '(denied)'}${detail ? ` (${detail})` : ''}${s.finalized ? '*' : ''}`;
  }).join(', ') || 'none'}]  (* = already in a prior recommendation)`);
  console.log(`  lastSubject:              ${subject ? JSON.stringify(subject) : 'none'}`);
  console.log(c('magenta', '----------------------'));
}

function printHelp() {
  console.log(c('cyan', `
Commands:
  /new           start a fresh session
  /raw           toggle full envelope dump per turn
  /state         print awaitingClarificationAnswer / clarification / symptoms / subject (on demand)
  /memory        toggle auto-printing that same state after every reply (ON by default)
  /patient <id>  switch patient id
  /diet          switch to DIET mode (routes to the DietBot service)
  /symptom       switch back to SYMPTOM mode (the default)
  /help          this list
  /exit          quit
`));
}

async function main() {
  let patientId = process.argv[2] || DEFAULT_PATIENT_ID;
  let sessionId = null;
  let showRaw = false;
  // Prints the session's stored state (see printState()) after every
  // turn, right below the bot's reply — not just on-demand via /state.
  // This is deliberately on by default: the whole point is to make it
  // visible, turn by turn, that nothing Infermedica-derived is ever
  // sitting in that state — only our own Groq classification of what
  // the patient said. Toggle off with /memory if it's too noisy.
  let showMemory = true;
  // Explicit mode switch — stands in for a real UI's mode buttons.
  // No AI guesses which bot a message is meant for; the user (or a
  // future front-end) picks, and BOTH modes still run through the
  // same shared safety gates (see processMessage.js) regardless.
  let mode = 'symptom'; // 'symptom' | 'diet'

  console.log(c('bold', '=== SehatAI Test Chat ==='));
  console.log(c('dim', `patient: ${patientId}`));
  console.log(c('dim', 'type /help for commands, /exit to quit — /diet and /symptom switch modes\n'));

  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  const ask = () => rl.question(c(mode === 'diet' ? 'green' : 'cyan', `You [${mode}]: `), onLine);

  async function onLine(line) {
    const input = line.trim();

    if (!input) return ask();

    if (input.startsWith('/')) {
      const [cmd, ...rest] = input.slice(1).split(/\s+/);
      switch (cmd) {
        case 'exit':
        case 'quit':
          rl.close();
          return;
        case 'new':
          sessionId = null;
          console.log(c('dim', 'started a fresh session\n'));
          return ask();
        case 'raw':
          showRaw = !showRaw;
          console.log(c('dim', `raw envelope dump: ${showRaw ? 'ON' : 'OFF'}\n`));
          return ask();
        case 'memory':
          showMemory = !showMemory;
          console.log(c('dim', `auto-print session memory after each reply: ${showMemory ? 'ON' : 'OFF'}\n`));
          return ask();
        case 'state':
          await printState(sessionId);
          console.log();
          return ask();
        case 'patient':
          if (rest[0]) {
            patientId = rest[0];
            sessionId = null;
            console.log(c('dim', `switched to patient ${patientId} (new session)\n`));
          } else {
            console.log(c('yellow', 'usage: /patient <id>\n'));
          }
          return ask();
        case 'diet':
          mode = 'diet';
          console.log(c('green', 'switched to DIET mode — messages now go to the DietBot service\n'));
          return ask();
        case 'symptom':
          mode = 'symptom';
          console.log(c('cyan', 'switched to SYMPTOM mode\n'));
          return ask();
        case 'help':
          printHelp();
          return ask();
        default:
          console.log(c('yellow', `unknown command: /${cmd} (try /help)\n`));
          return ask();
      }
    }

    try {
      const result = mode === 'diet'
        ? await processDietMessage(input, patientId, sessionId)
        : await processPatientMessage(input, patientId, sessionId);
      sessionId = result.sessionId || sessionId;
      printResult(result, showRaw);
      // Auto-print what's actually being held in RAM for this session
      // right after the reply — see printState(). Symptom mode only:
      // diet mode's state lives entirely in the separate DietBot
      // service (see getDietSessionId/saveDietSessionId), not in
      // chatLog.js's accumulatedSymptoms/etc., so there's nothing
      // relevant here to show for a diet turn.
      if (mode === 'symptom' && showMemory) {
        await printState(sessionId);
        console.log();
      }
    } catch (err) {
      console.error(c('red', `ERROR: ${err.message}\n`));
    }

    ask();
  }

  ask();
}

main().catch((err) => {
  console.error('Fatal:', err);
  process.exit(1);
});