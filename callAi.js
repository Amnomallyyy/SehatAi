// ============================================
// HealthMate AI: AI Client Wrapper — 2-provider fallback cascade
//
// UPDATED (this pass): this file used to hit exactly ONE model on ONE
// provider (Groq's qwen/qwen3.6-27b) with no fallback whatsoever. Every
// AI-driven decision in this entire pipeline — both safety gates
// (checkAISeverity, checkAICrisis, checkDangerousEvent), domain/off-
// topic classification, symptom extraction, clarifying-question
// composition (assessIntake), final-confirmation resolution,
// recommendation generation, and grounding verification, roughly 15
// call sites across the codebase — goes through ONLY callAI/
// callAIStructured below. That's actually good news for fixing this:
// it means the fallback logic can live entirely in this ONE file
// without touching any of those ~15 call sites, since none of them
// talk to Groq (or any provider) directly.
//
// It's also what made the recent outage as bad as it was: Groq
// deprecated qwen3.6-27b (following llama-3.3-70b-versatile,
// llama-3.1-8b-instant, kimi-k2-instruct, and llama-4-maverick before
// it — see the git history of this file for that whole saga), and
// because checkAISeverity/checkAICrisis in safetyCheck.js deliberately
// have NO try/catch around their callAI call (correct, fail-closed
// behavior for a SINGLE provider failing — better to surface an error
// than silently decide "not an emergency"), a provider outage meant
// EVERY message, safety-relevant or not, started failing with a 500,
// not just AI-classification accuracy degrading. One provider going
// down took the entire app down.
//
// Fix: callAI/callAIStructured now try each CONFIGURED provider in
// order and fail over to the next on ANY error — network failure, rate
// limit, a decommissioned/renamed model, a timeout, or output that
// doesn't parse — instead of throwing immediately. Only once EVERY
// configured provider has failed does the error finally propagate,
// preserving the exact same fail-closed behavior the safety gates rely
// on (still no silent "assume it's fine" default anywhere), just far
// less likely to ever be reached. Function SIGNATURES are UNCHANGED —
// every existing caller needed zero edits.
//
// 3 PROVIDERS: Groq (fast — LPU hardware, see DEFAULT_PROVIDER_ORDER
// below — used as PRIMARY); Gemini, via Google's official OpenAI-
// compatibility endpoint, as the 2nd fallback (Flash-Lite, no payment
// method required); and NVIDIA NIM (ONE key, not a primary+secondary
// pair; that was a real mix-up earlier while setting this up and is now
// corrected) kept as the LAST-RESORT fallback — its free "Prototype"
// tier runs on shared GPU infrastructure and has been clocked at 8-59
// SECONDS per call, wildly inconsistently. Cerebras was tried and
// removed — see DEFAULT_PROVIDER_ORDER's comment below for why. An
// earlier version of this file also wired up plain OpenAI and Anthropic
// as deeper fallbacks — deliberately cut back down, since those had no
// configured keys and were untested against a live API; simpler and
// less surface area to trust beats more options that were never
// actually verified. Add any of them back the same way (a small factory
// function + one line in PROVIDER_FACTORIES/DEFAULT_PROVIDER_ORDER
// below) if that ever changes.
//
// WHICH PROVIDER IS ACTUALLY USED: built from whichever API key(s) are
// actually present in .env — a provider with no key configured is
// skipped entirely (never attempted, never logged as a failure). Order
// is controlled by AI_PROVIDER_ORDER (comma-separated, e.g.
// "groq,gemini,nvidia") if set, else DEFAULT_PROVIDER_ORDER below.
// Groq, Gemini, and NVIDIA NIM (build.nvidia.com — a DIFFERENT NVIDIA
// key from the one the DietBot service uses; configured here in
// SehatAI's OWN .env, not shared with DietBot's process) all speak the
// exact same OpenAI-compatible chat-completions shape, so they share
// one adapter (makeOpenAICompatibleProvider) — only the
// baseURL/model/key differ.
//
// Relevant env vars (all optional — set only the one(s) you have keys for):
//   GROQ_API_KEY, GROQ_MODEL (default qwen/qwen3.6-27b), GROQ_BASE_URL
//   GEMINI_API_KEY, GEMINI_MODEL (default gemini-3.1-flash-lite), GEMINI_BASE_URL
//     — get a key at aistudio.google.com/apikey (free-tier by default,
//     no billing setup needed, unlike a regular Google Cloud API key).
//   NVIDIA_API_KEY_SEC, NVIDIA_MODEL (default nvidia/nemotron-3.5-lightning-30b-a3b), NVIDIA_BASE_URL
//     — YOUR OWN (only) NVIDIA key, read from SehatAI's own .env. It's
//     named NVIDIA_API_KEY_SEC (not NVIDIA_API_KEY) because that's what
//     it's actually called in .env — kept as configured rather than
//     renamed. Your teammate's NVIDIA key (for DietBot's own LLM calls)
//     lives in DietBot's separate .env, in DietBot's own process — a
//     completely different file on disk, loaded by a completely
//     different `node`/`uvicorn` process, so there's no shared
//     environment between the two services and no way for the two to
//     collide regardless of naming.
//   AI_PROVIDER_ORDER — overrides DEFAULT_PROVIDER_ORDER below
// ============================================
import 'dotenv/config';
import OpenAI from "openai";

