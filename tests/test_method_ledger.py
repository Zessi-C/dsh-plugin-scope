"""Behaviour tests for the project-level method ledger.

The ledger has no dependency on an imported dataset or a research run, so
these tests open bare project directories. They test the states the design
insists must never be self-reported: wiring comes from ``catalog.EXECUTABLE``,
installation only from a process this module actually started, and execution
only from ``task(status='succeeded')`` plus an ``artifact`` row. They are not a
biological benchmark and they never write the frozen corpus.
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from sctda import catalog
from sctda import method_ledger as L
from sctda.project import Project, get_or_create_project, now, sha256_file

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def root(tmp_path):
    return Project(tmp_path / "ledger").ensure().root


def _register(root, name="FindMarkers (wilcox)", **overrides):
    payload = {
        "purpose": "differential expression of one group against the rest",
        "inputs": ["annotated Seurat object"],
        "prerequisites": ["Seurat"],
        "limitations": ["cell-level p-values ignore sample structure"],
    }
    payload.update(overrides)
    return L.register_method(root, name, **payload)


def _insert_task(root, method_id, *, version, status="succeeded",
                 artifacts=1):
    """Write a protocol/task/artifact triple through the ordinary schema."""
    conn = Project(root).connect()
    try:
        project_id = get_or_create_project(conn, root)
        protocol_id = f"pr{version}"
        conn.execute(
            """INSERT INTO protocol(protocol_id, project_id, version, status,
                       created_utc, plan_json, plan_sha256)
               VALUES (?,?,?,?,?,?,?)""",
            (protocol_id, project_id, version, "frozen", now(), "{}", "plan-hash"))
        task_id = f"task{version}"
        conn.execute(
            """INSERT INTO task(task_id, protocol_id, method_id, stage, status,
                       finished_utc)
               VALUES (?,?,?,?,?,?)""",
            (task_id, protocol_id, method_id, "de", status, now()))
        for index in range(artifacts):
            conn.execute(
                """INSERT INTO artifact(artifact_id, task_id, protocol_id, kind,
                           path, checksum, checksum_algo, created_utc)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (f"art{version}_{index}", task_id, protocol_id, "csv",
                 f"out{version}_{index}.csv", "sum", "md5", now()))
        conn.commit()
        return task_id
    finally:
        conn.close()


# ------------------------------------------------------------------ registration
def test_registration_persists_and_derives_defaults(root):
    first = _register(root, adapter_method_id="de_vs_rest_wilcox",
                      aliases=["Wilcox DE"])
    assert first["created"] is True
    method = first["method"]
    assert method["name"] == "FindMarkers (wilcox)"
    assert method["aliases"] == ["Wilcox DE"]
    assert method["inputs"] == ["annotated Seurat object"]
    assert method["prerequisites"] == ["Seurat"]
    assert method["limitations"] == ["cell-level p-values ignore sample structure"]
    assert method["wiring"]["status"] == "wired"
    assert method["wiring"]["adapter"] == "r/adapters.R::task_de_vs_rest_wilcox"
    assert method["installation"]["status"] == "unprobed"
    assert method["installation"]["probe"] is None
    assert method["execution"]["status"] == "not_run"
    assert method["missing"] == ["external_source"]
    assert method["provenance"]["level"] == "none"
    assert method["provenance"]["semantic_verification"] == "not_performed"

    # a separate connection is the recovery path
    reopened = Project(root).connect()
    try:
        row = reopened.execute(
            "SELECT name, purpose, adapter_method_id FROM method_ledger "
            "WHERE ledger_id=?", (method["ledger_id"],)).fetchone()
        assert row["name"] == method["name"]
        assert row["adapter_method_id"] == "de_vs_rest_wilcox"
    finally:
        reopened.close()

    again = _register(root, adapter_method_id="de_vs_rest_wilcox",
                      aliases=["Wilcox DE", "Wilcox rank sum"])
    assert again["created"] is False
    assert again["method"]["ledger_id"] == method["ledger_id"]
    assert set(again["method"]["aliases"]) == {"Wilcox DE", "Wilcox rank sum"}
    assert L.get_method(root, method["ledger_id"])["name"] == method["name"]


