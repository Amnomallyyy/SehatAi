"""Offline test for agents/synthesizer.py -- no network required.

Run from the project root (sehatEvidence/):
    python -m tests.test_synthesizer

Coverage inventory: t01 happy path (tags stripped, raw preserved,
temperature/system call shape), t02 uncited-sentence deletion + kept
renumbering, t03 unknown-tag deletion (single and mixed-tag), t04
multi-tag citation + duplicate-tag dedup, t05 abstention token (exact vs
mid-prose), t06 evidence ordering (all items shown by default, descending
blended score) + abstract truncation (MAX_ABSTRACT_CHARS) + missing
abstract, t06b topical_floor guarantees an on-topic/low-blended-score item
a place ahead of the count/budget cut, t07 whats_coming hint present /
absent, t08 empty-evidence abstention without an LLM call, t09
empty-question ValueError, t10 LLMError printed then re-raised, t11
trailing fragment without punctuation, t12 prompt/system contents.
"""

import contextlib
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.synthesizer import Synthesizer, Sentence, SynthesisResult, ParseDeletion
from core.llm import LLMError

Q = "Is semaglutide effective for type 2 diabetes?"

_ABSTRACT = (
    "We randomly assigned 500 patients with type 2 diabetes to once-weekly "
    "semaglutide or placebo for 52 weeks; the primary endpoint was the "
    "change in glycated hemoglobin from baseline."
)

assert len(_ABSTRACT) > 50, "helper abstract must be realistic (>50 chars)"


# --- helpers ----------------------------------------------------------------


def make_evidence(sid, **overrides):
    """Build a valid evidence dict with realistic defaults (contract shape
    built by the pipeline: RCT, 2024, PubMed, abstract, relevance 80)."""
    item = {
        "sid": sid,
        "citation_key": "MED/12345678",
        "source": "pubmed",
        "native_id": "12345678",
        "doi": "10.1/x",
        "url": "https://pubmed.ncbi.nlm.nih.gov/12345678/",
        "title": "Semaglutide versus placebo for type 2 diabetes: a randomized trial",
        "journal": "JAMA",
        "publication_date": "2024-03-01",
        "study_design": "rct",
        "trial_status": None,
        "is_preprint": False,
        "is_retracted": False,
        "retraction_source": None,
        "relevance_score": 80,
        "topical_relevance": None,
        "rationale": "direct PICO match",
        "abstract": _ABSTRACT,
    }
    item.update(overrides)
    return item


class FakeLLM:
    """Scripted raw-text responses; records every call for inspection.

    Entries may be str (returned verbatim) or Exception instances (raised),
    so a dead endpoint is one scripted entry away. The last entry repeats.
    """

    def __init__(self, responses):
        if not responses:
            raise ValueError("FakeLLM needs at least one scripted response")
        self.responses = list(responses)
        self.calls = []

    @property
    def call_count(self):
        return len(self.calls)

    def complete(self, prompt, system=None, temperature=0.2):
        self.calls.append(
            {"prompt": prompt, "system": system, "temperature": temperature}
        )
        idx = min(len(self.calls), len(self.responses)) - 1
        response = self.responses[idx]
        if isinstance(response, Exception):
            raise response
        return response


# --- test cases ---------------------------------------------------------------


def t01_happy_path():
    raw = "Semaglutide reduced HbA1c [S1]. Adverse effects were mostly GI [S2]."
    llm = FakeLLM([raw])
    result = Synthesizer(llm=llm).synthesize(Q, [make_evidence("S1"), make_evidence("S2")])

    assert isinstance(result, SynthesisResult)
    assert result.abstained is False
    assert result.parse_deletions == []
    assert result.raw_text == raw, "raw_text must be exactly what the LLM returned"
    assert [s.text for s in result.sentences] == [
        "Semaglutide reduced HbA1c",
        "Adverse effects were mostly GI",
    ], [s.text for s in result.sentences]
    assert [s.citations for s in result.sentences] == [["S1"], ["S2"]]
    assert [s.index for s in result.sentences] == [0, 1]
    assert all(isinstance(s, Sentence) for s in result.sentences)

    # call shape: raw-text complete() at temperature 0.2, question in prompt
    assert llm.call_count == 1
    call = llm.calls[0]
    assert call["temperature"] == 0.2
    assert isinstance(call["system"], str) and call["system"].strip()
    assert Q in call["prompt"]
    print("PASS 01: happy path -- two cited sentences kept, tags stripped, raw preserved")