function sanitizeResponse(text) {
  if (!text) return "";
  let cleanText = text.replace(/<think>[\s\S]*?(?:<\/think>|$)/gi, "").trim();
  cleanText = cleanText.replace(/```(?:json)?\n?/gi, "").replace(/```/g, "").trim();
  return cleanText;
}

function extractJson(text) {
  const cleaned = sanitizeResponse(text);
  const jsonMatch = cleaned.match(/[\{\[][\s\S]*[\}\]]/);
  const targetText = jsonMatch ? jsonMatch[0] : cleaned;
  return JSON.parse(targetText);
}

// A provider that hangs instead of erroring would otherwise stall the
// whole cascade (and the turn behind it) indefinitely — this bounds
// each individual provider attempt so a stuck call fails over to the
// next provider instead of hanging forever. Independent of, and much
// tighter than, processMessage.js's SESSION_LOCK_TIMEOUT_MS (45s),
// which only protects the NEXT turn for the same session, not this one.
const PROVIDER_TIMEOUT_MS = Number(process.env.AI_PROVIDER_TIMEOUT_MS) || 25 * 1000;

function withTimeout(promise, ms, label) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label}: timed out after ${ms}ms`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

// ---- Provider adapter: any OpenAI-compatible chat-completions endpoint ----
// Covers both Groq and NVIDIA NIM — they speak the exact same
// request/response shape via the `openai` package; only
// baseURL/model/key differ. reasoningEffort is only ever sent when the
// factory explicitly opts in (Groq's qwen model REQUIRES this field —
// see the original comment preserved below — NVIDIA's DeepSeek endpoint
// hasn't shown the same requirement, and some APIs 400 on an
// unrecognized field, so it's opt-in per provider, not global).
function makeOpenAICompatibleProvider({ name, apiKeyEnv, baseURL, modelEnv, defaultModel, sendReasoningEffort = false, systemPrefix = null, minMaxTokens = null }) {
  const apiKey = process.env[apiKeyEnv];
  if (!apiKey) return null;
  const model = process.env[modelEnv] || defaultModel;
  const client = new OpenAI({ apiKey, baseURL });
  // FOUND live (while investigating provider "exhaustion" that turned
  // out not to be exhaustion at all): NVIDIA's Nemotron model
  // unreliably ignores the "detailed thinking off" systemPrefix above
  // and writes a full "Here's a thinking process: 1. ..." essay before
  // the real answer anyway — this ISN'T guaranteed suppressed, just
  // usually suppressed. The output-token caps below (300/1000) were
  // tightened for the well-behaved providers (Groq/Gemini/OpenRouter,
  // which reliably respect reasoning_effort:none or never reason at
  // all here) — but for a provider that can still ramble first, a tight
  // cap means the reasoning eats the WHOLE budget and the response gets
  // cut off before the actual JSON/word ever arrives, parsed as
  // "Unexpected non-whitespace character after JSON" — which looks
  // exactly like exhaustion in the logs but is actually a truncation
  // bug. minMaxTokens raises the floor for exactly this provider,
  // independent of whatever a caller asks for, without loosening the
  // cap for providers that don't need it.
  const effectiveMaxTokens = (requested) => (minMaxTokens ? Math.max(requested, minMaxTokens) : requested);

  // systemPrefix: for NVIDIA's Nemotron reasoning models specifically —
  // "detailed thinking off" as the FIRST LINE of the system message is
  // how NVIDIA's docs say to disable this model's visible chain-of-
  // thought (there is no separate API parameter for it, and unlike
  // Groq's qwen/DeepSeek this model doesn't wrap reasoning in <think>
  // tags at all — it just writes it as plain prose ahead of the real
  // answer). Without this, a call classifying CRISIS/NOT_CRISIS (or
  // any other exact-keyword classifier in this codebase) would get a
  // reply starting with "Here's a thinking process: ..." instead of
  // the actual word, silently breaking every .startsWith()-based check
  // — not an exception, just a wrong answer every classifier here
  // would misread as its "no" default. It also plausibly explains the
  // 90s+ timeouts seen before this was found: a long system prompt
  // with several few-shot examples gives the model a lot to reason
  // about out loud before ever reaching the actual answer.
  const buildSystem = (system) => (systemPrefix ? `${systemPrefix}\n\n${system}` : system);

  return {
    name,
    async callText({ system, message, temperature, maxTokens, reasoningEffort }) {
      const response = await withTimeout(client.chat.completions.create({
        model,
        messages: [{ role: "system", content: buildSystem(system) }, { role: "user", content: message }],
        temperature,
        max_tokens: effectiveMaxTokens(maxTokens),
        ...(sendReasoningEffort ? { reasoning_effort: reasoningEffort || "none", reasoning_format: "hidden" } : {}),
      }), PROVIDER_TIMEOUT_MS, name);
      const text = response.choices[0]?.message?.content;
      if (!text) throw new Error(`${name}: empty content in response`);
      return sanitizeResponse(text);
    },
    async callStructured({ system, message, schema, temperature }) {
      const schemaInstruction = `

