// ============================================
// HealthMate AI: AI Client Wrapper (Gemini)
// Same function signatures as the Anthropic version — safetyCheck.js
// and later pipeline stages call these without knowing which provider
// is behind them. Swapping providers later just means rewriting this
// one file.
// ============================================
import 'dotenv/config';
import { GoogleGenerativeAI } from "@google/generative-ai";

const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);

/**
 * Plain text-in, text-out call — used for simple classification tasks
 * like the safety gate, where you just need a short, deterministic answer.
 *
 * @param {Object} args
 * @param {string} args.system - system prompt / instructions
 * @param {string} args.message - the user message to classify or respond to
 * @param {number} [args.temperature] - defaults to 0 for deterministic tasks
 * @param {number} [args.maxTokens] - defaults to 200, plenty for classification
 * @returns {Promise<string>} - the model's text response
 */
export async function callAI({ system, message, temperature = 0, maxTokens = 200 }) {
  try {
    const model = genAI.getGenerativeModel({
      model: "gemini-flash-lite-latest",
      systemInstruction: system,
      generationConfig: {
        temperature,
        maxOutputTokens: maxTokens,
      },
    });

    const result = await model.generateContent(message);
    const text = result.response.text();

    if (!text) {
      throw new Error("No text content in AI response");
    }
    return text;
  } catch (err) {
    // Fail safe, not silent: classification callers should treat a thrown
    // error as "could not classify" and decide their own fallback behavior
    // (e.g. safetyCheck should NOT assume "not an emergency" on API failure).
    throw new Error(`AI call failed: ${err.message}`);
  }
}

/**
 * Structured call — used later for the constrained specialist/diet
 * recommendation generation, where output must match a strict JSON schema
 * (e.g. specialist_recommended locked to an enum from the specialists table).
 * Gemini supports this via responseSchema + responseMimeType.
 *
 * @param {Object} args
 * @param {string} args.system
 * @param {string} args.message
 * @param {Object} args.schema - a JSON schema object describing the required output shape
 * @returns {Promise<Object>} - the parsed structured output
 */
export async function callAIStructured({ system, message, schema }) {
  const model = genAI.getGenerativeModel({
    model: "gemini-2.5-flash",
    systemInstruction: system,
    generationConfig: {
      temperature: 0,
      responseMimeType: "application/json",
      responseSchema: schema,
    },
  });

  const result = await model.generateContent(message);
  const text = result.response.text();

  try {
    return JSON.parse(text);
  } catch (err) {
    throw new Error(`Model did not return valid structured JSON: ${err.message}`);
  }
}