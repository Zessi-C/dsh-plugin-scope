"""Restricted source verification: policy, snapshots, paging, corpus lookup.

No test here touches the network. DNS and the single HTTP seam
(`method_sources._open_url`) are monkeypatched; the paper path is exercised by
faking `literature.http_get`, which leaves the real JATS parser and rendering in
play. Real reads are limited to the frozen `research/code_sources.jsonl` and the
configured inclusion list, both treated as read-only; every other fixture is a
temporary file.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from sctda import literature as lit  # noqa: E402
from sctda import method_registry as MR  # noqa: E402
from sctda import method_sources as ms  # noqa: E402
from sctda.harness import invoke_tool  # noqa: E402
from sctda.project import Project  # noqa: E402
import scope_knowledge as SK  # noqa: E402

PUBLIC_IP = "93.184.216.34"


def make_project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    Project(root).ensure()
    return root


def fake_dns(monkeypatch, addresses=(PUBLIC_IP,)):
    monkeypatch.setattr(ms, "_resolve_public_ips",
                        lambda host, port=443: list(addresses))


def fake_open(monkeypatch, *, body=b"source body", status=200,
              content_type="text/html; charset=utf-8", final_url=None,
              calls=None):
    def opener(url, timeout, max_bytes):
        if calls is not None:
            calls.append({"url": url, "timeout": timeout,
                          "max_bytes": max_bytes})
        return {"body": body, "status": status,
                "content_type": content_type,
                "final_url": final_url or url}
    monkeypatch.setattr(ms, "_open_url", opener)


# ------------------------------------------------------------------ policy

def test_unlisted_host_is_blocked_before_any_request(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    monkeypatch.setattr(ms, "_open_url",
                        lambda *a, **k: pytest.fail("network must not be touched"))
    result = ms.fetch_source(str(project), "https://evil.example/paper", "doc")
    assert result["status"] == "blocked"
    assert "allowlist" in result["error"]
    assert result["recorded"] is False
    assert result["claims_verified"] is False
    assert not list((project / "downloads" / "sources").glob("src_*.json"))


def test_non_https_and_file_schemes_are_blocked(tmp_path):
    project = make_project(tmp_path)
    for url in ("http://satijalab.org/seurat/", "file:///etc/passwd",
                "ftp://cran.r-project.org/x", "https://127.0.0.1/x"):
        result = ms.fetch_source(str(project), url, "doc")
        assert result["status"] == "blocked", url
        assert result["snapshot_id"] is None


def test_host_suffix_trick_and_repo_html_page_are_blocked(tmp_path):
    project = make_project(tmp_path)
    suffix = ms.fetch_source(
        str(project), "https://github.com.evil.test/o/r/blob/main/x.R", "code")
    assert suffix["status"] == "blocked"
    page = ms.fetch_source(str(project), "https://github.com/o/r", "author_code")
    assert page["status"] == "blocked"
    assert "HTML pages" in page["error"]


def test_private_dns_answer_is_blocked(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    monkeypatch.setattr(
        ms.socket, "getaddrinfo",
        lambda *a, **k: [(ms.socket.AF_INET, ms.socket.SOCK_STREAM, 6, "",
                          ("127.0.0.1", 443))])
    monkeypatch.setattr(ms, "_open_url",
                        lambda *a, **k: pytest.fail("private target must not be opened"))
    result = ms.fetch_source(str(project), "https://satijalab.org/seurat/", "doc")
    assert result["status"] == "blocked"
    assert "non-public" in result["error"]


def test_redirect_is_refused_and_not_persisted(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    fake_dns(monkeypatch)

    def redirect(url, timeout, max_bytes):
        raise ms.SourceRedirectRefused(302, "https://evil.example/x")
    monkeypatch.setattr(ms, "_open_url", redirect)
    result = ms.fetch_source(str(project), "https://satijalab.org/seurat/", "doc")
    assert result["status"] == "blocked"
    assert result["detail"]["redirect_location"] == "https://evil.example/x"
    assert result["recorded"] is False
    assert not list((project / "downloads" / "sources").glob("src_*.json"))


def test_cross_host_final_url_is_blocked(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    fake_dns(monkeypatch)
    fake_open(monkeypatch, final_url="https://evil.example/x")
    result = ms.fetch_source(str(project), "https://satijalab.org/seurat/", "doc")
    assert result["status"] == "blocked"
    assert "differs from requested host" in result["error"]


def test_oversize_response_is_recorded_as_too_large(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    fake_dns(monkeypatch)

    def too_large(url, timeout, max_bytes):
        raise ms.SourceTooLarge("response exceeded 2097152 bytes")
    monkeypatch.setattr(ms, "_open_url", too_large)
    result = ms.fetch_source(str(project), "https://satijalab.org/seurat/", "doc")
    assert result["status"] == "too_large"
    assert result["recorded"] is True
    assert result["snapshot_id"] is None
    assert result["attempt_id"].startswith("src_")
    # The failure is listed, but cannot be read as a document.
    listed = ms.list_snapshots(str(project), status="too_large")
    assert listed["total"] == 1
    read = ms.read_snapshot(str(project), result["attempt_id"])
    assert read["status"] == "not_found"


def test_http_error_status_is_failed_not_unreachable(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    fake_dns(monkeypatch)
    fake_open(monkeypatch, body=b"", status=404)
    result = ms.fetch_source(str(project), "https://satijalab.org/seurat/", "doc")
    assert result["status"] == "failed"
    assert result["http_status"] == 404
    assert result["recorded"] is True
    assert result["snapshot_id"] is None


# --------------------------------------------------------------- snapshots

def test_fetch_caches_provenance_and_replays_without_network(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    fake_dns(monkeypatch)
    calls = []
    fake_open(monkeypatch, body=b"Seurat documentation body", calls=calls)

    first = ms.fetch_source(str(project), "https://satijalab.org/seurat/", "doc")
    assert first["status"] == "ok"
    assert first["kind"] == "tool_doc"
    assert first["cached"] is False
    assert first["claims_verified"] is False
    assert first["bytes"] == len(b"Seurat documentation body")
    assert first["response_sha256"] == hashlib.sha256(
        b"Seurat documentation body").hexdigest()
    body_path = Path(first["body_path"])
    assert body_path.exists()
    sidecar = body_path.with_suffix(".json")
    meta = json.loads(sidecar.read_text(encoding="utf-8"))
    for key in ("endpoint", "requested_utc", "http_status", "response_sha256",
                "body_path", "bytes"):
        assert meta[key] is not None, key
    assert meta["http_status"] == 200

    second = ms.fetch_source(str(project), "https://satijalab.org/seurat/", "doc")
    assert second["status"] == "ok"
    assert second["cached"] is True
    assert second["snapshot_id"] == first["snapshot_id"]
    assert len(calls) == 1, "a cached source must not issue another request"

    refreshed = ms.fetch_source(str(project), "https://satijalab.org/seurat/",
                                "doc", refresh=True)
    assert refreshed["cached"] is False
    assert len(calls) == 2


def test_read_snapshot_pages_without_loss(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    fake_dns(monkeypatch)
    body = "abcdefghij"
    fake_open(monkeypatch, body=body.encode())
    fetched = ms.fetch_source(str(project), "https://satijalab.org/seurat/", "doc")

    pages = []
    offset = 0
    while True:
        page = ms.read_snapshot(str(project), fetched["snapshot_id"],
                                offset=offset, limit=4)
        assert page["status"] == "ok"
        assert page["sha256_verified"] is True
        assert page["claims_verified"] is False
        assert page["evidence_status"] == "source_text_read"
        pages.append(page["text"])
        if not page["has_more"]:
            break
        offset = page["next_offset"]
    assert "".join(pages) == body
    assert [len(p) for p in pages] == [4, 4, 2]


def test_read_snapshot_withholds_text_after_tamper(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    fake_dns(monkeypatch)
    fake_open(monkeypatch, body=b"original")
    fetched = ms.fetch_source(str(project), "https://satijalab.org/seurat/", "doc")
    Path(fetched["body_path"]).write_text("tampered", encoding="utf-8")
    result = ms.read_snapshot(str(project), fetched["snapshot_id"])
    assert result["status"] == "integrity_failed"
    assert "text" not in result
    assert result["recorded_sha256"] != result["actual_sha256"]


def test_unknown_or_traversal_snapshot_ids_are_not_found(tmp_path):
    project = make_project(tmp_path)
    for sid in ("missing", "../../etc/passwd", "src_" + "f" * 20):
        assert ms.read_snapshot(str(project), sid)["status"] == "not_found"
        assert ms.verify_snapshot(str(project), sid)["status"] == "not_found"


def test_verify_is_hash_and_size_only_and_read_only(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    fake_dns(monkeypatch)
    fake_open(monkeypatch, body=b"verify me")
    fetched = ms.fetch_source(str(project), "https://satijalab.org/seurat/", "doc")
    before = sorted(p.name for p in (project / "downloads" / "sources").iterdir())
    result = ms.verify_snapshot(str(project), fetched["snapshot_id"])
    assert result["status"] == "ok"
    assert result["sha256_match"] is True
    assert result["size_match"] is True
    assert result["integrity_verified"] is True
    assert result["body_path"] == fetched["body_path"]
    assert result["kind"] == "tool_doc"
    assert result["bytes"] == len(b"verify me")
    assert result["claims_verified"] is False
    assert "does not verify any claim" in " ".join(result["limitations"])
    after = sorted(p.name for p in (project / "downloads" / "sources").iterdir())
    assert before == after, "verify must not write a snapshot or a log row"


def test_list_snapshots_pages_and_filters(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    fake_dns(monkeypatch)
    fake_open(monkeypatch, body=b"one")
    ms.fetch_source(str(project), "https://satijalab.org/seurat/", "doc")
    fake_open(monkeypatch, body=b"two")
    ms.fetch_source(str(project),
                    "https://cran.r-project.org/web/packages/Seurat/index.html",
                    "tool_doc")
    all_rows = ms.list_snapshots(str(project), limit=1)
    assert all_rows["status"] == "ok"
    assert all_rows["total"] == 2
    assert len(all_rows["snapshots"]) == 1
    assert all_rows["has_more"] is True
    second = ms.list_snapshots(str(project), limit=1,
                               offset=all_rows["next_offset"])
    assert second["has_more"] is False
    only_cran = ms.list_snapshots(str(project), query="cran")
    assert only_cran["total"] == 1
    assert "cran" in only_cran["snapshots"][0]["endpoint"]


def test_github_blob_url_is_rewritten_to_raw(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    fake_dns(monkeypatch)
    calls = []
    fake_open(monkeypatch, body=b"plot <- function() {}", calls=calls)
    result = ms.fetch_source(
        str(project),
        "https://github.com/satijalab/seurat/blob/master/R/visualization.R",
        "code")
    assert result["status"] == "ok"
    assert calls[0]["url"].startswith(
        "https://raw.githubusercontent.com/satijalab/seurat/master/R/visualization.R")
    assert "rewritten" in result["rewrite_note"]
    assert result["kind"] == "author_code"


# ------------------------------------------------------------------- papers

PAPER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<article>
  <front><article-meta>
    <abstract><title>Abstract</title><p>Abstract sentence.</p></abstract>
  </article-meta></front>
  <body>
    <sec><title>Methods</title>
      <p>We used Seurat for clustering.</p>
      <sec><title>Statistical analysis</title>
        <p>Wilcoxon rank-sum test was used.</p>
      </sec>
    </sec>
    <sec><title>Results</title>
      <p>Clusters differed.</p>
      <table-wrap><label>1</label><caption><p>Parameters used.</p></caption></table-wrap>
    </sec>
  </body>
</article>
"""


