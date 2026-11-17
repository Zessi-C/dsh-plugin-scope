"""Behavior tests for the method-discovery tools at the kernel boundary.

No test touches the network or a sibling module's build state:

- the local registry is a synthetic payload injected through the single
  ``method_search._load_registry_payload`` seam;
- public searches replace ``literature.http_get`` / ``fetch_abstracts`` with
  deterministic bodies and write the same ``literature_cache`` rows a real
  request would leave, so provenance fields are exercised rather than faked.

Every call goes through ``invoke_tool``, the same door the CLI, TUI and DSH
manifest proxy use.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from sctda import catalog  # noqa: E402
from sctda import literature as lit  # noqa: E402
from sctda import method_search as ms  # noqa: E402
from sctda.harness import TOOLS, invoke_tool, tool_manifest  # noqa: E402
from sctda.project import Project, now  # noqa: E402


# ------------------------------------------------------------------ fixtures
def method(method_id: str, name: str, **kwargs) -> dict:
    record = {
        "method_id": method_id,
        "kind": "literature_method",
        "name": name,
        "canonical_name": kwargs.pop("canonical_name", name),
        "aliases": [],
        "stages": [],
        "packages": [],
        "operations": [],
        "method_class": None,
        "in_scope": True,
        "upstream_background": False,
        "rerunnable_roles": {},
        "provenance": {
            "n_instances": kwargs.pop("n_instances", 1),
            "n_papers": kwargs.pop("n_papers", 1),
            "pmids": kwargs.pop("pmids", ["32997960"]),
            "examples": kwargs.pop("examples", []),
            "examples_truncated": False,
        },
        "adapter_method_id": None,
        "registry_mapped_adapters": [],
        "registry_mapping_quality": None,
        "registry_declared_support": "corpus_only",
    }
    record.update(kwargs)
    return record


def payload(methods: list[dict], *, registry_used: bool = True) -> dict:
    return {
        "source": {"name": "method_registry.load_registry", "registry_used": registry_used,
                   "n_records": len(methods)},
        "metadata": {
            "scope": {"pmid_list": "included_pmids_405.txt", "n_papers": 404,
                      "retraction_excluded": ["36450980"]},
            "incomplete": True,
            "corpus_sha256": "deadbeef",
        },
        "methods": methods,
    }


def install(monkeypatch, methods: list[dict], **kwargs) -> dict:
    bundle = payload(methods, **kwargs)
    monkeypatch.setattr(ms, "_load_registry_payload",
                        lambda: copy.deepcopy(bundle))
    # Keep this test surface independent of a sibling-built method_registry:
    # get() may enrich from registry_get, which is covered by its own unit test.
    monkeypatch.setattr(ms, "_registry_get_record", lambda method_id: None)
    # Related-adapter fallback would otherwise build the real registry; keep
    # module-level tests independent of sibling build state.
    monkeypatch.setattr(ms.catalog, "supported_by_detail", lambda name: [])
    return bundle


def call(name: str, **args) -> dict:
    return invoke_tool(name, args)


def ok(name: str, **args) -> dict:
    out = call(name, **args)
    assert out["status"] == "ok", out
    return out


AUCell = method(
    "lit:aucell-1111aaaa", "AUCell", aliases=["gene set scoring"],
    stages=["enrichment"], packages=["AUCell"], operations=["AUCell scoring"],
    n_instances=48, n_papers=48, pmids=["32997960", "33495604"],
    examples=[{"pmid": "32997960", "locator": "methods:L93:P22:S1",
               "verbatim": "AUCell v1.6.0 was used", "operation": "AUCell"},
              {"pmid": "33495604", "locator": "methods:L10:P2:S1",
               "verbatim": "gene set activity", "operation": "AUCell"}],
    registry_mapped_adapters=[{"method_id": "signature_score_aucell",
                               "quality": "operation",
                               "wired_operation": "AUCell"}],
)
UCell = method(
    "lit:ucell-2222bbbb", "UCell", aliases=["gene set scoring"],
    stages=["enrichment"], packages=["UCell"], operations=["UCell scoring"],
    n_instances=12, n_papers=12, pmids=["40000001"],
)
CopyKAT = method(
    "lit:copykat-3333cccc", "CopyKAT", stages=["cnv"], packages=["CopyKAT"],
    n_instances=7, n_papers=7, pmids=["40000002"],
    registry_mapped_adapters=[{"method_id": "cnv_infercnv",
                               "quality": "operation",
                               "wired_operation": "CopyKAT"}],
)
DESeq2 = method(
    "lit:deseq2-4444dddd", "DESeq2", stages=["de"], packages=["DESeq2"],
    n_instances=9, n_papers=9, pmids=["40000003"],
)
GSEA = method(
    "lit:gsea-5555eeee", "GSEA", stages=["enrichment"], packages=["GSEA"],
    n_instances=4, n_papers=4, pmids=["40000004"],
)
COMPOSITION = method(
    "adapter:composition", "composition", aliases=["cell composition"],
    stages=["composition"], kind="executable_adapter",
    provenance={"n_instances": None, "n_papers": None, "pmids": [],
                "examples": [], "examples_truncated": False},
    adapter_method_id="composition", registry_declared_support="executable",
)
DE = method(
    "adapter:de_vs_rest_wilcox", "differential expression vs rest",
    aliases=["FindMarkers", "Wilcoxon"], stages=["de"], packages=["Seurat"],
    kind="executable_adapter", operations=["FindMarkers"],
    provenance={"n_instances": None, "n_papers": None, "pmids": [],
                "examples": [], "examples_truncated": False},
    adapter_method_id="de_vs_rest_wilcox", registry_declared_support="executable",
)

DEFAULT_METHODS = [AUCell, UCell, CopyKAT, DESeq2, GSEA, COMPOSITION, DE]


# ------------------------------------------------------------------- manifest
def test_method_tools_are_registered_read_only_and_worker_visible():
    manifest = {tool["name"]: tool for tool in tool_manifest()}
    expected = {"sctda_method_search", "sctda_method_get",
                "sctda_method_stats", "sctda_method_external_search"}
    assert expected <= set(manifest)
    for name in expected:
        spec = TOOLS[name]
        assert spec.mutating is False, name
        assert spec.roles == ("worker", "lead"), name
        assert manifest[name]["mutating"] is False
        assert "worker" in manifest[name]["roles"]
    assert TOOLS["sctda_method_search"].requires_project is False
    assert TOOLS["sctda_method_get"].requires_project is False
    assert TOOLS["sctda_method_stats"].requires_project is False
    assert TOOLS["sctda_method_external_search"].requires_project is True

    search_params = TOOLS["sctda_method_search"].parameters
    assert set(search_params) == {"query", "page", "page_size", "stage",
                                  "package", "support", "in_scope"}
    assert search_params["support"]["enum"] == list(ms.SUPPORT_STATUSES)
    assert set(ms.SUPPORT_STATUSES) == {"executable", "corpus_only",
                                        "external_only", "not_found"}
    assert TOOLS["sctda_method_get"].parameters["method_id"]["required"] is True
    assert set(TOOLS["sctda_method_get"].parameters) == {
        "method_id", "anchor_page", "anchor_page_size"}
    external = TOOLS["sctda_method_external_search"].parameters
    assert external["query"]["required"] is True
    assert external["scope"]["enum"] == ["pubmed", "europepmc", "both"]


def test_unknown_tool_name_still_returns_the_error_envelope():
    out = call("sctda_method_does_not_exist")
    assert out["status"] == "error"
    assert "sctda_method_search" in out["available"]


def test_method_search_requires_no_project(monkeypatch):
    install(monkeypatch, [AUCell])
    out = ok("sctda_method_search", query="aucell")
    assert out["scope"] == "registry"
    assert out["returned"] == 1


def test_registry_unavailable_is_explicit_not_an_empty_answer(monkeypatch):
    monkeypatch.setattr(ms, "_load_registry_payload", lambda: {
        "source": {"name": "unavailable", "registry_used": False,
                   "n_records": 0, "reason": "method_registry unavailable"},
        "metadata": {}, "methods": []})
    for name, args in (("sctda_method_search", {"query": "AUCell"}),
                       ("sctda_method_get", {"method_id": "lit:aucell"}),
                       ("sctda_method_stats", {})):
        out = call(name, **args)
        assert out["status"] == "unavailable", (name, out)
        assert "unavailable" in out["error"], (name, out)
    search = call("sctda_method_search", query="AUCell")
    assert search["next"]["tool"] == "sctda_method_external_search"
    assert any("not an empty result" in note for note in search["notes"])


# --------------------------------------------------------------------- search
def test_search_paginates_deterministically_and_never_ranks_by_frequency(monkeypatch):
    high = method("lit:zeta", "Zeta scoring", n_instances=100, n_papers=100)
    low = method("lit:alpha", "Alpha scoring", n_instances=1, n_papers=1)
    mid = method("lit:mid", "Mid scoring", n_instances=50, n_papers=50)
    install(monkeypatch, [high, mid, low])

    first = ok("sctda_method_search", query="scoring", page=1, page_size=2)
    assert first["total"] == 3 and first["total_pages"] == 2
    assert [m["name"] for m in first["methods"]] == ["Alpha scoring", "Mid scoring"]
    assert first["has_more"] is True
    assert first["page_out_of_range"] is False

    second = ok("sctda_method_search", query="scoring", page=2, page_size=2)
    assert [m["name"] for m in second["methods"]] == ["Zeta scoring"]
    assert second["has_more"] is False

    repeat = ok("sctda_method_search", query="scoring", page=1, page_size=2)
    assert repeat["methods"] == first["methods"]
    # 100-paper Zeta is last: frequency is provenance, not rank.
    assert repeat["methods"][0]["provenance"]["n_papers"] == 1

    beyond = ok("sctda_method_search", query="scoring", page=9, page_size=2)
    assert beyond["returned"] == 0
    assert beyond["page_out_of_range"] is True
    assert beyond["has_more"] is False


def test_search_matches_aliases_packages_and_stages(monkeypatch):
    install(monkeypatch, DEFAULT_METHODS)

    by_alias = ok("sctda_method_search", query="FindMarkers")
    assert [m["method_id"] for m in by_alias["methods"]] == ["adapter:de_vs_rest_wilcox"]
    assert by_alias["methods"][0]["match"]["quality"] == "exact"

    by_package = ok("sctda_method_search", query="Seurat")
    assert [m["method_id"] for m in by_package["methods"]] == ["adapter:de_vs_rest_wilcox"]

    by_stage = ok("sctda_method_search", stage="de")
    assert {m["method_id"] for m in by_stage["methods"]} == {
        "adapter:de_vs_rest_wilcox", "lit:deseq2-4444dddd"}

    by_package_filter = ok("sctda_method_search", package="AUCell")
    assert [m["name"] for m in by_package_filter["methods"]] == ["AUCell"]


def test_search_support_filter_uses_exact_adapter_wiring(monkeypatch):
    install(monkeypatch, DEFAULT_METHODS)

    executable = ok("sctda_method_search", support="executable")
    assert {m["method_id"] for m in executable["methods"]} == {
        "adapter:composition", "adapter:de_vs_rest_wilcox"}
    for item in executable["methods"]:
        assert item["adapter"]["adapter_method_id"] in catalog.EXECUTABLE
        assert item["support"]["runtime_check"] == "not_performed_by_this_tool"

    corpus_only = ok("sctda_method_search", query="UCell")
    assert corpus_only["returned"] == 1, "UCell must not match AUCell by substring"
    assert corpus_only["methods"][0]["name"] == "UCell"
    assert corpus_only["methods"][0]["support"]["status"] == "corpus_only"
    assert corpus_only["methods"][0]["support"]["counts_as_support"] is False
    assert corpus_only["methods"][0]["adapter"] is None

    missing = ok("sctda_method_search", support="external_only")
    assert missing["total"] == 0 and missing["match_state"] == "not_found"


def test_descriptor_matches_never_promote_ucell_copykat_gsea_or_deseq2(monkeypatch):
    claims = []
    for record in (UCell, CopyKAT, GSEA, DESeq2):
        rec = copy.deepcopy(record)
        rec["registry_declared_support"] = "executable"   # registry claim, ignored
        claims.append(rec)
    install(monkeypatch, claims)

    for record in claims:
        detail = ok("sctda_method_get", method_id=record["method_id"])
        support = detail["method"]["support"]
        assert support["status"] == "corpus_only", detail
        assert support["counts_as_support"] is False
        assert "registry declared 'executable'" in support["reason"]
        for relation in detail["method"]["related_wiring"]:
            assert relation["counts_as_executable"] is False


def test_query_normalization_keeps_unicode_and_rejects_punctuation_only(monkeypatch):
    install(monkeypatch, [AUCell, UCell])

    cjk = ok("sctda_method_search", query="单细胞")
    assert cjk["total"] == 0
    assert cjk["match_state"] == "not_found", "a CJK query must not match every record"

    for query in ("***", "—", "___"):
        out = call("sctda_method_search", query=query)
        assert out["status"] == "error", (query, out)
        assert "no searchable terms" in out["error"], (query, out)

    # The same ASCII-only normalization used to collapse CJK-only titles into
    # one dedup key when there was no PMID/DOI.
    assert ms._hit_key({"title": "单细胞方法"}) != ms._hit_key({"title": "单细胞分析"})


def test_search_in_scope_filter_and_empty_answer_are_explicit(monkeypatch):
    out_of_scope = method("lit:old", "Legacy method", in_scope=False)
    install(monkeypatch, [AUCell, out_of_scope])

    both = ok("sctda_method_search")
    assert both["total"] == 2
    in_scope = ok("sctda_method_search", in_scope=True)
    assert [m["method_id"] for m in in_scope["methods"]] == [AUCell["method_id"]]
    out_scope = ok("sctda_method_search", in_scope=False)
    assert [m["method_id"] for m in out_scope["methods"]] == ["lit:old"]

    empty = ok("sctda_method_search", query="no such method anywhere")
    assert empty["total"] == 0
    assert empty["match_state"] == "not_found"
    assert empty["next"]["tool"] == "sctda_method_external_search"
    assert any("not a claim that no method exists" in note for note in empty["notes"])


# ------------------------------------------------------------------------ get
def test_get_returns_provenance_and_exact_adapter_detail(monkeypatch):
    install(monkeypatch, DEFAULT_METHODS)

    detail = ok("sctda_method_get", method_id="adapter:de_vs_rest_wilcox")
    assert detail["found"] is True and detail["matched_by"] == "method_id"
    item = detail["method"]
    assert item["support"]["status"] == "executable"
    assert item["adapter"]["adapter_method_id"] == "de_vs_rest_wilcox"
    assert item["adapter"]["requires"] == catalog.EXECUTABLE["de_vs_rest_wilcox"]["requires"]
    assert item["adapter"]["runtime_check"] == "not_performed_by_this_tool"
    assert detail["source"]["registry_used"] is True

    by_name = ok("sctda_method_get", method_id="composition")
    assert by_name["found"] is True and by_name["matched_by"] == "canonical_name"
    assert by_name["method"]["support"]["status"] == "executable"


def test_get_uses_exact_registry_lookup_for_short_adapter_id(monkeypatch):
    install(monkeypatch, DEFAULT_METHODS)
    native = copy.deepcopy(DE)
    monkeypatch.setattr(
        ms, "_registry_get_record",
        lambda query: copy.deepcopy(native) if query == "de_vs_rest_wilcox" else None)
    out = ok("sctda_method_get", method_id="de_vs_rest_wilcox")
    assert out["found"] is True
    assert out["matched_by"] == "registry_lookup"
    assert out["method_id"] == "adapter:de_vs_rest_wilcox"
    assert out["method"]["support"]["status"] == "executable"


def test_get_paginates_every_evidence_anchor(monkeypatch):
    anchors = [{"pmid": str(100 + i), "locator": f"L{i}",
                "verbatim": f"verbatim {i}", "operation": "AUCell"}
               for i in range(5)]
    record = method("lit:aucell-many", "AUCell", pmids=["100", "101"],
                    examples=anchors)
    install(monkeypatch, [record])

    page2 = ok("sctda_method_get", method_id="lit:aucell-many",
               anchor_page=2, anchor_page_size=2)
    assert page2["anchors"]["total"] == 5
    assert page2["anchors"]["total_pages"] == 3
    assert page2["anchors"]["returned"] == 2
    assert [item["locator"] for item in page2["anchors"]["items"]] == ["L2", "L3"]
    assert page2["anchors"]["has_more"] is True
    assert "examples" not in page2["method"]["provenance"]
    assert page2["method"]["provenance"]["n_examples"] == 5

    page3 = ok("sctda_method_get", method_id="lit:aucell-many",
               anchor_page=3, anchor_page_size=2)
    assert [item["locator"] for item in page3["anchors"]["items"]] == ["L4"]
    assert page3["anchors"]["has_more"] is False

    beyond = ok("sctda_method_get", method_id="lit:aucell-many",
                anchor_page=9, anchor_page_size=2)
    assert beyond["anchors"]["returned"] == 0
    assert beyond["anchors"]["page_out_of_range"] is True


def test_native_evidence_is_retained_beyond_the_compact_page_cap():
    raw = {
        "id": "lit:big-abcdef12",
        "kind": "literature_method",
        "name": "Big method",
        "canonical_name": "Big method",
        "aliases": [],
        "stages": ["de"],
        "packages": ["BigPkg"],
        "in_scope": True,
        "corpus": {"n_instances": 250, "n_papers": 250, "pmids": [],
                   "examples": []},
        "support": {"status": "corpus_only", "adapter_method_id": None,
                    "adapters": []},
        "evidence": [{"pmid": str(i), "locator": f"L{i}",
                      "verbatim": f"verbatim {i}"} for i in range(250)],
    }
    native = ms._normalize_record(raw, max_examples=None)
    assert len(native["provenance"]["examples"]) == 250
    assert native["provenance"]["examples_truncated"] is False

    capped = ms._normalize_record(raw, max_examples=200)
    assert len(capped["provenance"]["examples"]) == 200
    assert capped["provenance"]["examples_truncated"] is True

    merged = ms._merge_detail_record(capped, native, max_examples=None)
    assert len(merged["provenance"]["examples"]) == 250
    assert merged["provenance"]["examples_truncated"] is False


def test_search_metadata_is_compact_while_stats_keep_the_full_audit(monkeypatch):
    huge = [str(1000 + i) for i in range(500)]
    bundle = install(monkeypatch, [AUCell])
    bundle["metadata"]["scope"]["pmids_without_qualifying_records"] = huge
    bundle["metadata"]["stats"] = {
        "counts": {"n_records_total": 11214, "n_methods_literature": 3672,
                   "n_methods_total": 3676, "n_adapters": 4},
        "scope": {"pmids_without_qualifying_records": huge},
        "pmid_list": {"sha256": "abc", "n_pmids": 405},
        "retractions": [{"pmid": "36450980"}],
    }

    search = ok("sctda_method_search", query="AUCell")
    compact_scope = search["registry_metadata"]["scope"]
    assert "pmids_without_qualifying_records" not in compact_scope
    assert len(json.dumps(search["registry_metadata"])) < 2000
    assert "stats" not in search["registry_metadata"]
    assert search["registry_metadata"]["counts"]["n_adapters"] == 4
    assert search["registry_metadata"]["detail_tool"] == "sctda_method_stats"

    detail = ok("sctda_method_get", method_id="lit:aucell-1111aaaa")
    assert "pmids_without_qualifying_records" not in \
        detail["registry_metadata"]["scope"]

    stats = ok("sctda_method_stats")
    assert stats["registry_metadata"]["stats"]["scope"][
        "pmids_without_qualifying_records"] == huge


def test_get_unknown_returns_near_matches_not_a_fabricated_record(monkeypatch):
    install(monkeypatch, DEFAULT_METHODS)
    out = ok("sctda_method_get", method_id="AUCel")
    assert out["found"] is False
    assert out["support"]["status"] == "not_found"
    assert out["support"]["counts_as_support"] is False
    assert any(match["name"] == "AUCell" for match in out["near_matches"])
    assert "method" not in out
    assert any("does not prove the method does not exist" in note
               for note in out["notes"])


def test_get_reports_ambiguity_without_choosing(monkeypatch):
    first = method("lit:one", "Same Name")
    second = method("lit:two", "Same Name")
    install(monkeypatch, [first, second])
    out = ok("sctda_method_get", method_id="Same Name")
    assert out["found"] is False and out["ambiguous"] is True
    assert {row["method_id"] for row in out["candidates"]} == {"lit:one", "lit:two"}
    assert "no record was chosen" in out["notes"][0]


# ---------------------------------------------------------------------- stats
def test_stats_separate_support_kinds_and_do_not_claim_installation(monkeypatch):
    install(monkeypatch, DEFAULT_METHODS)
    out = ok("sctda_method_stats")
    assert out["counts"]["n_methods"] == len(DEFAULT_METHODS)
    assert out["counts"]["by_support"]["executable"] == 2
    assert out["counts"]["by_support"]["corpus_only"] == len(DEFAULT_METHODS) - 2
    assert out["counts"]["by_support"]["external_only"] == 0
    assert out["counts"]["by_support"]["not_found"] == 0
    assert out["counts"]["n_literature_methods"] == len(DEFAULT_METHODS) - 2
    assert out["counts"]["n_adapter_records"] == 2
    assert {row["adapter_method_id"] for row in out["adapters"]} == set(catalog.EXECUTABLE)
    assert all(row["runtime_check"] == "not_performed_by_this_tool"
               for row in out["adapters"])
    assert out["external_search"]["live_reachability"] == "not_tested"
    assert out["registry_metadata"]["incomplete"] is True
    assert out["registry_metadata"]["scope"]["retraction_excluded"] == ["36450980"]
    assert set(out["support_vocabulary"]) == set(ms.SUPPORT_STATUSES)
    assert any("not an exhaustive census" in note for note in out["notes"])


def test_detail_evidence_merge_adds_anchors_without_changing_support():
    base = method("lit:aucell-abcdef12", "AUCell", pmids=["1"],
                  examples=[{"pmid": "1", "locator": "methods:L1:P1:S1",
                             "verbatim": "first"}])
    extra = method("lit:aucell-abcdef12", "AUCell", pmids=["1", "2"],
                   examples=[{"pmid": "2", "locator": "methods:L2:P2:S2",
                              "verbatim": "second"}])
    extra["adapter_method_id"] = None
    merged = ms._merge_detail_record(base, extra)
    assert merged["provenance"]["pmids"] == ["1", "2"]
    assert [e["pmid"] for e in merged["provenance"]["examples"]] == ["1", "2"]
    assert merged["provenance"]["n_papers"] == 2
    assert ms._support_for(merged)["status"] == "corpus_only"


def test_catalog_fallback_is_labelled_and_never_claims_registry_use(monkeypatch):
    monkeypatch.setattr(
        ms.catalog, "discovered_methods",
        lambda limit=None: [{"canonical_name": "Monocle 3", "n_instances": 3,
                             "n_papers": 2, "pmids": ["1", "2"],
                             "stages": ["trajectory"], "packages": ["Monocle"],
                             "in_scope": True,
                             "examples": [{"pmid": "1",
                                           "locator": "methods:L1:P1:S1",
                                           "operation": "Monocle 3"}]}])
    out = ms._fallback_payload("method_registry unavailable")
    assert out["source"]["registry_used"] is False
    assert out["source"]["reason"] == "method_registry unavailable"
    assert out["methods"][0]["name"] == "Monocle 3"
    assert out["methods"][0]["provenance"]["n_instances"] == 3
    assert out["metadata"]["incomplete"] is True


# ------------------------------------------------------ registry normalization
def test_registry_native_records_map_ids_corpus_and_support():
    raw = {
        "methods": [
            {
                "id": "adapter:composition",
                "kind": "executable_adapter",
                "name": "composition",
                "canonical_name": "composition",
                "aliases": ["cell composition"],
                "stages": ["composition"],
                "packages": [],
                "in_scope": True,
                "corpus": {"n_instances": None, "n_papers": None, "pmids": [],
                           "examples": []},
                "support": {"status": "executable",
                            "adapter_method_id": "composition",
                            "mapping_quality": "local_adapter",
                            "adapters": [{"method_id": "composition",
                                          "quality": "local_adapter",
                                          "wired_operation": "task_composition"}]},
            },
            {
                "id": "lit:aucell-abcdef12",
                "kind": "literature_method",
                "name": "AUCell",
                "canonical_name": "AUCell",
                "aliases": ["gene set scoring"],
                "stages": ["enrichment"],
                "packages": ["AUCell"],
                "in_scope": True,
                "rerunnable_roles": {"analysis": 3, "qc": 1},
                "corpus": {"n_instances": 48, "n_papers": 48,
                           "pmids": ["32997960"],
                           "examples": [{"pmid": "32997960",
                                         "locator": "methods:L93:P22:S1",
                                         "verbatim": "AUCell v1.6.0"}],
                           "n_operations": 3},
                "support": {"status": "corpus_only",
                            "adapter_method_id": "signature_score_aucell",
                            "adapters": [{"method_id": "signature_score_aucell",
                                          "quality": "operation",
                                          "wired_operation": "AUCell"}],
                            "reason": "adapter exists as a related operation"},
            },
        ],
        "stats": {"n_methods": 2},
        "scope": {"pmid_list": "included_pmids_405.txt", "n_papers": 404,
                  "retraction_excluded": ["36450980"]},
        "incomplete": True,
    }
    normalized = ms._normalize_payload(raw, "method_registry.load_registry", True)
    assert normalized["source"]["registry_used"] is True
    assert normalized["metadata"]["scope"]["retraction_excluded"] == ["36450980"]

    by_id = {record["method_id"]: record for record in normalized["methods"]}
    adapter = by_id["adapter:composition"]
    assert adapter["kind"] == "executable_adapter"
    assert adapter["adapter_method_id"] == "composition"
    assert ms._support_for(adapter)["status"] == "executable"

    aucell = by_id["lit:aucell-abcdef12"]
    assert aucell["provenance"]["n_papers"] == 48
    assert aucell["provenance"]["examples"][0]["verbatim"] == "AUCell v1.6.0"
    assert aucell["rerunnable_roles"] == {"analysis": 3, "qc": 1}
    rendered = ms._method_payload(aucell, detail=True)
    assert rendered["rerunnable_roles"] == {"analysis": 3, "qc": 1}
    assert not isinstance(rendered["rerunnable_roles"], str)
    # A literature record may name a related adapter in support, but it must not
    # become executable itself; only adapter:<id> records carry that status.
    assert aucell["adapter_method_id"] is None
    assert ms._support_for(aucell)["status"] == "corpus_only"
    assert ms._related_wiring(aucell)[0]["counts_as_executable"] is False


# ------------------------------------------------------- external search fakes
class FakeLiterature:
    """Deterministic stand-in for the cached HTTP client.

    It writes the same ``literature_cache`` row a real response would leave, so
    the tool's endpoint/time/hash provenance is actually exercised.
    """

    def __init__(self, project: Project):
        self.project = project
        self.calls: list[str] = []
        self.pubmed_ids = ["111", "222"]
        self.pubmed_count = 2
        self.pubmed_records = {
            "111": {"title": "A single-cell method paper",
                    "journal": "Journal A", "year": "2020",
                    "abstract": "We used a method."},
            "222": {"title": "Another paper", "journal": "Journal B",
                    "year": "2021", "abstract": "Short abstract."},
        }
        self.epmc_pages: dict[str, dict] = {
            "*": {"hitCount": 3, "nextCursorMark": "C1",
                  "resultList": {"result": [
                      {"pmid": "111", "pmcid": "PMC111", "doi": "10.1/abc",
                       "title": "A single-cell method paper", "journalTitle": "Journal A",
                       "pubYear": "2020", "isOpenAccess": "Y", "inEPMC": "Y",
                       "abstractText": "<p>Full <b>abstract</b>.</p>"},
                      {"pmid": "333", "pmcid": None, "doi": None,
                       "title": "Third paper", "journalTitle": "Journal C",
                       "pubYear": "2022", "isOpenAccess": "N", "inEPMC": "N"},
                  ]}},
            "C1": {"hitCount": 3, "nextCursorMark": "C2",
                   "resultList": {"result": [
                       {"pmid": "444", "pmcid": None, "doi": "10.1/def",
                        "title": "Second page paper", "journalTitle": "Journal D",
                        "pubYear": "2023", "isOpenAccess": "Y", "inEPMC": "N"},
                   ]}},
        }
        self.fail: set[str] = set()

    def _store(self, conn, url: str, body: str) -> dict:
        path = self.project.dirs["downloads"] / f"fake_{len(self.calls)}.txt"
        path.write_text(body, encoding="utf-8")
        conn.execute(
            """INSERT OR REPLACE INTO literature_cache(cache_id, query, endpoint,
                 requested_utc, http_status, response_sha256, body_path, notes)
               VALUES (?,?,?,?,?,?,?,?)""",
            (lit._cache_key(url, None), url[:400], url.split("?")[0], now(), 200,
             hashlib.sha256(body.encode()).hexdigest(), str(path), None))
        conn.commit()
        return {"ok": True, "status": 200, "body": body, "path": str(path),
                "cached": False}

    def http_get(self, project, conn, url: str, timeout: int = 30) -> dict:
        self.calls.append(url)
        for marker in self.fail:
            if marker in url:
                return {"ok": False, "status": None,
                        "error": "URLError: simulated offline", "cached": False}
        if "esearch.fcgi" in url:
            if "retstart=2" in url:
                body = json.dumps({"esearchresult": {
                    "count": str(self.pubmed_count),
                    "idlist": self.pubmed_ids[2:]}})
            elif "retstart=1" in url:
                body = json.dumps({"esearchresult": {
                    "count": str(self.pubmed_count),
                    "idlist": self.pubmed_ids[1:]}})
            else:
                body = json.dumps({"esearchresult": {
                    "count": str(self.pubmed_count),
                    "idlist": self.pubmed_ids}})
            return self._store(conn, url, body)
        if "efetch.fcgi" in url:
            body = "<PubmedArticleSet/>"
            return self._store(conn, url, body)
        if "/search?" in url and "europepmc" in url:
            cursor = parse_qs(urlparse(url).query).get("cursorMark", ["*"])[0]
            body = json.dumps(self.epmc_pages.get(cursor, {
                "hitCount": 0, "nextCursorMark": "",
                "resultList": {"result": []}}))
            return self._store(conn, url, body)
        raise AssertionError(f"unexpected URL: {url}")

    def fetch_abstracts(self, project, conn, pmids: list[str]) -> dict:
        records = []
        for pmid in pmids:
            record = {"pmid": pmid, "title": None, "journal": None, "year": None,
                      "abstract": "", "abstract_available": False}
            record.update(self.pubmed_records.get(pmid, {}))
            records.append(record)
        # Exercise the efetch cache trace a real fetch would leave.
        url = (f"{lit.EUTILS}/efetch.fcgi?db=pubmed&retmode=xml"
               f"&id={','.join(pmids)}")
        self._store(conn, url, "<PubmedArticleSet/>")
        return {"status": "ok", "n": len(records), "records": records}


def offline_project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    Project(root).ensure()
    return root


def install_fake_literature(monkeypatch, project: Path) -> FakeLiterature:
    fake = FakeLiterature(Project(project))
    monkeypatch.setattr(lit, "http_get", fake.http_get)
    monkeypatch.setattr(lit, "fetch_abstracts", fake.fetch_abstracts)
    return fake


# ------------------------------------------------------------- external search
def test_external_search_returns_paged_provenance_and_dedupes_sources(tmp_path, monkeypatch):
    project = offline_project(tmp_path)
    fake = install_fake_literature(monkeypatch, project)

    out = ok("sctda_method_external_search", project=str(project),
             query="AUCell", scope="both", page=1, page_size=10,
             include_abstracts=True)
    assert out["status"] == "ok"
    assert out["scope"] == "both"
    assert out["total_by_source"] == {"pubmed": 2, "europepmc": 3}
    assert out["returned"] == 3                     # 111 deduped, 222 and 333 unique
    assert out["single_cell_filter"] is True
    assert "single-cell" in out["effective_query"]["pubmed"]
    assert "single-cell" in out["effective_query"]["europepmc"]

    merged = {hit["pmid"]: hit for hit in out["hits"]}
    assert set(merged) == {"111", "222", "333"}
    both = merged["111"]
    assert both["source"] == "both"
    assert set(both["sources"]) == {"pubmed", "europepmc"}
    assert both["identifiers"]["pmcid"] == "PMC111"
    assert both["identifiers"]["doi"] == "10.1/abc"
    assert both["is_open_access"] is True
    assert both["fulltext_status"] == "in_epmc"
    assert both["support"]["status"] == "external_only"
    assert both["support"]["usable_as_evidence"] is False
    assert both["method_support"] == "not_asserted"
    assert both["anchor_status"] == "not_checked"
    assert len(both["provenance"]) == 2
    for entry in both["provenance"]:
        assert entry["endpoint"]
        assert entry["requested_utc"]
        assert len(entry["response_sha256"]) == 64
        assert entry["cache_path"]

    pubmed_source = next(s for s in out["sources"] if s["source"] == "pubmed")
    assert pubmed_source["status"] == "ok"
    assert any("esearch.fcgi" in trace["endpoint"]
               for trace in pubmed_source["traces"])
    assert any("efetch.fcgi" in trace["endpoint"]
               for trace in pubmed_source["traces"])
    assert any("/search?" in trace["endpoint"]
               and "europepmc" in trace["endpoint"]
               for trace in next(s for s in out["sources"]
                                 if s["source"] == "europepmc")["traces"])
    assert fake.calls


def test_external_search_failure_is_explicit_never_an_empty_negative(tmp_path, monkeypatch):
    project = offline_project(tmp_path)
    fake = install_fake_literature(monkeypatch, project)
    fake.fail.add("pubmed")

    partial = call("sctda_method_external_search", project=str(project),
                   query="AUCell", scope="both", page=1, page_size=10)
    assert partial["status"] == "partial"
    by_source = {row["source"]: row for row in partial["sources"]}
    assert by_source["pubmed"]["status"] == "unreachable"
    assert "simulated offline" in by_source["pubmed"]["error"]
    assert by_source["europepmc"]["status"] == "ok"
    assert partial["hits"], "one healthy source must still return its hits"

    fake.fail.add("europepmc")
    down = call("sctda_method_external_search", project=str(project),
                query="AUCell", scope="both", page=1, page_size=10)
    assert down["status"] == "unreachable"
    assert down["returned"] == 0
    assert {row["status"] for row in down["sources"]} == {"unreachable"}
    assert all(row["error"] for row in down["sources"])
    assert any("never as an empty negative" in note for note in down["notes"])


def test_external_search_zero_hits_is_ok_but_not_a_negative_claim(tmp_path, monkeypatch):
    project = offline_project(tmp_path)
    fake = install_fake_literature(monkeypatch, project)
    fake.pubmed_ids = []
    fake.pubmed_count = 0
    fake.epmc_pages["*"] = {"hitCount": 0, "nextCursorMark": "",
                            "resultList": {"result": []}}

    out = ok("sctda_method_external_search", project=str(project),
             query="no such tool", scope="both")
    assert out["status"] == "ok"
    assert out["returned"] == 0
    assert out["total_by_source"] == {"pubmed": 0, "europepmc": 0}
    assert any("empty negative" in note for note in out["notes"])


def test_external_search_paginates_with_source_offsets_and_cursors(tmp_path, monkeypatch):
    project = offline_project(tmp_path)
    fake = install_fake_literature(monkeypatch, project)
    fake.pubmed_ids = ["111", "222", "333", "444"]
    fake.pubmed_count = 4

    out = ok("sctda_method_external_search", project=str(project),
             query="AUCell", scope="both", page=2, page_size=2)
    assert out["page"] == 2 and out["page_size"] == 2
    # PubMed uses retstart; EPMC walks cursors and returns the requested page.
    assert any("retstart=2" in url for url in fake.calls)
    assert any("cursorMark=*" in url or "cursorMark=%2A" in url for url in fake.calls)
    assert any("cursorMark=C1" in url for url in fake.calls)
    epmc = next(s for s in out["sources"] if s["source"] == "europepmc")
    assert epmc["traces"] and len(epmc["traces"]) == 2
    assert {hit["pmid"] for hit in out["hits"]} == {"333", "444"}


def test_pubmed_hits_carry_efetch_doi_and_pmcid(tmp_path, monkeypatch):
    project = offline_project(tmp_path)
    fake = install_fake_literature(monkeypatch, project)
    fake.pubmed_ids = ["43026950"]
    fake.pubmed_count = 1
    fake.pubmed_records["43026950"] = {
        "title": "A DOI-bearing paper", "journal": "Journal X", "year": "2024",
        "abstract": "Abstract.", "doi": "10.1/xyz", "pmcid": "PMC999999"}

    out = ok("sctda_method_external_search", project=str(project),
             query="AUCell", scope="pubmed", page=1, page_size=10)
    hit = out["hits"][0]
    assert hit["doi"] == "10.1/xyz"
    assert hit["pmcid"] == "PMC999999"
    assert hit["identifiers"]["doi"] == "10.1/xyz"
    assert hit["identifiers"]["pmcid"] == "PMC999999"
    assert hit["identifiers"]["epmc_url"].endswith("/PMC999999")


def test_epmc_page_past_end_is_an_empty_page_not_unreachable(tmp_path, monkeypatch):
    project = offline_project(tmp_path)
    fake = install_fake_literature(monkeypatch, project)
    fake.epmc_pages["*"] = {"hitCount": 1, "nextCursorMark": "",
                            "resultList": {"result": []}}

    out = ok("sctda_method_external_search", project=str(project),
             query="EXT_ID:32997960", scope="europepmc", page=2, page_size=10)
    assert out["status"] == "ok"
    assert out["returned"] == 0
    assert out["total_by_source"] == {"europepmc": 1}
    assert out["has_more"] is False
    assert out["page_out_of_range"] is True
    source = out["sources"][0]
    assert source["status"] == "ok"
    assert source["page_out_of_range"] is True
    assert source["pages_fetched"] == 1

    too_deep = call("sctda_method_external_search", project=str(project),
                    query="AUCell", scope="europepmc", page=401)
    assert too_deep["status"] == "error"
    assert str(ms.MAX_EPMC_PAGE) in too_deep["error"]


def test_cached_http_error_is_never_reported_as_an_ok_page(tmp_path, monkeypatch):
    project = offline_project(tmp_path)
    fake = install_fake_literature(monkeypatch, project)

    def cached_429(project, conn, url, timeout=30):
        return {"ok": False, "status": 429, "body": '{"error":"rate limited"}',
                "cached": True, "path": None}

    monkeypatch.setattr(lit, "http_get", cached_429)
    out = call("sctda_method_external_search", project=str(project),
               query="AUCell", scope="europepmc")
    assert out["status"] == "failed"
    assert out["sources"][0]["status"] == "failed"
    assert "429" in out["sources"][0]["error"]
    assert out["returned"] == 0


def test_boolean_strings_are_rejected_at_the_harness_boundary(tmp_path, monkeypatch):
    install(monkeypatch, [AUCell])
    project = str(tmp_path / "project")

    cases = [
        ("sctda_method_search", {"in_scope": "false"}, "in_scope"),
        ("sctda_method_external_search",
         {"project": project, "query": "AUCell", "single_cell_filter": "false"},
         "single_cell_filter"),
        ("sctda_method_external_search",
         {"project": project, "query": "AUCell", "include_abstracts": "false"},
         "include_abstracts"),
    ]
    for name, args, field in cases:
        out = call(name, **args)
        assert out["status"] == "error", (name, out)
        assert f"{field} must be a boolean" in out["error"], (name, out)

    valid = ok("sctda_method_search", query="AUCell", in_scope=True)
    assert valid["returned"] == 1


def test_external_search_scope_and_query_validation(tmp_path, monkeypatch):
    project = offline_project(tmp_path)
    install_fake_literature(monkeypatch, project)

    bad_scope = call("sctda_method_external_search", project=str(project),
                     query="AUCell", scope="google")
    assert bad_scope["status"] == "error"
    assert "scope" in bad_scope["error"]

    bad_page = call("sctda_method_external_search", project=str(project),
                    query="AUCell", page=0)
    assert bad_page["status"] == "error"
    assert "page" in bad_page["error"]

    empty = call("sctda_method_external_search", project=str(project), query="   ")
    assert empty["status"] == "error"
    assert "query" in empty["error"]

    too_large = call("sctda_method_external_search", project=str(project),
                     query="AUCell", page_size=26)
    assert too_large["status"] == "error"
    assert "page_size" in too_large["error"]
