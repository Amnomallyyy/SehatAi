// ============================================
// HealthMate AI: Patient History RAG Search
// Embeds the patient's message, searches summaries_vectors for that
// patient via pgvector, then re-ranks results so clinical_advice rows
// (doctor-authored, authoritative) always outrank ai_summary rows
// (AI-generated, supporting context only) — regardless of raw
// similarity score. This is the trust hierarchy decided earlier:
// a doctor's note that's slightly less semantically similar should
// still surface before a more-similar AI summary.
//
// Uses the shared embeddingProvider (see embeddingProvider.js) —
// currently Jina by default, at 1024 dimensions, matching
// summaries_vectors.embedding. Do NOT call Gemini/GoogleGenerativeAI
// directly here — that was the earlier bug (403 Forbidden, this
// project's Gemini access is not working). All embedding calls go
// through embedText() so switching providers is a one-file change.
// ============================================

import { embedText, isFakeMode } from "./embeddingProvider.js";
import { supabase } from "./supabaseClient.js";

const SIMILARITY_FLOOR = 0.65; // below this, a result is too weak to be worth including at all
const MAX_HISTORY_ITEMS = 4;   // cap what actually enters the generation context

// source_type on summaries_vectors is only ever 'ai_summary' or
// 'clinical_advice' (see the table's CHECK constraint) — 'clinical_note'
// never matches a real row.
const SOURCE_TYPE_PRIORITY = { clinical_advice: 0, ai_summary: 1 };

/**
 * @param {string} patientId
 * @param {string} queryText - typically the patient's raw message, or the matched symptom names joined
 * @returns {Promise<Array<{
 *   sourceType: 'clinical_advice' | 'ai_summary',
 *   content: string,
 *   similarity: number,
 *   createdAt: string
 * }>>}
 */
export async function getPatientHistory(patientId, queryText) {
  const queryEmbedding = await embedText(queryText, 1024); // summaries_vectors.embedding is vector(1024)

  if (isFakeMode()) {
    console.warn("⚠️  getPatientHistory running in FAKE embedding mode — similarity scores are not meaningful.");
  }

  const { data, error } = await supabase.rpc("match_patient_history", {
    query_embedding: queryEmbedding,
    match_patient_id: patientId,
    match_count: 8,
  });

  if (error) {
    // History search failing shouldn't crash the whole response pipeline —
    // the caller can still generate a response from lab values + knowledge
    // graph alone. Log and return empty rather than throwing.
    console.error(`Patient history search failed: ${error.message}`);
    return [];
  }

  if (!data || data.length === 0) {
    return [];
  }

  const aboveFloor = data.filter((row) => row.similarity >= SIMILARITY_FLOOR);

  const ranked = aboveFloor.sort((a, b) => {
    const priorityDiff = SOURCE_TYPE_PRIORITY[a.source_type] - SOURCE_TYPE_PRIORITY[b.source_type];
    if (priorityDiff !== 0) return priorityDiff; // clinical_advice (0) sorts before ai_summary (1)
    return b.similarity - a.similarity; // within the same source_type, higher similarity first
  });

  return ranked.slice(0, MAX_HISTORY_ITEMS).map((row) => ({
    sourceType: row.source_type,
    content: row.content,
    similarity: row.similarity,
    createdAt: row.created_at,
  }));
}