// ============================================
// HealthMate AI: Symptom Matching Module
// Turns a raw patient message into matched symptom_id(s) from the
// knowledge graph tables. Nothing downstream (specialist routing,
// relevant lab lookup) can run without this.
//
// Layer 1: direct alias match (Postgres, no AI)
// Layer 2: AI-assisted extraction (only if Layer 1 finds nothing)
// Layer 3: semantic match via pgvector (only if Layers 1 and 2 find nothing)
// Layer 4: safe "no match" — caller falls back to the safe default response
//
// Also exports getHighestUrgency(), which looks up whether any matched
// symptom's own urgency_level (from symptom_specialist_map) warrants
// treating this message as an emergency — even if no alarming keyword
// was ever typed. See processMessage.js for how this connects to the
// keyword-based safety check.
// ============================================

import { createClient } from "@supabase/supabase-js";
import { GoogleGenerativeAI } from "@google/generative-ai";
import { callAI } from "./callAi.js";
import 'dotenv/config';
const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_SERVICE_KEY);
const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);

// ------------------------------------------
// Layer 1: Direct match against symptoms.name and symptoms.aliases
// ------------------------------------------

/**
 * Loads the full symptoms table once. Small table (10-20 rows for v1),
 * so no pagination needed. Used both for direct matching and to build
 * the known-terms list for Layer 2's AI prompt.
 */
async function loadAllSymptoms() {
  const { data, error } = await supabase.from("symptoms").select("id, name, aliases");
  if (error) throw new Error(`Failed to load symptoms table: ${error.message}`);
  return data;
}

/**
 * Plain substring matching — checks if any known symptom name or alias
 * appears in the patient's message. Deterministic, no AI, near-instant.
 *
 * @param {string} message - raw patient message
 * @param {Array} allSymptoms - result of loadAllSymptoms()
 * @returns {Array<{id: string, name: string}>} - matched symptoms (can be more than one)
 */
function directMatch(message, allSymptoms) {
  const lowerMsg = message.toLowerCase();
  const matches = [];

  for (const symptom of allSymptoms) {
    const allPhrases = [symptom.name, ...(symptom.aliases || [])];
    const hit = allPhrases.some((phrase) => lowerMsg.includes(phrase.toLowerCase()));
    if (hit) {
      matches.push({ id: symptom.id, name: symptom.name });
    }
  }
  return matches;
}

// ------------------------------------------
// Layer 2: AI-assisted extraction
// Only runs if Layer 1 found nothing. The model is constrained to
// ONLY return names that exist in our table, or "NONE" — it never
// invents a symptom name we don't already have a mapping for.
// ------------------------------------------

/**
 * @param {string} message - raw patient message
 * @param {Array} allSymptoms - result of loadAllSymptoms()
 * @returns {Promise<Array<{id: string, name: string}>>}
 */
async function aiAssistedMatch(message, allSymptoms) {
  const knownNames = allSymptoms.map((s) => s.name);

  const systemPrompt = `You match patient messages to known medical symptom terms.
Known terms: ${knownNames.join(", ")}

Read the patient's message and return ONLY the known term(s) above that apply,
comma-separated. If none apply, return exactly: NONE
Never invent a term that is not in the known list above.`;

  const result = await callAI({ system: systemPrompt, message, temperature: 0 });
  const cleaned = result.trim();

  if (cleaned.toUpperCase() === "NONE") {
    return [];
  }

  const extractedNames = cleaned
    .split(",")
    .map((n) => n.trim().toLowerCase())
    .filter(Boolean);

  // Re-map extracted names back to real symptom rows — this also guards
  // against the model returning something slightly off from our known list.
  return allSymptoms
    .filter((s) => extractedNames.includes(s.name.toLowerCase()))
    .map((s) => ({ id: s.id, name: s.name }));
}

// ------------------------------------------
// Layer 3: Semantic match via pgvector
// Only runs if Layers 1 and 2 both find nothing. Catches phrasing
// that's genuinely novel — no shared words with any known alias, and
// too indirect for the AI extraction prompt to map confidently.
// ------------------------------------------