def test_registration_conflicts_and_validation_are_explicit(root):
    first = _register(root, name="Alpha", aliases=["Shared alias"])
    with pytest.raises(L.ConflictError):
        _register(root, name="Alpha", purpose="a different purpose")
    with pytest.raises(L.ConflictError):
        _register(root, name="Alpha", adapter_method_id="composition")
    with pytest.raises(L.ValidationError):
        _register(root, name="   ")
    with pytest.raises(L.ValidationError):
        _register(root, name="Bad adapter", adapter_method_id="not-in-catalog")
    with pytest.raises(L.ValidationError):
        L.register_method(root, "Bad inputs", inputs=17)

    _register(root, name="Beta")
    with pytest.raises(L.ConflictError):
        L.register_method(root, "Shared alias")  # an existing alias
    with pytest.raises(L.ConflictError):
        _register(root, name="Gamma", aliases=["Alpha"])
    with pytest.raises(L.ConflictError):
        _register(root, name="Delta", aliases=["Shared alias"])
    with pytest.raises(L.ConflictError):
        L.add_alias(root, first["method"]["ledger_id"], "Beta")

    created = L.add_alias(root, first["method"]["ledger_id"], "A-one")
    assert created["created"] is True
    repeated = L.add_alias(root, first["method"]["ledger_id"], "A-one")
    assert repeated["created"] is False
    assert "A-one" in repeated["method"]["aliases"]
    assert L.get_method(root, "a one")["ledger_id"] == first["method"]["ledger_id"]


def test_dedup_key_is_an_idempotent_import_handle(root):
    first = L.register_method(root, "Imported method", dedup_key="import:1")
    assert first["created"] is True
    again = L.register_method(root, "Imported method", dedup_key="import:1")
    assert again["created"] is False
    assert again["method"]["ledger_id"] == first["method"]["ledger_id"]

    with pytest.raises(L.ConflictError):
        L.register_method(root, "Different method", dedup_key="import:1")
    with pytest.raises(L.ConflictError):
        L.register_method(root, "Imported method", dedup_key="import:2")


def test_wiring_is_derived_from_catalog_not_stored(root):
    method = _register(root, adapter_method_id="composition")["method"]
    assert method["wiring"]["status"] == "wired"
    assert method["wiring"]["package"] is None
    assert method["wiring"]["adapter"] == "r/adapters.R::task_composition"

    unbound = _register(root, name="Descriptive only")["method"]
    assert unbound["wiring"]["status"] == "not_wired"
    assert unbound["missing"] == ["external_source", "adapter_binding"]

    conn = Project(root).connect()
    try:
        columns = {row["name"] for row in
                   conn.execute("PRAGMA table_info(method_ledger)")}
        foreign_tables = {row["table"] for row in
                          conn.execute("PRAGMA foreign_key_list(method_ledger)")}
    finally:
        conn.close()
    assert not columns & {"wired", "installed", "installation", "execution",
                          "support_status", "verified"}
    assert foreign_tables == {"project"}

    # A row written by an older build can name an adapter this build dropped.
    conn = Project(root).connect()
    try:
        conn.execute("UPDATE method_ledger SET adapter_method_id='retired_adapter' "
                     "WHERE ledger_id=?", (method["ledger_id"],))
        conn.commit()
    finally:
        conn.close()
    stale = L.get_method(root, method["ledger_id"])
    assert stale["wiring"]["status"] == "stale_binding"
    assert "retired_adapter" == stale["wiring"]["adapter_method_id"]

    # Registration refuses to assert a binding this build cannot wire.
    with pytest.raises(L.ValidationError):
        _register(root, name="Ghost", adapter_method_id="retired_adapter")
    assert catalog.EXECUTABLE["composition"]["adapter"]


