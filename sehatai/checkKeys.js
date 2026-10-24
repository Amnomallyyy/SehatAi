// ============================================
// SehatAI: check which AI API keys still work
//
//   npm run check:keys
//
// Sends each configured provider a tiny request (a few tokens) and
// reports: OK / OUT OF QUOTA / RATE LIMITED / INVALID KEY / NOT SET.
// Used by both the triage chatbot and DietBot (same .env keys).
// Never calls Infermedica.
// ============================================

import 'dotenv/config';

const env = (k) => (process.env[k] || '').trim();

const PROVIDERS = [
  { name: 'Groq', key: 'GROQ_API_KEY', url: 'https://api.groq.com/openai/v1', model: env('GROQ_MODEL') || 'qwen/qwen3.6-27b', extra: { reasoning_effort: /gpt-oss/i.test(env('GROQ_MODEL')) ? 'low' : 'none' } },
  { name: 'AionLabs', key: 'AIONLABS_API_KEY', url: 'https://api.aionlabs.ai/v1', model: env('AIONLABS_MODEL') || 'aion-labs/aion-3.0-mini', extra: { reasoning_effort: 'none' } },
  { name: 'Gemini', key: 'GEMINI_API_KEY', url: 'https://generativelanguage.googleapis.com/v1beta/openai', model: env('GEMINI_MODEL') || 'gemini-3.1-flash-lite' },
  { name: 'NVIDIA (triage)', key: 'NVIDIA_API_KEY_SEC', url: env('NVIDIA_BASE_URL') || 'https://integrate.api.nvidia.com/v1', model: env('NVIDIA_MODEL') || 'nvidia/nemotron-3.5-lightning-30b-a3b' },
  { name: 'NVIDIA (DietBot)', key: 'NVIDIA_API_KEY', url: env('NVIDIA_BASE_URL') || 'https://integrate.api.nvidia.com/v1', model: env('NVIDIA_MODEL') || 'nvidia/nemotron-3.5-lightning-30b-a3b' },
  { name: 'Gamma (Gemma)', key: 'GAMMA_API_KEY', url: env('GAMMA_BASE_URL'), model: env('GAMMA_MODEL') || 'gemma-4-31b-it' },
  { name: 'OpenRouter', key: 'OPENROUTER_API_KEY', url: 'https://openrouter.ai/api/v1', model: env('OPENROUTER_MODEL') || 'openai/gpt-4o-mini' },
  { name: 'DeepSeek', key: 'DEEPSEEK_API_KEY', url: 'https://api.deepseek.com', model: env('DEEPSEEK_MODEL') || 'deepseek-chat' },
];

function verdict(status, text) {
  if (status === 401 || status === 403 || /invalid api key|authentication/i.test(text)) return '❌ INVALID KEY';
  if (status === 402 || /insufficient balance|payment required|credits/i.test(text)) return '❌ NO CREDIT (top up)';
  if (status === 429 && /per day|\bTPD\b|\bRPD\b|daily|PerDay|quota/i.test(text)) return '❌ OUT OF QUOTA for today';
  if (status === 429) return '⏳ RATE LIMITED (per minute — fine again in a moment)';
  if (status === 404 || /model.*not found|unknown model|does not exist/i.test(text)) return '⚠️  KEY OK, but model name is wrong';
  return `⚠️  HTTP ${status}`;
}

async function check(p) {
  const key = env(p.key);
  if (!key) return `${p.name.padEnd(17)} — not set (${p.key})`;
  const started = Date.now();
  try {
    const res = await fetch(`${p.url.replace(/\/$/, '')}/chat/completions`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${key}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: p.model, messages: [{ role: 'user', content: 'hi' }], max_tokens: 5, ...(p.extra || {}) }),
      signal: AbortSignal.timeout(60000),
    });
    const secs = ((Date.now() - started) / 1000).toFixed(1);
    if (res.ok) {
      // Groq tells us how much of the per-minute / per-day allowance is left.
      const left = res.headers.get('x-ratelimit-remaining-requests');
      const extra = left ? `  (requests left today: ${left})` : '';
      return `${p.name.padEnd(17)} ✅ OK  ${secs}s${extra}`;
    }
    const text = await res.text();
    let hint = '';
    // Wrong model name: show which models this key can actually use.
    if (p.listModelsOnError && /model/i.test(text) && res.status !== 401) {
      try {
        const models = await (await fetch(`${p.url.replace(/\/$/, '')}/models`, { headers: { Authorization: `Bearer ${key}` } })).json();
        const ids = (models.data || []).map((m) => m.id);
        if (ids.length) hint = `\n${' '.repeat(20)}models this key can use: ${ids.join(', ')}\n${' '.repeat(20)}set one in .env as ${p.key.replace('_API_KEY', '_MODEL')}=...`;
      } catch {}
    }
    return `${p.name.padEnd(17)} ${verdict(res.status, text)}  ${secs}s${hint}`;
  } catch (err) {
    return `${p.name.padEnd(17)} ⚠️  no answer (${err.name === 'TimeoutError' ? 'timed out after 60s' : err.message})`;
  }
}

console.log('\nChecking AI keys (a tiny request each)...\n');
const results = await Promise.all(PROVIDERS.map(check));
results.forEach((r) => console.log(`  ${r}`));
console.log(`\n  Order the chatbot tries them in: ${env('AI_PROVIDER_ORDER') || '(default)'}\n`);
