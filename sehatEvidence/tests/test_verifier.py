"""
Offline test for agents/verifier.py -- no network required.

Run from the project root (sehatEvidence/):
    python -m tests.test_verifier

Plain-script convention (no pytest). The Synthesizer result, the LLM, the
HTTP session and the PubMed client are all faked locally; nothing here
touches the network and agents.synthesizer is NEVER imported (it is built
in parallel -- the synthesis result is duck-typed).

Coverage inventory: t01 happy path (+ prompts content, citations, esummary
params, no-pubmed supersession print); t02 decomposition split; t03 REFUTES
deletion; t04 NOT_ENOUGH_INFO deletion; t05 weak-support flag; t06 judge
failure -> fail closed; t07 existence missing (uids + error entry); t08
existence network fail-open; t09 NCT/doi.org existence paths; t10 retraction
deletion; t11 expression-of-concern flag; t12 all-deleted abstention; t13
collapse trigger; t14 supersession (refute / survive / no-reviews / search
failure); t15 decomposition fallback to whole-sentence claims; t16
synthesizer-abstained shortcut (zero LLM calls).
"""

import contextlib
import io
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests

from agents.verifier import Verifier, Claim, VerificationReport, EntailmentVerdict
from core.llm import LLMError

ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
CTGOV_URL = "https://clinicaltrials.gov/api/v2/studies/{nct}"
DOI_URL = "https://doi.org/{doi}"

QUESTION = "Is drug X effective for condition Y?"
QUERY = "drug X condition Y mortality"


# --- local duck-typed fakes (no agents.synthesizer import) ----------------------


class FakeSentence:
    def __init__(self, index, text, citations):
        self.index = index
        self.text = text
        self.citations = list(citations)


class FakeSynthesis:
    def __init__(self, sentences, abstained=False):
        self.sentences = list(sentences)
        self.abstained = abstained
        self.parse_deletions = []


class FakeLLM:
    """complete_json with a scripted response list consumed in order.

    Indices listed in fail_indices raise LLMError instead of returning the
    scripted value. When the script is exhausted the last entry repeats.
    An EMPTY script turns any call into an AssertionError so tests can
    prove the LLM was never contacted.
    """

    def __init__(self, script=(), fail_indices=()):
        self.script = list(script)
        self.fail_indices = set(fail_indices)
        self.calls = []

    def complete_json(self, prompt, system=None, temperature=0.1):
        self.calls.append(
            {"prompt": prompt, "system": system, "temperature": temperature}
        )
        idx = len(self.calls) - 1
        if not self.script:
            raise AssertionError("LLM was called but no responses were scripted")
        if idx in self.fail_indices:
            raise LLMError(f"simulated LLM failure (call {idx})")
        return self.script[idx] if idx < len(self.script) else self.script[-1]


class FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data

    def json(self):
        if self._json_data is None:
            raise ValueError("no JSON body scripted")
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} simulated error")


class FakeSession:
    """Scripted GET: routes map full URL -> FakeResponse (or Exception)."""

    def __init__(self, routes=None, raise_exc=None):
        self.routes = dict(routes or {})
        self.raise_exc = raise_exc
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None, allow_redirects=True):
        self.calls.append(
            {
                "url": url,
                "params": params,
                "headers": headers,
                "timeout": timeout,
                "allow_redirects": allow_redirects,
            }
        )
        if self.raise_exc is not None:
            raise self.raise_exc("simulated network failure")
        if url not in self.routes:
            raise AssertionError(f"unexpected registry URL: {url}")
        route = self.routes[url]
        if isinstance(route, Exception):
            raise route
        return route


class FakePubMed:
    def __init__(self, esearch_ids=None, efetch_records=None, esearch_exc=None):
        self.esearch_ids = list(esearch_ids or [])
        self.efetch_records = list(efetch_records or [])
        self.esearch_exc = esearch_exc
        self.esearch_calls = []
        self.efetch_calls = []

    def esearch(self, query, retmax=20):
        self.esearch_calls.append({"query": query, "retmax": retmax})
        if self.esearch_exc is not None:
            raise self.esearch_exc
        return list(self.esearch_ids)

    def esearch_relaxed(self, query, retmax=20, **kwargs):
        from retrieval.pubmed import RelaxationTrace

        ids = self.esearch(query, retmax=retmax)
        trace = RelaxationTrace(original_term=query, final_term=query, floor=retmax, cleared_floor=True)
        return ids, trace

    def efetch(self, pmids):
        self.efetch_calls.append(list(pmids))
        return list(self.efetch_records)


class FakeReviewRecord:
    def __init__(self, title, abstract):
        self.title = title
        self.abstract = abstract


# --- builders --------------------------------------------------------------------


def make_evidence(
    sid,
    citation_key,
    source="pubmed",
    native_id=None,
    doi=None,
    title="Study title",
    abstract="We enrolled 500 patients and followed them for two years.",
    is_preprint=False,
    is_retracted=False,
    retraction_source=None,
    url=None,
):
    return {
        "sid": sid,
        "citation_key": citation_key,
        "source": source,
        "native_id": native_id if native_id is not None else citation_key.split("/", 1)[-1],
        "doi": doi,
        "url": url,
        "title": title,
        "journal": "J Clin Evid",
        "publication_date": "2024-03-01",
        "study_design": "rct",
        "trial_status": None,
        "is_preprint": is_preprint,
        "is_retracted": is_retracted,
        "retraction_source": retraction_source,
        "relevance_score": 87,
        "rationale": "topically relevant",
        "abstract": abstract,
    }


def decomp(*claims):
    return {"claims": list(claims)}


def judge(verdict, confidence=0.9, quote="reduced mortality", reason="abstract states it"):
    return {
        "verdict": verdict,
        "confidence": confidence,
        "evidence_quote": quote,
        "reason": reason,
    }


def esummary_ok(*pmids):
    result = {"uids": list(pmids)}
    for pmid in pmids:
        result[pmid] = {"title": f"Record {pmid}"}
    return FakeResponse(200, {"result": result})