def t02_uncited_deleted():
    llm = FakeLLM(["Drug X works [S1]. It is very safe. Side effects are rare [S2]."])
    result = Synthesizer(llm=llm).synthesize(
        Q, [make_evidence("S1"), make_evidence("S2")]
    )

    assert result.abstained is False
    assert len(result.parse_deletions) == 1, result.parse_deletions
    deletion = result.parse_deletions[0]
    assert isinstance(deletion, ParseDeletion)
    assert deletion.reason == "uncited claim"
    assert "very safe" in deletion.text, deletion.text
    # kept sentences are renumbered 0,1 with no gap
    assert [s.index for s in result.sentences] == [0, 1]
    assert [s.citations for s in result.sentences] == [["S1"], ["S2"]]
    print("PASS 02: uncited middle sentence deleted; kept sentences renumbered 0,1")


def t03_unknown_tag_deleted():
    llm = FakeLLM(["Effect A is real [S9]."])
    result = Synthesizer(llm=llm).synthesize(
        Q, [make_evidence("S1"), make_evidence("S2")]
    )
    assert result.sentences == [], result.sentences
    assert result.abstained is False
    assert len(result.parse_deletions) == 1
    assert result.parse_deletions[0].reason == "citation to unknown evidence"
    assert "[S9]" in result.parse_deletions[0].text, (
        "deletion text must include the tags so the UI can show them"
    )

    # ANY unknown tag in a multi-tag sentence kills the whole sentence
    llm2 = FakeLLM(["Mixed tags fail wholesale [S1][S9]."])
    result2 = Synthesizer(llm=llm2).synthesize(
        Q, [make_evidence("S1"), make_evidence("S2")]
    )
    assert result2.sentences == []
    assert len(result2.parse_deletions) == 1
    assert result2.parse_deletions[0].reason == "citation to unknown evidence"
    print("PASS 03: unknown [S9] citation deleted (single-tag and mixed-tag cases)")


def t04_multi_tag_and_dedup():
    llm = FakeLLM(["Combined outcomes improved [S1][S2]."])
    result = Synthesizer(llm=llm).synthesize(
        Q, [make_evidence("S1"), make_evidence("S2")]
    )
    assert len(result.sentences) == 1
    sentence = result.sentences[0]
    assert sentence.citations == ["S1", "S2"], sentence.citations
    assert sentence.text == "Combined outcomes improved"
    assert sentence.index == 0

    llm2 = FakeLLM(["Effect A is real [S1] and effect B too [S1]."])
    result2 = Synthesizer(llm=llm2).synthesize(Q, [make_evidence("S1")])
    assert len(result2.sentences) == 1
    assert result2.sentences[0].citations == ["S1"], (
        "duplicate tags must dedupe to a single citation"
    )
    print("PASS 04: multi-tag sentence keeps both citations; duplicate tags dedupe")


def t04b_comma_grouped_tags():
    # Regression test: confirmed live 2026-08-30 -- the model wrote every
    # multi-citation as ONE bracket with a comma-separated list instead of
    # separate [S1][S2] brackets, and the old regex (which only matched a
    # bracket containing exactly one S-id) treated every sentence in an
    # otherwise correct, comprehensive 8-sentence answer as uncited,
    # deleting the entire answer down to an abstention.
    llm = FakeLLM([
        "Compared with warfarin, DOACs were associated with a reduction in "
        "major bleeding risk [S1, S2, S4, S6, S12, S28, S49]. "
        "Apixaban had the most favorable safety profile [S1, S12]."
    ])
    result = Synthesizer(llm=llm).synthesize(
        Q, [make_evidence(f"S{i}") for i in (1, 2, 4, 6, 12, 28, 49)]
    )
    assert result.parse_deletions == [], result.parse_deletions
    assert len(result.sentences) == 2
    assert result.sentences[0].citations == ["S1", "S2", "S4", "S6", "S12", "S28", "S49"]
    assert result.sentences[0].text == "Compared with warfarin, DOACs were associated with a reduction in major bleeding risk"
    assert result.sentences[1].citations == ["S1", "S12"]

    # Mixed styles in one sentence: a grouped bracket plus a separate one.
    llm2 = FakeLLM(["Both endpoints improved [S1, S3][S7]."])
    result2 = Synthesizer(llm=llm2).synthesize(
        Q, [make_evidence("S1"), make_evidence("S3"), make_evidence("S7")]
    )
    assert result2.sentences[0].citations == ["S1", "S3", "S7"]
    print("PASS 04b: comma-grouped citation brackets [S1, S2, S4] parse identically to [S1][S2][S4]")