# ----------------------------------------------------------------------- search
def test_search_pages_and_matches_name_alias_and_purpose(root):
    _register(root, name="Alpha", aliases=["first tool"], purpose="composition")
    _register(root, name="Beta", aliases=["second tool"],
              purpose="differential expression")
    _register(root, name="Gamma", aliases=["third tool"], purpose="cnv inference")

    page = L.search_methods(root, limit=2, offset=0)
    assert page["total"] == 3
    assert [m["name"] for m in page["methods"]] == ["Alpha", "Beta"]
    assert page["has_more"] is True
    assert page["next_offset"] == 2
    assert page["page"]["returned"] == 2

    tail = L.search_methods(root, limit=2, offset=2)
    assert [m["name"] for m in tail["methods"]] == ["Gamma"]
    assert tail["has_more"] is False
    assert tail["next_offset"] is None

    assert [m["name"] for m in L.search_methods(root, "first")["methods"]] == ["Alpha"]
    assert [m["name"] for m in L.search_methods(root, "DIFFERENTIAL")["methods"]] == ["Beta"]
    assert [m["name"] for m in L.search_methods(
        root, "cnv", adapter_method_id=None)["methods"]] == ["Gamma"]
    assert L.search_methods(root, "no-such-method")["total"] == 0

    for bad in (0, 201, True, -1, 1.5):
        with pytest.raises(L.ValidationError):
            L.search_methods(root, limit=bad)
    with pytest.raises(L.ValidationError):
        L.search_methods(root, offset=-1)
    with pytest.raises(L.ValidationError):
        L.search_methods(root, query=17)


def test_get_accepts_id_name_and_alias(root):
    method = _register(root, name="FindMarkers (wilcox)",
                       aliases=["Wilcox DE"])["method"]
    for identifier in (method["ledger_id"], "FindMarkers (wilcox)",
                       "findmarkers wilcox", "Wilcox DE", "wilcox-de"):
        assert L.get_method(root, identifier)["ledger_id"] == method["ledger_id"]
    with pytest.raises(L.NotFoundError):
        L.get_method(root, "FindMarkers")
    with pytest.raises(L.ValidationError):
        L.get_method(root, "")


