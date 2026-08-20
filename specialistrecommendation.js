// ============================================
// HealthMate AI: Specialist Recommendation Generation
// Takes the knowledge graph context (candidate specialists + urgency),
// lab values, and patient history — all gathered by the retrieval layer
// — and produces a structured recommendation. The enum is built
// dynamically from the mapped specialists so the model can never
// invent one that isn't in the graph.
// ============================================

import { callAIStructured } from "./callAi.js";

const SAFE_DEFAULT = {
  id: null,
  name: "General Physician",
  description: "Safe default when no specialist mapping exists.",
};

/**
 * @param {Object} args
 * @param {Array<{id: string, name: string}>} args.candidateSpecialists
 * @param {string|null} args.urgency
 * @param {Array} args.labValues
 * @param {Array} args.history
 * @returns {Promise<{
 *   specialist_recommended: string,
 *   rationale: string,
 *   urgency: string,
 *   next_steps: string
 * }>}
 */
export async function generateSpecialistRecommendation({
  candidateSpecialists,
  urgency,
  labValues,
  history,
}) {
  // If the graph had no mapping, fall back to the safe default rather
  // than inventing a specialist — same philosophy as matchSymptoms Layer 4.
  const pool = candidateSpecialists.length > 0
    ? candidateSpecialists
    : [SAFE_DEFAULT];

  const schema = {
    type: "object",
    properties: {
      specialist_recommended: {
        type: "string",
        enum: pool.map((s) => s.name), // locked to real, mapped specialists
      },
      rationale: { type: "string" },
      urgency: {
        type: "string",
        enum: ["routine", "urgent", "emergency"],
      },
      next_steps: { type: "string" },
    },
    required: ["specialist_recommended", "rationale", "urgency"],
  };

  const system = `You are HealthMate AI. Recommend a specialist based ONLY on the
patient's matched symptoms, lab values, and history. Choose from the provided
enum. Never recommend a specialist outside the enum. If the data is
insufficient, recommend General Physician.`;

  const message = JSON.stringify({
    candidateSpecialists: pool.map((s) => s.name),
    urgency,
    labValues,
    history,
  });

  return callAIStructured({ system, message, schema });
}