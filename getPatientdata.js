// ============================================
// HealthMate AI: Patient History RAG Search
// Embeds the patient's message, searches summaries_vectors for that
// patient via pgvector, then re-ranks results so clinical_note rows
// (doctor-authored, authoritative) always outrank ai_summary rows
// (AI-generated, supporting context only) — regardless of raw
// similarity score. This is the trust hierarchy decided earlier:
// a doctor's note that's slightly less semantically similar should
// still surface before a more-similar AI summary.
// ============================================

import { createClient } from "@supabase/supabase-js";
import { GoogleGenerativeAI } from "@google/generative-ai";
import "dotenv/config";

const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_SERVICE_KEY);
const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);

const SIMILARITY_FLOOR = 0.65; // below this, a result is too weak to be worth including at all
const MAX_HISTORY_ITEMS = 4;   // cap what actually enters the generation context

const SOURCE_TYPE_PRIORITY = { clinical_note: 0, ai_summary: 1 };

/**
 * @param {string} patientId
 * @param {string} queryText - typically the patient's raw message, or the matched symptom names joined
 * @returns {Promise<Array<{
 *   sourceType: 'clinical_note' | 'ai_summary',
 *   content: string,
 *   similarity: number,
 *   createdAt: string
 * }>>}
 */
export async function getPatientHistory(patientId, queryText) {
  const model = genAI.getGenerativeModel({ model: "text-embedding-004" });
  const embedResult = await model.embedContent(queryText);
  const queryEmbedding = embedResult.embedding.values;

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
    if (priorityDiff !== 0) return priorityDiff; // clinical_note (0) sorts before ai_summary (1)
    return b.similarity - a.similarity; // within the same source_type, higher similarity first
  });

  return ranked.slice(0, MAX_HISTORY_ITEMS).map((row) => ({
    sourceType: row.source_type,
    content: row.content,
    similarity: row.similarity,
    createdAt: row.created_at,
  }));
}