def fake_paper(monkeypatch, body=PAPER_XML, status=200):
    def get(project, conn, url, timeout=30):
        return {"ok": status is not None and 200 <= status < 300,
                "status": status, "body": body, "cached": False,
                "path": "/nonexistent"}
    monkeypatch.setattr(lit, "http_get", get)


def test_paper_nested_sections_are_preserved_and_readable(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    fake_paper(monkeypatch)
    result = ms.fetch_paper(str(project), "12345")
    assert result["status"] == "ok"
    assert result["pmcid"] == "PMC12345"
    assert result["n_sections"] >= 4
    assert result["n_nested_sections"] == 1
    assert result["claims_verified"] is False
    assert result["text_sha256"] == result["response_sha256"]

    first = ms.read_snapshot(str(project), result["snapshot_id"], limit=20000)
    assert first["status"] == "ok"
    text = first["text"]
    assert "### Methods > Statistical analysis" in text
    assert "Wilcoxon rank-sum test was used." in text
    # The parent section owns only its own paragraph.
    assert "## Methods\nWe used Seurat for clustering." in text
    assert "Table 1" in text
    assert "Parameters used." in text


def test_paper_404_is_not_available_and_recorded(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    fake_paper(monkeypatch, body="", status=404)
    result = ms.fetch_paper(str(project), "PMC999")
    assert result["status"] == "not_available"
    assert result["snapshot_id"] is None
    assert result["attempt_id"].startswith("src_")
    assert result["http_status"] == 404
    assert result["recorded"] is True
    assert result["claims_verified"] is False


def test_paper_transport_failure_is_unreachable(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    fake_paper(monkeypatch, body="", status=None)
    result = ms.fetch_paper(str(project), "PMC999")
    assert result["status"] == "unreachable"
    assert result["snapshot_id"] is None


def test_invalid_pmcid_is_failed_without_request(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    monkeypatch.setattr(lit, "http_get",
                        lambda *a, **k: pytest.fail("invalid id must not fetch"))
    assert ms.fetch_paper(str(project), "")["status"] == "failed"
    assert ms.fetch_paper(str(project), "PMCabc")["status"] == "failed"


# -------------------------------------------------------------- code corpus

CODE_ROWS = [
    {"source_id": "1:cs1", "pmid": "1", "url": "https://github.com/a/b",
     "url_kind": "code_repo", "ownership": "author", "locator": "methods:L1:P1:S1",
     "verbatim": "code at github", "status": "author_code_stated",
     "checked_utc": None, "params_found": [], "notes": "n1"},
    {"source_id": "1:cs2", "pmid": "1", "url": "https://github.com/a/c",
     "url_kind": "code_repo", "ownership": "author", "locator": "methods:L2:P1:S1",
     "verbatim": "second code", "status": "no_author_code",
     "checked_utc": "2026-01-01T00:00:00+00:00", "params_found": [],
     "notes": "n2"},
    {"source_id": "2:cs1", "pmid": "2", "url": "https://github.com/d/e",
     "url_kind": "code_repo", "ownership": "author", "locator": "methods:L1:P1:S1",
     "verbatim": "other", "status": "author_code_stated",
     "checked_utc": None, "params_found": [], "notes": "n3"},
]


def write_code_corpus(tmp_path: Path) -> Path:
    research = tmp_path / "research"
    research.mkdir()
    path = research / "code_sources.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in CODE_ROWS),
                    encoding="utf-8")
    return path


def write_pmid_list(tmp_path: Path, pmids=("1", "2"),
                    name: str = "included_pmids_405.txt") -> Path:
    path = tmp_path / name
    path.write_text("\n".join(pmids) + "\n", encoding="utf-8")
    return path


def test_code_sources_pages_by_pmid_and_marks_every_record_unverified(
        tmp_path, monkeypatch):
    path = write_code_corpus(tmp_path)
    list_path = write_pmid_list(tmp_path, ("1", "2", "999"))
    monkeypatch.setattr(ms, "CODE_SOURCES_PATH", path)
    first = ms.code_sources("1", page=1, page_size=1,
                            pmid_list_path=list_path)
    assert first["status"] == "ok"
    assert first["total_for_pmid"] == 2
    assert first["total_in_file"] == 3
    assert first["has_more"] is True
    assert first["next_page"] == 2
    assert first["pmid_in_scope"] is True
    assert first["scope"]["pmid_list_path"] == str(list_path)
    assert first["scope"]["n_included_pmids"] == 3
    assert first["scope"]["pmid_list_sha256"] == hashlib.sha256(
        list_path.read_bytes()).hexdigest()
    record = first["records"][0]
    assert record["verified"] is False
    assert record["verification"] == "unchecked"
    assert "extraction record only" in record["verification_note"]
    assert first["claims_verified"] is False
    assert first["file_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()

    second = ms.code_sources("1", page=2, page_size=1,
                             pmid_list_path=list_path)
    assert second["records"][0]["source_id"] == "1:cs2"
    assert second["records"][0]["verified"] is False
    # checked_utc is extraction bookkeeping, not a verification result.
    assert second["records"][0]["checked_utc"] == "2026-01-01T00:00:00+00:00"


def test_code_sources_absent_pmid_is_explicit_not_found(tmp_path, monkeypatch):
    path = write_code_corpus(tmp_path)
    list_path = write_pmid_list(tmp_path, ("1", "999"))
    monkeypatch.setattr(ms, "CODE_SOURCES_PATH", path)
    result = ms.code_sources("999", pmid_list_path=list_path)
    assert result["status"] == "not_found"
    assert result["total_for_pmid"] == 0
    assert result["pmid_in_scope"] is True
    assert "not proof" in result["limitations"][0]


def test_code_sources_rejects_bad_pmid_and_arbitrary_path(tmp_path, monkeypatch):
    path = write_code_corpus(tmp_path)
    list_path = write_pmid_list(tmp_path, ("1",))
    monkeypatch.setattr(ms, "CODE_SOURCES_PATH", path)
    assert ms.code_sources("abc", pmid_list_path=list_path)["status"] == "failed"
    assert ms.code_sources("1", page=0,
                           pmid_list_path=list_path)["status"] == "failed"
    assert ms.code_sources("1", page_size=10_000,
                           pmid_list_path=list_path)["status"] == "failed"
    elsewhere = tmp_path / "secret" / "code_sources.jsonl"
    elsewhere.parent.mkdir()
    elsewhere.write_text("{}\n", encoding="utf-8")
    blocked = ms.code_sources("1", path=str(elsewhere),
                              pmid_list_path=list_path)
    assert blocked["status"] == "failed"
    assert "frozen research/code_sources.jsonl" in blocked["error"]


def test_code_sources_refuses_out_of_scope_pmid_without_reading_corpus(
        tmp_path, monkeypatch):
    path = write_code_corpus(tmp_path)
    list_path = write_pmid_list(tmp_path, ("1",))
    monkeypatch.setattr(ms, "CODE_SOURCES_PATH", path)
    monkeypatch.setattr(
        ms, "_load_code_records",
        lambda *a, **k: pytest.fail("out-of-scope must not read the corpus"))
    # PMID 2 has a real extraction record, but the configured list excludes it.
    result = ms.code_sources("2", pmid_list_path=list_path)
    assert result["status"] == "out_of_scope"
    assert result["records"] == []
    assert result["total_for_pmid"] == 0
    assert result["pmid_in_scope"] is False
    assert "not in the configured inclusion list" in result["error"]
    assert "total_in_file" not in result
    assert result["scope"]["n_included_pmids"] == 1
    assert result["claims_verified"] is False


def test_code_sources_fails_closed_when_the_pmid_list_is_missing(
        tmp_path, monkeypatch):
    path = write_code_corpus(tmp_path)
    monkeypatch.setattr(ms, "CODE_SOURCES_PATH", path)
    monkeypatch.setattr(
        ms, "_load_code_records",
        lambda *a, **k: pytest.fail("no list must mean no corpus read"))
    missing = tmp_path / "no_such_list.txt"
    monkeypatch.delenv(SK.PMID_LIST_ENV, raising=False)
    # The defaults are exercised through the registry resolver, so this test
    # pins the fail-closed branch instead of whatever exists on this machine.
    monkeypatch.setattr(SK, "DEFAULT_PMID_LIST_CANDIDATES", (missing,))

    implicit = ms.code_sources("1")
    assert implicit["status"] == "failed"
    assert implicit["error"].startswith("pmid_list_unavailable:")
    assert "not found" in implicit["error"]
    assert implicit["records"] == []
    assert "fails closed" in implicit["limitations"][0]

    explicit = ms.code_sources("1", pmid_list_path=str(missing))
    assert explicit["status"] == "failed"
    assert explicit["records"] == []

    monkeypatch.setenv(SK.PMID_LIST_ENV, str(missing))
    env = ms.code_sources("1")
    assert env["status"] == "failed"
    assert SK.PMID_LIST_ENV in env["error"]
    assert env["records"] == []


def test_code_sources_reads_env_and_default_configured_list(tmp_path, monkeypatch):
    path = write_code_corpus(tmp_path)
    list_path = write_pmid_list(tmp_path, ("1",))
    monkeypatch.setattr(ms, "CODE_SOURCES_PATH", path)

    monkeypatch.setenv(SK.PMID_LIST_ENV, str(list_path))
    assert ms.code_sources("1")["status"] == "ok"

    monkeypatch.delenv(SK.PMID_LIST_ENV, raising=False)
    monkeypatch.setattr(SK, "DEFAULT_PMID_LIST_CANDIDATES", (list_path,))
    assert ms.code_sources("1")["status"] == "ok"


def test_code_sources_rejects_malformed_or_empty_pmid_lists(tmp_path, monkeypatch):
    path = write_code_corpus(tmp_path)
    monkeypatch.setattr(ms, "CODE_SOURCES_PATH", path)
    bad = tmp_path / "bad_list.txt"
    bad.write_text("1\nnot-a-pmid\n", encoding="utf-8")
    malformed = ms.code_sources("1", pmid_list_path=bad)
    assert malformed["status"] == "failed"
    assert "non-numeric PMID line" in malformed["error"]
    assert malformed["records"] == []

    empty = tmp_path / "empty_list.txt"
    empty.write_text("# no PMIDs\n\n", encoding="utf-8")
    blank = ms.code_sources("1", pmid_list_path=empty)
    assert blank["status"] == "failed"
    assert "empty" in blank["error"]


def test_code_sources_explicit_historical_list_is_marked_superseded(
        tmp_path, monkeypatch):
    path = write_code_corpus(tmp_path)
    monkeypatch.setattr(ms, "CODE_SOURCES_PATH", path)
    current = write_pmid_list(tmp_path, ("1", "2"))
    historical = write_pmid_list(tmp_path, ("1",),
                                 name="included_pmids_422.txt")
    current_sha = hashlib.sha256(current.read_bytes()).hexdigest()
    historical_sha = hashlib.sha256(historical.read_bytes()).hexdigest()
    monkeypatch.setattr(SK, "KNOWN_PMID_LIST_SHA256", current_sha)
    monkeypatch.setattr(SK, "KNOWN_PMID_LIST_SNAPSHOTS", {
        current_sha: {"label": "included_pmids_405", "status": "current"},
        historical_sha: {
            "label": "included_pmids_422", "status": "superseded",
            "superseded": "2026-11-13",
            "superseded_by": "included_pmids_405",
        },
    })
    result = ms.code_sources("1", pmid_list_path=historical)
    assert result["status"] == "ok"
    assert result["scope"]["pmid_list_matches_known"] is False
    assert result["scope"]["pmid_list_known_snapshot"]["status"] == "superseded"
    assert any("superseded" in note for note in result["limitations"])


def test_code_sources_custom_list_is_used_but_reported_as_unrecognised(
        tmp_path, monkeypatch):
    path = write_code_corpus(tmp_path)
    monkeypatch.setattr(ms, "CODE_SOURCES_PATH", path)
    custom = write_pmid_list(tmp_path, ("1",), name="custom_scope.txt")
    result = ms.code_sources("1", pmid_list_path=custom)
    assert result["status"] == "ok"
    assert result["scope"]["pmid_list_matches_known"] is False
    assert result["scope"]["pmid_list_known_snapshot"] is None
    assert any("does not match the recorded current snapshot" in note
               for note in result["limitations"])


def test_real_code_sources_corpus_is_readable_without_modification():
    if not ms.CODE_SOURCES_PATH.exists():
        pytest.skip("frozen code_sources.jsonl unavailable")
    try:
        scope = MR.read_pmid_list(None)
    except (FileNotFoundError, ValueError) as exc:
        pytest.skip(str(exc))
    if "32997960" not in scope["pmid_set"]:
        pytest.skip("the configured inclusion list does not contain 32997960")
    before = hashlib.sha256(ms.CODE_SOURCES_PATH.read_bytes()).hexdigest()
    result = ms.code_sources("32997960", page=1, page_size=5,
                             pmid_list_path=scope["path"])
    after = hashlib.sha256(ms.CODE_SOURCES_PATH.read_bytes()).hexdigest()
    assert before == after
    assert result["status"] in ("ok", "not_found"), result
    assert result["pmid_in_scope"] is True
    assert result["scope"]["n_included_pmids"] >= 1
    assert result["total_in_file"] > 1000
    if result["status"] == "ok":
        assert all(r["verified"] is False for r in result["records"])



def test_code_sources_out_of_scope_answer_survives_the_tool_boundary(tmp_path):
    list_path = tmp_path / "included_pmids_405.txt"
    list_path.write_text("32997960\n", encoding="utf-8")
    out = invoke_tool("sctda_code_sources", {
        "pmid": "1", "pmid_list_path": str(list_path),
    })
    assert out["status"] == "out_of_scope", out
    assert out["records"] == []
    assert out["pmid_in_scope"] is False
    assert out["scope"]["pmid_list_path"] == str(list_path)


def test_code_sources_missing_list_fails_closed_at_the_tool_boundary(tmp_path):
    out = invoke_tool("sctda_code_sources", {
        "pmid": "32997960",
        "pmid_list_path": str(tmp_path / "missing.txt"),
    })
    assert out["status"] == "failed", out
    assert out["error"].startswith("pmid_list_unavailable:")
    assert out["records"] == []


def test_fetch_requires_an_initialised_project(tmp_path, monkeypatch):
    fresh = tmp_path / "never_imported"
    monkeypatch.setattr(ms, "_open_url",
                        lambda *a, **k: pytest.fail("uninitialised project must not fetch"))
    doc = ms.fetch_source(str(fresh), "https://satijalab.org/seurat/", "doc")
    paper = ms.fetch_paper(str(fresh), "PMC123")
    assert doc["status"] == "failed"
    assert paper["status"] == "failed"
    assert "not an initialised project" in doc["error"]
    assert doc["recorded"] is False and paper["recorded"] is False
    assert not fresh.exists(), "a read/fetch refusal must not create a project tree"


def test_read_paths_do_not_create_a_project_tree(tmp_path):
    missing = tmp_path / "absent"
    assert ms.read_snapshot(str(missing), "src_" + "a" * 20)["status"] == "not_found"
    assert ms.verify_snapshot(str(missing), "src_" + "a" * 20)["status"] == "not_found"
    assert ms.list_snapshots(str(missing))["total"] == 0
    assert not missing.exists()


def test_tampered_cached_snapshot_is_not_replayed(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    fake_dns(monkeypatch)
    calls = []
    fake_open(monkeypatch, body=b"original body", calls=calls)
    first = ms.fetch_source(str(project), "https://satijalab.org/seurat/", "doc")
    Path(first["body_path"]).write_text("tampered", encoding="utf-8")
    second = ms.fetch_source(str(project), "https://satijalab.org/seurat/", "doc")
    assert len(calls) == 2, "a hash-mismatched cache must be re-fetched"
    assert second["status"] == "ok"
    assert Path(first["body_path"]).read_bytes() == b"original body"


def test_snapshot_provenance_is_also_in_literature_cache(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    fake_dns(monkeypatch)
    fake_open(monkeypatch, body=b"cache row body")
    fetched = ms.fetch_source(str(project), "https://satijalab.org/seurat/", "doc")
    conn = Project(project).connect()
    try:
        rows = conn.execute(
            "SELECT endpoint, requested_utc, http_status, response_sha256, "
            "body_path, notes FROM literature_cache").fetchall()
    finally:
        conn.close()
    row = next(r for r in rows if fetched["snapshot_id"] in (r["notes"] or ""))
    assert row["endpoint"] == "https://satijalab.org/seurat/"
    assert row["requested_utc"]
    assert row["http_status"] == 200
    assert row["response_sha256"] == fetched["response_sha256"]
    assert row["body_path"] == fetched["body_path"]
    assert Path(row["body_path"]).exists()


def test_paper_snapshot_lists_under_the_paper_fulltext_alias(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    fake_paper(monkeypatch)
    fetched = ms.fetch_paper(str(project), "PMC12345")
    listed = ms.list_snapshots(str(project), kind="paper_fulltext")
    assert listed["total"] == 1
    assert listed["snapshots"][0]["snapshot_id"] == fetched["snapshot_id"]
    assert listed["snapshots"][0]["pmcid"] == "PMC12345"
