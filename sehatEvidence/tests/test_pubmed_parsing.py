"""
tests/test_pubmed_parsing.py

Offline test -- no internet required. Feeds realistic efetch XML directly
into PubMedClient's parser to prove the extraction logic works, without
depending on network access to eutils.ncbi.nlm.nih.gov (which this sandbox
can't reach anyway).

Run: python -m tests.test_pubmed_parsing   (from evidenceboard/ root)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import os

os.environ.setdefault("NCBI_TOOL_NAME", "evidenceboard_test")
os.environ.setdefault("NCBI_EMAIL", "test@example.com")

from retrieval.pubmed import PubMedClient
from core.schema import StudyDesign

# A realistic record: structured abstract (BACKGROUND/METHODS/RESULTS/CONCLUSIONS),
# an RCT publication type, a DOI, and -- critically -- native retraction markers.
SAMPLE_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID Version="1">12345678</PMID>
      <Article>
        <Journal>
          <JournalIssue>
            <PubDate><Year>2024</Year><Month>Mar</Month></PubDate>
          </JournalIssue>
          <Title>Journal of Fictional Cardiology</Title>
          <ISOAbbreviation>J Fict Cardiol</ISOAbbreviation>
        </Journal>
        <ArticleTitle>Effect of Drug X on Outcome Y: A Randomized Controlled Trial</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND" NlmCategory="BACKGROUND">Prior evidence on Drug X is limited.</AbstractText>
          <AbstractText Label="METHODS" NlmCategory="METHODS">We randomized 400 patients to Drug X or placebo.</AbstractText>
          <AbstractText Label="RESULTS" NlmCategory="RESULTS">Drug X reduced Outcome Y by 12 percent (p=0.01).</AbstractText>
          <AbstractText Label="CONCLUSIONS" NlmCategory="CONCLUSIONS">Drug X shows benefit for Outcome Y.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><LastName>Smith</LastName><ForeName>Jane</ForeName></Author>
          <Author><LastName>Doe</LastName><ForeName>John</ForeName></Author>
        </AuthorList>
        <PublicationTypeList>
          <PublicationType>Randomized Controlled Trial</PublicationType>
          <PublicationType>Retracted Publication</PublicationType>
        </PublicationTypeList>
        <ELocationID EIdType="doi">10.1000/fictional.2024.001</ELocationID>
      </Article>
      <CommentsCorrectionsList>
        <CommentsCorrections RefType="RetractionIn">
          <PMID>99999999</PMID>
        </CommentsCorrections>
      </CommentsCorrectionsList>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">12345678</ArticleId>
        <ArticleId IdType="doi">10.1000/fictional.2024.001</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>

  <PubmedArticle>
    <MedlineCitation>
      <PMID Version="1">22222222</PMID>
      <Article>
        <Journal>
          <JournalIssue>
            <PubDate><MedDate>2019 Jan-Feb</MedDate></PubDate>
          </JournalIssue>
          <Title>Fictional Case Reports Quarterly</Title>
        </Journal>
        <ArticleTitle>[Unstructured abstract case report, no DOI]</ArticleTitle>
        <Abstract>
          <AbstractText>A single unstructured abstract with no Label or NlmCategory.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><CollectiveName>Fictional Study Group</CollectiveName></Author>
        </AuthorList>
        <PublicationTypeList>
          <PublicationType>Case Reports</PublicationType>
        </PublicationTypeList>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>
"""


def run():
    client = PubMedClient(api_key=None)  # env vars supply tool/email
    records = client._parse_efetch_xml(SAMPLE_XML)

    assert len(records) == 2, f"expected 2 records, got {len(records)}"

    r1 = records[0]
    print("--- Record 1 ---")
    print("citation_key:", r1.citation_key())
    print("title:", r1.title)
    print("doi:", r1.doi)
    print("journal:", r1.journal)
    print("publication_date:", r1.publication_date)
    print("authors:", r1.authors)
    print("study_design:", r1.study_design)
    print("is_retracted:", r1.is_retracted)
    print("retraction_notice_id:", r1.retraction_notice_id)
    print("retraction_source:", r1.retraction_source)
    print("abstract_sections keys:", list(r1.abstract_sections.keys()))
    print("abstract (flat):", r1.abstract[:80], "...")
    print()

    assert r1.citation_key() == "MED/12345678"
    assert r1.doi == "10.1000/fictional.2024.001"
    assert r1.study_design == StudyDesign.RCT
    assert r1.is_retracted is True, "native retraction detection failed"
    assert r1.retraction_notice_id == "99999999"
    assert r1.retraction_source == "pubmed"
    assert set(r1.abstract_sections.keys()) == {
        "BACKGROUND", "METHODS", "RESULTS", "CONCLUSIONS"
    }
    assert r1.authors == ["Jane Smith", "John Doe"]
    assert r1.publication_date.year == 2024 and r1.publication_date.month == 3

    r2 = records[1]
    print("--- Record 2 ---")
    print("citation_key:", r2.citation_key())
    print("doi:", r2.doi)
    print("publication_date:", r2.publication_date)
    print("authors:", r2.authors)
    print("is_retracted:", r2.is_retracted)
    print("abstract_sections keys:", list(r2.abstract_sections.keys()))

    assert r2.doi is None, "should gracefully handle missing DOI"
    assert r2.publication_date.year == 2019, "should parse year out of MedDate fallback"
    assert r2.authors == ["Fictional Study Group"], "should handle collective authors"
    assert r2.is_retracted is False
    assert set(r2.abstract_sections.keys()) == {"UNASSIGNED"}, (
        "unstructured abstract should default to UNASSIGNED category"
    )

    print("\nAll assertions passed.")


if __name__ == "__main__":
    run()