def t05_abstention_token():
    llm = FakeLLM(["INSUFFICIENT_EVIDENCE"])
    result = Synthesizer(llm=llm).synthesize(Q, [make_evidence("S1")])
    assert result.abstained is True
    assert result.sentences == [] and result.parse_deletions == []
    assert result.raw_text == "INSUFFICIENT_EVIDENCE"

    # mid-prose token inside a TAGGED sentence: kept as ordinary text
    llm2 = FakeLLM(
        ["The authors conclude evidence is INSUFFICIENT_EVIDENCE for rare outcomes [S1]."]
    )
    result2 = Synthesizer(llm=llm2).synthesize(Q, [make_evidence("S1")])
    assert result2.abstained is False
    assert len(result2.sentences) == 1
    assert "INSUFFICIENT_EVIDENCE" in result2.sentences[0].text
    assert result2.sentences[0].citations == ["S1"]
    assert result2.parse_deletions == []

    # mid-prose token in an UNTAGGED sentence: ordinary uncited-claim deletion
    llm3 = FakeLLM(["Evidence is INSUFFICIENT_EVIDENCE here. But mortality fell [S1]."])
    result3 = Synthesizer(llm=llm3).synthesize(Q, [make_evidence("S1")])
    assert result3.abstained is False
    assert len(result3.sentences) == 1
    assert len(result3.parse_deletions) == 1
    assert result3.parse_deletions[0].reason == "uncited claim"
    print("PASS 05: exact token abstains; mid-prose token is plain text")


def t06_ordering_and_truncation():
    # 12 items, distinct relevance scores, input order deliberately shuffled
    # so the descending-relevance sort is actually exercised. With no
    # max_evidence cap (the new default) and a generous char budget, ALL 12
    # must reach the prompt now -- the old "top 10 only" cutoff is gone.
    scores = {f"S{i}": 101 - 5 * i for i in range(1, 13)}  # S1=96 ... S12=41
    shuffled = ["S3", "S1", "S12", "S7", "S2", "S9", "S5", "S11", "S4", "S8", "S6", "S10"]
    long_abstract = "Semaglutide suppresses appetite meaningfully. " * 200  # ~9400 chars
    assert len(long_abstract) > 6000

    evidence = []
    for sid in shuffled:
        overrides = {"relevance_score": scores[sid], "title": f"Study {sid}"}
        if sid == "S1":
            overrides["abstract"] = long_abstract
        if sid == "S2":
            overrides["abstract"] = "   "  # blank -> "No abstract available."
        evidence.append(make_evidence(sid, **overrides))

    llm = FakeLLM(["Answer sentence [S1]."])
    Synthesizer(llm=llm).synthesize(Q, evidence)
    prompt = llm.calls[0]["prompt"]

    # all 12, in descending relevance order -- no count cutoff by default
    headers = re.findall(r"\[(S\d+)\] \(", prompt)
    assert headers == [f"S{i}" for i in range(1, 13)], headers

    # header format: [S1] (relevance 96, rct, 2024, JAMA) Title...
    lines = prompt.splitlines()
    i1 = next(i for i, ln in enumerate(lines) if ln.startswith("[S1] ("))
    assert lines[i1].startswith("[S1] (relevance 96, rct, 2024, JAMA) Study S1"), lines[i1]

    # long abstract truncated with an ellipsis at MAX_ABSTRACT_CHARS (6000),
    # head preserved -- not the old 1500-char cap.
    assert lines[i1 + 1].startswith("Abstract: ")
    shown = lines[i1 + 1][len("Abstract: "):]
    assert shown.endswith("…"), "truncated abstract must end with an ellipsis"
    assert len(shown) <= 6001, len(shown)  # 6000 chars + ellipsis
    assert long_abstract[:80] in shown, "truncation must keep the head of the abstract"

    # blank abstract -> placeholder
    assert "No abstract available." in prompt
    print("PASS 06: all 12 items shown (no default count cap), descending blended order, 6000-char abstract cap")


