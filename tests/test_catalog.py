"""Behaviour tests for the method catalog.

The catalog is a thin, honest adapter over two authorities:

* ``EXECUTABLE`` (this module): the four wired adapters and nothing else;
* ``method_registry``: the 405-scoped corpus index and the explicit
  adapter-to-instance mapping.

These tests use a fake registry for the contract and, when the real registry is
installed, one integration check that the 405/retraction filter and the
AUCell/UCell, inferCNV/CopyKAT distinctions hold on the frozen corpus.
They do not run the R adapters.
"""
from __future__ import annotations

import json
import re
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import sctda.catalog as catalog  # noqa: E402


# --------------------------------------------------------------------- fakes
class FakeRegistry:
    """Contract-shaped registry; records every adapter-name lookup."""

    def __init__(self, methods=None, adapters=None, backing=None,
                 search_error=None):
        self.methods = list(methods or [])
        self.adapters = dict(adapters or {})
        self.backing = dict(backing or {})
        self.search_error = search_error
        self.search_calls: list[dict] = []
        self.adapter_calls: list[str] = []

    def registry_search(self, query=None, stages=None, limit=20, offset=0,
                        kinds=None):
        if self.search_error:
            raise self.search_error
        self.search_calls.append({"query": query, "stages": stages,
                                  "limit": limit, "offset": offset,
                                  "kinds": kinds})
        rows = self.methods
        if kinds:
            wanted = {kinds} if isinstance(kinds, str) else set(kinds)
            rows = [r for r in rows
                    if (r.get("kind") or "literature_method") in wanted]
        if query:
            needle = query.lower()
            rows = [r for r in rows
                    if needle in json.dumps(r, ensure_ascii=False).lower()]
        if stages:
            wanted = set(stages)
            rows = [r for r in rows
                    if wanted & set(r.get("stages") or [])]
        total = len(rows)
        page = rows[offset:offset + limit]
        end = offset + len(page)
        return {
            "total": total, "offset": offset, "limit": limit,
            "returned": len(page), "has_more": end < total,
            "next_offset": end if end < total else None,
            "methods": deepcopy(page),
            "scope": {"n_included_pmids": 405, "n_effective_pmids": 404,
                      "retracted_pmids": ["36450980"]},
            "corpus": {"sha256": "0" * 64, "n_records_qualifying": 6646},
            "incomplete": True,
            "notes": ["extraction is incomplete"],
        }

    def registry_stats(self):
        return {
            "schema": "sctda.method_registry.stats/1",
            "scope": {
                "pmid_list_path": "/data/included_pmids_405.txt",
                "pmid_list_sha256": "f" * 64,
                "pmid_list_matches_known": True,
                "n_included_pmids": 405,
                "n_effective_pmids": 404,
                "retracted_pmids": ["36450980"],
            },
            "counts": {"n_methods_literature": len(self.methods)},
            "incomplete": True,
            "notes": ["extraction is incomplete"],
        }

    def registry_adapters_for(self, canonical_name):
        self.adapter_calls.append(canonical_name)
        return deepcopy(self.adapters.get(canonical_name, []))

    def registry_backing(self, method_id, limit=None):
        if method_id in self.backing:
            return deepcopy(self.backing[method_id])
        return {"method_id": method_id, "found": False, "quality": "not_found",
                "instances": [], "n_instances": 0, "n_papers": 0,
                "excluded_related": [], "mapped_canonical_names": [],
                "reason": "no backing in this fake"}

    def load_registry(self, include_evidence=False):
        return {"methods": deepcopy(self.methods), "stats": {}, "scope": {},
                "incomplete": True}


def record(name, pmid="32997960", n_instances=1, n_papers=1, stages=None,
           pmids=None, examples=None):
    return {
        "registry_id": f"m:{name}",
        "canonical_name": name,
        "n_instances": n_instances,
        "n_papers": n_papers,
        "pmids": pmids or [pmid],
        "stages": stages or ["enrichment"],
        "packages": ["AUCell"],
        "in_scope": True,
        "method_class": "computational",
        "rerunnable_roles": ["rerunnable"],
        "examples": examples or [{"pmid": pmid, "locator": "methods:L1:P1:S1",
                                  "operation": "score", "verbatim": "quote"}],
    }


