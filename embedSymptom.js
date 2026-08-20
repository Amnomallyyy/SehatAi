// ============================================
// HealthMate AI: Symptom Embedding Seed Script
// Run this ONCE after adding new rows to the symptoms table (or after
// running symptom_embedding_migration.sql for the first time) to
// generate and store embeddings for semantic matching (Layer 3).
//
// Usage: node embedSymptoms.js
// ============================================

import { createClient } from "@supabase/supabase-js";
import { GoogleGenerativeAI } from "@google/generative-ai";
import 'dotenv/config';
const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_SERVICE_KEY);
const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);

/**
 * Embeds a single piece of text using Gemini's embedding model.
 * Includes the symptom name + its aliases together, so the embedding
 * captures the full range of phrasings, not just the canonical term.
 */
async function embedText(text) {
  const model = genAI.getGenerativeModel({ model: "text-embedding-004" });
  const result = await model.embedContent(text);
  return result.embedding.values; // array of 768 floats
}

async function run() {
  console.log("--- Loading symptoms table ---");
  const { data: symptoms, error } = await supabase
    .from("symptoms")
    .select("id, name, aliases");

  if (error) throw new Error(`Failed to load symptoms: ${error.message}`);
  console.log(`Found ${symptoms.length} symptoms to embed.`);

  for (const symptom of symptoms) {
    const combinedText = [symptom.name, ...(symptom.aliases || [])].join(", ");

    console.log(`Embedding: "${symptom.name}"...`);
    const embedding = await embedText(combinedText);

    const { error: updateError } = await supabase
      .from("symptoms")
      .update({ embedding })
      .eq("id", symptom.id);

    if (updateError) {
      console.error(`  Failed to save embedding for "${symptom.name}": ${updateError.message}`);
      continue;
    }
    console.log(`  Saved.`);
  }

  console.log("\n✅ Done. All symptoms embedded.");
}

run().catch((err) => console.error("❌ Embedding script failed:", err));
