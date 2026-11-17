"""Kernel-boundary tests for the project method ledger and source snapshots.

Every call goes through ``invoke_tool``, the same door the CLI, TUI and the DSH
plugin use. The tests stay offline: ledger writes need only an initialised
project, and source reads target ids that do not exist, which is a real
``not_found`` answer rather than a network attempt.

The property under test is the one the scope-methods worker depends on:
registration is a real write into an existing project, the derived wiring /
installation / execution states cannot be set from model text, and an
uninitialised directory is refused instead of being created.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from sctda.harness import TOOLS, invoke_tool, tool_manifest  # noqa: E402
from sctda.method_registry import read_pmid_list  # noqa: E402
from sctda.project import Project  # noqa: E402


LEDGER_WRITES = {
    "sctda_method_ledger_register",
    "sctda_method_ledger_alias_add",
    "sctda_method_ledger_source_add",
}
LEDGER_READS = {
    "sctda_method_ledger_list",
    "sctda_method_ledger_get",
    "sctda_method_ledger_sources",
}
SNAPSHOT_WRITES = {"sctda_source_fetch", "sctda_lit_fulltext"}
SOURCE_READS = {
    "sctda_source_read",
    "sctda_source_verify",
    "sctda_source_list",
}


def _initialised(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    Project(root).ensure()
    assert (root / "project.db").exists()
    return root


# ------------------------------------------------------------------- manifest
def test_the_ledger_and_source_tools_are_registered_with_true_write_flags():
    manifest = {entry["name"]: entry for entry in tool_manifest()}
    for name in LEDGER_WRITES | SNAPSHOT_WRITES:
        entry = manifest[name]
        assert entry["mutating"] is True, f"{name} hides a real write"
        assert entry["requires_project"] is True
        assert entry["requires_initialised_project"] is True, (
            f"{name} could create a project on its way in")
        assert entry["roles"] == ["worker", "lead"]
    for name in LEDGER_READS | SOURCE_READS:
        entry = manifest[name]
        assert entry["mutating"] is False, f"{name} claims a write it does not do"
        assert entry["requires_project"] is True
        assert entry["requires_initialised_project"] is False
        assert entry["roles"] == ["worker", "lead"]
    code = manifest["sctda_code_sources"]
    assert code["mutating"] is False
    assert code["requires_project"] is False


def test_probe_installation_is_deliberately_not_a_model_tool():
    # The ledger can record a real process probe, but a model must not get an
    # argv-execution surface through a "no shell" preset.
    assert "sctda_method_probe_install" not in TOOLS
    for name, spec in TOOLS.items():
        assert "probe_installation" not in spec.summary, name


def test_the_register_and_source_schemas_require_the_fields_the_prompt_names():
    register = TOOLS["sctda_method_ledger_register"].parameters
    assert register["name"]["required"] is True
    assert register["inputs"]["type"] == "array"
    assert register["prerequisites"]["type"] == "array"
    assert register["limitations"]["type"] == "array"

    source = TOOLS["sctda_method_ledger_source_add"].parameters
    assert source["ledger_id"]["required"] is True
    assert source["source_kind"]["required"] is True
    assert source["source_kind"]["enum"] == [
        "paper", "preprint", "package", "repository", "documentation",
        "database", "web", "other",
    ]
    assert source["match_kind"]["enum"] == ["metadata", "text_match", "snapshot_bytes"]


# ------------------------------------------------------ project gate and writes
def test_a_ledger_write_refuses_an_uninitialised_project_and_creates_nothing(tmp_path):
    root = tmp_path / "not-a-project"
    root.mkdir()
    out = invoke_tool("sctda_method_ledger_register",
                      {"project": str(root), "name": "AUCell"})
    assert out["status"] == "error"
    assert out["error_kind"] == "uninitialised_project"
    assert "never creates one" in out["error"]
    assert not (root / "project.db").exists(), "the gate created the project it refused"
    assert list(root.iterdir()) == [], "the gate left project directories behind"


def test_registration_is_idempotent_and_conflicts_are_refused(tmp_path):
    project = _initialised(tmp_path)
    payload = {
        "project": str(project),
        "name": "AUCell",
        "aliases": ["AUCell scoring"],
        "purpose": "per-cell gene-set activity",
        "inputs": ["expression matrix", "gene set"],
        "prerequisites": ["gene-set snapshot"],
        "limitations": ["AUCell is not UCell"],
    }
    first = invoke_tool("sctda_method_ledger_register", payload)
    assert first["status"] == "created"
    ledger_id = first["method"]["ledger_id"]

    repeat = invoke_tool("sctda_method_ledger_register", payload)
    assert repeat["status"] == "existing"
    assert repeat["method"]["ledger_id"] == ledger_id

    conflict = invoke_tool("sctda_method_ledger_register",
                           {**payload, "purpose": "something else"})
    assert conflict["status"] == "conflict"

    listing = invoke_tool("sctda_method_ledger_list",
                          {"project": str(project), "query": "aucell"})
    assert listing["status"] == "ok"
    assert listing["total"] == 1
    assert listing["methods"][0]["ledger_id"] == ledger_id


def test_an_unknown_ledger_id_is_not_found_not_an_exception(tmp_path):
    project = _initialised(tmp_path)
    out = invoke_tool("sctda_method_ledger_get",
                      {"project": str(project), "identifier": "ml_missing"})
    assert out["status"] == "not_found"
    assert out["found"] is False
    assert "not found" in out["error"]

    sources = invoke_tool("sctda_method_ledger_sources",
                          {"project": str(project), "ledger_id": "ml_missing"})
    assert sources["status"] == "not_found"


def test_derived_states_are_computed_and_cannot_be_asserted(tmp_path):
    project = _initialised(tmp_path)
    registered = invoke_tool("sctda_method_ledger_register", {
        "project": str(project),
        "name": "AUCell",
        "adapter_method_id": "signature_score_aucell",
    })
    assert registered["status"] == "created"
    # A caller cannot smuggle installation/execution/support through the
    # registration payload: those parameters do not exist.
    schemas = TOOLS["sctda_method_ledger_register"].parameters
    for forbidden in ("support", "support_status", "installed", "installation",
                      "executed", "execution", "wiring"):
        assert forbidden not in schemas

    detail = invoke_tool("sctda_method_ledger_get", {
        "project": str(project),
        "identifier": registered["method"]["ledger_id"],
    })
    assert detail["status"] == "ok"
    assert detail["found"] is True
    assert detail["wiring"]["status"] == "wired"
    assert detail["wiring"]["adapter_method_id"] == "signature_score_aucell"
    assert detail["installation"]["status"] == "unprobed"
    assert detail["execution"]["status"] == "not_run"
    assert "purpose" in detail["missing"]
    assert detail["note"].startswith("derived statuses")


def test_an_unwired_adapter_id_is_a_validation_error_not_a_wired_record(tmp_path):
    project = _initialised(tmp_path)
    out = invoke_tool("sctda_method_ledger_register", {
        "project": str(project),
        "name": "CellChat-ish communication",
        "adapter_method_id": "de_vs_rest_wilcox_like",
    })
    assert out["status"] == "error"
    assert out["error_kind"] == "validation"


def test_alias_and_source_appends_keep_history(tmp_path):
    project = _initialised(tmp_path)
    registered = invoke_tool("sctda_method_ledger_register", {
        "project": str(project), "name": "AUCell",
    })
    ledger_id = registered["method"]["ledger_id"]

    alias = invoke_tool("sctda_method_ledger_alias_add", {
        "project": str(project), "ledger_id": ledger_id, "alias": "AUCell score",
    })
    assert alias["status"] == "created"
    repeat = invoke_tool("sctda_method_ledger_alias_add", {
        "project": str(project), "ledger_id": ledger_id, "alias": "AUCell score",
    })
    assert repeat["status"] == "existing"

    source = invoke_tool("sctda_method_ledger_source_add", {
        "project": str(project),
        "ledger_id": ledger_id,
        "source_kind": "documentation",
        "external_id": "https://satijalab.org/seurat/reference/findmarkers",
        "locator": "section 'Value'",
        "quote": "AUCell scores the activity of gene sets",
        "match_kind": "text_match",
    })
    assert source["status"] == "created"
    assert source["source"]["semantic_verification"] == "not_performed"

    sources = invoke_tool("sctda_method_ledger_sources", {
        "project": str(project), "ledger_id": ledger_id,
    })
    assert sources["status"] == "ok"
    assert sources["total"] >= 1
    assert all(item["semantic_verification"] == "not_performed"
               for item in sources["sources"])


# --------------------------------------------------------------------- sources
def test_snapshot_read_and_verify_are_honest_about_unknown_ids(tmp_path):
    project = _initialised(tmp_path)
    for tool in ("sctda_source_read", "sctda_source_verify"):
        out = invoke_tool(tool, {"project": str(project), "snapshot_id": "src_missing"})
        assert out["status"] == "not_found", (tool, out)
    listing = invoke_tool("sctda_source_list", {"project": str(project)})
    assert listing["status"] == "ok"
    assert listing["total"] == 0


def test_a_source_without_any_checkable_reference_is_refused(tmp_path):
    project = _initialised(tmp_path)
    registered = invoke_tool("sctda_method_ledger_register", {
        "project": str(project), "name": "AUCell",
    })
    ledger_id = registered["method"]["ledger_id"]
    out = invoke_tool("sctda_method_ledger_source_add", {
        "project": str(project),
        "ledger_id": ledger_id,
        "source_kind": "documentation",
        "title": "Some page",
        "query": "AUCell tutorial",
    })
    assert out["status"] == "error"
    assert out["error_kind"] == "validation"


def test_source_read_never_invents_text_for_an_unknown_snapshot(tmp_path):
    project = _initialised(tmp_path)
    out = invoke_tool("sctda_source_read", {
        "project": str(project), "snapshot_id": "src_missing", "limit": 10,
    })
    assert out["status"] == "not_found"
    assert "text" not in out


def test_code_sources_reads_the_frozen_file_and_marks_records_unverified():
    try:
        scope = read_pmid_list(None)
    except (FileNotFoundError, ValueError) as exc:
        pytest.skip(str(exc))
    if "32997960" not in scope["pmid_set"]:
        pytest.skip("the configured inclusion list does not contain 32997960")
    out = invoke_tool("sctda_code_sources", {
        "pmid": "32997960", "page_size": 5, "pmid_list_path": scope["path"],
    })
    assert out["status"] in ("ok", "not_found"), out
    assert out["pmid_in_scope"] is True
    assert out["scope"]["n_included_pmids"] >= 1
    assert out["claims_verified"] is False
    if out["status"] == "ok":
        assert out["file_sha256"]
        for record in out["records"]:
            assert record["verified"] is False
            assert record["verification"] == "unchecked"