@pytest.fixture
def fake(monkeypatch):
    def install(registry):
        monkeypatch.setattr(catalog, "_registry", lambda: registry)
        return registry
    return install


# ------------------------------------------------------------- executable layer
def test_executable_is_exactly_the_four_wired_adapters():
    assert list(catalog.EXECUTABLE) == [
        "composition", "de_vs_rest_wilcox",
        "signature_score_aucell", "cnv_infercnv"]
    assert catalog.get_method("cnv_infercnv") is catalog.EXECUTABLE["cnv_infercnv"]
    assert catalog.get_method("ucell") is None


def test_cnv_entry_does_not_claim_copykat_as_executable():
    cnv = catalog.EXECUTABLE["cnv_infercnv"]
    assert cnv["package"] == "infercnv"
    assert "CopyKAT" not in cnv["name"]
    assert "CopyKAT" not in cnv["adapter"]
    notes = " ".join(cnv["statistical_notes"]).lower()
    assert "copykat" in notes and "not an executable alternative" in notes


def test_every_executable_anchor_resolves_in_frozen_snapshot():
    snapshot = ROOT / "research/brief_snapshot"
    if not snapshot.is_dir() or not any(snapshot.glob("*.txt")):
        pytest.skip("frozen brief snapshot unavailable")
    sys.path.insert(0, str(ROOT / "tools"))
    from brief_index import parse_brief

    list_path = Path("/data/disk1/zeiss/singlecell-quant/data/included_pmids_405.txt")
    scope_pmids = ({line.strip() for line in
                    list_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()} if list_path.exists() else set())

    checked = 0
    for method_id, spec in catalog.EXECUTABLE.items():
        refs = spec.get("literature_refs") or []
        assert refs, f"{method_id} has no hand anchor"
        seen: set[str] = set()
        for ref in refs:
            assert set(ref) >= {"pmid", "locator", "verbatim", "note"}
            if scope_pmids:
                assert ref["pmid"] in scope_pmids, (
                    f"{method_id}: anchor PMID {ref['pmid']} is outside the 405 scope")
                assert ref["pmid"] != "36450980", "retracted PMID cannot be an anchor"
            assert ref["locator"] not in seen, f"{method_id}: duplicate locator"
            seen.add(ref["locator"])
            path = snapshot / f"{ref['pmid']}.txt"
            assert path.exists(), f"{method_id}: brief missing for {ref['pmid']}"
            rows = list(parse_brief(path))
            hit = [r for r in rows if r[5] == ref["locator"]]
            assert hit, f"{method_id}: {ref['pmid']} {ref['locator']} does not resolve"
            section, _, _, _, sentence, _ = hit[0]
            assert ref["verbatim"] in sentence, (
                f"{method_id}: verbatim is not in {ref['locator']}")
            assert ref["locator"].split(":", 1)[0] == section
            checked += 1
    assert checked >= 5


def test_no_fuzzy_backing_pattern_table_remains():
    assert not hasattr(catalog, "BACKING_PATTERNS")


# --------------------------------------------------------------- discovery page
def test_discovered_methods_uses_registry_and_pages_the_whole_index(fake):
    rows = [record("AUCell"), record("inferCNV"), record("UCell")]
    fake(FakeRegistry(methods=rows))
    out = catalog.discovered_methods()
    assert [r["canonical_name"] for r in out] == ["AUCell", "inferCNV", "UCell"]
    first = out[0]
    for key in ("canonical_name", "n_instances", "n_papers", "pmids", "stages",
                "packages", "in_scope", "examples"):
        assert key in first
    assert first["registry_id"] == "m:AUCell"
    assert first["examples"][0]["locator"] == "methods:L1:P1:S1"


def test_discovered_methods_honours_offset_and_limit(fake):
    reg = fake(FakeRegistry(methods=[record(f"M{i}") for i in range(5)]))
    out = catalog.discovered_methods(limit=2, offset=1)
    assert [r["canonical_name"] for r in out] == ["M1", "M2"]
    assert reg.search_calls[0]["offset"] == 1
    assert reg.search_calls[0]["limit"] == 2


