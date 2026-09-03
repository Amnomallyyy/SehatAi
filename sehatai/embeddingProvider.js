// ============================================
// HealthMate AI: Embedding Provider Abstraction
// One shared function for generating embeddings, used by matchSymptoms.js
// (Layer 3), getPatientdata.js, embedSymptom.js, and db-functions.js.
//
// Mode is controlled by EMBEDDING_MODE in .env:
//   "jina" (default) — real Jina embeddings (jina-embeddings-v3), which
//                       supports requesting any output dimension directly
//                       (Matryoshka embeddings). Standardized to 1024 for
//                       BOTH symptoms and summaries_vectors — one
//                       dimension, one call shape, used everywhere.
//   "fake"            — local random vector, no network call, no API
//                        key needed. Similarity scores are meaningless —
//                        only use this to sanity-check pipeline mechanics
//                        when Jina itself is unavailable.
//
// Requires JINA_API_KEY in .env. Get one free at https://jina.ai/embeddings
// ============================================

import "dotenv/config";

const MODE = process.env.EMBEDDING_MODE || "jina";
const JINA_API_URL = "https://api.jina.ai/v1/embeddings";
const JINA_MODEL = "jina-embeddings-v3";

/**
 * Generates a local random vector of the given dimension. No network
 * call, no semantic meaning — diagnostic/fallback use only.
 *
 * Components are drawn from [-1, 1), NOT [0, 1). This matters more than
 * it looks. Math.random() alone gives all-positive vectors, and any two
 * all-positive vectors in high dimensions have a cosine similarity of
 * about 0.75 no matter what text they came from — comfortably above
 * getPatientHistory()'s 0.65 SIMILARITY_FLOOR and matchSymptoms()'s 0.75
 * SEMANTIC_MATCH_THRESHOLD. Fake mode would therefore report confident
 * semantic matches for completely unrelated content, which is the worst
 * possible failure shape: silent, plausible, and wrong.
 *
 * Zero-centred components give a mean cosine similarity of ~0, so fake
 * mode now fails the way it should — by matching nothing — instead of
 * matching everything.
 */
function fakeVector(dimension) {
  return Array.from({ length: dimension }, () => Math.random() * 2 - 1);
}

/**
 * Calls Jina's embeddings API, requesting the exact output dimension
 * needed for the target column (1024 everywhere in this codebase) —
 * Jina truncates/optimizes for this natively, no separate resizing step
 * required.
 *
 * @param {string} text
 * @param {number} dimension
 * @returns {Promise<number[]>}
 */
async function embedWithJina(text, dimension) {
  const apiKey = process.env.JINA_API_KEY;
  if (!apiKey) {
    throw new Error("JINA_API_KEY is not set in .env");
  }

  const response = await fetch(JINA_API_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({
      model: JINA_MODEL,
      task: "retrieval.query", // optimizes the embedding for search-query use, matching how these are used (querying against stored content)
      dimensions: dimension,
      input: [text],
    }),
  });

  if (!response.ok) {
    const errorBody = await response.text().catch(() => "");
    throw new Error(`Jina embedding request failed (${response.status}): ${errorBody}`);
  }

  const result = await response.json();
  const embedding = result?.data?.[0]?.embedding;

  if (!embedding || embedding.length !== dimension) {
    throw new Error(
      `Jina returned an unexpected embedding shape (expected ${dimension} dims, ` +
      `got ${embedding?.length ?? "none"})`
    );
  }

  return embedding;
}

/**
 * @param {string} text - text to embed
 * @param {number} dimension - required output dimension (1024 for both symptoms and summaries_vectors)
 * @returns {Promise<number[]>}
 */
export async function embedText(text, dimension) {
  if (MODE === "fake") {
    return fakeVector(dimension);
  }

  if (MODE === "jina") {
    return embedWithJina(text, dimension);
  }

  throw new Error(`Unknown EMBEDDING_MODE: "${MODE}"`);
}

/**
 * Whether the current mode produces semantically meaningful embeddings.
 * Useful for callers that want to log a warning, skip presenting
 * "confident" semantic-match results, or refuse to persist the vector
 * at all (see db-functions.js's saveSummaryEmbedding).
 */
export function isFakeMode() {
  return MODE === "fake";
}