# ---------------------------------------------------------------------- sources
def test_sources_are_append_only_with_versions_queries_and_snapshots(root):
    method = _register(root)["method"]
    first = L.add_source(
        root, method["ledger_id"], source_kind="paper", external_id="PMID:1",
        title="paper one", version="v1", query="FindMarkers wilcox",
        endpoint="https://example.invalid/paper", locator="methods:L10:S2",
        match_kind="metadata", retrieved_utc="2020-01-02T03:04:05Z")
    assert first["created"] is True
    assert first["source"]["record_version"] == 1
    assert first["source"]["version"] == "v1"
    assert first["source"]["query"] == "FindMarkers wilcox"
    assert first["source"]["locator"] == "methods:L10:S2"
    assert first["source"]["retrieved_utc"] == "2020-01-02T03:04:05+00:00"

    duplicate = L.add_source(
        root, method["ledger_id"], source_kind="paper", external_id="PMID:1",
        title="paper one", version="v1", query="FindMarkers wilcox",
        endpoint="https://example.invalid/paper", locator="methods:L10:S2",
        match_kind="metadata")
    assert duplicate["created"] is False
    assert duplicate["source"]["source_id"] == first["source"]["source_id"]
    assert duplicate["source"]["record_version"] == 1

    second = L.add_source(
        root, method["ledger_id"], source_kind="paper", external_id="PMID:1",
        version="v2", locator="methods:L12:S1", match_kind="metadata")
    assert second["source"]["record_version"] == 2

    snapshot = root / "snapshots" / "paper.txt"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("the method section text", encoding="utf-8")
    digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    third = L.add_source(
        root, method["ledger_id"], source_kind="paper", external_id="PMID:1",
        version="v3", locator="methods:L12:S1", snapshot_path="snapshots/paper.txt",
        expected_sha256=digest.upper(), match_kind="text_match",
        quote="we used FindMarkers with a Wilcoxon test")
    assert third["created"] is True
    assert third["source"]["record_version"] == 3
    assert third["source"]["snapshot"]["status"] == "verified"
    assert third["source"]["snapshot"]["recorded_sha256"] == digest
    assert third["source"]["snapshot"]["path"] == str(snapshot.resolve())

    page = L.list_sources(root, method["ledger_id"], limit=2, offset=0)
    assert page["total"] == 3
    assert [s["record_version"] for s in page["sources"]] == [3, 2]
    assert page["has_more"] is True
    older = L.list_sources(root, method["ledger_id"], limit=2, offset=2)
    assert [s["record_version"] for s in older["sources"]] == [1]
    assert older["has_more"] is False

    sliced = L.get_method(root, method["ledger_id"], source_limit=1,
                          source_offset=1)
    assert sliced["sources_total"] == 3
    assert [s["record_version"] for s in sliced["sources"]] == [2]
    assert sliced["sources_page"]["has_more"] is True
    assert sliced["sources_page"]["next_offset"] == 2

    # Snapshot checks are current at read time and can be skipped explicitly.
    detail = L.get_method(root, method["ledger_id"])
    latest = detail["sources"][0]
    assert latest["snapshot"]["status"] == "verified"
    assert detail["provenance"]["snapshot_checks"]["status_counts"] == {
        "absent": 2, "verified": 1}
    assert detail["provenance"]["snapshot_checks"]["page_truncated"] is False
    assert detail["external_source_ids"] == ["PMID:1"]
    assert "external_source" not in detail["missing"]
    assert L.get_method(root, method["ledger_id"], verify_snapshots=False)[
        "sources"][0]["snapshot"]["status"] == "not_checked"
    snapshot.write_text("changed bytes", encoding="utf-8")
    assert L.get_method(root, method["ledger_id"])["sources"][0][
        "snapshot"]["status"] == "drift"
    snapshot.unlink()
    assert L.get_method(root, method["ledger_id"])["sources"][0][
        "snapshot"]["status"] == "missing"


def test_snapshot_bytes_records_content_hash_not_semantics(root):
    method = _register(root)["method"]
    body = root / "body.txt"
    body.write_text("method text", encoding="utf-8")
    source = L.add_source(
        root, method["ledger_id"], source_kind="documentation",
        external_id="snapshot:1", version="1", snapshot_path="body.txt",
        match_kind="snapshot_bytes")["source"]
    assert source["verification_scope"] == "content_hash"
    assert source["semantic_verification"] == "not_performed"
    assert source["snapshot"]["status"] == "verified"
    detail = L.get_method(root, method["ledger_id"])
    assert detail["provenance"]["level"] == "snapshot_recorded"
    assert detail["provenance"]["n_snapshots"] == 1
    assert detail["provenance"]["semantic_verification"] == "not_performed"