def test_discovered_page_excludes_executable_adapter_records(fake):
    adapter = record("per-sample cell composition")
    adapter.update({"kind": "executable_adapter", "in_scope": None,
                    "method_class": None})
    fake(FakeRegistry(methods=[record("AUCell"), adapter]))
    out = catalog.catalog_summary()
    assert [r["canonical_name"] for r in out["discovered"]] == ["AUCell"]
    assert out["n_discovered"] == 1
    assert {r["kind"] for r in out["discovered"]} == {"literature_method"}

    out2 = catalog.discovered_methods()
    assert [r["canonical_name"] for r in out2] == ["AUCell"]


def test_discovered_instances_flattens_evidence_and_skips_adapters(fake):
    rec = record("AUCell")
    rec["evidence"] = [
        {"instance_id": "32997960:methods:L1:P1:S1", "pmid": "32997960",
         "locator": "methods:L1:P1:S1", "verbatim": "quote",
         "canonical_name": "AUCell", "operation": "score"},
        {"instance_id": "32997960:methods:L2:P1:S1", "pmid": "32997960",
         "locator": "methods:L2:P1:S1", "verbatim": "quote2",
         "canonical_name": "AUCell", "operation": "score"},
    ]
    adapter = record("composition")
    adapter["kind"] = "executable_adapter"
    adapter["evidence"] = [{"instance_id": "adapter:composition", "pmid": None}]
    fake(FakeRegistry(methods=[rec, adapter]))
    out = catalog.discovered_instances()
    assert [i["instance_id"] for i in out] == [
        "32997960:methods:L1:P1:S1", "32997960:methods:L2:P1:S1"]
    assert len(catalog.discovered_instances(limit=1, offset=1)) == 1


def test_discovered_methods_rejects_untraceable_records(fake):
    fake(FakeRegistry(methods=[{"n_instances": 1}]))
    with pytest.raises(catalog.CatalogContractError):
        catalog.discovered_methods()


# --------------------------------------------------------------- support mapping
def test_supported_by_detail_is_exact_registry_mapping(fake):
    reg = fake(FakeRegistry(adapters={
        "AUCell": [{"method_id": "signature_score_aucell", "quality": "exact",
                    "wired_operation": "AUCell"}],
        "Seurat": [{"method_id": "de_vs_rest_wilcox", "quality": "toolkit",
                    "wired_operation": "Seurat::FindMarkers (wilcox)"}],
        "Ghost": [{"method_id": "not_shipped", "quality": "exact",
                   "wired_operation": "ghost"}],
    }))
    assert catalog.supported_by("AUCell") == ["signature_score_aucell"]
    assert catalog.supported_by("Seurat") == ["de_vs_rest_wilcox"]
    # The registry says no mapping for UCell; catalog must not search substrings.
    assert catalog.supported_by_detail("UCell") == []
    assert catalog.supported_by_detail("CopyKAT") == []
    # A mapping to an id this build does not ship is dropped, not surfaced.
    assert catalog.supported_by_detail("Ghost") == []
    assert reg.adapter_calls == ["AUCell", "Seurat", "UCell", "CopyKAT", "Ghost"]


def test_supported_by_detail_keeps_only_declared_quality_values(fake):
    fake(FakeRegistry(adapters={
        "AUCell": [{"method_id": "signature_score_aucell", "quality": "fuzzy"}],
    }))
    with pytest.raises(catalog.CatalogContractError):
        catalog.supported_by_detail("AUCell")