Respond ONLY with valid JSON matching this exact schema.
Do not use markdown code fences.
Do not add commentary before or after the JSON.

${JSON.stringify(schema)}`;
      const response = await withTimeout(client.chat.completions.create({
        model,
        messages: [{ role: "system", content: buildSystem(system) + schemaInstruction }, { role: "user", content: message }],
        temperature,
        // TIGHTENED (found while investigating Groq's daily token budget
        // exhaustion): this was 4000, sized for "reasoning (if any) +
        // full JSON answer both need to fit" — but reasoning_effort is
        // hardcoded to "none" two lines below for Groq specifically
        // (the only provider that actually honors that field — see
        // sendReasoningEffort), so there is no reasoning output to
        // budget room for there; the comment was stale for Groq. Every
        // schema in this codebase returns a small JSON object (a
        // handful of short fields/array entries) — 1000 tokens is still
        // several times more than any real response needs for a
        // provider that actually stays quiet. minMaxTokens (see the
        // factory's own doc comment above) raises this floor back up
        // for NVIDIA specifically, whose "detailed thinking off"
        // suppression isn't reliable — a tight cap there just means the
        // response gets truncated mid-reasoning before the JSON ever
        // arrives, which looks exactly like provider exhaustion in the
        // logs but is actually this.
        max_tokens: effectiveMaxTokens(1000),
        ...(sendReasoningEffort ? { reasoning_effort: "none", reasoning_format: "hidden" } : {}),
      }), PROVIDER_TIMEOUT_MS, name);
      const text = response.choices[0]?.message?.content;
      if (!text) throw new Error(`${name}: empty content in structured response`);
      return extractJson(text);
    },
  };
}

const PROVIDER_FACTORIES = {
  // There is only ONE NVIDIA key (not a primary+secondary pair — that
  // was a real misunderstanding earlier in getting this set up, now
  // corrected) and its env var happens to be named NVIDIA_API_KEY_SEC,
  // not NVIDIA_API_KEY — that's just what it's called in .env, kept as
  // configured rather than renamed.
  nvidia: () => makeOpenAICompatibleProvider({
    name: "nvidia",
    apiKeyEnv: "NVIDIA_API_KEY_SEC",
    baseURL: process.env.NVIDIA_BASE_URL || "https://integrate.api.nvidia.com/v1",
    modelEnv: "NVIDIA_MODEL",
    // UPDATED: was deepseek-ai/deepseek-v4-pro-0813 (a frontier reasoning
    // model), switched after that model's real-world latency on the NIM
    // "Prototype" free tier proved too slow for this pipeline's pattern
    // of several short classification/extraction calls per turn (one
    // turn hit the 45s session-lock backstop). Nemotron 3.5 Lightning is
    // a 30B MoE with ~3B active params/token, purpose-built for exactly
    // this app's actual workload — classification, extraction, short
    // structured output — rather than deep reasoning, and NVIDIA cites
    // roughly 4x the throughput of comparable Qwen models.
    //
    // CONFIRMED LIVE (test-nvidia-call.js): unlike qwen/DeepSeek, this
    // model does NOT wrap its reasoning in <think> tags — it writes it
    // as plain prose ahead of the real answer ("Here's a thinking
    // process: 1. Analyze User Input..."), which sanitizeResponse()
    // can't strip and which broke every .startsWith()-keyword-based
    // classifier in this codebase (a reply starting with "Here's a
    // thinking process..." never matches .startsWith("CRISIS") etc —
    // fails silently to the "not flagged" default, not an exception).
    // Also the likely real cause of the 90s+ timeouts seen before this
    // was diagnosed: a long system prompt with several few-shot
    // examples gives it a lot to visibly reason through before ever
    // reaching the actual answer. Fixed via systemPrefix below —
    // NVIDIA's own docs: prepending "detailed thinking off" as the
    // FIRST LINE of the system message is how you disable this model's
    // chain-of-thought (no separate API parameter for it).
    //
    // One known tradeoff still worth spot-checking after switching:
    // weaker prompt-injection resistance than frontier reasoning
    // models, which is relevant to this app's off-topic/CONCERNING-
    // content classifier specifically — re-verify the REGRESSION
    // scenarios in testEdgeCases.js still catch emergency/profanity
    // content smuggled into an otherwise ordinary-looking message.
    defaultModel: "nvidia/nemotron-3.5-lightning-30b-a3b",
    systemPrefix: "detailed thinking off",
    // FOUND live: "detailed thinking off" isn't reliably honored — this
    // model still sometimes writes a full "Here's a thinking process:
    // 1. ..." essay before the real answer despite the instruction. A
    // tight output-token cap (see makeOpenAICompatibleProvider's own
    // doc comment on minMaxTokens) then truncates the response before
    // the actual JSON/word ever arrives, surfacing as "Unexpected
    // non-whitespace character after JSON" — indistinguishable in the
    // logs from real provider exhaustion, but actually just this
    // model rambling into its own token budget.
    minMaxTokens: 4000,
  }),
  groq: () => makeOpenAICompatibleProvider({
    name: "groq",
    apiKeyEnv: "GROQ_API_KEY",
    baseURL: process.env.GROQ_BASE_URL || "https://api.groq.com/openai/v1",
    modelEnv: "GROQ_MODEL",
    defaultModel: "qwen/qwen3.6-27b",
    sendReasoningEffort: true, // this model 400s without it — see file header
  }),
  // ADDED: Gemini via Google's official OpenAI-compatibility endpoint
  // (ai.google.dev/gemini-api/docs/openai — confirmed live). Not
  // custom-silicon-fast like Groq/Cerebras, but Flash-Lite is still
  // low-latency, and — unlike Cerebras — Google's free tier genuinely
  // requires NO payment method on file (confirmed live). Get a key at
  // aistudio.google.com/apikey (a different kind of key than a regular
  // Cloud API key — this one is free-tier by default, no billing setup).
  gemini: () => makeOpenAICompatibleProvider({
    name: "gemini",
    apiKeyEnv: "GEMINI_API_KEY",
    baseURL: process.env.GEMINI_BASE_URL || "https://generativelanguage.googleapis.com/v1beta/openai/",
    modelEnv: "GEMINI_MODEL",
    // Flash-Lite over plain Flash: roughly 4x the free daily request
    // quota (1000/day vs 250/day as of this writing) for a workload
    // that's all short classification/extraction calls, not deep
    // reasoning — the same tradeoff that picked Nemotron Lightning for
    // NVIDIA. VERIFY against your own key before trusting this —
    // Google's exact model-ID naming has moved more than once even
    // within this same project's debugging history; UPDATED again here
    // after gemini-3.1-flash-lite came back 404 "no longer available to
    // new users", per Google's own error message pointing at
    // gemini-3.5-flash-lite as the replacement.
    defaultModel: "gemini-3.5-flash-lite",
  }),
  // ADDED: OpenRouter (openrouter.ai) — an aggregator over many models
  // from many labs, OpenAI-compatible like Groq/NVIDIA/Gemini above, so
  // it slots into the exact same adapter. Picked up specifically as a
  // 4th fallback for the day Groq's daily token limit AND Gemini's
  // free-tier quota were both exhausted at once, leaving only the slow
  // NVIDIA tier working. Like Gemini, OpenRouter's ":free"-suffixed
  // models need no payment method on file — get a key at
  // openrouter.ai/keys. defaultModel below is a reasonable ":free"
  // pick as of this writing, but unlike Groq/Gemini (single vendor,
  // fairly stable naming), OpenRouter's specific free-model catalog and
  // per-model rate limits shift over time and vary by account — set
  // OPENROUTER_MODEL in .env to override with whatever
  // openrouter.ai/models?max_price=0 currently lists if this default
  // ever 404s or disappears.
  //
  // UPDATED (found live): meta-llama/llama-3.3-70b-instruct:free was
  // removed from OpenRouter's free catalog entirely — confirmed via
  // https://openrouter.ai/api/v1/models, which no longer lists it at
  // all (its paid-only replacement is what the 404 error was pointing
  // to, not a free option). Replaced with a model actually present in
  // that live free listing as of this fix.
  openrouter: () => makeOpenAICompatibleProvider({
    name: "openrouter",
    apiKeyEnv: "OPENROUTER_API_KEY",
    baseURL: process.env.OPENROUTER_BASE_URL || "https://openrouter.ai/api/v1",
    modelEnv: "OPENROUTER_MODEL",
    defaultModel: "google/gemma-4-31b-it:free",
  }),
  // ADDED: Requesty (router.requesty.ai) — another OpenAI-compatible
  // multi-provider router, same shape as OpenRouter (model names use
  // the same "vendor/model" convention). Added as a 5th fallback during
  // the same infrastructure crisis that motivated Cerebras above —
  // unlike Cerebras, added directly via this shared adapter rather than
  // a dedicated one, so if its free tier turns out to have the same
  // kind of hidden requirement, removing it is a one-line change here,
  // same as it would have been for Cerebras.
  requesty: () => makeOpenAICompatibleProvider({
    name: "requesty",
    apiKeyEnv: "REQUESTY_API_KEY",
    baseURL: process.env.REQUESTY_BASE_URL || "https://router.requesty.ai/v1",
    modelEnv: "REQUESTY_MODEL",
    defaultModel: "openai/gpt-4o-mini",
  }),
  // ADDED: DeepSeek's own official API (not via a router) — OpenAI-
  // compatible, same adapter as everything else here. Deliberately
  // "deepseek-chat" (their standard non-reasoning model), NOT
  // "deepseek-reasoner" — the reasoner variant visibly chain-of-thoughts
  // like NVIDIA's Nemotron does, which would need the same
  // minMaxTokens headroom (see nvidia's own doc comment above) to avoid
  // truncating before the real answer; deepseek-chat doesn't do that by
  // default, so it doesn't need it. CONFIRMED LIVE: key/URL/model are
  // all correctly configured (a real 402 came back, not an auth/404
  // error) — the account itself just has no funded balance yet, same
  // situation as Requesty and Cerebras before it. Left out of
  // AI_PROVIDER_ORDER until that's resolved.
  deepseek: () => makeOpenAICompatibleProvider({
    name: "deepseek",
    apiKeyEnv: "DEEPSEEK_API_KEY",
    baseURL: process.env.DEEPSEEK_BASE_URL || "https://api.deepseek.com",
    modelEnv: "DEEPSEEK_MODEL",
    defaultModel: "deepseek-chat",
  }),
  // ADDED: AionLabs — OpenAI-compatible, same adapter as everything
  // else here. CONFIRMED LIVE (unlike Requesty/DeepSeek/Cerebras
  // above): both a plain call and a structured/JSON call succeeded
  // cleanly with gpt-4o-mini — no visible-reasoning quirk, no
  // truncation, no billing block. Actually usable right now, so
  // included in DEFAULT_PROVIDER_ORDER below, unlike the three
  // above that are wired in but dormant pending funding.
  aionlabs: () => makeOpenAICompatibleProvider({
    name: "aionlabs",
    apiKeyEnv: "AIONLABS_API_KEY",
    baseURL: process.env.AIONLABS_BASE_URL || "https://api.aionlabs.ai/v1",
    modelEnv: "AIONLABS_MODEL",
    defaultModel: "gpt-4o-mini",
  }),
};

// UPDATED: was ["nvidia", "groq"] (NVIDIA first, since it was the
// confirmed-working provider at the time this cascade was built).
// Flipped after live logs showed NVIDIA's NIM "Prototype" free-tier
// endpoint taking 8-59 SECONDS per call, wildly inconsistently (shared,
// lower-priority GPU infrastructure — see the conversation this was
// diagnosed in). Groq's LPU hardware is dramatically faster for this
// exact workload (short classification/extraction calls), so it leads.
//
// Cerebras was tried as a 3rd provider (custom-silicon-fast, same
// category as Groq) but pulled back out — its free tier turned out to
// require a payment method on file despite marketing otherwise (every
// call 402'd), and rather than leave a permanently-failing provider in
// the chain, it was removed outright. Add it back the same way as any
// other provider (a small factory function + one line here) if that
// account ever gets billing sorted — see git history for the removed
// factory. Gemini stays as the 2nd provider: not custom-silicon-fast,
// but Flash-Lite is low-latency and Google's free tier genuinely needs
// no card on file.
//
// UPDATED: openrouter added as a 4th provider, ahead of nvidia — added
// on a day Groq's daily token limit AND Gemini's free-tier quota were
// both exhausted simultaneously, leaving only the slow NVIDIA tier
// (8-59s/call) actually working. openrouter's free-tier models aren't
// custom-silicon-fast either, but they're a genuinely separate quota
// pool from Groq/Gemini and typically faster than NVIDIA's shared
// "Prototype" tier, so it's worth trying before falling all the way to
// NVIDIA, not instead of it (NVIDIA stays as the last-resort catch-all
// — see its own factory comment for why). Override with
// AI_PROVIDER_ORDER in .env for a different order.
// requesty is defined in PROVIDER_FACTORIES above but deliberately left
// out of the default order — its account has no funded balance yet
// (every call 402s, confirmed live), same situation Cerebras hit
// earlier. Add "requesty" to AI_PROVIDER_ORDER in .env once it's
// topped up; no code change needed at that point.
const DEFAULT_PROVIDER_ORDER = ["aionlabs", "groq", "gemini", "nvidia"];

function buildProviderChain() {
  const orderEnv = process.env.AI_PROVIDER_ORDER;
  const order = orderEnv
    ? orderEnv.split(",").map((s) => s.trim()).filter(Boolean)
    : DEFAULT_PROVIDER_ORDER;

  const chain = [];
  for (const name of order) {
    const factory = PROVIDER_FACTORIES[name];
    if (!factory) {
      console.warn(`[callAi] AI_PROVIDER_ORDER names unknown provider "${name}" — skipping.`);
      continue;
    }
    const provider = factory();
    if (provider) chain.push(provider); // null = no API key configured for it, silently skipped
  }
  return chain;
}

// Built once at module load (env is already loaded via 'dotenv/config'
// above by the time this runs) — provider identity/config doesn't
// change at runtime, so there's no reason to rebuild this per call.
const PROVIDER_CHAIN = buildProviderChain();

if (PROVIDER_CHAIN.length === 0) {
  console.error(
    "[callAi] NO AI PROVIDER IS CONFIGURED — every callAI/callAIStructured call will fail immediately. " +
    "Set NVIDIA_API_KEY_SEC and/or GROQ_API_KEY in .env."
  );
} else {
  console.log(`[callAi] AI provider fallback chain: ${PROVIDER_CHAIN.map((p) => p.name).join(" -> ")}`);
}

// Logs how long a SUCCESSFUL call actually took, not just failures —
// added after a session repeatedly took 45s+ per turn with no failed-
// provider warnings at all, meaning every individual call was
// succeeding and the time was going somewhere invisible (this pipeline
// chains several separate callAI/callAIStructured calls per turn — the
// crisis check, the severity check, domain classification, symptom
// extraction, etc. — so a turn's total time is the SUM of however many
// of those run, not any single one). Without per-call timing there was
// no way to tell "one call is oddly slow" from "many calls each took a
// few seconds and it adds up" — this makes that visible in the log
// instead of guessed at. SLOW_CALL_WARN_MS is deliberately generous
// (most classification calls should be a couple seconds); tune it down
// if you want to see timings for every call, not just slow ones.
const SLOW_CALL_WARN_MS = Number(process.env.AI_SLOW_CALL_WARN_MS) || 5000;

function logCallDuration(providerName, startedAt, structured) {
  const ms = Date.now() - startedAt;
  const label = structured ? "structured call" : "call";
  if (ms >= SLOW_CALL_WARN_MS) {
    console.warn(`[callAi] provider "${providerName}" ${label} took ${ms}ms (slow)`);
  } else {
    console.log(`[callAi] provider "${providerName}" ${label} took ${ms}ms`);
  }
}

// TIGHTENED (found while investigating Groq's daily token budget
// exhaustion, applies to every provider — all four share the same
// makeOpenAICompatibleProvider adapter): was 1200. Every caller of
// plain callAI() in this codebase (safetyCheck.js's crisis/severity/
// emotional-concern checks, offtopiccheck.js's domain classifier) asks
// for either a single classification word or a 1-2 sentence reply —
// none need anywhere near 1200 output tokens. 300 is still several
// times more than any real response needs, cutting the worst-case
// per-call output-token cost (and therefore the shared daily budget on
// Groq/Gemini) by 4x if a call ever rambles instead of answering
// cleanly. Pass an explicit maxTokens at the call site if a genuinely
// longer plain-text response is ever needed.
export async function callAI({ system, message, temperature = 0, maxTokens = 300, reasoningEffort = "none" }) {
  if (PROVIDER_CHAIN.length === 0) {
    throw new Error("AI call failed: no AI provider is configured (see callAi.js startup warning).");
  }
  const failures = [];
  for (const provider of PROVIDER_CHAIN) {
    const startedAt = Date.now();
    try {
      const result = await provider.callText({ system, message, temperature, maxTokens, reasoningEffort });
      logCallDuration(provider.name, startedAt, false);
      return result;
    } catch (err) {
      logCallDuration(provider.name, startedAt, false);
      failures.push(`${provider.name}: ${err.message}`);
      console.warn(`[callAi] provider "${provider.name}" failed — trying next in chain (if any):`, err.message);
    }
  }
  throw new Error(`AI call failed on every configured provider — ${failures.join(" | ")}`);
}

/**
 * @param {object} params
 * @param {string} params.system
 * @param {string} params.message
 * @param {object} params.schema
 * @param {number} [params.temperature=0] - defaults to 0 (deterministic) for
 *   every structured call that makes a factual/clinical extraction decision
 *   (symptom classification, confirmation resolution, etc). Pass a higher
 *   value ONLY for a call that is composing free-text PROSE with no
 *   clinical assertion of its own (e.g. symptomClassifier.js's
 *   composeNaturalDescription, which just rephrases already-decided
 *   structured data into a natural sentence) — see that function's doc
 *   comment for why a little more room there is safe.
 */
export async function callAIStructured({ system, message, schema, temperature = 0 }) {
  if (PROVIDER_CHAIN.length === 0) {
    throw new Error("Structured AI call failed: no AI provider is configured (see callAi.js startup warning).");
  }
  const failures = [];
  for (const provider of PROVIDER_CHAIN) {
    const startedAt = Date.now();
    try {
      const result = await provider.callStructured({ system, message, schema, temperature });
      logCallDuration(provider.name, startedAt, true);
      return result;
    } catch (err) {
      logCallDuration(provider.name, startedAt, true);
      failures.push(`${provider.name}: ${err.message}`);
      console.warn(`[callAi] provider "${provider.name}" failed structured call — trying next in chain (if any):`, err.message);
    }
  }
  throw new Error(`Structured AI call failed on every configured provider — ${failures.join(" | ")}`);
}