def test_snapshot_must_exist_and_match_its_sha(root):
    method = _register(root)["method"]
    L.add_source(root, method["ledger_id"], source_kind="paper",
                 external_id="PMID:2", version="v1")
    before = L.list_sources(root, method["ledger_id"])["total"]

    with pytest.raises(L.ValidationError):
        L.add_source(root, method["ledger_id"], source_kind="paper",
                     external_id="PMID:2", version="v2",
                     snapshot_path="does-not-exist.txt", match_kind="metadata")
    with pytest.raises(L.ValidationError):
        L.add_source(root, method["ledger_id"], source_kind="paper",
                     external_id="PMID:2", version="v2",
                     snapshot_path="does-not-exist.txt", match_kind="snapshot_bytes")

    snapshot = root / "paper-v2.txt"
    snapshot.write_text("v2", encoding="utf-8")
    with pytest.raises(L.ValidationError):
        L.add_source(root, method["ledger_id"], source_kind="paper",
                     external_id="PMID:2", version="v2",
                     snapshot_path="paper-v2.txt",
                     expected_sha256="0" * 64, match_kind="metadata")
    with pytest.raises(L.ValidationError):
        L.add_source(root, method["ledger_id"], source_kind="paper",
                     external_id="PMID:2", version="v2",
                     snapshot_path="paper-v2.txt",
                     expected_sha256="not-a-hash", match_kind="metadata")
    with pytest.raises(L.ValidationError):
        L.add_source(root, method["ledger_id"], source_kind="paper",
                     external_id="PMID:2", version="v2",
                     expected_sha256="0" * 64, match_kind="metadata")
    assert L.list_sources(root, method["ledger_id"])["total"] == before


def test_text_match_is_not_semantic_or_install_or_execution_evidence(root):
    method = _register(root, adapter_method_id="de_vs_rest_wilcox")["method"]
    source = L.add_source(
        root, method["ledger_id"], source_kind="documentation",
        external_id="docs:pkg", version="1.0", locator="install section",
        match_kind="text_match",
        quote="The package is installed and the method ran successfully.")["source"]
    assert source["verification_scope"] == "text_present"
    assert source["semantic_verification"] == "not_performed"
    assert "installed" not in source
    assert "executed" not in source

    detail = L.get_method(root, method["ledger_id"])
    assert detail["installation"]["status"] == "unprobed"
    assert detail["execution"]["status"] == "not_run"
    assert detail["provenance"]["semantic_verification"] == "not_performed"
    assert detail["provenance"]["level"] == "text_match_only"

    for kind in ("install", "installed", "execution", "run"):
        with pytest.raises(L.ValidationError):
            L.add_source(root, method["ledger_id"], source_kind="paper",
                         external_id="PMID:1", match_kind=kind)
    with pytest.raises(L.ValidationError):
        L.add_source(root, method["ledger_id"], source_kind="paper",
                     external_id="PMID:1",
                     match_kind="text_match")  # a text match needs a quote
    with pytest.raises(L.ValidationError):
        L.add_source(root, method["ledger_id"], source_kind="paper",
                     query="only a query is not a reference")


def test_source_dedup_key_conflicts_instead_of_rewriting(root):
    method = _register(root)["method"]
    first = L.add_source(root, method["ledger_id"], source_kind="web",
                         external_id="https://example.invalid",
                         dedup_key="observation-1", version="v1")
    assert first["source"]["record_version"] == 1
    with pytest.raises(L.ConflictError):
        L.add_source(root, method["ledger_id"], source_kind="web",
                     external_id="https://example.invalid",
                     dedup_key="observation-1", version="v2")
    assert L.list_sources(root, method["ledger_id"])["total"] == 1

    appended = L.add_source(root, method["ledger_id"], source_kind="web",
                            external_id="https://example.invalid",
                            dedup_key="observation-2", version="v2")
    assert appended["source"]["record_version"] == 2