# --------------------------------------------------------------- backing verdict
def test_literature_backing_returns_only_mapped_instances(fake):
    fake(FakeRegistry(backing={
        "signature_score_aucell": {
            "method_id": "signature_score_aucell", "found": True, "quality": "exact",
            "n_instances": 2, "n_papers": 2,
            "mapped_canonical_names": ["AUCell"],
            "instances": [
                {"pmid": "1", "locator": "methods:L1:P1:S1",
                 "canonical_name": "AUCell", "operation": "score"},
                {"pmid": "2", "locator": "methods:L2:P1:S1",
                 "canonical_name": "AUCell", "operation": "score"},
            ],
            "excluded_related": [{"name": "UCell", "reason": "different algorithm",
                                  "n_papers": 21}],
            "reason": "exact canonical name",
        },
        "composition": {
            "method_id": "composition", "found": False, "quality": "local_adapter",
            "n_instances": 0, "n_papers": 0, "instances": [],
            "excluded_related": [], "mapped_canonical_names": [],
            "reason": "no corpus instance records per-sample composition by name",
        },
    }))
    groups = catalog.literature_backing("signature_score_aucell")
    assert [g["pmid"] for g in groups] == ["1", "2"]
    assert groups[0]["examples"][0]["canonical_name"] == "AUCell"
    detail = catalog.literature_backing("signature_score_aucell", detail=True)
    assert detail["found"] is True and detail["quality"] == "exact"
    # UCell is reported as excluded context, never promoted into backing.
    assert [x["name"] for x in detail["excluded_related"]] == ["UCell"]
    assert all(g["pmid"] != "ucell" for g in detail["groups"])

    assert catalog.literature_backing("composition") == []
    verdict = catalog.literature_backing("composition", detail=True)
    assert verdict["found"] is False and verdict["quality"] == "local_adapter"
    assert "no corpus instance" in verdict["reason"]


def test_literature_backing_does_not_upgrade_not_found_with_instances(fake):
    fake(FakeRegistry(backing={
        "cnv_infercnv": {
            "method_id": "cnv_infercnv", "found": False, "quality": "not_found",
            "n_instances": 1, "n_papers": 1,
            "instances": [{"pmid": "9", "locator": "methods:L9:P1:S1",
                           "canonical_name": "CopyKAT", "operation": "cnv"}],
            "excluded_related": [], "mapped_canonical_names": [], "reason": "mismatch",
        },
    }))
    verdict = catalog.literature_backing("cnv_infercnv", detail=True)
    assert verdict["found"] is False
    assert catalog.literature_backing("cnv_infercnv") == []


def test_literature_backing_unknown_id_is_explicitly_not_found(fake):
    fake(FakeRegistry())
    assert catalog.literature_backing("ucell") == []
    verdict = catalog.literature_backing("ucell", detail=True)
    assert verdict["found"] is False and verdict["quality"] == "not_found"
    assert "not one of the four executable adapters" in verdict["reason"]


def test_literature_backing_is_bounded(fake):
    instances = [{"pmid": str(i), "locator": f"methods:L{i}:P1:S1",
                  "canonical_name": "AUCell", "operation": "score"}
                 for i in range(150)]
    fake(FakeRegistry(backing={
        "signature_score_aucell": {
            "method_id": "signature_score_aucell", "found": True, "quality": "exact",
            "n_instances": 150, "n_papers": 150, "instances": instances,
            "excluded_related": [], "mapped_canonical_names": ["AUCell"],
            "reason": "exact",
        },
    }))
    groups = catalog.literature_backing("signature_score_aucell", limit=3)
    assert len(groups) == 3
    verdict = catalog.literature_backing("signature_score_aucell", limit=3, detail=True)
    assert verdict["n_groups"] == 150 and verdict["groups_truncated"] is True
    assert len(verdict["instances"]) == catalog.DETAIL_INSTANCE_MAX
    assert verdict["instances_truncated"] is True


