/**
 * Mirrors pipeline.py's report dict, api/contract.md's Claim/Evidence
 * schema, and the additive fields introduced by this rebuild (queries,
 * synthesizer_parse_deletions, flag_details, retraction_source, the
 * history/cache endpoints, and the /api/ask/stream NDJSON event shapes).
 * Keep in lockstep with the Python side -- see pipeline.py's
 * serialize_claim()/_build_evidence_items()/_run_stages() and
 * api/server.py's _handle_ask_stream()/_handle_ask()/core/store.py.
 */

export type CheckOutcome = "pass" | "fail" | "skipped";
export type EntailmentOutcome = "supports" | "refutes" | "nei" | "skipped";
export type StandingOutcome = "pass" | "fail" | "flag" | "skipped";
export type ClaimStatus = "kept" | "flagged" | "deleted";
export type FlagSource = "verifier" | "red_team";
export type EvidenceSource = "pubmed" | "europepmc" | "clinicaltrials";

export interface Citation {
  sid: string;
  citation_key: string;
  title: string | null;
  url: string | null;
}

export interface FlagDetail {
  source: FlagSource;
  label: string;
  note: string | null;
}

export interface ClaimChecks {
  existence: CheckOutcome;
  entailment: EntailmentOutcome;
  standing: StandingOutcome;
}

export interface Claim {
  claim_id: string;
  text: string;
  status: ClaimStatus;
  deletion_reason: string | null;
  /** Plain-string flags -- kept exactly as the backend has always shaped
   * it (tests assert `flags[0].startswith("overstatement: ")`), so this
   * stays a string array even though flag_details now exists alongside it. */
  flags: string[];
  /** NEW: parallel to `flags`, tags each one by which agent raised it --
   * this is what lets the UI show "Red Team flags, only Verifier deletes"
   * instead of merging both into indistinguishable badges. */
  flag_details: FlagDetail[];
  checks: ClaimChecks;
  verdict: string | null;
  confidence: number | null;
  evidence_quote: string | null;
  citations: Citation[];
}

export interface Evidence {
  sid: string;
  citation_key: string;
  source: EvidenceSource;
  native_id: string | null;
  doi: string | null;
  url: string | null;
  title: string | null;
  journal: string | null;
  publication_date: string | null;
  study_design: string | null;
  trial_status: string | null;
  is_preprint: boolean;
  is_retracted: boolean;
  /** NEW: "pubmed" | "crossref" -- which check caught the retraction.
   * Previously always lost before reaching the API; see the pipeline.py
   * _build_evidence_items() fix. */
  retraction_source: string | null;
  relevance_score: number;
  rationale: string | null;
  abstract: string | null;
}

export interface Funnel {
  claims_generated: number;
  claims_deleted: number;
  claims_kept: number;
  by_reason: Record<string, number>;
}

export interface SynthesizerParseDeletion {
  text: string;
  reason: string;
}

export interface Report {
  question: string;
  abstained: boolean;
  abstain_reasons: string[];
  funnel: Funnel;
  answer_text: string;
  claims: Claim[];
  evidence: Evidence[];
  disclaimer: string;
  /** NEW: the Strategist's planned search queries -- previously only
   * visible transiently in the live stream, never persisted. */
  queries: string[];
  /** NEW: sentences the Synthesizer's own deterministic parser dropped
   * before the Verifier ever saw them (uncited / unknown-citation
   * sentences the LLM wrote). Distinct from the verification funnel. */
  synthesizer_parse_deletions: SynthesizerParseDeletion[];
  /** NEW: the Synthesizer's own self-reported statements ([GAP]-tagged
   * sentences) that a specific facet of the question isn't addressed by
   * the evidence set. No citation, not a claim, never Verifier-checked --
   * self-reported by the model, not independently verified. A partial
   * answer can have both a non-empty answer_text and non-empty entries
   * here. Always [] for a total abstention. */
  unanswered_aspects: string[];
  /** NEW: present when served via /api/ask or /api/ask/stream (not on
   * GET /api/history/{id}, where it's implied by `source`). */
  cached?: boolean;
  run_id?: string;
}

// --- /api/ask/stream NDJSON event union --------------------------------

interface StageEventBase {
  type: "stage";
  stage: "strategist" | "retrieval" | "appraiser" | "synthesizer" | "verifier" | "red_team" | "complete";
  status: string;
}

export interface StrategistStartEvent extends StageEventBase {
  stage: "strategist";
  status: "start";
}
/** NEW: fired once per proposer draft (agents/strategist.py's on_round) --
 * the proposer<->critic loop is unbounded by design (a dense, multi-part
 * question can take several rounds before the critic is satisfied), so
 * without this the panel would sit on "start" for the whole loop, the same
 * gap the Appraiser/Verifier's own on_progress hooks close for their
 * loops. */