# ----------------------------------------------------------------- installation
def test_installation_changes_only_after_a_real_probe(root):
    method = _register(root, adapter_method_id="de_vs_rest_wilcox")["method"]
    assert L.get_method(root, method["ledger_id"])["installation"]["status"] == "unprobed"

    ok = L.probe_installation(root, method["ledger_id"],
                              [sys.executable, "-c", "print('ok')"], timeout=10)
    assert ok["probe"]["returncode"] == 0
    assert ok["probe"]["record_version"] == 1
    assert ok["probe"]["stdout_excerpt"] == "ok\n"
    assert ok["probe"]["stdout_sha256"] == hashlib.sha256(b"ok\n").hexdigest()
    assert ok["installation"]["status"] == "present"
    assert ok["installation"]["interpretation"] == (
        "return code of exactly the recorded argv")

    failed = L.probe_installation(
        root, method["ledger_id"],
        [sys.executable, "-c", "import sys; sys.exit(7)"], timeout=10)
    assert failed["probe"]["returncode"] == 7
    assert failed["probe"]["record_version"] == 2
    assert failed["installation"]["status"] == "absent"
    assert L.get_method(root, method["ledger_id"])["installation"]["status"] == "absent"

    missing = L.probe_installation(root, method["ledger_id"],
                                   [str(root / "no-such-executable")])
    assert missing["probe"]["launch_error"]
    assert missing["installation"]["status"] == "unavailable"

    slow = L.probe_installation(
        root, method["ledger_id"],
        [sys.executable, "-c", "import time; time.sleep(2)"], timeout=0.2)
    assert slow["probe"]["timed_out"] is True
    assert slow["installation"]["status"] == "unavailable"

    # A probe is not a method run: execution stays derived from task rows.
    assert L.get_method(root, method["ledger_id"])["execution"]["status"] == "not_run"


def test_probe_rejects_a_shell_string_and_bad_timeout(root):
    method = _register(root)["method"]
    with pytest.raises(L.ValidationError):
        L.probe_installation(root, method["ledger_id"], "echo ok")
    with pytest.raises(L.ValidationError):
        L.probe_installation(root, method["ledger_id"], [])
    with pytest.raises(L.ValidationError):
        L.probe_installation(root, method["ledger_id"], [sys.executable], timeout=0)
    with pytest.raises(L.ValidationError):
        L.probe_installation(root, method["ledger_id"], [sys.executable],
                             timeout=True)


# -------------------------------------------------------------------- execution
def test_execution_is_derived_from_succeeded_task_and_artifact_rows(root):
    method = _register(root, adapter_method_id="de_vs_rest_wilcox")["method"]
    detail = L.get_method(root, method["ledger_id"])
    assert detail["execution"]["status"] == "not_run"
    assert detail["execution"]["n_succeeded_tasks"] == 0

    _insert_task(root, "de_vs_rest_wilcox", version=1, status="failed",
                 artifacts=3)
    assert L.get_method(root, method["ledger_id"])["execution"]["status"] == "not_run"

    _insert_task(root, "de_vs_rest_wilcox", version=2, status="succeeded",
                 artifacts=0)
    partial = L.get_method(root, method["ledger_id"])["execution"]
    assert partial["status"] == "no_artifact"
    assert partial["n_succeeded_tasks"] == 1
    assert partial["n_succeeded_with_artifacts"] == 0
    assert partial["n_artifacts"] == 0
    assert partial["last_success"] is None

    last_task = _insert_task(root, "de_vs_rest_wilcox", version=3,
                             status="succeeded", artifacts=2)
    ran = L.get_method(root, method["ledger_id"])["execution"]
    assert ran["status"] == "ran"
    assert ran["n_succeeded_tasks"] == 2
    assert ran["n_succeeded_with_artifacts"] == 1
    assert ran["n_artifacts"] == 2
    assert ran["last_success"]["task_id"] == last_task
    assert len(ran["artifacts"]) == 2
    assert ran["derived_from"] == "protocol.task(status='succeeded') + artifact rows"
    assert ran["artifact_file_check"] == "not_performed"

    listed = L.search_methods(root, "FindMarkers")["methods"][0]
    assert listed["execution"]["status"] == "ran"
    assert listed["execution"]["n_succeeded_tasks"] == 2


def test_execution_does_not_count_another_projects_tasks(root, tmp_path):
    method = _register(root, adapter_method_id="de_vs_rest_wilcox")["method"]
    other = Project(tmp_path / "other").ensure().root
    _insert_task(other, "de_vs_rest_wilcox", version=1, status="succeeded",
                 artifacts=2)
    assert L.get_method(root, method["ledger_id"])["execution"]["status"] == "not_run"