# ------------------------------------------------------------------- summary
def test_catalog_summary_pages_and_reports_totals(fake):
    fake(FakeRegistry(methods=[record(f"M{i}") for i in range(200)]))
    out = catalog.catalog_summary()
    assert out["n_executable"] == 4
    assert len(out["discovered"]) == catalog.DISCOVERED_PAGE_DEFAULT
    assert out["n_discovered"] == 200
    assert out["discovered_total"] == 200
    assert out["page"]["has_more"] is True
    assert out["page"]["next_offset"] == catalog.DISCOVERED_PAGE_DEFAULT
    assert out["truncated"] is True
    assert out["has_more"] is True
    assert out["next_offset"] == catalog.DISCOVERED_PAGE_DEFAULT
    assert out["limit"] == catalog.DISCOVERED_PAGE_DEFAULT
    assert out["incomplete"] is True
    assert out["status"] == "ok"
    assert out["scope"]["n_included_pmids"] == 405
    assert out["scope"]["retracted_pmids"] == ["36450980"]
    assert out["scope"]["pmid_list_sha256"] == "f" * 64
    assert out["scope"]["corpus_sha256"] == "0" * 64
    assert out["registry"]["stats"]["counts"]["n_methods_literature"] == 200


def test_catalog_summary_caps_an_oversized_limit(fake):
    fake(FakeRegistry(methods=[record(f"M{i}") for i in range(800)]))
    out = catalog.catalog_summary(limit=10 ** 6)
    assert out["page"]["limit"] == catalog.DISCOVERED_PAGE_MAX
    assert out["page"]["limit_clamped"] is True
    assert len(out["discovered"]) == catalog.DISCOVERED_PAGE_MAX


def test_catalog_summary_response_is_bounded(fake):
    rows = []
    for i in range(400):
        rows.append(record(
            f"M{i}", pmids=[str(10_000_000 + i) for _ in range(50)],
            examples=[{"pmid": str(20_000_000 + i), "locator": "methods:L1:P1:S1",
                       "operation": "score", "verbatim": "q" * 200} for _ in range(3)]))
    fake(FakeRegistry(methods=rows))
    blob = json.dumps(catalog.catalog_summary(), ensure_ascii=False)
    assert len(blob.encode("utf-8")) < 2_000_000


def test_catalog_summary_passes_query_and_stage(fake):
    reg = fake(FakeRegistry(methods=[record("AUCell", stages=["enrichment"]),
                                     record("inferCNV", stages=["cnv"])]))
    out = catalog.catalog_summary(query="AUC", stage="enrichment")
    assert [r["canonical_name"] for r in out["discovered"]] == ["AUCell"]
    assert reg.search_calls[-1] == {"query": "AUC", "stages": ["enrichment"],
                                    "limit": catalog.DISCOVERED_PAGE_DEFAULT,
                                    "offset": 0,
                                    "kinds": "literature_method"}
    assert out["n_discovered"] == 1
    assert out["filtered"] is True
    assert out["n_discovered_in_scope"] == 2  # fake registry total, not page


def test_catalog_listing_omits_full_anchor_instances(fake):
    instances = [{"pmid": str(i), "locator": f"methods:L{i}:P1:S1",
                  "canonical_name": "AUCell", "operation": "score",
                  "verbatim": "q" * 300} for i in range(5)]
    fake(FakeRegistry(backing={
        "signature_score_aucell": {
            "method_id": "signature_score_aucell", "found": True,
            "quality": "exact", "n_instances": 5, "n_papers": 5,
            "instances": instances, "excluded_related": [],
            "mapped_canonical_names": ["AUCell"], "reason": "exact"},
    }))
    out = catalog.catalog_summary(executable_only=True)
    row = next(r for r in out["executable"]
               if r["method_id"] == "signature_score_aucell")
    detail = row["literature_backing_detail"]
    assert detail["instances"] == []
    assert detail["instances_omitted"] is True
    assert detail["returned_instances"] == 5
    assert detail["groups"]
    assert len(json.dumps(out, ensure_ascii=False).encode()) < 20_000


def test_catalog_propagates_registry_match_metadata(fake):
    named = record("Seurat")
    named["match"] = {"kind": "exact", "terms": 1, "fields": ["name"],
                      "name_matched": True}
    package_hit = record("AddModuleScore")
    package_hit["match"] = {"kind": "exact", "terms": 1, "fields": ["package"],
                            "name_matched": True}
    fake(FakeRegistry(methods=[package_hit, named]))
    out = catalog.catalog_summary()
    by_name = {r["canonical_name"]: r for r in out["discovered"]}
    assert by_name["Seurat"]["match"] == named["match"]
    assert by_name["Seurat"]["matched_fields"] == ["name"]
    assert by_name["AddModuleScore"]["matched_fields"] == ["package"]


