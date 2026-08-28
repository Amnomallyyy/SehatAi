"""
agents/verifier.py -- the three-way verification gate for EvidenceBoard.

Every sentence the Synthesizer emits is decomposed into atomic claims
(SAFE, arXiv:2403.18802) and each claim must survive three independent
checks before a doctor ever sees it:

  1. EXISTENCE  -- the cited evidence id is in the frozen pool AND the
     record's identifier resolves against its authoritative registry
     (PubMed esummary / doi.org / ClinicalTrials.gov). Registry checks
     are FAIL-OPEN: a network error never deletes a claim; only an
     authoritative "not found" does.
  2. ENTAILMENT -- an LLM NLI judge in the style of SciFact
     (arXiv:2004.14974) / HealthVer decides SUPPORTS / REFUTES /
     NOT_ENOUGH_INFO strictly from the cited title + abstract. The judge
     FAILS CLOSED: an unparseable or unreachable judge counts as
     NOT_ENOUGH_INFO, which deletes the claim.
  3. STANDING  -- the source is not retracted, is not under an
     expression of concern / erratum, and (heuristically) is not
     superseded by a newer systematic review or meta-analysis found via
     one extra PubMed query. This mirrors the FEVER-style label pipeline
     and the VerifAI claim-verification architecture
     (arXiv:2604.08549).

Failure = deletion. The report keeps every claim (kept, flagged AND
deleted) plus the "9 generated -> 2 deleted -> 7 shown" funnel so the
API layer can render the verification-first story honestly.

Deletion precedence
-------------------
existence > retraction (standing-fail) > entailment-refutes >
entailment-nei > supersession. A claim is deleted exactly once; the
first matching rule in that order supplies the deletion_reason, while
any other computed check outcomes are still recorded on the claim.
Concretely: a claim whose citation does not resolve records
{existence: fail, entailment: skipped, standing: skipped}; a claim
already deleted by entailment records standing "fail" when its source
is retracted (the retraction reason wins by precedence) but otherwise
records standing "skipped".

The Synthesizer result is DUCK-TYPED (attributes `sentences`,
`abstained`) because agents/synthesizer.py is built in parallel; the
PubMed client used by the supersession heuristic is injected and is
never imported here, so this module stays offline-safe to import.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

import requests

from core.llm import LLMClient, LLMError

# --- authoritative registries (existence checks) -----------------------------

_ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
_CTGOV_STUDY_URL = "https://clinicaltrials.gov/api/v2/studies/{nct_id}"
_DOI_RESOLVE_URL = "https://doi.org/{doi}"

# --- check outcome vocabularies ------------------------------------------------

_EXISTS, _MISSING, _SKIPPED = "exists", "missing", "skipped"

_VERDICTS = ("SUPPORTS", "REFUTES", "NOT_ENOUGH_INFO")

# A superseding review only deletes a claim when the judge refutes it with
# real conviction -- the whole standing check is a heuristic, so its bar sits
# at the ordinary support threshold rather than at zero.
_SUPERSESSION_REFUTE_CONFIDENCE = 0.70

# Erratum / expression-of-concern markers (standing flag, not deletion).
_STANDING_FLAG_MARKERS = ("expression of concern", "erratum")

_ABSTAIN_SYNTH = "synthesizer abstained: insufficient evidence"
_ABSTAIN_ALL_DELETED = "all claims deleted during verification"

# --- LLM prompt templates -------------------------------------------------------

_DECOMPOSE_SYSTEM = (
    "You decompose cited clinical sentences into atomic claims for "
    "fact-checking. One predicate per claim (split dosages, durations, "
    "populations, effect sizes into separate claims). Copy wording from the "
    "sentence; do not add facts. Keep every claim attached to the sentence's "
    "citations. Return ONLY JSON: "
    '{"claims": [{"sentence_index": <int>, "claim": "...", "citations": '
    '["S1", ...]}]} — at most {max_claims} claims total.'
)

_JUDGE_SYSTEM = (
    "You are a strict scientific evidence entailment judge, modeled on the "
    "SciFact/HealthVer annotation guidelines. Definitions: SUPPORTS — a "
    "careful reader of the evidence alone would conclude the claim is true. "
    "REFUTES — the evidence alone indicates the claim is false. "
    "NOT_ENOUGH_INFO — the evidence neither confirms nor contradicts the "
    "claim. Decide ONLY from the evidence provided. Do not use medical "
    "knowledge not in the evidence. Numbers, populations, and timeframes "
    "must match exactly. Return ONLY valid JSON: "
    '{"verdict": "SUPPORTS"|"REFUTES"|"NOT_ENOUGH_INFO", '
    '"confidence": 0.0-1.0, "evidence_quote": "<shortest verbatim span that '
    'justifies the verdict, or empty>", "reason": "<one sentence>"}'
)


# --- dataclasses ----------------------------------------------------------------


@dataclass
class ClaimCheck:
    """Outcome of each of the three checks for one claim."""

    existence: str   # "pass" | "fail" | "skipped"
    entailment: str  # "supports" | "refutes" | "nei" | "skipped"
    standing: str    # "pass" | "fail" | "flag" | "skipped"


@dataclass
class Claim:
    """One atomic claim, its verification outcome and its resolved citations."""

    claim_id: str            # "s{n}-c{m}"
    text: str
    status: str              # "kept" | "flagged" | "deleted"
    deletion_reason: Optional[str]
    flags: list[str]         # e.g. ["weakly supported", "expression of concern"]
    checks: ClaimCheck
    verdict: Optional[str]   # "SUPPORTS" | "REFUTES" | "NOT_ENOUGH_INFO" | None
    confidence: Optional[float]
    evidence_quote: Optional[str]
    citations: list[dict]    # [{"sid","citation_key","title","url"}]


@dataclass
class VerificationReport:
    """Full verifier-stage output: every claim plus the deletion funnel."""

    claims: list[Claim]      # ALL claims incl. deleted, in generation order
    funnel: dict             # {"claims_generated", "claims_deleted",
                             #  "claims_kept", "by_reason": {reason: count}}
    abstained: bool
    abstain_reasons: list[str]
    answer_text: str         # kept+flagged texts joined with " "


@dataclass
class EntailmentVerdict:
    """One NLI judge decision (SciFact-style)."""

    verdict: str             # "SUPPORTS" | "REFUTES" | "NOT_ENOUGH_INFO"
    confidence: float        # 0.0-1.0
    evidence_quote: str
    reason: str


# --- module helpers ---------------------------------------------------------------


def _empty_funnel() -> dict:
    return {
        "claims_generated": 0,
        "claims_deleted": 0,
        "claims_kept": 0,
        "by_reason": {},
    }


def _normalize_sentences(synthesis: Any) -> list[dict]:
    """Duck-type the Synthesizer result into plain sentence dicts.

    The synthesizer is built in parallel, so we only rely on the structural
    contract (.sentences with .index/.text/.citations, .abstained) and never
    import its module.
    """
    raw = getattr(synthesis, "sentences", None) or []
    out: list[dict] = []
    for i, sentence in enumerate(raw):
        idx = getattr(sentence, "index", None)
        if isinstance(idx, bool) or not isinstance(idx, int):
            idx = i
        text = str(getattr(sentence, "text", "") or "").strip()
        citations = getattr(sentence, "citations", None) or []
        cits = [str(c) for c in citations if isinstance(c, str)]
        if text:
            out.append({"index": idx, "text": text, "citations": cits})
    return out


def _evidence_blocks(
    sids: list[str], ev_by_sid: dict, max_records: int = 2
) -> list[tuple[str, str, str, str]]:
    """(sid, citation_key, title, abstract) tuples for the judge, capped at
    max_records records per claim."""
    blocks = []
    for sid in sids[:max_records]:
        rec = ev_by_sid.get(sid)
        if rec is None:
            continue
        blocks.append(
            (
                sid,
                str(rec.get("citation_key") or sid),
                str(rec.get("title") or ""),
                str(rec.get("abstract") or ""),
            )
        )
    return blocks


def _judge_prompt(claim_text: str, blocks: list[tuple[str, str, str, str]]) -> str:
    parts = [f"CLAIM: {claim_text}", ""]
    for sid, key, title, abstract in blocks:
        parts.append(f"EVIDENCE (title + abstract of {sid}, {key}):")
        parts.append(title or "(no title)")
        parts.append(abstract[:2000])
        parts.append("")
    return "\n".join(parts).rstrip()


def _parse_verdict(response: Any) -> Optional[EntailmentVerdict]:
    """Defensively validate one judge payload. None when unusable."""
    if not isinstance(response, dict):
        return None
    raw_verdict = response.get("verdict")
    if not isinstance(raw_verdict, str):
        return None
    verdict = raw_verdict.strip().upper()
    if verdict not in _VERDICTS:
        return None
    confidence = response.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        confidence = 0.0
    confidence = min(1.0, max(0.0, float(confidence)))
    quote = response.get("evidence_quote")
    quote = quote if isinstance(quote, str) else ""
    reason = response.get("reason")
    reason = reason if isinstance(reason, str) else ""
    return EntailmentVerdict(verdict, confidence, quote, reason)


# --- the Verifier -----------------------------------------------------------------


class Verifier:
    """Three-way claim verification: existence, entailment, standing.

    All external dependencies are injected (LLM client, HTTP session for the
    registries, optional PubMed client for the supersession heuristic), so
    tests never touch the network. Registry (existence) checks fail open on
    any transport error; the entailment judge fails closed.
    """

    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        pubmed: Optional[Any] = None,
        enable_supersession: bool = True,
        session: Optional[requests.Session] = None,
        registry_timeout: int = 15,
        min_support_confidence: float = 0.70,
        max_claims: int = 12,
    ) -> None:
        self.llm = llm or LLMClient()
        self.pubmed = pubmed
        self.enable_supersession = enable_supersession
        self.session = session or requests.Session()
        self.registry_timeout = registry_timeout
        self.min_support_confidence = min_support_confidence
        self.max_claims = max(1, int(max_claims))

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def verify(
        self,
        question: str,
        synthesis: Any,
        evidence: list[dict],
        queries: list[str],
    ) -> VerificationReport:
        """Verify a synthesis against its evidence pool.

        Returns a VerificationReport containing ALL claims (kept, flagged and
        deleted) plus the verifier-stage funnel. Synthesizer parse deletions
        are NOT part of this funnel -- the pipeline merges them separately.
        """
        ev_by_sid: dict[str, dict] = {}
        for item in evidence or []:
            if isinstance(item, dict) and item.get("sid"):
                ev_by_sid[str(item["sid"])] = item

        # Stage A -- decomposition (with the abstention shortcut).
        sentences = _normalize_sentences(synthesis)
        if getattr(synthesis, "abstained", False) or not sentences:
            print("[verifier] synthesizer abstained; nothing to verify")
            return VerificationReport(
                claims=[],
                funnel=_empty_funnel(),
                abstained=True,
                abstain_reasons=[_ABSTAIN_SYNTH],
                answer_text="",
            )

        valid_sids = set(ev_by_sid)
        rows = [
            {
                "claim_id": rc["claim_id"],
                "text": rc["text"],
                "sids": rc["sids"],
                "existence": _SKIPPED,
                "entailment": _SKIPPED,
                "standing": _SKIPPED,
                "status": "kept",
                "deletion_reason": None,
                "flags": [],
                "verdict": None,
                "confidence": None,
                "quote": None,
            }
            for rc in self._decompose(sentences, valid_sids)
        ]

        # Stage B -- existence (deterministic, registry-backed, fail-open).
        record_status = self._existence_statuses(rows, ev_by_sid)
        for row in rows:
            statuses = [record_status.get(sid, _MISSING) for sid in row["sids"]]
            if _MISSING in statuses:
                row["existence"] = "fail"
            elif _SKIPPED in statuses:
                row["existence"] = _SKIPPED
            else:
                row["existence"] = "pass"
            if row["existence"] == "fail":
                missing_keys = [
                    str(ev_by_sid[sid].get("citation_key") or sid)
                    for sid in row["sids"]
                    if record_status.get(sid) == _MISSING
                ]
                self._delete(
                    row,
                    "citation unresolvable: " + ", ".join(missing_keys or ["unknown"]),
                )

        # Stage C -- entailment (one judge call per surviving claim).
        for row in rows:
            if row["existence"] == "fail":
                continue  # checks recorded as "skipped"
            blocks = _evidence_blocks(row["sids"], ev_by_sid)
            verdict = self._judge_entailment(row["claim_id"], row["text"], blocks)
            row["verdict"] = verdict.verdict
            row["confidence"] = verdict.confidence
            row["quote"] = verdict.evidence_quote
            if verdict.verdict == "SUPPORTS":
                row["entailment"] = "supports"
                if verdict.confidence < self.min_support_confidence:
                    row["flags"].append("weakly supported")
                    row["status"] = "flagged"
            elif verdict.verdict == "REFUTES":
                row["entailment"] = "refutes"
                self._delete(row, "contradicted by its own citation")
            else:
                row["entailment"] = "nei"
                self._delete(row, "unsupported by cited evidence")

        # Stage D -- standing: retraction, erratum/EoC flag, supersession.
        for row in rows:
            if row["existence"] == "fail":
                continue
            cited = [ev_by_sid[sid] for sid in row["sids"] if sid in ev_by_sid]
            retracted = [rec for rec in cited if rec.get("is_retracted")]
            if retracted:
                source = retracted[0].get("retraction_source") or "retracted"
                row["standing"] = "fail"
                # Precedence: retraction outranks entailment deletions.
                self._delete(row, f"source retracted ({source})")
            elif row["status"] == "deleted":
                # Deleted by entailment: standing was not the deciding check.
                row["standing"] = _SKIPPED
            else:
                haystack = " ".join(
                    f"{rec.get('title') or ''} {rec.get('abstract') or ''}"
                    for rec in cited
                ).lower()
                if any(marker in haystack for marker in _STANDING_FLAG_MARKERS):
                    row["standing"] = "flag"
                    row["flags"].append("expression of concern")
                else:
                    row["standing"] = "pass"

        if self.enable_supersession:
            if self.pubmed is None:
                print(
                    "[verifier] supersession enabled but no pubmed client "
                    "injected; skipping supersession"
                )
            else:
                self._run_supersession(rows, question, queries, evidence)

        # Stage E -- funnel, abstention, assembly.
        report = self._assemble(rows, ev_by_sid)
        print(
            f"[verifier] {report.funnel['claims_generated']} claims generated "
            f"-> {report.funnel['claims_deleted']} deleted "
            f"-> {report.funnel['claims_kept']} kept"
        )
        return report

    # ------------------------------------------------------------------
    # Stage A -- decomposition (SAFE-style, one LLM call per answer)
    # ------------------------------------------------------------------

    def _decompose(self, sentences: list[dict], valid_sids: set) -> list[dict]:
        """Split cited sentences into atomic claims.

        One LLM call, one retry (with a JSON nudge appended), then a
        whole-sentence fallback -- verification never dies because the
        decomposer hiccuped.
        """
        sentence_map: dict[int, dict] = {}
        for sentence in sentences:
            sentence_map.setdefault(sentence["index"], sentence)

        prompt = self._build_decomposition_prompt(sentences)
        # .replace (not .format): the template embeds literal JSON whose
        # braces would otherwise be interpreted as format fields.
        system = _DECOMPOSE_SYSTEM.replace("{max_claims}", str(self.max_claims))
        last_error = "invalid decomposition output"
        for attempt in range(2):
            try:
                response = self.llm.complete_json(
                    prompt, system=system, temperature=0.1
                )
            except Exception as exc:  # LLMError per contract; stay defensive
                last_error = str(exc)
            else:
                parsed = self._parse_decomposition(response, sentence_map, valid_sids)
                if parsed:
                    return parsed
                last_error = "decomposition output had no valid claims"
            if attempt == 0:
                prompt = prompt + "\n\nYour previous output was not valid JSON."

        print(
            f"[verifier] decomposition fallback to whole-sentence claims "
            f"({last_error})"
        )
        return self._whole_sentence_claims(sentences, valid_sids)

    def _build_decomposition_prompt(self, sentences: list[dict]) -> str:
        lines = ["Sentences (each with its evidence citations):"]
        for sentence in sentences:
            cits = ", ".join(sentence["citations"]) or "(none)"
            lines.append("")
            lines.append(f"[{sentence['index']}] {sentence['text']}")
            lines.append(f"    citations: {cits}")
        lines.append("")
        lines.append(
            f"Decompose these sentences into at most {self.max_claims} atomic "
            "claims, keeping each claim attached to its sentence's citations."
        )
        return "\n".join(lines)

    def _parse_decomposition(
        self, response: Any, sentence_map: dict, valid_sids: set
    ) -> list[dict]:
        """Validate the decomposer payload defensively.

        Entries with a non-int sentence_index, a non-str claim, non-list
        citations, an unknown sentence index or zero valid citations are
        dropped; citations not in the frozen pool are dropped; the total is
        capped at max_claims. Returns [] when nothing survives.
        """
        if not isinstance(response, dict):
            return []
        entries = response.get("claims")
        if not isinstance(entries, list):
            return []
        out: list[dict] = []
        counters: dict[int, int] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            idx = entry.get("sentence_index")
            if isinstance(idx, bool) or not isinstance(idx, int):
                continue
            sentence = sentence_map.get(idx)
            if sentence is None:
                continue  # unknown sentence index
            text = entry.get("claim")
            if not isinstance(text, str) or not text.strip():
                text = sentence["text"]  # fallback = the sentence text
            raw_cits = entry.get("citations")
            if not isinstance(raw_cits, list):
                continue
            sids: list[str] = []
            for cit in raw_cits:
                if isinstance(cit, str) and cit in valid_sids and cit not in sids:
                    sids.append(cit)
            if not sids:
                continue  # a claim with no pool-backed citation cannot be checked
            counters[idx] = counters.get(idx, 0) + 1
            out.append(
                {
                    "claim_id": f"s{idx}-c{counters[idx]}",
                    "text": str(text).strip(),
                    "sids": sids,
                }
            )
            if len(out) >= self.max_claims:
                break
        return out

    def _whole_sentence_claims(
        self, sentences: list[dict], valid_sids: set
    ) -> list[dict]:
        """Fallback: one claim per sentence, wording copied verbatim."""
        out: list[dict] = []
        for sentence in sentences:
            sids = [c for c in sentence["citations"] if c in valid_sids]
            if not sids:
                continue
            out.append(
                {
                    "claim_id": f"s{sentence['index']}-c1",
                    "text": sentence["text"],
                    "sids": sids,
                }
            )
            if len(out) >= self.max_claims:
                break
        return out

    # ------------------------------------------------------------------
    # Stage B -- existence (registry-backed, fail-open)
    # ------------------------------------------------------------------

    def _existence_statuses(self, rows: list[dict], ev_by_sid: dict) -> dict:
        """Registry status per sid, running each registry check ONCE per
        unique cited record. Returns {sid: "exists"|"missing"|"skipped"}."""
        cited: list[str] = []
        for row in rows:
            for sid in row["sids"]:
                if sid not in cited:
                    cited.append(sid)

        status: dict[str, str] = {}
        pmid_sids: list[tuple[str, str]] = []
        nct_sids: list[tuple[str, str]] = []
        doi_sids: list[tuple[str, str]] = []
        for sid in cited:
            rec = ev_by_sid.get(sid)
            if rec is None:
                status[sid] = _MISSING  # not in the frozen pool
                continue
            key = str(rec.get("citation_key") or "")
            native = str(rec.get("native_id") or "")
            if not native and "/" in key:
                native = key.split("/", 1)[1]
            if key.startswith("MED/") and native.isdigit():
                pmid_sids.append((sid, native))
            elif key.startswith("EPMC/") and native.isdigit():
                pmid_sids.append((sid, native))
            elif key.startswith("NCT/"):
                nct_sids.append((sid, native))
            elif key.startswith("EPMC/") and rec.get("doi"):
                doi_sids.append((sid, str(rec["doi"])))
            else:
                # EPMC preprint id without a DOI (or an unknown prefix):
                # cannot verify -- fail open.
                status[sid] = _SKIPPED

        if pmid_sids:
            batch = self._check_pubmed_exists([pmid for _, pmid in pmid_sids])
            for sid, pmid in pmid_sids:
                status[sid] = batch.get(pmid, _SKIPPED)
        for sid, nct_id in nct_sids:
            status[sid] = self._check_nct_exists(nct_id)
        for sid, doi in doi_sids:
            status[sid] = self._check_doi_exists(doi)
        return status

    def _check_pubmed_exists(self, pmids: list[str]) -> dict:
        """Batch esummary check: {pmid: "exists"|"missing"|"skipped"}.

        One call for the whole batch. HTTP/network/JSON errors mark every id
        in the batch "skipped" (fail-open: never delete on a network
        failure).
        """
        ids = list(dict.fromkeys(pmids))
        if not ids:
            return {}
        try:
            resp = self.session.get(
                _ESUMMARY_URL,
                params={"db": "pubmed", "id": ",".join(ids), "retmode": "json"},
                timeout=self.registry_timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError(
                    f"unexpected esummary payload type {type(data).__name__}"
                )
            result_obj = data.get("result")
            uids = result_obj.get("uids") if isinstance(result_obj, dict) else None
            if not isinstance(uids, list):
                raise ValueError("esummary response missing result.uids")
            uid_set = {str(u) for u in uids}
            out: dict[str, str] = {}
            for pmid in ids:
                if str(pmid) not in uid_set:
                    out[pmid] = _MISSING
                else:
                    entry = result_obj.get(str(pmid))
                    if isinstance(entry, dict) and entry.get("error"):
                        out[pmid] = _MISSING
                    else:
                        out[pmid] = _EXISTS
            return out
        except Exception as exc:
            print(
                f"[verifier] esummary failed ({exc}); existence check skipped "
                f"for {len(ids)} records"
            )
            return {pmid: _SKIPPED for pmid in ids}

    def _check_nct_exists(self, nct_id: str) -> str:
        """ClinicalTrials.gov v2 study lookup -> exists/missing/skipped."""
        if not nct_id:
            return _SKIPPED
        try:
            resp = self.session.get(
                _CTGOV_STUDY_URL.format(nct_id=nct_id),
                params={"format": "json"},
                timeout=self.registry_timeout,
            )
            if resp.status_code == 404:
                return _MISSING
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "protocolSection" in data:
                return _EXISTS
            return _MISSING
        except Exception as exc:
            print(
                f"[verifier] clinicaltrials.gov check failed for {nct_id} "
                f"({exc}); existence check skipped"
            )
            return _SKIPPED

    def _check_doi_exists(self, doi: str) -> str:
        """doi.org content-negotiation lookup -> exists/missing/skipped."""
        if not doi:
            return _SKIPPED
        try:
            resp = self.session.get(
                _DOI_RESOLVE_URL.format(doi=doi),
                headers={"Accept": "application/vnd.citationstyles.csl+json"},
                allow_redirects=True,
                timeout=self.registry_timeout,
            )
            if resp.status_code == 404:
                return _MISSING
            resp.raise_for_status()
            return _EXISTS
        except Exception as exc:
            print(
                f"[verifier] doi.org check failed for {doi} ({exc}); "
                "existence check skipped"
            )
            return _SKIPPED

    # ------------------------------------------------------------------
    # Stage C -- entailment (SciFact-style NLI judge, fails closed)
    # ------------------------------------------------------------------

    def _judge_entailment(
        self, claim_id: str, claim_text: str, blocks: list[tuple[str, str, str, str]]
    ) -> EntailmentVerdict:
        """Judge one claim against its cited evidence at temperature 0.0.

        One retry on LLMError or an unparseable payload (a failover client
        may recover); after that the judge FAILS CLOSED to
        NOT_ENOUGH_INFO 0.0.
        """
        if not blocks:
            return EntailmentVerdict(
                "NOT_ENOUGH_INFO", 0.0, "", "no evidence text available; failing closed"
            )
        prompt = _judge_prompt(claim_text, blocks)
        last_exc: Optional[Exception] = None
        for _attempt in range(2):
            try:
                response = self.llm.complete_json(
                    prompt, system=_JUDGE_SYSTEM, temperature=0.0
                )
            except Exception as exc:  # LLMError per contract; stay defensive
                last_exc = exc
                continue
            parsed = _parse_verdict(response)
            if parsed is not None:
                return parsed
        if last_exc is not None:
            print(
                f"[verifier] entailment judge failed for {claim_id}; "
                f"failing closed ({last_exc})"
            )
        else:
            print(
                f"[verifier] entailment judge output unparseable for "
                f"{claim_id}; failing closed"
            )
        return EntailmentVerdict(
            "NOT_ENOUGH_INFO", 0.0, "", "judge output unparseable; failing closed"
        )

    # ------------------------------------------------------------------
    # Stage D -- supersession heuristic (fail-open)
    # ------------------------------------------------------------------

    def _run_supersession(
        self, rows: list[dict], question: str, queries: list[str], evidence: list[dict]
    ) -> None:
        """Delete kept claims that a newer review/meta-analysis refutes.

        One PubMed query per verify() call (first strategist query, or the
        question itself), up to 2 fresh reviews, one judge call per
        (claim, review). Any failure skips the whole stage without
        deletions -- it is only a heuristic.
        """
        reviews = self._fetch_supersession_reviews(question, queries, evidence)
        if not reviews:
            return
        for row in rows:
            if row["status"] == "deleted":
                continue  # only surviving (kept/flagged) claims are checked
            for pmid, title, abstract in reviews:
                blocks = [(f"MED/{pmid}", f"MED/{pmid}", title, abstract)]
                verdict = self._judge_entailment(row["claim_id"], row["text"], blocks)
                if (
                    verdict.verdict == "REFUTES"
                    and verdict.confidence >= _SUPERSESSION_REFUTE_CONFIDENCE
                ):
                    row["standing"] = "fail"
                    self._delete(
                        row, f"superseded by newer evidence (MED/{pmid})"
                    )
                    break

    def _fetch_supersession_reviews(
        self, question: str, queries: list[str], evidence: list[dict]
    ) -> list[tuple[str, str, str]]:
        """One PubMed query for newer syntheses; returns (pmid, title,
        abstract) tuples for up to 2 reviews not already in the pool."""
        try:
            query_list = [
                q for q in (queries or []) if isinstance(q, str) and q.strip()
            ]
            base = query_list[0] if query_list else (question or "")
            year = date.today().year
            term = (
                f'({base}) AND (systematic[sb] OR meta-analysis[pt]) '
                f'AND ("{year - 5}":"{year}"[dp])'
            )
            pmids = self.pubmed.esearch(term, retmax=5) or []
            pool_ids = {
                str(item.get("native_id") or "")
                for item in (evidence or [])
                if isinstance(item, dict)
            }
            fresh = [str(p) for p in pmids if str(p) not in pool_ids][:2]
            if not fresh:
                return []
            records = self.pubmed.efetch(fresh) or []
            reviews = []
            for pmid, record in zip(fresh, records):
                title = str(getattr(record, "title", "") or "")
                abstract = str(getattr(record, "abstract", "") or "")
                reviews.append((pmid, title, abstract))
            return reviews
        except Exception as exc:
            print(f"[verifier] supersession check skipped ({exc})")
            return []

    # ------------------------------------------------------------------
    # Stage E -- assembly
    # ------------------------------------------------------------------

    @staticmethod
    def _delete(row: dict, reason: str) -> None:
        """Mark a row deleted (exactly once; a later, higher-precedence rule
        may still overwrite the reason -- see the module docstring)."""
        if row["status"] != "deleted":
            row["status"] = "deleted"
            row["deletion_reason"] = reason
            print(f"[verifier] deleted {row['claim_id']}: {reason}")
        else:
            row["deletion_reason"] = reason
            print(f"[verifier] {row['claim_id']} already deleted; reason -> {reason}")

    def _assemble(self, rows: list[dict], ev_by_sid: dict) -> VerificationReport:
        claims: list[Claim] = []
        for row in rows:
            citations = []
            for sid in row["sids"]:
                rec = ev_by_sid.get(sid)
                if rec is not None:
                    citations.append(
                        {
                            "sid": sid,
                            "citation_key": rec.get("citation_key"),
                            "title": rec.get("title"),
                            "url": rec.get("url"),
                        }
                    )
            claims.append(
                Claim(
                    claim_id=row["claim_id"],
                    text=row["text"],
                    status=row["status"],
                    deletion_reason=row["deletion_reason"],
                    flags=list(row["flags"]),
                    checks=ClaimCheck(
                        existence=row["existence"],
                        entailment=row["entailment"],
                        standing=row["standing"],
                    ),
                    verdict=row["verdict"],
                    confidence=row["confidence"],
                    evidence_quote=row["quote"],
                    citations=citations,
                )
            )

        deleted = [c for c in claims if c.status == "deleted"]
        kept = [c for c in claims if c.status in ("kept", "flagged")]
        by_reason: dict[str, int] = {}
        for claim in deleted:
            if claim.deletion_reason:
                by_reason[claim.deletion_reason] = (
                    by_reason.get(claim.deletion_reason, 0) + 1
                )
        funnel = {
            "claims_generated": len(claims),
            "claims_deleted": len(deleted),
            "claims_kept": len(kept),
            "by_reason": by_reason,
        }

        reasons: list[str] = []
        if not kept:
            reasons.append(_ABSTAIN_ALL_DELETED)
        if claims and len(kept) / len(claims) < 0.5:
            reasons.append(
                f"post-verification collapse: only {len(kept)}/{len(claims)} "
                "claims survived"
            )
        abstained = bool(reasons)
        answer_text = "" if abstained else " ".join(c.text for c in kept)

        return VerificationReport(
            claims=claims,
            funnel=funnel,
            abstained=abstained,
            abstain_reasons=reasons,
            answer_text=answer_text,
        )
