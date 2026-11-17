"""Cached HTTP replay and PubMed record extraction.

Both regressions are about failures being turned into results: a cached 429 was
replayed as ``ok`` (so an error body looked like an empty page), and efetch's
DOI/PMCID plus any title text inside inline markup (``SCF<i>FBW7</i>...``) were
dropped. These tests pin the cache-status contract and the record shape that
``method_search._pubmed_page`` consumes.
"""
from __future__ import annotations

import hashlib
import urllib.parse
from pathlib import Path

from sctda import literature as lit
from sctda.project import Project, now

EPMC_429_BODY = '{"status":"error","message":"rate limit exceeded"}'

# efetch shape: nested inline markup in the title/abstract, a structured
# abstract, and identifiers in PubmedData/ArticleIdList.
EFETCH_XML = """<?xml version="1.0" ?>
<PubmedArticleSet>
<PubmedArticle><MedlineCitation Status="MEDLINE"><PMID Version="1">32997960</PMID>
<Article PubModel="Print">
<Journal><JournalIssue CitedMedium="Internet"><PubDate><Year>2021</Year><Month>Jan</Month></PubDate></JournalIssue>
<Title>Cell stem cell</Title><ISOAbbreviation>Cell Stem Cell</ISOAbbreviation></Journal>
<ArticleTitle>SCF<i>FBW7</i>-mediated degradation of Brg1 in <sup>2</sup> models.</ArticleTitle>
<Abstract><AbstractText Label="RESULTS">We used <i>Seurat</i> and <sup>2</sup> methods.</AbstractText>
<AbstractText Label="CONCLUSIONS">Degradation was confirmed.</AbstractText></Abstract>
</Article></MedlineCitation>
<PubmedData><ArticleIdList>
<ArticleId IdType="pubmed">32997960</ArticleId>
<ArticleId IdType="mid">NIHMS1634089</ArticleId>
<ArticleId IdType="pmc">PMC7796920</ArticleId>
<ArticleId IdType="doi">10.1016/j.stem.2020.09.004</ArticleId>
</ArticleIdList></PubmedData></PubmedArticle>
<PubmedArticle><MedlineCitation Status="MEDLINE"><PMID Version="1">30177679</PMID>
<Article PubModel="Electronic">
<Journal><JournalIssue CitedMedium="Internet"><PubDate><MedlineDate>2020 Nov-Dec</MedlineDate></PubDate></JournalIssue>
<Title>Nature communications</Title></Journal>
<ArticleTitle>No identifiers here.</ArticleTitle>
<Abstract><AbstractText>Plain abstract.</AbstractText></Abstract>
</Article></MedlineCitation>
<PubmedData><ArticleIdList>
<ArticleId IdType="pubmed">30177679</ArticleId>
</ArticleIdList></PubmedData></PubmedArticle>
</PubmedArticleSet>
"""


def offline_project(tmp_path: Path) -> Project:
    project = Project(tmp_path / "project")
    project.ensure()
    return project


def seed_cache(conn, project: Project, url: str, body: str,
               status: int | None) -> Path:
    """Write the rows a real response leaves behind, without any HTTP call."""
    path = project.dirs["downloads"] / f"seed_{lit._cache_key(url, None)}.txt"
    path.write_text(body, encoding="utf-8")
    conn.execute(
        """INSERT OR REPLACE INTO literature_cache(cache_id, query, endpoint,
             requested_utc, http_status, response_sha256, body_path, notes)
           VALUES (?,?,?,?,?,?,?,?)""",
        (lit._cache_key(url, None), url, url.split("?")[0], now(), status,
         hashlib.sha256(body.encode("utf-8")).hexdigest(), str(path), None))
    conn.commit()
    return path