def capture(func, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = func(*args, **kwargs)
    return result, buf.getvalue()


def make_verifier(llm, session, **kwargs):
    kwargs.setdefault("enable_supersession", False)
    return Verifier(llm=llm, session=session, **kwargs)


_S1 = make_evidence(
    sid="S1",
    citation_key="MED/12345678",
    native_id="12345678",
    title="Randomized trial of drug X",
    abstract="In a randomized trial, drug X 200 mg reduced mortality at 30 days.",
    url="https://pubmed.ncbi.nlm.nih.gov/12345678/",
)
_SENT = FakeSentence(0, "Drug X 200 mg reduced mortality at 30 days.", ["S1"])
_CLAIM = {"sentence_index": 0, "claim": "Drug X 200 mg reduced mortality at 30 days.", "citations": ["S1"]}


# --- test cases --------------------------------------------------------------------


def t01_happy_path():
    llm = FakeLLM([decomp(_CLAIM), judge("SUPPORTS", 0.9)])
    session = FakeSession(routes={ESUMMARY_URL: esummary_ok("12345678")})
    verifier = make_verifier(llm, session)
    report = verifier.verify(QUESTION, FakeSynthesis([_SENT]), [_S1], [QUERY])

    assert isinstance(report, VerificationReport)
    assert len(report.claims) == 1
    claim = report.claims[0]
    assert isinstance(claim, Claim)
    assert claim.claim_id == "s0-c1"
    assert claim.status == "kept"
    assert claim.checks.existence == "pass"
    assert claim.checks.entailment == "supports"
    assert claim.checks.standing == "pass"
    assert claim.verdict == "SUPPORTS" and claim.confidence == 0.9
    assert claim.deletion_reason is None and claim.flags == []
    assert report.funnel == {
        "claims_generated": 1,
        "claims_deleted": 0,
        "claims_kept": 1,
        "claims_repaired": 0,
        "by_reason": {},
    }
    assert report.answer_text == claim.text
    assert not report.abstained and report.abstain_reasons == []
    assert claim.citations == [
        {
            "sid": "S1",
            "citation_key": "MED/12345678",
            "title": "Randomized trial of drug X",
            "url": "https://pubmed.ncbi.nlm.nih.gov/12345678/",
        }
    ]

    # decomposition prompt contains the sentence; entailment prompt contains
    # the claim text and the evidence title + abstract.
    assert "Drug X 200 mg reduced mortality at 30 days." in llm.calls[0]["prompt"]
    entail_prompt = llm.calls[1]["prompt"]
    assert "CLAIM: Drug X 200 mg reduced mortality at 30 days." in entail_prompt
    assert "Randomized trial of drug X" in entail_prompt
    assert "drug X 200 mg reduced mortality at 30 days" in entail_prompt
    assert "EVIDENCE (title + abstract of S1, MED/12345678):" in entail_prompt
    assert llm.calls[0]["temperature"] == 0.1  # decomposition
    assert llm.calls[1]["temperature"] == 0.0  # entailment judge

    # one batched esummary call with the right params
    assert len(session.calls) == 1
    assert session.calls[0]["params"] == {"db": "pubmed", "id": "12345678", "retmode": "json"}
    assert session.calls[0]["timeout"] == 15

    # EntailmentVerdict is constructible per the dataclass contract
    assert EntailmentVerdict("SUPPORTS", 1.0, "", "").verdict == "SUPPORTS"

    # enable_supersession=True with no pubmed client: one print, standing pass
    llm2 = FakeLLM([decomp(_CLAIM), judge("SUPPORTS", 0.9)])
    verifier2 = make_verifier(llm2, FakeSession(routes={ESUMMARY_URL: esummary_ok("12345678")}), enable_supersession=True)
    report2, out = capture(verifier2.verify, QUESTION, FakeSynthesis([_SENT]), [_S1], [QUERY])
    assert report2.claims[0].checks.standing == "pass"
    assert "no pubmed client" in out and "[verifier]" in out
    assert len(llm2.calls) == 2  # no extra judge calls
    print("PASS 01: happy path kept claim; prompts carry sentence/claim/evidence; esummary batched")


def t02_decomposition_split():
    sentence = FakeSentence(0, "Drug X 200 mg for 12 weeks reduced mortality in adults over 65.", ["S1"])
    claims = [
        {"sentence_index": 0, "claim": "Drug X was given at 200 mg for 12 weeks.", "citations": ["S1"]},
        {"sentence_index": 0, "claim": "The study population was adults over 65.", "citations": ["S1"]},
    ]
    llm = FakeLLM([decomp(*claims), judge("SUPPORTS", 0.9), judge("SUPPORTS", 0.85)])
    session = FakeSession(routes={ESUMMARY_URL: esummary_ok("12345678")})
    report = make_verifier(llm, session).verify(QUESTION, FakeSynthesis([sentence]), [_S1], [QUERY])

    assert [c.claim_id for c in report.claims] == ["s0-c1", "s0-c2"]
    assert all(c.citations[0]["sid"] == "S1" for c in report.claims)
    assert [c.text for c in report.claims] == [claims[0]["claim"], claims[1]["claim"]]
    assert report.funnel["claims_generated"] == 2
    assert report.funnel["claims_kept"] == 2
    assert report.answer_text == claims[0]["claim"] + " " + claims[1]["claim"]
    # the shared evidence record is checked ONCE (one batched esummary call)
    assert len(session.calls) == 1
    print("PASS 02: one sentence -> two atomic claims (s0-c1/s0-c2), shared citations")


def t03_refutes_deletion():
    llm = FakeLLM([decomp(_CLAIM), judge("REFUTES", 0.8)])
    session = FakeSession(routes={ESUMMARY_URL: esummary_ok("12345678")})
    report = make_verifier(llm, session).verify(QUESTION, FakeSynthesis([_SENT]), [_S1], [QUERY])

    claim = report.claims[0]
    assert claim.status == "deleted"
    assert claim.deletion_reason == "contradicted by its own citation"
    assert claim.checks.entailment == "refutes"
    assert claim.checks.existence == "pass"
    assert claim.checks.standing == "skipped"  # deleted by entailment, no retraction
    assert claim.verdict == "REFUTES" and claim.confidence == 0.8
    assert report.funnel["by_reason"] == {"contradicted by its own citation": 1}
    assert report.abstained and report.answer_text == ""
    print("PASS 03: REFUTES -> deleted 'contradicted by its own citation'; by_reason counted")


def t04_nei_deletion():
    llm = FakeLLM([decomp(_CLAIM), judge("NOT_ENOUGH_INFO", 0.4)])
    session = FakeSession(routes={ESUMMARY_URL: esummary_ok("12345678")})
    report = make_verifier(llm, session).verify(QUESTION, FakeSynthesis([_SENT]), [_S1], [QUERY])

    claim = report.claims[0]
    assert claim.status == "deleted"
    assert claim.deletion_reason == "unsupported by cited evidence"
    assert claim.checks.entailment == "nei"
    assert report.funnel["by_reason"] == {"unsupported by cited evidence": 1}
    print("PASS 04: NOT_ENOUGH_INFO -> deleted 'unsupported by cited evidence'")


def t05_weak_support_flag():
    llm = FakeLLM([decomp(_CLAIM), judge("SUPPORTS", 0.55)])
    session = FakeSession(routes={ESUMMARY_URL: esummary_ok("12345678")})
    report = make_verifier(llm, session).verify(QUESTION, FakeSynthesis([_SENT]), [_S1], [QUERY])

    claim = report.claims[0]
    assert claim.status == "flagged"
    assert "weakly supported" in claim.flags
    assert claim.verdict == "SUPPORTS" and claim.confidence == 0.55
    assert report.funnel["claims_kept"] == 1  # flagged counts as kept
    assert report.funnel["claims_deleted"] == 0
    assert not report.abstained
    assert report.answer_text == claim.text
    print("PASS 05: SUPPORTS 0.55 -> flagged 'weakly supported', still counted as kept")


def t06_judge_failure_fail_closed():
    # decomposition succeeds (call 0); both judge attempts raise LLMError.
    llm = FakeLLM([decomp(_CLAIM), judge("SUPPORTS", 0.9)], fail_indices={1, 2})
    session = FakeSession(routes={ESUMMARY_URL: esummary_ok("12345678")})
    verifier = make_verifier(llm, session)
    report, out = capture(verifier.verify, QUESTION, FakeSynthesis([_SENT]), [_S1], [QUERY])

    claim = report.claims[0]
    assert claim.status == "deleted"
    assert claim.deletion_reason == "unsupported by cited evidence"
    assert claim.checks.entailment == "nei"
    assert claim.verdict == "NOT_ENOUGH_INFO" and claim.confidence == 0.0
    assert "[verifier]" in out and "failing closed" in out
    assert "s0-c1" in out
    assert len(llm.calls) == 3  # decomposition + 2 judge attempts
    print("PASS 06: dead judge retried once, then fails closed to NEI 0.0 with a print")


def t07_existence_missing():
    # uids list does not contain the cited PMID
    llm = FakeLLM([decomp(_CLAIM)])
    session = FakeSession(routes={ESUMMARY_URL: FakeResponse(200, {"result": {"uids": []}})})
    report = make_verifier(llm, session).verify(QUESTION, FakeSynthesis([_SENT]), [_S1], [QUERY])

    claim = report.claims[0]
    assert claim.status == "deleted"
    assert claim.deletion_reason == "citation unresolvable: MED/12345678"
    assert claim.checks.existence == "fail"
    assert claim.checks.entailment == "skipped"
    assert claim.checks.standing == "skipped"
    assert claim.verdict is None and claim.confidence is None
    assert len(llm.calls) == 1  # judge never called for existence-failed claims
    assert report.funnel["by_reason"] == {"citation unresolvable: MED/12345678": 1}

    # variant: PMID present in uids but flagged with an error entry
    llm2 = FakeLLM([decomp(_CLAIM)])
    session2 = FakeSession(
        routes={
            ESUMMARY_URL: FakeResponse(
                200,
                {"result": {"uids": ["12345678"], "12345678": {"error": "cannot find document"}}},
            )
        }
    )
    report2 = make_verifier(llm2, session2).verify(QUESTION, FakeSynthesis([_SENT]), [_S1], [QUERY])
    assert report2.claims[0].checks.existence == "fail"
    assert report2.claims[0].deletion_reason == "citation unresolvable: MED/12345678"
    print("PASS 07: unresolvable PMID (absent from uids / error entry) -> existence fail deletion")


def t08_existence_network_fail_open():
    llm = FakeLLM([decomp(_CLAIM), judge("SUPPORTS", 0.9)])
    session = FakeSession(raise_exc=requests.ConnectionError)
    verifier = make_verifier(llm, session)
    report, out = capture(verifier.verify, QUESTION, FakeSynthesis([_SENT]), [_S1], [QUERY])

    claim = report.claims[0]
    assert claim.checks.existence == "skipped"  # fail-open: network error
    assert claim.status == "kept"  # entailment still decides
    assert claim.checks.entailment == "supports"
    assert claim.checks.standing == "pass"
    assert not report.abstained
    assert "esummary failed" in out and "skipped for 1 records" in out
    print("PASS 08: ConnectionError -> existence 'skipped', claim kept (fail-open)")


def t09_nct_and_doi_existence():
    # --- NCT exists ---
    nct_ev = make_evidence(
        sid="S1",
        citation_key="NCT/01234567",
        source="clinical_trials",
        native_id="01234567",
        title="Trial of drug X",
        abstract="Participants receiving drug X improved.",
    )
    sent = FakeSentence(0, "Drug X improved outcomes in the trial.", ["S1"])
    claim = {"sentence_index": 0, "claim": "Drug X improved outcomes in the trial.", "citations": ["S1"]}
    nct_url = CTGOV_URL.format(nct="01234567")

    llm = FakeLLM([decomp(claim), judge("SUPPORTS", 0.9)])
    session = FakeSession(
        routes={nct_url: FakeResponse(200, {"protocolSection": {"idModule": {"nctId": "NCT01234567"}}})}
    )
    report = make_verifier(llm, session).verify(QUESTION, FakeSynthesis([sent]), [nct_ev], [QUERY])
    assert report.claims[0].checks.existence == "pass"
    assert report.claims[0].status == "kept"
    assert session.calls[0]["url"] == nct_url
    assert session.calls[0]["params"] == {"format": "json"}

    # --- NCT 404 ---
    llm = FakeLLM([decomp(claim)])
    session = FakeSession(routes={nct_url: FakeResponse(404, None)})
    report = make_verifier(llm, session).verify(QUESTION, FakeSynthesis([sent]), [nct_ev], [QUERY])
    assert report.claims[0].checks.existence == "fail"
    assert report.claims[0].deletion_reason == "citation unresolvable: NCT/01234567"
    assert len(llm.calls) == 1  # judge skipped

    # --- EPMC preprint via doi.org: exists ---
    doi_ev = make_evidence(
        sid="S1",
        citation_key="EPMC/PPR123",
        source="europe_pmc",
        native_id="PPR123",
        doi="10.1101/2024.05.01.123",
        title="Preprint of drug X",
        abstract="Drug X reduced mortality.",
        is_preprint=True,
    )
    doi_url = DOI_URL.format(doi="10.1101/2024.05.01.123")
    llm = FakeLLM([decomp(claim), judge("SUPPORTS", 0.9)])
    session = FakeSession(routes={doi_url: FakeResponse(200, {"title": "crossref metadata"})})
    report = make_verifier(llm, session).verify(QUESTION, FakeSynthesis([sent]), [doi_ev], [QUERY])
    assert report.claims[0].checks.existence == "pass"
    assert report.claims[0].status == "kept"
    assert session.calls[0]["headers"] == {"Accept": "application/vnd.citationstyles.csl+json"}
    assert session.calls[0]["allow_redirects"] is True

    # --- EPMC preprint doi 404 ---
    llm = FakeLLM([decomp(claim)])
    session = FakeSession(routes={doi_url: FakeResponse(404, None)})
    report = make_verifier(llm, session).verify(QUESTION, FakeSynthesis([sent]), [doi_ev], [QUERY])
    assert report.claims[0].checks.existence == "fail"
    assert report.claims[0].deletion_reason == "citation unresolvable: EPMC/PPR123"

    # --- EPMC non-numeric id without a DOI: cannot verify -> skipped ---
    no_doi_ev = make_evidence(
        sid="S1",
        citation_key="EPMC/PPR456",
        source="europe_pmc",
        native_id="PPR456",
        doi=None,
        title="Preprint without doi",
        abstract="Drug X reduced mortality.",
    )
    llm = FakeLLM([decomp(claim), judge("SUPPORTS", 0.9)])
    session = FakeSession()  # no routes: any registry call would AssertionError
    report = make_verifier(llm, session).verify(QUESTION, FakeSynthesis([sent]), [no_doi_ev], [QUERY])
    assert report.claims[0].checks.existence == "skipped"
    assert report.claims[0].status == "kept"
    assert len(session.calls) == 0
    print("PASS 09: NCT pass/404, doi.org pass/404, EPMC-without-doi skipped")


def t10_retraction_deletion():
    s2 = make_evidence(
        sid="S2",
        citation_key="MED/22222222",
        native_id="22222222",
        title="Retracted trial of drug X",
        abstract="Drug X reduced mortality.",
        is_retracted=True,
        retraction_source="pubmed",
    )
    sent = FakeSentence(0, "Drug X reduced mortality.", ["S2"])
    claim = {"sentence_index": 0, "claim": "Drug X reduced mortality.", "citations": ["S2"]}
    llm = FakeLLM([decomp(claim), judge("SUPPORTS", 0.9)])
    session = FakeSession(routes={ESUMMARY_URL: esummary_ok("22222222")})
    report = make_verifier(llm, session).verify(QUESTION, FakeSynthesis([sent]), [s2], [QUERY])

    c = report.claims[0]
    assert c.status == "deleted"
    assert c.deletion_reason == "source retracted (pubmed)"
    assert c.checks.standing == "fail"
    assert c.checks.entailment == "supports"  # entailment ran and passed
    assert c.verdict == "SUPPORTS"
    assert report.funnel["by_reason"] == {"source retracted (pubmed)": 1}
    assert report.abstained

    # retraction_source None -> generic '(retracted)'
    s3 = dict(s2, sid="S3", citation_key="MED/33333333", native_id="33333333", retraction_source=None)
    sent3 = FakeSentence(0, "Drug X reduced mortality.", ["S3"])
    claim3 = {"sentence_index": 0, "claim": "Drug X reduced mortality.", "citations": ["S3"]}
    llm3 = FakeLLM([decomp(claim3), judge("SUPPORTS", 0.9)])
    session3 = FakeSession(routes={ESUMMARY_URL: esummary_ok("33333333")})
    report3 = make_verifier(llm3, session3).verify(QUESTION, FakeSynthesis([sent3]), [s3], [QUERY])
    assert report3.claims[0].deletion_reason == "source retracted (retracted)"
    print("PASS 10: retracted source -> deleted 'source retracted (...)', standing fail")


def t11_expression_of_concern_flag():
    s1 = make_evidence(
        sid="S1",
        citation_key="MED/12345678",
        native_id="12345678",
        title="Trial of drug X",
        abstract="Drug X reduced mortality. The journal issued an Expression of Concern regarding this article.",
    )
    llm = FakeLLM([decomp(_CLAIM), judge("SUPPORTS", 0.9)])
    session = FakeSession(routes={ESUMMARY_URL: esummary_ok("12345678")})
    report = make_verifier(llm, session).verify(QUESTION, FakeSynthesis([_SENT]), [s1], [QUERY])

    c = report.claims[0]
    assert c.status == "kept"  # flag, not delete
    assert "expression of concern" in c.flags
    assert c.checks.standing == "flag"
    assert not report.abstained
    assert report.answer_text == c.text

    # 'erratum' in the title also flags
    s2 = make_evidence(
        sid="S1",
        citation_key="MED/12345678",
        native_id="12345678",
        title="Erratum: Trial of drug X",
        abstract="Drug X reduced mortality.",
    )
    llm2 = FakeLLM([decomp(_CLAIM), judge("SUPPORTS", 0.9)])
    session2 = FakeSession(routes={ESUMMARY_URL: esummary_ok("12345678")})
    report2 = make_verifier(llm2, session2).verify(QUESTION, FakeSynthesis([_SENT]), [s2], [QUERY])
    assert "expression of concern" in report2.claims[0].flags
    assert report2.claims[0].checks.standing == "flag"
    print("PASS 11: expression of concern / erratum -> kept + flagged source")


def t12_all_deleted_abstention():
    sentences = [FakeSentence(i, f"Claim text {i}.", ["S1"]) for i in range(3)]
    claims = [
        {"sentence_index": i, "claim": f"Claim text {i}.", "citations": ["S1"]}
        for i in range(3)
    ]
    llm = FakeLLM([decomp(*claims), judge("NOT_ENOUGH_INFO", 0.5)])
    session = FakeSession(routes={ESUMMARY_URL: esummary_ok("12345678")})
    report = make_verifier(llm, session).verify(QUESTION, FakeSynthesis(sentences), [_S1], [QUERY])

    assert report.funnel["claims_generated"] == 3
    assert report.funnel["claims_deleted"] == 3
    assert report.funnel["claims_kept"] == 0
    assert report.abstained is True
    assert any("all claims deleted during verification" in r for r in report.abstain_reasons)
    assert report.answer_text == ""
    assert all(c.status == "deleted" for c in report.claims)
    print("PASS 12: all 3 claims deleted -> abstained with empty answer_text")


def t13_collapse_trigger():
    sentences = [FakeSentence(i, f"Claim text {i}.", ["S1"]) for i in range(3)]
    claims = [
        {"sentence_index": i, "claim": f"Claim text {i}.", "citations": ["S1"]}
        for i in range(3)
    ]
    session = FakeSession(routes={ESUMMARY_URL: esummary_ok("12345678")})

    # 1 of 3 kept (1/3 < 0.5) -> collapse abstention
    llm = FakeLLM([decomp(*claims), judge("SUPPORTS", 0.9), judge("NOT_ENOUGH_INFO", 0.5)])
    report = make_verifier(llm, session).verify(QUESTION, FakeSynthesis(sentences), [_S1], [QUERY])
    assert report.funnel["claims_kept"] == 1 and report.funnel["claims_deleted"] == 2
    assert report.abstained is True
    assert any("post-verification collapse" in r for r in report.abstain_reasons)
    assert "only 1/3 claims survived" in " ".join(report.abstain_reasons)
    assert report.answer_text == ""

    # 2 of 3 kept (2/3 >= 0.5) -> no abstention, answer keeps both texts
    llm2 = FakeLLM(
        [decomp(*claims), judge("SUPPORTS", 0.9), judge("SUPPORTS", 0.9), judge("NOT_ENOUGH_INFO", 0.5)]
    )
    report2 = make_verifier(llm2, session).verify(QUESTION, FakeSynthesis(sentences), [_S1], [QUERY])
    assert report2.funnel["claims_kept"] == 2
    assert not report2.abstained and report2.abstain_reasons == []
    assert report2.answer_text == "Claim text 0. Claim text 1."
    print("PASS 13: 1/3 kept -> collapse abstention; 2/3 kept -> answer survives")


def t14_supersession():
    review = FakeReviewRecord(
        "Meta-analysis of drug X",
        "Pooled analysis of 20 trials found drug X does not reduce mortality.",
    )

    # (a) newer review REFUTES with 0.9 -> deleted 'superseded by newer evidence'
    llm = FakeLLM([decomp(_CLAIM), judge("SUPPORTS", 0.9), judge("REFUTES", 0.9)])
    session = FakeSession(routes={ESUMMARY_URL: esummary_ok("12345678")})
    pubmed = FakePubMed(esearch_ids=["999"], efetch_records=[review])
    verifier = Verifier(llm=llm, session=session, pubmed=pubmed, enable_supersession=True)
    report, _ = capture(verifier.verify, QUESTION, FakeSynthesis([_SENT]), [_S1], [QUERY])

    c = report.claims[0]
    assert c.status == "deleted"
    assert c.deletion_reason == "superseded by newer evidence (MED/999)"
    assert c.checks.standing == "fail"
    assert c.checks.entailment == "supports"  # original citation still supports it
    assert report.funnel["by_reason"] == {"superseded by newer evidence (MED/999)": 1}

    year = date.today().year
    assert pubmed.esearch_calls == [
        {
            "query": f'({QUERY}) AND (systematic[sb] OR meta-analysis[pt]) AND ("{year - 5}":"{year}"[dp])',
            "retmax": 5,
        }
    ]
    assert pubmed.efetch_calls == [["999"]]
    assert "Meta-analysis of drug X" in llm.calls[2]["prompt"]  # supersession judge prompt

    # (b) newer review SUPPORTS -> claim survives, standing pass
    llm_b = FakeLLM([decomp(_CLAIM), judge("SUPPORTS", 0.9), judge("SUPPORTS", 0.85)])
    pubmed_b = FakePubMed(esearch_ids=["999"], efetch_records=[review])
    verifier_b = Verifier(llm=llm_b, session=session, pubmed=pubmed_b, enable_supersession=True)
    report_b = capture(verifier_b.verify, QUESTION, FakeSynthesis([_SENT]), [_S1], [QUERY])[0]
    assert report_b.claims[0].status == "kept"
    assert report_b.claims[0].checks.standing == "pass"
    assert not report_b.abstained

    # (c) no newer reviews found -> no extra judge calls
    llm_c = FakeLLM([decomp(_CLAIM), judge("SUPPORTS", 0.9)])
    pubmed_c = FakePubMed(esearch_ids=[])
    verifier_c = Verifier(llm=llm_c, session=session, pubmed=pubmed_c, enable_supersession=True)
    report_c = capture(verifier_c.verify, QUESTION, FakeSynthesis([_SENT]), [_S1], [QUERY])[0]
    assert len(llm_c.calls) == 2  # decomposition + one entailment only
    assert report_c.claims[0].status == "kept"
    assert report_c.claims[0].checks.standing == "pass"
    assert pubmed_c.efetch_calls == []

    # (d) esearch raises -> stage skipped (fail-open), no deletions
    llm_d = FakeLLM([decomp(_CLAIM), judge("SUPPORTS", 0.9)])
    pubmed_d = FakePubMed(esearch_exc=RuntimeError("pubmed down"))
    verifier_d = Verifier(llm=llm_d, session=session, pubmed=pubmed_d, enable_supersession=True)
    report_d, out_d = capture(verifier_d.verify, QUESTION, FakeSynthesis([_SENT]), [_S1], [QUERY])
    assert report_d.claims[0].status == "kept"
    assert "supersession check skipped" in out_d
    assert len(llm_d.calls) == 2
    print("PASS 14: supersession refute/survive/no-results/search-failure paths")


def t15_decomposition_fallback():
    sentences = [
        FakeSentence(0, "Sentence one text.", ["S1"]),
        FakeSentence(1, "Sentence two text.", ["S1"]),
    ]
    # both decomposition attempts raise LLMError (calls 0 and 1); the
    # whole-sentence fallback then runs entailment for each claim (2 and 3).
    llm = FakeLLM(
        [decomp(), decomp(), judge("SUPPORTS", 0.9), judge("SUPPORTS", 0.9)],
        fail_indices={0, 1},
    )
    session = FakeSession(routes={ESUMMARY_URL: esummary_ok("12345678")})
    verifier = make_verifier(llm, session)
    report, out = capture(verifier.verify, QUESTION, FakeSynthesis(sentences), [_S1], [QUERY])

    assert "decomposition fallback to whole-sentence claims" in out
    assert len(report.claims) == 2
    assert [c.claim_id for c in report.claims] == ["s0-c1", "s1-c1"]
    assert [c.text for c in report.claims] == ["Sentence one text.", "Sentence two text."]
    assert all(c.citations[0]["sid"] == "S1" for c in report.claims)
    assert all(c.status == "kept" for c in report.claims)
    assert len(llm.calls) == 4  # 2 failed decompositions + 2 entailments
    assert not report.abstained
    print("PASS 15: LLMError x2 -> whole-sentence fallback claims, pipeline continues")


def t16_synthesizer_abstained_shortcut():
    llm = FakeLLM()  # empty script: any call raises AssertionError
    session = FakeSession()  # no routes: any registry call raises AssertionError
    verifier = make_verifier(llm, session)
    report, out = capture(
        verifier.verify, QUESTION, FakeSynthesis([], abstained=True), [_S1], [QUERY]
    )

    assert len(llm.calls) == 0  # LLM contacted ZERO times
    assert len(session.calls) == 0
    assert report.abstained is True
    assert report.abstain_reasons == ["synthesizer abstained: insufficient evidence"]
    assert report.claims == []
    assert report.funnel == {
        "claims_generated": 0, "claims_deleted": 0, "claims_kept": 0, "claims_repaired": 0, "by_reason": {},
    }
    assert report.answer_text == ""
    assert "[verifier]" in out

    # empty sentence list (not abstained flag) takes the same shortcut
    report2 = make_verifier(FakeLLM(), FakeSession()).verify(
        QUESTION, FakeSynthesis([]), [_S1], [QUERY]
    )
    assert report2.abstained and report2.claims == []
    print("PASS 16: synthesizer abstained -> shortcut, zero LLM calls, empty funnel")


def t17_on_progress_callback():
    # Two claims: one citing a resolvable record (S1), one citing a record
    # whose pmid never appears in the esummary response (S2) -- existence
    # fails for the second, so Stage C skips its judge call entirely.
    # on_progress must still fire once per row (skipped included), 1-based,
    # in order, with the correct final count.
    s2 = make_evidence(
        sid="S2", citation_key="MED/99999999", native_id="99999999",
        title="Unrelated study",
    )
    sentence = FakeSentence(0, "Two things are true.", ["S1", "S2"])
    claims = [
        {"sentence_index": 0, "claim": "Drug X 200 mg reduced mortality at 30 days.", "citations": ["S1"]},
        {"sentence_index": 0, "claim": "An unrelated finding also held.", "citations": ["S2"]},
    ]
    llm = FakeLLM([decomp(*claims), judge("SUPPORTS", 0.9)])
    session = FakeSession(routes={ESUMMARY_URL: esummary_ok("12345678")})  # 99999999 absent
    calls: list[tuple[int, int]] = []
    report = make_verifier(llm, session).verify(
        QUESTION, FakeSynthesis([sentence]), [_S1, s2], [QUERY],
        on_progress=lambda i, n: calls.append((i, n)),
    )

    assert [c.checks.existence for c in report.claims] == ["pass", "fail"]
    assert [c.checks.entailment for c in report.claims] == ["supports", "skipped"]
    assert calls == [(1, 2), (2, 2)], calls

    # A broken callback must never break verification itself (fail-open).
    def boom(i, n):
        raise RuntimeError("simulated callback failure")

    llm2 = FakeLLM([decomp(*claims), judge("SUPPORTS", 0.9)])
    session2 = FakeSession(routes={ESUMMARY_URL: esummary_ok("12345678")})
    report2 = make_verifier(llm2, session2).verify(
        QUESTION, FakeSynthesis([sentence]), [_S1, s2], [QUERY], on_progress=boom
    )
    assert len(report2.claims) == 2, "a raising on_progress must not stop verification"
    print("PASS 17: on_progress fires once per row (skipped included), (index, count); fails open")


def t18_judge_sees_all_cited_records():
    # A claim citing THREE records: the old max_records=2 cap meant only
    # S1/S2's evidence ever reached the judge -- a claim actually grounded
    # in S3 alone would have been judged blind to it. All three must now
    # appear in the entailment prompt.
    s2 = make_evidence(sid="S2", citation_key="MED/22222222", native_id="22222222", title="Second study")
    s3 = make_evidence(sid="S3", citation_key="MED/33333333", native_id="33333333", title="Third study")
    claim = {"sentence_index": 0, "claim": "Drug X 200 mg reduced mortality at 30 days.", "citations": ["S1", "S2", "S3"]}
    sentence = FakeSentence(0, claim["claim"], ["S1", "S2", "S3"])
    llm = FakeLLM([decomp(claim), judge("SUPPORTS", 0.9)])
    session = FakeSession(routes={ESUMMARY_URL: esummary_ok("12345678", "22222222", "33333333")})
    make_verifier(llm, session).verify(QUESTION, FakeSynthesis([sentence]), [_S1, s2, s3], [QUERY])

    entail_prompt = llm.calls[1]["prompt"]
    assert "EVIDENCE (title + abstract of S1, MED/12345678):" in entail_prompt
    assert "EVIDENCE (title + abstract of S2, MED/22222222):" in entail_prompt
    assert "EVIDENCE (title + abstract of S3, MED/33333333):" in entail_prompt
    print("PASS 18: entailment judge sees every cited record, not just the first two")


def t19_self_contained_decomposition_instruction():
    # Prompt-contract regression guard (matches t12_prompt_contents' own
    # style elsewhere in this suite): a referentially incomplete claim
    # ("this reduced mortality") is unverifiable by construction and gets
    # judged NOT_ENOUGH_INFO -- that's a decomposer defect, not judge
    # strictness, so the fix belongs in the decompose prompt.
    from agents.verifier import _DECOMPOSE_SYSTEM

    assert "self-contained" in _DECOMPOSE_SYSTEM.lower(), _DECOMPOSE_SYSTEM
    assert "resolve" in _DECOMPOSE_SYSTEM.lower() and "pronoun" in _DECOMPOSE_SYSTEM.lower()
    print("PASS 19: decomposition system prompt requires self-contained, reference-resolved claims")


def t20_judge_allows_hedged_numbers_and_composition():
    # Prompt-contract regression guard for the two _JUDGE_SYSTEM edits: the
    # exact-match clause is gone (it fought the Synthesizer's own
    # hedging instruction) and a composition carve-out exists, while the
    # outside-knowledge ban stays verbatim (the anti-hallucination
    # guarantee this project is built on).
    from agents.verifier import _JUDGE_SYSTEM

    assert "must match exactly" not in _JUDGE_SYSTEM, _JUDGE_SYSTEM
    assert "Do not use medical knowledge not in the evidence" in _JUDGE_SYSTEM
    assert "combine" in _JUDGE_SYSTEM.lower(), _JUDGE_SYSTEM
    assert "hedged" in _JUDGE_SYSTEM.lower(), _JUDGE_SYSTEM
    print("PASS 20: judge prompt drops exact-number-match, keeps the outside-knowledge ban, allows composition")


def t21_citation_repair_rescues_misattributed_claim():
    # The claim actually describes S2's finding, but the Synthesizer cited
    # S1 -- existence resolves fine (S1 is a real pool record), but S1's
    # abstract doesn't support this particular claim. This is a
    # misattribution, not a genuine evidence gap: S2 is right there in the
    # pool, just never cited by this claim.
    s2 = make_evidence(
        sid="S2", citation_key="MED/22222222", native_id="22222222",
        title="Ibuprofen reduces postoperative pain scores",
        abstract=(
            "In a randomized trial, ibuprofen 400 mg reduced postoperative "
            "pain scores at 24 hours compared to placebo."
        ),
    )
    claim_text = "Ibuprofen 400 mg reduced postoperative pain scores at 24 hours."
    sentence = FakeSentence(0, claim_text, ["S1"])
    claim = {"sentence_index": 0, "claim": claim_text, "citations": ["S1"]}
    llm = FakeLLM([decomp(claim), judge("NOT_ENOUGH_INFO", 0.0), judge("SUPPORTS", 0.9)])
    session = FakeSession(routes={ESUMMARY_URL: esummary_ok("12345678", "22222222")})
    report = make_verifier(llm, session).verify(
        QUESTION, FakeSynthesis([sentence]), [_S1, s2], [QUERY]
    )

    claim_out = report.claims[0]
    assert claim_out.status == "flagged", claim_out.status
    assert claim_out.deletion_reason is None, claim_out.deletion_reason
    assert claim_out.citations[0]["sid"] == "S2", claim_out.citations
    assert "citation corrected during verification" in claim_out.flags, claim_out.flags
    assert report.funnel["claims_repaired"] == 1, report.funnel
    print("PASS 21: citation-repair reattributes a misfiled claim to the record that actually supports it")


def t22_citation_repair_never_rescues_refutes_or_missing():
    # REFUTES: repair must not even be attempted -- only a genuine
    # "unsupported by cited evidence" NEI deletion is eligible.
    llm = FakeLLM([decomp(_CLAIM), judge("REFUTES", 0.9)])
    session = FakeSession(routes={ESUMMARY_URL: esummary_ok("12345678")})
    report = make_verifier(llm, session).verify(QUESTION, FakeSynthesis([_SENT]), [_S1], [QUERY])
    assert report.claims[0].status == "deleted"
    assert report.claims[0].deletion_reason == "contradicted by its own citation"
    assert len(llm.calls) == 2, f"no extra repair attempt for a REFUTES deletion: {len(llm.calls)} calls"

    # existence-fail: repair must not be attempted either.
    llm2 = FakeLLM([decomp(_CLAIM)])
    session2 = FakeSession(routes={ESUMMARY_URL: FakeResponse(200, {"result": {"uids": []}})})
    report2 = make_verifier(llm2, session2).verify(QUESTION, FakeSynthesis([_SENT]), [_S1], [QUERY])
    assert report2.claims[0].status == "deleted"
    assert report2.claims[0].checks.existence == "fail"
    assert len(llm2.calls) == 1, f"no extra repair attempt for an existence-fail deletion: {len(llm2.calls)} calls"
    print("PASS 22: REFUTES and existence-fail deletions are never repair-attempted")


def t23_answer_text_is_punctuated_prose():
    # Regression test: agents/synthesizer.py's _parse() deliberately strips
    # every sentence's trailing terminator before it becomes Claim.text
    # (see Sentence's docstring) -- realistic fixtures here mirror that
    # (NO trailing '.'), unlike this file's other fixtures (_SENT/_CLAIM),
    # which happen to keep one. Confirmed live, 2026-08-31: with no
    # terminator re-added, a real multi-claim answer_text rendered in
    # web/AnswerPanel.tsx as one unreadable, unpunctuated run-on sentence.
    sentence_a = FakeSentence(0, "Drug X reduced mortality at 30 days", ["S1"])
    sentence_b = FakeSentence(1, "Drug X increased bleeding risk", ["S1"])
    claim_a = {"sentence_index": 0, "claim": "Drug X reduced mortality at 30 days", "citations": ["S1"]}
    claim_b = {"sentence_index": 1, "claim": "Drug X increased bleeding risk!", "citations": ["S1"]}
    llm = FakeLLM([decomp(claim_a, claim_b), judge("SUPPORTS", 0.9), judge("SUPPORTS", 0.9)])
    session = FakeSession(routes={ESUMMARY_URL: esummary_ok("12345678")})
    report = make_verifier(llm, session).verify(
        QUESTION, FakeSynthesis([sentence_a, sentence_b]), [_S1], [QUERY]
    )
    assert not report.abstained, report.abstain_reasons
    # Individual Claim.text is untouched (the Claims list renders each one
    # separately and must not gain punctuation the model never wrote) --
    # only the joined answer_text paragraph gains terminators.
    assert report.claims[0].text == "Drug X reduced mortality at 30 days"
    assert report.claims[1].text == "Drug X increased bleeding risk!"
    assert report.answer_text == (
        "Drug X reduced mortality at 30 days. Drug X increased bleeding risk!"
    ), report.answer_text
    print("PASS 23: answer_text terminates each claim ('.' unless already punctuated) without altering claims[].text")


def t24_answer_text_capitalizes_mid_sentence_fragments():
    # Regression test found live, 2026-08-31, on a real ESRD/DOAC question:
    # a claim decomposed from the MIDDLE of a longer sentence (SAFE-style
    # decomposition routinely does this) starts lower-case in the model's
    # own wording -- once t23's punctuation fix added a real '.' before
    # it, that read as a sentence-case error in the Answer panel ("...also
    # observed in meta-analyses. the effect of DOACs on ischemic stroke
    # ..."). An already-capitalized start (a drug name, an acronym) must
    # be left untouched -- this is a start-of-string fix, not a general
    # case-normalizer.
    sentence_a = FakeSentence(0, "the effect of DOACs on stroke risk was inconsistent", ["S1"])
    sentence_b = FakeSentence(1, "DOACs reduced bleeding risk", ["S1"])
    claim_a = {"sentence_index": 0, "claim": "the effect of DOACs on stroke risk was inconsistent", "citations": ["S1"]}
    claim_b = {"sentence_index": 1, "claim": "DOACs reduced bleeding risk", "citations": ["S1"]}
    llm = FakeLLM([decomp(claim_a, claim_b), judge("SUPPORTS", 0.9), judge("SUPPORTS", 0.9)])
    session = FakeSession(routes={ESUMMARY_URL: esummary_ok("12345678")})
    report = make_verifier(llm, session).verify(
        QUESTION, FakeSynthesis([sentence_a, sentence_b]), [_S1], [QUERY]
    )
    assert not report.abstained, report.abstain_reasons
    # claims[].text is untouched either way (unchanged from t23's contract).
    assert report.claims[0].text == "the effect of DOACs on stroke risk was inconsistent"
    assert report.claims[1].text == "DOACs reduced bleeding risk"
    assert report.answer_text == (
        "The effect of DOACs on stroke risk was inconsistent. DOACs reduced bleeding risk."
    ), report.answer_text
    print("PASS 24: answer_text capitalizes a lower-case claim start without touching an already-capitalized acronym")


def run():
    t01_happy_path()
    t02_decomposition_split()
    t03_refutes_deletion()
    t04_nei_deletion()
    t05_weak_support_flag()
    t06_judge_failure_fail_closed()
    t07_existence_missing()
    t08_existence_network_fail_open()
    t09_nct_and_doi_existence()
    t10_retraction_deletion()
    t11_expression_of_concern_flag()
    t12_all_deleted_abstention()
    t13_collapse_trigger()
    t14_supersession()
    t15_decomposition_fallback()
    t16_synthesizer_abstained_shortcut()
    t17_on_progress_callback()
    t18_judge_sees_all_cited_records()
    t19_self_contained_decomposition_instruction()
    t20_judge_allows_hedged_numbers_and_composition()
    t21_citation_repair_rescues_misattributed_claim()
    t22_citation_repair_never_rescues_refutes_or_missing()
    t23_answer_text_is_punctuated_prose()
    t24_answer_text_capitalizes_mid_sentence_fragments()
    print("\nAll verifier tests passed.")


if __name__ == "__main__":
    run()