const SEMANTIC_MATCH_THRESHOLD = 0.75; // below this, treat as no match — don't guess

/**
 * @param {string} message - raw patient message
 * @returns {Promise<Array<{id: string, name: string}>>}
 */
async function semanticMatch(message) {
  const model = genAI.getGenerativeModel({ model: "text-embedding-004" });
  const result = await model.embedContent(message);
  const queryEmbedding = result.embedding.values;

  // Note: match_symptom_embedding only takes query_embedding — the
  // threshold is applied here in JS, not inside the SQL function.
  const { data, error } = await supabase.rpc("match_symptom_embedding", {
    query_embedding: queryEmbedding,
  });

  if (error) {
    // Semantic layer failing shouldn't crash the whole match chain —
    // log it and let the caller fall through to the safe default.
    console.error(`Semantic match RPC failed: ${error.message}`);
    return [];
  }

  if (!data || data.length === 0 || data[0].similarity < SEMANTIC_MATCH_THRESHOLD) {
    return [];
  }

  return [{ id: data[0].id, name: data[0].name }];
}

// ------------------------------------------
// Combined entry point
// ------------------------------------------

/**
 * Runs the full symptom-matching chain: direct match first, AI fallback second,
 * semantic match third.
 * This is the ONLY matching function the rest of the app should call.
 *
 * @param {string} message - raw patient message
 * @returns {Promise<{matched: boolean, symptoms: Array<{id: string, name: string}>, source?: string}>}
 */
export async function matchSymptoms(message) {
  const allSymptoms = await loadAllSymptoms();

  const direct = directMatch(message, allSymptoms);
  if (direct.length > 0) {
    return { matched: true, symptoms: direct, source: "direct" };
  }

  const aiMatched = await aiAssistedMatch(message, allSymptoms);
  if (aiMatched.length > 0) {
    return { matched: true, symptoms: aiMatched, source: "ai_extraction" };
  }

  const semanticMatched = await semanticMatch(message);
  if (semanticMatched.length > 0) {
    return { matched: true, symptoms: semanticMatched, source: "semantic" };
  }

  // Layer 4: no match found anywhere. Caller (router) is responsible for
  // falling back to the safe default response — this function does not
  // guess or invent a symptom.
  return { matched: false, symptoms: [], source: "none" };
}

// ------------------------------------------
// Urgency lookup — connects matched symptoms to symptom_specialist_map
// ------------------------------------------

const URGENCY_RANK = { routine: 1, urgent: 2, emergency: 3 };

/**
 * Looks up the highest urgency_level among all matched symptoms.
 * A symptom can map to multiple specialists at different urgency
 * levels — this returns the most severe one found.
 *
 * Used by processMessage.js to decide whether a matched symptom's
 * own severity should ALSO trigger an emergency response, even when
 * safetyCheck.js's keyword scan found nothing alarming in the wording.
 *
 * @param {Array<{id: string, name: string}>} matchedSymptoms - from matchSymptoms()
 * @returns {Promise<{urgency: string|null, matchedVia?: string}>}
 */
export async function getHighestUrgency(matchedSymptoms) {
  if (matchedSymptoms.length === 0) {
    return { urgency: null };
  }

  const symptomIds = matchedSymptoms.map((s) => s.id);

  const { data, error } = await supabase
    .from("symptom_specialist_map")
    .select("urgency_level, symptom_id")
    .in("symptom_id", symptomIds);

  if (error) {
    console.error(`Urgency lookup failed: ${error.message}`);
    return { urgency: null }; // fail safe — don't block the flow, just skip this signal
  }

  if (!data || data.length === 0) {
    return { urgency: null };
  }

  const highest = data.reduce((max, row) =>
    URGENCY_RANK[row.urgency_level] > URGENCY_RANK[max.urgency_level] ? row : max
  );

  const matchedSymptom = matchedSymptoms.find((s) => s.id === highest.symptom_id);

  return { urgency: highest.urgency_level, matchedVia: matchedSymptom?.name };
}