def test_catalog_summary_rejects_bad_paging(fake):
    fake(FakeRegistry(methods=[record("AUCell")]))
    with pytest.raises(catalog.CatalogError):
        catalog.catalog_summary(offset=-1)
    with pytest.raises(catalog.CatalogError):
        catalog.catalog_summary(limit=0)


def test_executable_only_does_not_request_the_discovered_index(fake):
    class NoSearch(FakeRegistry):
        def registry_search(self, **kwargs):
            raise AssertionError("executable_only must not page the registry")

    fake(NoSearch(backing={
        "composition": {"method_id": "composition", "found": False,
                        "quality": "local_adapter", "instances": [],
                        "excluded_related": [], "reason": "local"},
    }))
    out = catalog.catalog_summary(executable_only=True)
    assert "discovered" not in out
    assert out["n_executable"] == 4
    detail = {r["method_id"]: r["literature_backing_detail"]
              for r in out["executable"]}
    assert detail["composition"]["found"] is False


def test_catalog_summary_registry_unavailable_is_not_an_empty_corpus(monkeypatch):
    def unavailable():
        raise catalog.CatalogUnavailable("registry import failed")

    monkeypatch.setattr(catalog, "_registry", unavailable)
    out = catalog.catalog_summary()
    assert out["status"] == "unavailable"
    assert out["discovered"] == []
    assert out["discovered_total"] is None
    assert "not an empty corpus" in out["note"]
    for row in out["executable"]:
        assert row["backing_status"] == "unavailable"
    only = catalog.catalog_summary(executable_only=True)
    assert only["status"] == "ok"
    assert only["n_executable"] == 4


# --------------------------------------------------------- caller compatibility
def test_report_consumer_keys_survive(fake):
    fake(FakeRegistry(methods=[record("AUCell", n_papers=43, stages=["enrichment"])]))
    for row in catalog.discovered_methods():
        assert isinstance(row["canonical_name"], str)
        assert isinstance(row["n_papers"], int)
        assert isinstance(row["stages"], list)


def test_protocol_literature_basis_still_accepts_the_list_shape(fake):
    from sctda import protocol

    fake(FakeRegistry(backing={
        "composition": {"method_id": "composition", "found": False,
                        "quality": "local_adapter", "instances": [],
                        "excluded_related": [], "reason": "local"},
        "signature_score_aucell": {
            "method_id": "signature_score_aucell", "found": False,
            "quality": "not_found", "instances": [], "excluded_related": [],
            "reason": "none"},
    }))
    basis = protocol._literature_basis(["composition", "signature_score_aucell"])
    assert basis["composition"]["corpus_instances"] == []
    assert isinstance(basis["composition"]["anchors"], list)
    assert "context only" in basis["composition"]["note"]


