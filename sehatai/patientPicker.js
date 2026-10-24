// ============================================
// SehatAI: Patient picker for the local chat tools (testChat.js,
// startChat.js). Lists the patients in Supabase (read-only) and asks in
// the terminal which one to chat as. Names are shown only in the terminal.
// ============================================

import { supabase } from './supabaseClient.js';

/**
 * @param {import('node:readline').Interface} rl
 * @param {(line: string) => void} say - prints one line to the terminal
 * @returns {Promise<string>} the chosen patient id
 */
export async function choosePatient(rl, say = console.log) {
  const { data, error } = await supabase
    .from('patients')
    .select('id, name, date_of_birth, sex')
    .order('created_at')
    .limit(20);
  if (error || !data?.length) {
    say(`  Couldn't load patients from Supabase${error ? ` (${error.message})` : ' (none found)'}.`);
    say('  Pass one explicitly with --patient <patientId>.');
    process.exit(1);
  }
  if (data.length === 1) return data[0].id;
  say('  Chat as which patient?');
  data.forEach((p, i) => say(`  ${i + 1}. ${p.name || '(no name)'} — ${p.sex || '?'}, born ${p.date_of_birth || '?'}`));
  for (;;) {
    const answer = await new Promise((resolve) => rl.question('  number: ', resolve));
    const pick = data[Number(answer.trim()) - 1];
    if (pick) { say(''); return pick.id; }
  }
}
