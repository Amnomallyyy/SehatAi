"""
agents/red_team.py -- Red Team auditor.

Red Team auditor — one adversarial LLM pass over the claims that SURVIVED
verification, flagging overstatement, absolute claims, missing caveats, and
population mismatches. Flag-only by design: deletion authority belongs
exclusively to the Verifier so the deletion funnel stays auditable.
Inspired by debate/critique patterns in LLM verification literature and
adversarial evaluation practice.

Separation of powers
--------------------
Pipeline position: Strategist -> retrieval -> Appraiser -> Synthesizer ->
Verifier (deletes unsupported claims) -> Red Team (this module) -> UI.
The Verifier owns every deletion decision; the Red Team only ANNOTATES the
survivors. Keeping the two powers separate means a claim can never vanish
from the funnel because an adversarial reviewer disliked its rhetoric —
every deletion stays traceable to a citation-verification failure the
Verifier logged, and every Red Team flag stays a visible, reversible
annotation the UI can render next to the claim.

Failure posture
---------------
Fail-open, like every optional LLM stage in this pipeline: transport
errors, malformed JSON and unexpected response shapes all yield zero
flags. An auditor that cannot run must never block the answer — and must
certainly never quietly widen its own mandate into deletion.

Claims are duck-typed: only ``.claim_id`` and ``.text`` are accessed, so
this module works with whatever claim objects the Verifier passes through
(agents.verifier is intentionally NOT imported — it is built in parallel).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from core.llm import LLMClient

# The only flag values the UI knows how to render; anything else the LLM
# invents is dropped during validation.
_ALLOWED_FLAGS = frozenset(
    {"overstatement", "absolute_claim", "missing_caveat", "population_mismatch"}
)

_SYSTEM_PROMPT = (
    "You are an adversarial reviewer auditing clinical evidence claims that "
    "have already passed citation verification. You do NOT judge whether "
    "citations exist or support the claims — that is done. You flag "
    "RHETORICAL and robustness problems only: overstatement (effect "
    "larger/stronger than the evidence supports), absolute_claim (unguarded "
    "absolutes like 'always', 'never', 'eliminates', 'proves'), "
    "missing_caveat (important limitation left unstated: sample size, "
    "population, duration, surrogate outcomes), population_mismatch (claim "
    "generalizes to a population the cited evidence did not study). Be "
    "selective: flag only genuine problems, not style nitpicks. Respond "
    "ONLY with JSON."
)


@dataclass
class RedTeamFlag:
    """One rhetorical/robustness problem found on a surviving claim.

    ``note`` is a one-sentence explanation shown in the UI next to the
    claim, so it is LLM-authored UNTRUSTED text: renderers must treat it as
    plain text only (same policy as AppraisedRecord.rationale in
    agents/appraiser.py).
    """

    claim_id: str
    flag: str  # "overstatement" | "absolute_claim" | "missing_caveat" | "population_mismatch"
    note: str  # one-sentence explanation, shown in the UI


class RedTeam:
    """Adversarial auditor over the claims the Verifier kept.

    Runs exactly ONE LLM pass and returns flags; it never deletes, rewrites
    or re-orders claims. audit() is fail-open and never raises.
    """

    def __init__(self, llm: Optional[LLMClient] = None):
        """llm: injectable LLMClient (or anything with a compatible
        complete_json); defaults to a real LLMClient. Constructing the
        client performs no network I/O."""
        self.llm = llm or LLMClient()

    def audit(self, question: str, kept_claims: list) -> list[RedTeamFlag]:
        """Flag rhetorical/robustness problems on the surviving claims.

        question: the clinical question the claims answer.
        kept_claims: duck-typed claim objects; only ``.claim_id`` and
        ``.text`` are accessed.

        Returns RedTeamFlags in the order the LLM returned them, after
        defensive filtering (unknown claim_ids, invalid flag values and
        duplicates are dropped). An empty question or no kept_claims
        returns [] without calling the LLM. Any failure returns []
        (fail-open) — audit() never raises.
        """
        if not question or not kept_claims:
            return []

        try:
            prompt = self._build_prompt(question, kept_claims)
            response = self.llm.complete_json(
                prompt, system=_SYSTEM_PROMPT, temperature=0.1
            )
            flags = self._validate(response, kept_claims)
        except Exception as exc:  # fail-open: the auditor never blocks the pipeline
            print(f"[red_team] audit failed ({exc}); no flags applied")
            return []

        print(
            f"[red_team] audit complete: {len(flags)} flag(s) across "
            f"{len(kept_claims)} kept claim(s)"
        )
        return flags

    @staticmethod
    def _build_prompt(question: str, kept_claims: list) -> str:
        """Build the user prompt: the clinical question, the numbered
        surviving claims (id in brackets so the LLM can echo it back
        exactly), and the JSON response contract."""
        claim_lines = "\n".join(
            f"{i}. [{claim.claim_id}] {claim.text}"
            for i, claim in enumerate(kept_claims, 1)
        )
        return (
            f'Clinical question: "{question}"\n\n'
            f"Claims that passed verification:\n"
            f"{claim_lines}\n\n"
            'Return a JSON object: {"flags": [{"claim_id": "...", '
            '"flag": "overstatement"|"absolute_claim"|"missing_caveat"|'
            '"population_mismatch", "note": "<one sentence>"}]}. '
            'Return {"flags": []} if no problems.'
        )

    @staticmethod
    def _validate(response: Any, kept_claims: list) -> list[RedTeamFlag]:
        """Defensively filter the LLM response into RedTeamFlags.

        complete_json may return ANY JSON value, so nothing is trusted: the
        envelope must be a dict with a "flags" list; each entry must
        reference a claim_id that exists in the input (hallucination guard)
        and carry an allowed flag value; a missing or non-string note
        becomes ""; identical (claim_id, flag) pairs deduplicate with the
        first occurrence winning. A bad envelope shape yields [] instead of
        raising — the caller's except clause stays a last-resort net.
        """
        if not isinstance(response, dict):
            print(
                "[red_team] unexpected LLM response shape "
                f"({type(response).__name__}); no flags applied"
            )
            return []
        raw_flags = response.get("flags")
        if not isinstance(raw_flags, list):
            print(
                "[red_team] unexpected LLM response shape "
                f"(flags is {type(raw_flags).__name__}); no flags applied"
            )
            return []

        valid_ids = {claim.claim_id for claim in kept_claims}
        flags: list[RedTeamFlag] = []
        seen: set[tuple[str, str]] = set()
        for entry in raw_flags:
            if not isinstance(entry, dict):
                print(f"[red_team] skipping non-object flag entry: {entry!r}")
                continue
            claim_id = entry.get("claim_id")
            if not isinstance(claim_id, str) or claim_id not in valid_ids:
                print(
                    f"[red_team] dropping flag for unknown claim_id "
                    f"{claim_id!r} (not in input)"
                )
                continue
            flag = entry.get("flag")
            if not isinstance(flag, str) or flag not in _ALLOWED_FLAGS:
                print(
                    f"[red_team] dropping invalid flag value {flag!r} "
                    f"for claim {claim_id!r}"
                )
                continue
            if (claim_id, flag) in seen:
                print(f"[red_team] dropping duplicate flag ({claim_id}, {flag})")
                continue
            seen.add((claim_id, flag))
            note = entry.get("note")
            if not isinstance(note, str):
                note = ""  # missing or junk note -> empty string, not an error
            flags.append(RedTeamFlag(claim_id=claim_id, flag=flag, note=note))
        return flags
