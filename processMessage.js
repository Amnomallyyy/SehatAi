
import { runSafetyCheck, checkMisuseRequest } from "./safetyCheck.js";
import { matchSymptoms, getHighestUrgency } from "./matchSymptoms.js";
import { getKnowledgeGraphContext } from "./knowledgegraphlookup.js";
import { getRelevantLabValues } from "./getlabvalues.js";
import { getPatientHistory } from "./getPatientdata.js";
import { generateSpecialistRecommendation } from "./specialistrecommendation.js";
import { callAI } from "./callAi.js";
import "dotenv/config";
/**
 * @param {string} message - raw patient message
 * @param {string} patientId - required for lab/history lookups
 */
export async function processPatientMessage(message, patientId) {
  // Step 1: keyword + AI emergency check — always runs first, no exceptions
  const safetyResult = await runSafetyCheck(message, callAI);
  if (safetyResult.isEmergency) {
    return {
      isEmergency: true,
      source: safetyResult.source,
      category: safetyResult.category,
    };
  }

  // Step 2: symptom matching
  const matchResult = await matchSymptoms(message);

  // Step 3: matched symptom's OWN urgency level
  if (matchResult.matched) {
    const urgencyResult = await getHighestUrgency(matchResult.symptoms);
    if (urgencyResult.urgency === "emergency") {
      return {
        isEmergency: true,
        source: "symptom_urgency",
        matchedSymptom: urgencyResult.matchedVia,
      };
    }
  }

  // Step 4: diagnosis-request check (unchanged)
  const misuseResult = checkMisuseRequest(message);

  // Step 5: if no symptom matched, return the safe default — don't
  // run the generation pipeline on nothing.
  if (!matchResult.matched) {
    return {
      isEmergency: false,
      isDiagnosisRequest: misuseResult.isDiagnosisRequest,
      matchedSymptoms: [],
      matchSource: matchResult.source,
      recommendation: {
        specialist_recommended: "General Physician",
        rationale: "No symptoms could be confidently matched.",
        urgency: "routine",
        next_steps: "Please describe your symptoms in more detail, or consult a doctor.",
      },
    };
  }

  // Step 6: knowledge graph context (candidates + urgency + related tests)
  const { candidateSpecialists, urgency, relatedTestNames } =
    await getKnowledgeGraphContext(matchResult.symptoms);

  // Step 7: pull lab values + history in parallel
  const [labValues, history] = await Promise.all([
    getRelevantLabValues(patientId, relatedTestNames),
    getPatientHistory(patientId, message),
  ]);

  // Step 8: generate the structured specialist recommendation
  const recommendation = await generateSpecialistRecommendation({
    candidateSpecialists,
    urgency,
    labValues,
    history,
  });

  return {
    isEmergency: false,
    isDiagnosisRequest: misuseResult.isDiagnosisRequest,
    matchedSymptoms: matchResult.symptoms,
    matchSource: matchResult.source,
    recommendation,
  };
}