def t06b_topical_floor_guarantees_admission():
    # Three off-topic-adjacent items with a HIGH blended score (a plain
    # top-N-by-blended-score cut would keep these), plus one genuinely
    # on-topic item with a LOW blended score but topical_relevance >= the
    # floor (a case-report-style item a bounded blend structurally caps
    # low -- see agents/appraiser.py). With max_evidence=3, a naive cut
    # would exclude S4 entirely; pass 1 must guarantee it a place instead.
    evidence = [
        make_evidence("S1", relevance_score=90, topical_relevance=40, title="Off-topic review A"),
        make_evidence("S2", relevance_score=85, topical_relevance=30, title="Off-topic review B"),
        make_evidence("S3", relevance_score=80, topical_relevance=None, title="Off-topic review C"),
        make_evidence("S4", relevance_score=45, topical_relevance=95, title="On-topic case report"),
    ]
    llm = FakeLLM(["Answer sentence [S1]."])
    Synthesizer(llm=llm, max_evidence=3).synthesize(Q, evidence)
    prompt = llm.calls[0]["prompt"]
    headers = re.findall(r"\[(S\d+)\] \(", prompt)

    assert "S4" in headers, f"the on-topic low-blended item must be guaranteed a place: {headers}"
    assert "S3" not in headers, f"the lowest-priority off-topic item must be cut instead: {headers}"
    assert len(headers) == 3, headers
    # Output order is still blended-score descending -- the guarantee wins
    # entry, not ranking.
    assert headers[-1] == "S4", f"S4 has the lowest blended score, must still sort last: {headers}"
    print("PASS 06b: topical_floor guarantees an on-topic, low-blended item a place ahead of the count/budget cut")


def t07_whats_coming_hint():
    llm = FakeLLM(["Answer sentence [S1]."])
    evidence = [
        make_evidence("S1"),
        make_evidence(
            "S2",
            source="clinical_trials",
            citation_key="NCT/01234567",
            native_id="NCT01234567",
            url="https://clinicaltrials.gov/study/NCT01234567",
            study_design="clinical_trial_record",
            trial_status="RECRUITING",
            journal=None,
            title="Semaglutide cardiovascular outcomes trial",
        ),
    ]
    Synthesizer(llm=llm).synthesize(Q, evidence)
    prompt = llm.calls[0]["prompt"]
    assert "Ongoing/unreported trials" in prompt
    assert "[S2] (NCT/01234567, status RECRUITING)" in prompt
    assert prompt.count("Ongoing/unreported trials") == 1, "hint must be ONE line"

    # no ongoing trials -> no hint line at all
    llm2 = FakeLLM(["Answer sentence [S1]."])
    Synthesizer(llm=llm2).synthesize(Q, [make_evidence("S1"), make_evidence("S2")])
    assert "Ongoing/unreported" not in llm2.calls[0]["prompt"]
    print("PASS 07: whats_coming hint appears for RECRUITING, absent otherwise")


def t08_empty_evidence_abstains_without_llm():
    llm = FakeLLM(["This must never be returned [S1]."])
    result = Synthesizer(llm=llm).synthesize(Q, [])
    assert result.abstained is True
    assert result.sentences == [] and result.parse_deletions == []
    assert result.raw_text == ""
    assert llm.call_count == 0, "empty evidence must abstain WITHOUT calling the LLM"
    print("PASS 08: empty evidence pool abstains without calling the LLM")


def t09_empty_question_raises():
    synth = Synthesizer(llm=FakeLLM(["placeholder [S1]"]))
    for bad in ("", "   ", "\n\t "):
        try:
            synth.synthesize(bad, [make_evidence("S1")])
        except ValueError as exc:
            assert "question must be non-empty" in str(exc), str(exc)
        else:
            raise AssertionError(f"empty question {bad!r} must raise ValueError")
    print("PASS 09: empty/whitespace question raises ValueError")


def t10_llm_failure_reraises():
    llm = FakeLLM([LLMError("simulated endpoint failure")])
    synth = Synthesizer(llm=llm)
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            synth.synthesize(Q, [make_evidence("S1")])
    except LLMError as exc:
        assert "simulated endpoint failure" in str(exc), str(exc)
    else:
        raise AssertionError("LLMError from complete() must propagate, not be swallowed")
    assert llm.call_count == 1
    out = buffer.getvalue()
    assert "[synthesizer] LLM synthesis failed" in out, out
    print("PASS 10: LLMError printed first, then re-raised for the pipeline")


def t11_trailing_fragment():
    llm = FakeLLM(["Effect shown [S1]. Then a final note [S2]"])
    result = Synthesizer(llm=llm).synthesize(
        Q, [make_evidence("S1"), make_evidence("S2")]
    )
    assert result.abstained is False
    assert result.parse_deletions == []
    assert [s.text for s in result.sentences] == ["Effect shown", "Then a final note"]
    assert [s.citations for s in result.sentences] == [["S1"], ["S2"]]
    assert [s.index for s in result.sentences] == [0, 1]
    print("PASS 11: trailing fragment without punctuation becomes the final sentence")


