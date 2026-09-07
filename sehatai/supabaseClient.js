// ============================================
// SehatAI: Shared Supabase client
// Every module in this project was creating its own client inline.
// That still works, but the newer modules (chatLog, groundingVerifier,
// profileFact) import a shared one — this file is that shared one.
// Nothing else needs to change; both styles coexist fine.
// ============================================

import { createClient } from '@supabase/supabase-js';
import 'dotenv/config';

if (!process.env.SUPABASE_URL || !process.env.SUPABASE_SERVICE_KEY) {
  console.warn(
    '⚠️  SUPABASE_URL / SUPABASE_SERVICE_KEY missing from .env — every DB call will fail.'
  );
}

export const supabase = createClient(
  process.env.SUPABASE_URL,
  process.env.SUPABASE_SERVICE_KEY
);