# -------------------------------------------------------------------- isolation
def test_ledger_rows_sources_and_probes_are_project_scoped(root, tmp_path):
    method = _register(root)["method"]
    L.add_source(root, method["ledger_id"], source_kind="paper",
                 external_id="PMID:7", version="v1")
    L.probe_installation(root, method["ledger_id"],
                         [sys.executable, "-c", "print('ok')"])

    other = Project(tmp_path / "isolated").ensure().root
    assert L.search_methods(other)["total"] == 0
    with pytest.raises(L.NotFoundError):
        L.get_method(other, method["ledger_id"])
    with pytest.raises(L.NotFoundError):
        L.list_sources(other, method["ledger_id"])
    with pytest.raises(L.NotFoundError):
        L.add_source(other, method["ledger_id"], source_kind="paper",
                     external_id="PMID:8")
    marker = other / "should-not-exist"
    with pytest.raises(L.NotFoundError):
        L.probe_installation(other, method["ledger_id"],
                             [sys.executable, "-c",
                              f"open({str(marker)!r}, 'w').write('ran')"])
    assert not marker.exists()

    # The composite foreign key refuses a source row that pairs another
    # project's id with this project's ledger id.
    conn = Project(other).connect()
    try:
        project_id = get_or_create_project(conn, other)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO method_ledger_source(source_id, project_id,
                       ledger_id, record_version, source_kind, match_kind,
                       retrieved_utc, dedup_key, created_utc)
                   VALUES ('x',?,?,1,'paper','metadata',?,'x',?)""",
                (project_id, method["ledger_id"], now(), now()))
        conn.rollback()
    finally:
        conn.close()


def test_concurrent_registration_and_appends_stay_consistent(root):
    def register(_: int) -> dict:
        return _register(root, name="Concurrent method")

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(register, range(8)))
    assert len({o["method"]["ledger_id"] for o in outcomes}) == 1
    assert sum(1 for o in outcomes if o["created"]) == 1
    ledger_id = outcomes[0]["method"]["ledger_id"]
    assert L.search_methods(root, "Concurrent")["total"] == 1

    def append(index: int) -> dict:
        return L.add_source(root, ledger_id, source_kind="web",
                            external_id=f"https://example.invalid/{index}",
                            dedup_key=f"concurrent-{index}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        sources = list(pool.map(append, range(8)))
    versions = sorted(s["source"]["record_version"] for s in sources)
    assert versions == list(range(1, 9))
    page = L.list_sources(root, ledger_id, limit=20)
    assert page["total"] == 8
    assert [s["record_version"] for s in page["sources"]] == list(range(8, 0, -1))


# ------------------------------------------------------------------ boundaries
def test_missing_arguments_and_cross_references_raise(root):
    method = _register(root)["method"]
    with pytest.raises(L.NotFoundError):
        L.list_sources(root, "mled_does_not_exist")
    with pytest.raises(L.NotFoundError):
        L.add_source(root, "mled_does_not_exist", source_kind="paper",
                     external_id="PMID:1")
    with pytest.raises(L.ValidationError):
        L.add_source(root, method["ledger_id"], source_kind="not-a-kind")
    with pytest.raises(L.ValidationError):
        L.list_sources(root, method["ledger_id"], limit=0)
    with pytest.raises(L.ValidationError):
        L.get_method(root, method["ledger_id"], verify_snapshots="yes")


def test_the_frozen_corpus_is_not_written(root):
    corpus = ROOT / "research" / "corpus" / "methods.jsonl"
    if not corpus.exists():
        pytest.skip("frozen corpus not present")
    before = sha256_file(corpus)
    method = _register(root, adapter_method_id="de_vs_rest_wilcox")["method"]
    L.add_source(root, method["ledger_id"], source_kind="paper",
                 external_id="PMID:3", version="v1", query="inspect")
    L.get_method(root, method["ledger_id"])
    L.search_methods(root, "FindMarkers")
    assert sha256_file(corpus) == before