def forbid_network(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise AssertionError("a cache hit must not issue a request")
    monkeypatch.setattr(lit.urllib.request, "urlopen", boom)


# ------------------------------------------------------------------ http_get

def test_cached_200_replays_as_ok(tmp_path, monkeypatch):
    project = offline_project(tmp_path)
    conn = project.connect()
    url = f"{lit.EPMC}/search?query=single%20cell&format=json"
    seed_cache(conn, project, url, '{"hitCount":1}', 200)
    forbid_network(monkeypatch)

    replay = lit.http_get(project, conn, url)

    assert replay["ok"] is True
    assert replay["cached"] is True
    assert replay["status"] == 200
    assert "error" not in replay
    assert replay["body"] == '{"hitCount":1}'


def test_cached_429_replays_as_failure_not_a_page(tmp_path, monkeypatch):
    project = offline_project(tmp_path)
    conn = project.connect()
    url = f"{lit.EPMC}/search?query=single%20cell&format=json"
    seed_cache(conn, project, url, EPMC_429_BODY, 429)
    forbid_network(monkeypatch)

    replay = lit.http_get(project, conn, url)

    assert replay["ok"] is False
    assert replay["cached"] is True
    assert replay["status"] == 429
    assert replay["error"] == "HTTP 429"
    # The error body is still readable as provenance, never as a result.
    assert replay["body"] == EPMC_429_BODY
    assert replay["path"].endswith(".txt")


def test_cached_5xx_replays_as_failure(tmp_path, monkeypatch):
    project = offline_project(tmp_path)
    conn = project.connect()
    url = f"{lit.CHEMBL}/mechanism.json?target_chembl_id=CHEMBL1&limit=1"
    seed_cache(conn, project, url, "<html>upstream timeout</html>", 504)
    forbid_network(monkeypatch)

    assert lit.http_get(project, conn, url)["ok"] is False


def test_cached_status_less_body_is_not_ok(tmp_path, monkeypatch):
    project = offline_project(tmp_path)
    conn = project.connect()
    url = f"{lit.EUTILS}/esearch.fcgi?db=pubmed&retmode=json&retmax=1&term=x"
    seed_cache(conn, project, url, "{}", None)
    forbid_network(monkeypatch)

    replay = lit.http_get(project, conn, url)

    assert replay["ok"] is False
    assert "no recorded HTTP status" in replay["error"]


def test_europepmc_search_cached_429_is_failed_with_status(tmp_path, monkeypatch):
    project = offline_project(tmp_path)
    conn = project.connect()
    url = (f"{lit.EPMC}/search?query={urllib.parse.quote('single cell')}"
           f"&format=json&pageSize=1&resultType=core")
    seed_cache(conn, project, url, EPMC_429_BODY, 429)
    forbid_network(monkeypatch)

    result = lit.europepmc_search(project, conn, "single cell", page_size=1)

    assert result["status"] == "failed"
    assert result["http_status"] == 429
    assert result["error"] == "HTTP 429"
    assert "hit_count" not in result          # not a zero-hit negative
    assert "hits" not in result


def test_open_targets_cached_429_is_failed(tmp_path, monkeypatch):
    project = offline_project(tmp_path)
    conn = project.connect()
    query = '{ target(ensemblId:"ENSG00000130234"){ id } }'
    seed_cache(conn, project, lit.OPEN_TARGETS + query,
               '{"errors":[{"message":"Too many requests"}]}', 429)
    forbid_network(monkeypatch)

    result = lit.open_targets_query(project, conn, query)

    assert result["status"] == "failed"
    assert result["http_status"] == 429
    assert result["cached"] is True


# ---------------------------------------------------------- PubMed metadata

def test_fetch_abstracts_keeps_nested_text_and_extracts_ids(tmp_path, monkeypatch):
    project = offline_project(tmp_path)
    conn = project.connect()
    url = (f"{lit.EUTILS}/efetch.fcgi?db=pubmed&retmode=xml"
           f"&id=32997960,30177679")
    seed_cache(conn, project, url, EFETCH_XML, 200)
    forbid_network(monkeypatch)

    result = lit.fetch_abstracts(project, conn, ["32997960", "30177679"])

    assert result["status"] == "ok"
    assert result["n"] == 2
    first, second = result["records"]
    # findtext would stop at <i> and return "SCF".
    assert first["title"] == ("SCFFBW7-mediated degradation of Brg1 in 2 models.")
    assert first["journal"] == "Cell stem cell"
    assert first["year"] == "2021"
    assert first["abstract"] == ("We used Seurat and 2 methods. "
                                 "Degradation was confirmed.")
    assert first["abstract_available"] is True
    assert first["doi"] == "10.1016/j.stem.2020.09.004"
    assert first["pmcid"] == "PMC7796920"

    assert second["pmid"] == "30177679"
    assert second["year"] == "2020"                  # MedlineDate fallback
    assert second["doi"] is None
    assert second["pmcid"] is None
    assert second["abstract"] == "Plain abstract."


def test_fetch_abstracts_cached_500_reports_failure_without_records(tmp_path,
                                                                    monkeypatch):
    project = offline_project(tmp_path)
    conn = project.connect()
    url = (f"{lit.EUTILS}/efetch.fcgi?db=pubmed&retmode=xml"
           f"&id=32997960")
    seed_cache(conn, project, url, "<html>service unavailable</html>", 500)
    forbid_network(monkeypatch)

    result = lit.fetch_abstracts(project, conn, ["32997960"])

    assert result["status"] == "failed"
    assert result["http_status"] == 500
    assert result["endpoint"] == url
    assert "records" not in result                   # never an empty record set