export interface StrategistProgressEvent extends StageEventBase {
  stage: "strategist";
  status: "progress";
  round: number;
  query_count: number;
}
export interface StrategistDoneEvent extends StageEventBase {
  stage: "strategist";
  status: "done";
  queries: string[];
  llm_calls: number | null;
}
export interface RetrievalStartEvent extends StageEventBase {
  stage: "retrieval";
  status: "start";
}
export interface RetrievalDoneEvent extends StageEventBase {
  stage: "retrieval";
  status: "done";
  pool_size: number;
  retracted: number;
  llm_calls: 0;
}
export interface AppraiserStartEvent extends StageEventBase {
  stage: "appraiser";
  status: "start";
}
/** NEW: fired once per LLM batch (agents/appraiser.py's on_progress) so the
 * panel can show real mid-stage movement instead of sitting on "start" for
 * the whole batch loop -- this is the exact gap that made a slow appraisal
 * look identical to a hung one. */
export interface AppraiserProgressEvent extends StageEventBase {
  stage: "appraiser";
  status: "progress";
  batch: number;
  batch_count: number;
}
export interface AppraiserDoneEvent extends StageEventBase {
  stage: "appraiser";
  status: "done";
  appraised: number;
  top_score: number | null;
  llm_calls: number | null;
}
export interface SynthesizerStartEvent extends StageEventBase {
  stage: "synthesizer";
  status: "start";
}
export interface SynthesizerDoneEvent extends StageEventBase {
  stage: "synthesizer";
  status: "done";
  sentences: number;
  abstained: boolean;
  llm_calls: number | null;
  error?: string;
}
export interface VerifierStartEvent extends StageEventBase {
  stage: "verifier";
  status: "start";
}
/** NEW: fired once per claim as Stage C (entailment) checks it -- same
 * rationale as AppraiserProgressEvent, for the Verifier's own slow loop. */
export interface VerifierProgressEvent extends StageEventBase {
  stage: "verifier";
  status: "progress";
  claim: number;
  claim_count: number;
}
export interface VerifierDoneEvent extends StageEventBase {
  stage: "verifier";
  status: "done";
  funnel: Funnel;
  llm_calls: number | null;
}
export interface RedTeamStartEvent extends StageEventBase {
  stage: "red_team";
  status: "start";
}
export interface RedTeamDoneEvent extends StageEventBase {
  stage: "red_team";
  status: "done";
  flagged_claims: number;
  llm_calls: number | null;
}
export interface CompleteEvent extends StageEventBase {
  stage: "complete";
  status: "answered" | "abstained";
  reason?: string | null;
}

export type StageEvent =
  | StrategistStartEvent
  | StrategistProgressEvent
  | StrategistDoneEvent
  | RetrievalStartEvent
  | RetrievalDoneEvent
  | AppraiserStartEvent
  | AppraiserProgressEvent
  | AppraiserDoneEvent
  | SynthesizerStartEvent
  | SynthesizerDoneEvent
  | VerifierStartEvent
  | VerifierProgressEvent
  | VerifierDoneEvent
  | RedTeamStartEvent
  | RedTeamDoneEvent
  | CompleteEvent;

/** NEW: the whole point of the cache -- sent instead of the six stage
 * events when a prior identical run is replayed, so the UI never fakes
 * an animation for agents that didn't actually run this time. */
export interface CacheHitMessage {
  type: "cache_hit";
  run_id: string;
  cached_at: string;
}

export interface ResultMessage {
  type: "result";
  report: Report;
}

export interface StreamErrorMessage {
  type: "error";
  error: string;
}

export type StreamMessage = ({ type: "stage" } & StageEvent) | CacheHitMessage | ResultMessage | StreamErrorMessage;

// --- /api/history* ------------------------------------------------------

export type RunSource = "live" | "cache_hit" | "mock";

export interface HistorySummary {
  id: string;
  question: string;
  abstained: boolean;
  created_at: string;
  source: RunSource;
}

export interface HistoryListResponse {
  runs: HistorySummary[];
  total: number;
}

export interface HistoryDetailResponse {
  id: string;
  question: string;
  report: Report;
  created_at: string;
  source: RunSource;
}

// --- /api/health ----------------------------------------------------------

export interface HealthResponse {
  status: string;
  llm_model: string;
  sensitive_model: string | null;
  keys_count: number;
}

export interface ApiErrorBody {
  error: string;
}