def t12_prompt_contents():
    llm = FakeLLM(["Answer sentence [S1]."])
    Synthesizer(llm=llm).synthesize(Q, [make_evidence("S1")])
    call = llm.calls[0]
    prompt, system = call["prompt"], call["system"]

    assert f'Clinical question: "{Q}"' in prompt
    assert "Evidence set:" in prompt
    assert "Answer (every sentence cited, or INSUFFICIENT_EVIDENCE):" in prompt
    assert "6-14 sentences" in system
    assert "EVERY sentence" in system
    assert "INSUFFICIENT_EVIDENCE" in system
    assert "hedged" in system
    print("PASS 12: prompt carries the question; system demands 6-14 fully-cited sentences")


def t13_gap_only_sentence():
    llm = FakeLLM([
        "Drug X reduced mortality [S1]. "
        "The exact weight-based dosing regimen is not stated in the evidence set [GAP]."
    ])
    result = Synthesizer(llm=llm).synthesize(Q, [make_evidence("S1")])

    assert result.abstained is False
    assert result.parse_deletions == [], "a [GAP] sentence must never be treated as a deletion"
    assert [s.text for s in result.sentences] == ["Drug X reduced mortality"]
    assert result.unanswered_aspects == [
        "The exact weight-based dosing regimen is not stated in the evidence set"
    ], result.unanswered_aspects
    print("PASS 13: [GAP] sentence extracted to unanswered_aspects, not deleted, no citation required")


def t14_mixed_cited_and_gap_response():
    llm = FakeLLM([
        "Semaglutide reduced HbA1c [S1]. "
        "GI adverse effects were common [S2]. "
        "Long-term cardiovascular outcomes beyond 5 years are not reported in this evidence set [GAP]."
    ])
    result = Synthesizer(llm=llm).synthesize(Q, [make_evidence("S1"), make_evidence("S2")])

    assert len(result.sentences) == 2
    assert [s.citations for s in result.sentences] == [["S1"], ["S2"]]
    assert len(result.unanswered_aspects) == 1
    assert "cardiovascular outcomes" in result.unanswered_aspects[0]
    print("PASS 14: a response can mix cited answer sentences with a separate [GAP] statement")


def t15_mixed_gap_and_citation_tag_falls_back_to_gap():
    # Malformed/mixed output (prompt tells the model never to do this) --
    # must be treated as a gap statement, not silently accepted as a cited
    # claim with an untrusted extra tag riding along.
    llm = FakeLLM(["Drug X is definitely superior [S1] [GAP]."])
    result = Synthesizer(llm=llm).synthesize(Q, [make_evidence("S1")])

    assert result.sentences == [], "a mixed [S#]+[GAP] sentence must not become a cited claim"
    assert result.parse_deletions == []
    assert result.unanswered_aspects == ["Drug X is definitely superior"], result.unanswered_aspects
    print("PASS 15: a sentence mixing [GAP] and [S#] falls back to a gap statement, never a citable claim")


def t16_abstention_and_empty_pool_never_produce_gaps():
    llm = FakeLLM(["INSUFFICIENT_EVIDENCE"])
    result = Synthesizer(llm=llm).synthesize(Q, [make_evidence("S1")])
    assert result.abstained is True
    assert result.unanswered_aspects == []

    result2 = Synthesizer(llm=FakeLLM(["should never be called"])).synthesize(Q, [])
    assert result2.abstained is True
    assert result2.unanswered_aspects == []
    print("PASS 16: the exact-token and empty-pool abstention paths always report unanswered_aspects=[]")


def t17_gap_instruction_in_system_prompt():
    llm = FakeLLM(["Answer sentence [S1]."])
    Synthesizer(llm=llm).synthesize(Q, [make_evidence("S1")])
    system = llm.calls[0]["system"]
    assert "[GAP]" in system
    assert "sparingly" in system
    print("PASS 17: system prompt teaches the [GAP] convention and asks for restraint")


def run():
    t01_happy_path()
    t02_uncited_deleted()
    t03_unknown_tag_deleted()
    t04_multi_tag_and_dedup()
    t04b_comma_grouped_tags()
    t05_abstention_token()
    t06_ordering_and_truncation()
    t06b_topical_floor_guarantees_admission()
    t07_whats_coming_hint()
    t08_empty_evidence_abstains_without_llm()
    t09_empty_question_raises()
    t10_llm_failure_reraises()
    t11_trailing_fragment()
    t12_prompt_contents()
    t13_gap_only_sentence()
    t14_mixed_cited_and_gap_response()
    t15_mixed_gap_and_citation_tag_falls_back_to_gap()
    t16_abstention_and_empty_pool_never_produce_gaps()
    t17_gap_instruction_in_system_prompt()
    print("\nAll synthesizer tests passed.")


if __name__ == "__main__":
    run()