# ------------------------------------------------------ real registry integration
def test_real_registry_scope_and_exact_adapter_distinctions():
    reg = pytest.importorskip("sctda.method_registry")
    try:
        stats = reg.registry_stats()
    except FileNotFoundError as exc:
        pytest.skip(str(exc))
    scope = stats.get("scope") or {}
    assert scope.get("n_included_pmids") == 405
    assert "36450980" in (scope.get("retracted_pmids") or [])
    assert scope.get("n_effective_pmids") == 404

    # Exact-name adapter mapping, not substring matching.
    assert [h["method_id"] for h in reg.registry_adapters_for("UCell")] == []
    assert [h["method_id"] for h in reg.registry_adapters_for("CopyKAT")] == []
    aucell = reg.registry_adapters_for("AUCell")
    assert aucell and aucell[0]["method_id"] == "signature_score_aucell"
    assert aucell[0]["quality"] in ("exact", "operation")
    infer = reg.registry_adapters_for("inferCNV")
    assert infer and infer[0]["method_id"] == "cnv_infercnv"

    # Catalog delegates exactly, so its public surface keeps the distinction.
    assert catalog.supported_by("AUCell") == ["signature_score_aucell"]
    assert catalog.supported_by("UCell") == []
    assert catalog.supported_by("inferCNV") == ["cnv_infercnv"]
    assert catalog.supported_by("CopyKAT") == []

    auc_backing = reg.registry_backing("signature_score_aucell")
    assert auc_backing["found"] is True
    for inst in auc_backing["instances"]:
        name = str(inst.get("canonical_name") or "").lower()
        assert not re.search(r"(?<![a-z])ucell(?![a-z])", name)

    cnv_backing = reg.registry_backing("cnv_infercnv")
    assert cnv_backing["found"] is True
    for inst in cnv_backing["instances"]:
        name = str(inst.get("canonical_name") or "").lower()
        assert "copykat" not in name
        assert "infercnvpy" not in name

    # The grouped catalog view carries the same mapped canonical names only.
    for g in catalog.literature_backing("cnv_infercnv", limit=100):
        for ex in g["examples"]:
            assert "copykat" not in str(ex.get("canonical_name") or "").lower()
    for g in catalog.literature_backing("signature_score_aucell", limit=100):
        for ex in g["examples"]:
            name = str(ex.get("canonical_name") or "").lower()
            assert not re.search(r"(?<![a-z])ucell(?![a-z])", name)

    comp = reg.registry_backing("composition")
    assert comp["found"] is False and comp["quality"] == "local_adapter"
    assert catalog.literature_backing("composition", detail=True)["found"] is False


def test_real_catalog_summary_is_405_scoped_and_does_not_page_unbounded():
    pytest.importorskip("sctda.method_registry")
    list_path = Path("/data/disk1/zeiss/singlecell-quant/data/included_pmids_405.txt")
    if not list_path.exists():
        pytest.skip("405-PMID inclusion list unavailable")
    pmids = {line.strip() for line in
             list_path.read_text(encoding="utf-8").splitlines() if line.strip()}
    if not pmids:
        pytest.skip("405-PMID inclusion list unavailable")
    out = catalog.catalog_summary()
    assert out["status"] == "ok"
    assert out["n_executable"] == 4
    assert len(out["discovered"]) <= catalog.DISCOVERED_PAGE_DEFAULT
    assert out["incomplete"] is True
    assert 1_000 < out["n_discovered"] < 6_012, "must move off the 458-paper basis"
    assert out["page"]["has_more"] is True
    for row in out["discovered"]:
        assert set(row["pmids"]) <= pmids
        assert "36450980" not in row["pmids"]
    full = catalog.discovered_methods()
    assert len(full) == out["n_discovered"]
    assert all(set(r["pmids"]) <= pmids for r in full)
    assert all("36450980" not in r["pmids"] for r in full)
    scope = out["registry"]["scope"]
    assert scope.get("n_included_pmids") == 405
    assert scope.get("n_effective_pmids") == 404
    assert "36450980" in (scope.get("retracted_pmids") or [])

    # RegistryReview payload regression: the listing must not embed every
    # verbatim anchor; full evidence is served by the detail tools.
    blob = json.dumps(out, ensure_ascii=False)
    assert len(blob.encode("utf-8")) < 200_000
    only = catalog.catalog_summary(executable_only=True)
    only_blob = json.dumps(only, ensure_ascii=False)
    assert len(only_blob.encode("utf-8")) < 60_000
    for row in only["executable"]:
        detail = row["literature_backing_detail"]
        assert detail["instances"] == []
        if detail["returned_instances"]:
            assert detail["instances_omitted"] is True


def test_real_seurat_query_surfaces_named_entry_with_match_label():
    pytest.importorskip("sctda.method_registry")
    out = catalog.catalog_summary(query="seurat", limit=50)
    by_name = {r["canonical_name"]: r for r in out["discovered"]}
    assert "Seurat" in by_name, (
        "name hits must not be buried by package-only hits in the default page")
    assert "name" in by_name["Seurat"]["match"]["fields"]
    assert by_name["Seurat"]["matched_fields"] == ["name"]
    assert any("name" not in r["matched_fields"] for r in out["discovered"]), (
        "package/stage hits must remain labelled as such at the catalog surface")
