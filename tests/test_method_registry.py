"""Behaviour tests for the unified method registry.

Synthetic corpora pin the scope/aggregation rules; one module-scoped fixture
checks the real frozen corpus and the four catalog adapters. No network, no R.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from sctda import method_registry as MR


# ------------------------------------------------------------------- fixtures
def _record(pmid: str, name: str, **overrides) -> dict:
    rec = {
        "instance_id": f"{pmid}:methods:L1:P1:S1",
        "pmid": pmid,
        "locator": "methods:L1:P1:S1",
        "verbatim": f"{name or 'method'} was used.",
        "mentioned_name": name,
        "canonical_name": name,
        "package": None,
        "operation": "analysis",
        "pipeline_stage": "de",
        "analysis_object": "cells",
        "inputs": [],
        "outputs": [],
        "purpose": "characterization",
        "constraints": [],
        "version_in_text": None,
        "method_class": "computational",
        "in_scope": True,
        "scope_reason": "within record-layer scope",
        "rerunnable_role": "rerunnable",
    }
    rec.update(overrides)
    return rec


def _build(tmp_path, records, pmids, retractions=None, executable=None):
    methods = tmp_path / "methods.jsonl"
    methods.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    pmid_list = tmp_path / "included_pmids_405.txt"
    pmid_list.write_text("\n".join(pmids) + "\n", encoding="utf-8")
    return MR.build_registry(
        methods_path=methods,
        pmid_list_path=pmid_list,
        retractions=retractions,
        executable=executable if executable is not None else {},
    ), methods, pmid_list


def _names(page: dict) -> list[str]:
    return [m["canonical_name"] for m in page["methods"]]


# --------------------------------------------------------------- scope rules
def test_scope_filter_retraction_and_exclusion_accounting(tmp_path):
    records = [
        _record("111", "Alpha"),
        _record("222", "RetractedMethod"),
        _record("333", "ScopedOut", in_scope=False),
        _record("444", "WetLab", method_class="wet_lab"),
        _record("999", "OutsideList"),
    ]
    reg, _, _ = _build(
        tmp_path, records, ["111", "222", "333", "444"],
        retractions=[{"pmid": "222"}],
    )

    page = reg.search(limit=50)
    assert page["total"] == 1
    assert _names(page) == ["Alpha"]
    assert reg.get("RetractedMethod") is None
    assert reg.get("OutsideList") is None
    assert reg.get("ScopedOut") is None
    assert reg.get("WetLab") is None

    stats = reg.stats()
    assert stats["scope"]["n_included_pmids"] == 4
    assert stats["scope"]["n_effective_pmids"] == 3
    assert stats["scope"]["n_pmids_with_qualifying_records"] == 1
    assert stats["scope"]["pmids_without_qualifying_records"] == ["333", "444"]

    excluded = stats["counts"]["records_excluded"]
    assert excluded["retracted"]["records"] == 1
    assert excluded["retracted"]["pmids"] == ["222"]
    assert excluded["not_in_scope"]["records"] == 1
    assert excluded["not_computational"]["records"] == 1
    assert excluded["pmid_not_in_included_list"]["records"] == 1
    assert excluded["pmid_not_in_included_list"]["pmids"] == ["999"]

    # Built-in retraction survives and the injected notice is reported.
    asserted_pmids = {notice["pmid"] for notice in stats["retractions"]}
    assert {"36450980", "222"} <= asserted_pmids
    # Nothing from the retracted/out-of-scope papers leaks into the list.
    all_pmids = {p for m in reg.load()["methods"] for p in m["pmids"]}
    assert not ({"222", "999"} & all_pmids)


def test_read_pmid_list_shares_scope_rules_and_reports_snapshot_metadata(tmp_path):
    path = tmp_path / "included_pmids_405.txt"
    path.write_text("# comment\n\n111\n222\n111\n", encoding="utf-8")
    parsed = MR.read_pmid_list(path)
    assert parsed["pmids"] == ["111", "222"]
    assert parsed["pmid_set"] == frozenset({"111", "222"})
    assert parsed["n_duplicates"] == 1
    assert parsed["path"] == str(path)
    assert parsed["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert parsed["matches_known_snapshot"] is False
    assert parsed["known_snapshot"] is None

    bad = tmp_path / "bad.txt"
    bad.write_text("111\nnot-a-pmid\n", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        MR.read_pmid_list(bad)
    assert "non-numeric PMID line" in str(excinfo.value)
    # The error must not echo caller-supplied file content.
    assert "not-a-pmid" not in str(excinfo.value)

    with pytest.raises(FileNotFoundError):
        MR.read_pmid_list(tmp_path / "missing.txt")


def test_upstream_background_is_preserved_not_filtered(tmp_path):
    records = [
        _record("111", "BackgroundTool", rerunnable_role="upstream_background",
                instance_id="111:methods:L1:P1:S1", locator="methods:L1:P1:S1"),
        _record("111", "BackgroundTool", rerunnable_role="rerunnable",
                instance_id="111:methods:L2:P1:S1", locator="methods:L2:P1:S1"),
        _record("111", "PureBackground", rerunnable_role="upstream_background"),
    ]
    reg, _, _ = _build(tmp_path, records, ["111"])

    entry = reg.get("BackgroundTool")
    assert entry["corpus"]["n_instances"] == 2
    assert entry["rerunnable_roles"] == {"rerunnable": 1, "upstream_background": 1}
    assert entry["upstream_background"] is False

    pure = reg.get("PureBackground")
    assert pure["upstream_background"] is True
    assert pure["rerunnable_roles"] == {"upstream_background": 1}

    stats = reg.stats()
    assert stats["counts"]["n_methods_with_background_instances"] == 2
    assert stats["counts"]["n_upstream_background_methods"] == 1


# --------------------------------------------------------------- aggregation
def test_conservative_normalization_never_merges_different_algorithms(tmp_path):
    pmids = [str(100 + i) for i in range(22)]
    records = [
        _record("111", "AUCell"),
        _record("112", "auCell"),                      # same surface name
        _record("113", "UCell"),                       # different algorithm
        _record("114", "inferCNV"),
        _record("115", "InferCNV"),                    # same surface name
        _record("116", "CopyKAT"),                     # different algorithm
        _record("117", "Seurat FindMarkers"),
        _record("118", "Seurat FindMarkers (Wilcoxon)"),
        _record("119", "AUCell gene set scoring"),
        _record("120", "ToolX v1.0"),
        _record("121", "ToolX"),
    ]
    reg, _, _ = _build(tmp_path, records, pmids)

    aucell = reg.get("AUCell")
    ucell = reg.get("UCell")
    assert aucell["id"] != ucell["id"]
    assert aucell["corpus"]["n_instances"] == 2
    assert set(aucell["aliases"]) == {"AUCell", "auCell"}

    infercnv = reg.get("inferCNV")
    assert infercnv["corpus"]["n_instances"] == 2
    assert reg.get("CopyKAT")["id"] != infercnv["id"]

    assert (reg.get("Seurat FindMarkers")["id"]
            != reg.get("Seurat FindMarkers (Wilcoxon)")["id"])
    assert (reg.get("AUCell")["id"]
            != reg.get("AUCell gene set scoring")["id"])
    # Version tokens are not stripped.
    assert reg.get("ToolX v1.0")["id"] != reg.get("ToolX")["id"]

    # Querying UCell must not drag in AUCell through an infix match.
    ucell_names = _names(reg.search("UCell", limit=50))
    assert "UCell" in ucell_names
    assert "AUCell" not in ucell_names


def test_long_tail_is_listed_without_frequency_truncation(tmp_path):
    records = []
    pmids = []
    pmid = 1000
    for method in range(10):
        for _ in range(3):
            pmid += 1
            pmids.append(str(pmid))
            records.append(_record(str(pmid), f"CommonMethod{method}"))
    pmids.append("9999")
    records.append(_record("9999", "RareMethod"))

    reg, _, _ = _build(tmp_path, records, pmids)
    page = reg.search(limit=100)
    assert page["total"] == 11
    assert "RareMethod" in _names(page)
    assert len(reg.load()["methods"]) == 11
    assert reg.stats()["counts"]["n_single_paper_methods"] == 1


def test_unknown_methods_are_kept_and_never_merged(tmp_path):
    first = _record("111", "placeholder", instance_id="111:methods:L1:P1:S1")
    first.update({"canonical_name": None, "mentioned_name": None,
                  "package": None, "operation": None,
                  "pipeline_stage": "other"})
    second = _record("111", "placeholder", instance_id="111:methods:L2:P1:S1")
    second.update({"canonical_name": None, "mentioned_name": None,
                   "package": None, "operation": None,
                   "pipeline_stage": "other"})
    reg, _, _ = _build(tmp_path, [first, second], ["111"])

    page = reg.search(limit=10)
    assert page["total"] == 2
    assert len({m["id"] for m in page["methods"]}) == 2
    for method in page["methods"]:
        assert method["canonical_name"].startswith("unknown method")
        assert method["name_source"] == "instance"
        assert method["completeness"]["incomplete"] is True
        assert method["completeness"]["stage_other"] is True
        assert {"name", "package", "operation"} <= set(
            method["completeness"]["unknown_fields"]
        )


# -------------------------------------------------------------------- search
def test_query_and_stage_filters_distinguish_matches(tmp_path):
    records = [
        _record("111", "Alpha", package="Seurat",
                operation="Wilcoxon rank-sum test", pipeline_stage="de"),
        _record("112", "Beta", package="AUCell",
                operation="AUCell scoring", pipeline_stage="enrichment"),
        _record("113", "Gamma", operation="graph clustering",
                pipeline_stage="clustering"),
    ]
    reg, _, _ = _build(tmp_path, records, ["111", "112", "113"])

    exact = reg.search("Alpha")
    assert _names(exact) == ["Alpha"]
    assert exact["methods"][0]["match"]["kind"] == "exact"
    assert "name" in exact["methods"][0]["match"]["fields"]

    assert _names(reg.search("Alp")) == ["Alpha"]
    assert reg.search("Alp")["methods"][0]["match"]["kind"] == "prefix"

    wilcoxon = reg.search("wilcoxon")
    assert _names(wilcoxon) == ["Alpha"]
    assert wilcoxon["methods"][0]["match"]["kind"] == "prefix"

    # Terms are ANDed after normalization ("rank-sum" -> rank + sum).
    rank_sum = reg.search("rank-sum")
    assert _names(rank_sum) == ["Alpha"]
    assert rank_sum["methods"][0]["match"]["kind"] == "token"

    assert _names(reg.search(stages="de")) == ["Alpha"]
    assert set(_names(reg.search(stages=["enrichment", "clustering"]))) == {"Beta", "Gamma"}
    assert reg.search("AUCell", stages="de")["total"] == 0
    assert reg.search("does-not-exist")["total"] == 0

    unknown_stage = reg.search(stages="not-a-stage")
    assert unknown_stage["total"] == 0
    assert any("not a stage" in note for note in unknown_stage["notes"])

    # Package matches are labelled so callers can tell them from name matches.
    beta = reg.search("AUCell")["methods"][0]
    assert beta["canonical_name"] == "Beta"
    assert "package" in beta["match"]["fields"]


def test_pagination_bounds_and_deterministic_order(tmp_path):
    names = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    records = [_record(str(100 + i), name) for i, name in enumerate(names)]
    reg, _, _ = _build(tmp_path, records, [str(100 + i) for i in range(5)])

    first = reg.search(limit=2, offset=0)
    assert (first["total"], first["returned"], first["has_more"]) == (5, 2, True)
    assert first["next_offset"] == 2
    assert _names(first) == ["Alpha", "Bravo"]

    second = reg.search(limit=2, offset=2)
    assert _names(second) == ["Charlie", "Delta"]
    assert second["next_offset"] == 4

    last = reg.search(limit=2, offset=4)
    assert _names(last) == ["Echo"]
    assert last["has_more"] is False
    assert last["next_offset"] is None

    beyond = reg.search(limit=2, offset=99)
    assert beyond["total"] == 5
    assert beyond["returned"] == 0
    assert beyond["has_more"] is False

    # Pages are disjoint and cover the whole list.
    paged = []
    offset = 0
    while True:
        page = reg.search(limit=2, offset=offset)
        paged.extend(_names(page))
        if not page["has_more"]:
            break
        offset = page["next_offset"]
    assert paged == names

    with pytest.raises(ValueError):
        reg.search(limit=0)
    with pytest.raises(ValueError):
        reg.search(offset=-1)
    with pytest.raises(TypeError):
        reg.search(limit="2")

    capped = reg.search(limit=10_000)
    assert capped["limit"] == MR.MAX_LIMIT
    assert capped["requested_limit"] == 10_000
    assert capped["limit_capped"] is True


def test_kinds_filter_keeps_pagination_correct(tmp_path):
    records = [_record("111", "Alpha"), _record("112", "Beta")]
    fake = {
        "name": "Fake adapter",
        "package": None,
        "stage": "de",
        "adapter": "r/fake.R::task_fake",
        "status": "executable",
        "requires": {},
        "provides": ["fake.csv"],
        "defaults": {},
        "statistical_notes": [],
        "literature_refs": [],
    }
    reg, _, _ = _build(tmp_path, records, ["111", "112"],
                       executable={"fake_adapter": fake})

    lit = reg.search(kinds="literature_method", limit=1)
    assert lit["total"] == 2
    assert lit["returned"] == 1
    assert lit["has_more"] is True
    assert lit["next_offset"] == 1
    assert {m["kind"] for m in lit["methods"]} == {"literature_method"}

    adapters = reg.search(kinds="executable_adapter")
    assert adapters["total"] == 1
    assert adapters["methods"][0]["kind"] == "executable_adapter"

    assert reg.search()["total"] == 3
    with pytest.raises(ValueError):
        reg.search(kinds="not-a-kind")


# ---------------------------------------------------------------- provenance
def test_evidence_provenance_and_corpus_hashes(tmp_path):
    records = [
        _record("111", "ProvenanceTool", locator="methods:L9:P2:S3",
                verbatim="Unique verbatim string.", instance_id="111:methods:L9:P2:S3"),
        _record("112", "NoVerbatimTool"),
    ]
    records[1]["verbatim"] = None
    reg, methods_path, pmid_path = _build(tmp_path, records, ["111", "112"])

    page = reg.search("ProvenanceTool")
    method = page["methods"][0]
    example = method["examples"][0]
    assert example["pmid"] == "111"
    assert example["locator"] == "methods:L9:P2:S3"
    assert example["verbatim"] == "Unique verbatim string."
    assert example["has_verbatim"] is True
    assert example["operation"] == "analysis"
    assert method["evidence_count"] == 1

    full = reg.get(method["id"])
    evidence = full["evidence"]
    assert len(evidence) == 1
    anchor = evidence[0]
    for key in ("instance_id", "pmid", "locator", "verbatim", "operation",
                "canonical_name", "package", "pipeline_stage",
                "rerunnable_role", "method_class"):
        assert key in anchor
    assert anchor["instance_id"] == "111:methods:L9:P2:S3"
    assert anchor["verbatim"] == "Unique verbatim string."

    # Missing source text stays missing; nothing is fabricated.
    missing = reg.get("NoVerbatimTool")["evidence"][0]
    assert missing["verbatim"] is None
    assert missing["has_verbatim"] is False

    stats = reg.stats()
    assert stats["corpus"]["sha256"] == hashlib.sha256(methods_path.read_bytes()).hexdigest()
    assert stats["corpus"]["n_records_parsed"] == 2
    assert stats["pmid_list"]["sha256"] == hashlib.sha256(pmid_path.read_bytes()).hexdigest()
    assert stats["pmid_list"]["n_pmids"] == 2


def test_get_keeps_every_anchor_beyond_the_three_examples(tmp_path):
    records = [
        _record(str(200 + i), "ManyAnchors",
                instance_id=f"{200 + i}:methods:L{i}:P1:S1",
                locator=f"methods:L{i}:P1:S1",
                verbatim=f"anchor {i}")
        for i in range(4)
    ]
    reg, _, _ = _build(tmp_path, records, [str(200 + i) for i in range(4)])

    page = reg.search("ManyAnchors")
    method = page["methods"][0]
    assert method["n_instances"] == 4
    assert method["evidence_count"] == 4
    assert len(method["examples"]) == 3

    full = reg.get(method["id"])
    assert len(full["evidence"]) == 4
    assert {e["locator"] for e in full["evidence"]} == {
        f"methods:L{i}:P1:S1" for i in range(4)
    }
    assert all(e["has_verbatim"] for e in full["evidence"])


def test_backing_filters_instances_by_row_stage(tmp_path):
    from sctda.catalog import EXECUTABLE

    records = [
        _record("501", "Wilcoxon rank-sum test", pipeline_stage="de"),
        _record("502", "Wilcoxon rank-sum test", pipeline_stage="metric"),
        _record("503", "Seurat FindMarkers (Wilcoxon)", pipeline_stage="other"),
        _record("504", "Seurat FindMarkers (Wilcoxon)", pipeline_stage="de"),
    ]
    reg, _, _ = _build(
        tmp_path, records, ["501", "502", "503", "504"],
        executable=EXECUTABLE,
    )

    # The corpus record itself keeps every instance; only adapter backing is
    # narrowed by the mapping row's stage predicate.
    wilcoxon_record = reg.get("Wilcoxon rank-sum test")
    assert wilcoxon_record["n_instances"] == 2
    assert {e["pipeline_stage"] for e in wilcoxon_record["evidence"]} == {
        "de", "metric",
    }

    backing = reg.backing("de_vs_rest_wilcox")
    assert backing["found"] is True
    assert backing["n_instances"] == 2
    assert backing["n_papers"] == 2
    assert {i["pmid"] for i in backing["instances"]} == {"501", "504"}
    assert all(i["pipeline_stage"] == "de" for i in backing["instances"])
    assert set(backing["mapped_canonical_names"]) == {
        "Wilcoxon rank-sum test", "Seurat FindMarkers (Wilcoxon)",
    }
    # The non-DE-only paper is not backing.
    assert "502" not in {i["pmid"] for i in backing["instances"]}

    adapter = reg.get("adapter:de_vs_rest_wilcox")
    links = {link["normalized_key"]: link for link in adapter["literature_links"]}
    assert links["wilcoxon rank sum test"]["required_stages"] == ["de"]
    assert links["wilcoxon rank sum test"]["n_instances"] == 1
    assert links["wilcoxon rank sum test"]["pmids"] == ["501"]
    assert links["seurat findmarkers wilcoxon"]["n_instances"] == 1
    de_stats = next(a for a in reg.stats()["adapters"]
                    if a["method_id"] == "de_vs_rest_wilcox")
    assert de_stats["n_mapped_instances"] == 2
    assert de_stats["n_mapped_papers"] == 2


def test_search_ranks_exact_name_before_package_hits(tmp_path):
    records = [_record("601", "Seurat")]
    records.extend(
        _record(str(610 + i), f"PackageFiller{i}", package="Seurat")
        for i in range(20)
    )
    records.append(_record("602", "Seurat FindMarkers", package="Seurat"))
    reg, _, _ = _build(
        tmp_path, records, [r["pmid"] for r in records], executable={},
    )

    top = reg.search("Seurat", limit=3)
    assert [m["canonical_name"] for m in top["methods"]] == [
        "Seurat", "Seurat FindMarkers", "PackageFiller0",
    ]
    assert top["methods"][0]["match"]["fields"] == ["name"]
    assert top["methods"][0]["match"]["name_exact"] is True
    filler = top["methods"][2]
    assert filler["match"]["fields"] == ["package"]
    assert filler["match"]["name_matched"] is False

    assert reg.search("Seurat", limit=1)["methods"][0]["canonical_name"] == "Seurat"
    assert (reg.search("Seurat FindMarkers", limit=1)["methods"][0]["canonical_name"]
            == "Seurat FindMarkers")


def test_load_and_stats_are_consistent(tmp_path):
    records = [_record("111", "Alpha"), _record("112", "Beta")]
    fake = {
        "name": "Fake adapter", "package": None, "stage": "de",
        "adapter": "r/fake.R::task_fake", "status": "executable",
        "requires": {}, "provides": [], "defaults": {},
        "statistical_notes": [], "literature_refs": [],
    }
    reg, _, _ = _build(tmp_path, records, ["111", "112"],
                       executable={"fake_adapter": fake})

    loaded = reg.load()
    stats = reg.stats()
    assert len(loaded["methods"]) == stats["counts"]["n_methods_total"]
    assert stats["counts"]["n_methods_literature"] == 2
    assert stats["counts"]["n_adapters"] == 1
    assert stats["counts"]["n_adapters_local_only"] == 1
    assert loaded["incomplete"] is True
    assert stats["incomplete"] is True
    assert any("known incomplete" in note for note in stats["notes"])
    assert any("No frequency threshold" in note for note in stats["notes"])

    ids = [m["id"] for m in loaded["methods"]]
    assert len(ids) == len(set(ids))
    for method in loaded["methods"]:
        assert method["support"]["status"] in {"executable", "corpus_only"}
        fetched = reg.get(method["id"])
        assert fetched is not None
        assert fetched["id"] == method["id"]
        assert "evidence" not in method

    with_evidence = reg.load(include_evidence=True)
    assert all("evidence" in m for m in with_evidence["methods"])


# --------------------------------------------------- adapter mapping (unit)
def test_adapter_definitions_are_read_not_copied(tmp_path):
    reg, _, _ = _build(tmp_path, [_record("111", "Alpha")], ["111"],
                       executable={})
    assert reg.stats()["counts"]["n_adapters"] == 0
    assert reg.get("adapter:composition") is None
    assert "EXECUTABLE" not in vars(MR)

    fake = {
        "name": "Fake adapter",
        "package": "FakePkg::FindFake",
        "stage": "de",
        "adapter": "r/fake.R::task_fake",
        "status": "executable",
        "requires": {"annotation_column": True},
        "provides": ["fake.csv"],
        "defaults": {"k": 1},
        "statistical_notes": ["no runtime probe"],
        "literature_refs": [
            {"pmid": "111", "locator": "methods:L1:P1:S1",
             "verbatim": "verified quote"},
            {"pmid": "112", "locator": "methods:L2:P1:S1",
             "note": "anchor without verbatim"},
        ],
    }
    reg2, _, _ = _build(tmp_path, [_record("111", "Alpha")], ["111"],
                        executable={"fake_adapter": fake})
    record = reg2.get("fake_adapter")
    assert record["kind"] == "executable_adapter"
    assert record["name"] == "Fake adapter"
    assert record["adapter"]["path"] == "r/fake.R::task_fake"
    assert [r["has_verbatim"] for r in record["literature_refs"]] == [True, False]
    assert record["support"]["status"] == "executable"
    # No corpus entry maps to this adapter: local adapter, no fabricated backing.
    assert record["support"]["mapping_quality"] == "local_adapter"
    backing = reg2.backing("fake_adapter")
    assert backing["quality"] == "local_adapter"
    assert backing["found"] is False
    assert backing["instances"] == []
    assert reg2.backing("not_an_adapter")["quality"] == "not_found"


# ------------------------------------------------------------ real snapshot
@pytest.fixture(scope="module")
def real_registry():
    try:
        return MR.build_registry()
    except FileNotFoundError as exc:
        pytest.skip(str(exc))


def test_real_corpus_scope_and_retraction(real_registry):
    stats = real_registry.stats()
    assert stats["scope"]["n_included_pmids"] == 405
    assert "36450980" in stats["scope"]["retracted_pmids"]
    assert stats["scope"]["n_effective_pmids"] == 404
    assert stats["corpus"]["sha256"]
    assert stats["corpus"]["n_parse_errors"] == 0

    all_pmids = {p for m in real_registry.load()["methods"] for p in m["pmids"]}
    assert "36450980" not in all_pmids
    assert stats["counts"]["records_excluded"]["retracted"]["records"] > 0


def test_real_corpus_four_adapter_mappings(real_registry):
    composition = real_registry.backing("composition")
    assert composition["quality"] == "local_adapter"
    assert composition["found"] is False
    assert composition["instances"] == []
    assert composition["mapped_canonical_names"] == []

    de = real_registry.backing("de_vs_rest_wilcox")
    assert de["quality"] == "operation"
    assert de["found"] is True
    assert de["instances"]
    de_names = {MR._normalize_key(name) for name in de["mapped_canonical_names"]}
    assert de_names
    forbidden_de = {
        "deseq2", "mast", "edger", "limma", "bimod",
        "seurat findallmarkers", "seurat findmarkers logistic regression",
        "wilcoxon signed rank test",
    }
    assert not (de_names & forbidden_de)
    assert all(instance["pmid"] != "36450980" for instance in de["instances"])
    # Per-row stage predicate: only DE-stage instances back the DE adapter.
    assert all(instance["pipeline_stage"] == "de" for instance in de["instances"])
    assert de["n_papers"] == len({i["pmid"] for i in de["instances"]})
    wilcoxon_record = real_registry.get("Wilcoxon rank-sum test")
    assert any(e["pipeline_stage"] != "de" for e in wilcoxon_record["evidence"])
    assert de["n_papers"] < wilcoxon_record["n_papers"]
    # Named by the review as a non-DE use that was previously miscounted.
    assert "38100545" not in {i["pmid"] for i in de["instances"]}

    aucell = real_registry.backing("signature_score_aucell")
    assert aucell["quality"] == "exact"
    assert aucell["found"] is True
    assert aucell["instances"]
    aucell_names = {MR._normalize_key(name) for name in aucell["mapped_canonical_names"]}
    assert "aucell" in aucell_names
    assert "ucell" not in aucell_names
    assert all("aucell" in MR._normalize_key(i["canonical_name"])
               for i in aucell["instances"])

    cnv = real_registry.backing("cnv_infercnv")
    assert cnv["quality"] == "exact"
    assert cnv["found"] is True
    assert cnv["instances"]
    cnv_names = {MR._normalize_key(name) for name in cnv["mapped_canonical_names"]}
    assert "infercnv" in cnv_names
    assert not any("copykat" in name for name in cnv_names)
    assert "infercnvpy" not in cnv_names
    assert all("copykat" not in MR._normalize_key(i["canonical_name"])
               for i in cnv["instances"])

    # Exact-name adapter lookup, no substring support.
    assert real_registry.adapters_for("UCell") == []
    assert real_registry.adapters_for("CopyKAT") == []
    assert real_registry.adapters_for("GSEA") == []
    assert real_registry.adapters_for("DESeq2") == []
    assert real_registry.adapters_for("Seurat FindMarkers") == []
    assert real_registry.adapters_for("AUCell")[0]["method_id"] == "signature_score_aucell"
    seurat = real_registry.adapters_for("Seurat")
    assert seurat and seurat[0]["quality"] == "toolkit"
    assert seurat[0]["method_id"] == "de_vs_rest_wilcox"

    # Only the adapter records are executable definitions. A literature record
    # that names the same tool/operation stays corpus_only and exposes the
    # relation separately, so "Seurat" is never reported as executable.
    literature_aucell = real_registry.get("AUCell")
    assert literature_aucell["support"]["status"] == "corpus_only"
    assert literature_aucell["support"]["adapter_method_id"] is None
    assert literature_aucell["support"]["mapping_quality"] == "exact"
    assert literature_aucell["support"]["adapters"][0]["method_id"] == (
        "signature_score_aucell"
    )
    literature_seurat = real_registry.get("Seurat")
    assert literature_seurat["support"]["status"] == "corpus_only"
    assert literature_seurat["support"]["adapters"][0]["quality"] == "toolkit"
    adapter_record = real_registry.get("adapter:signature_score_aucell")
    assert adapter_record["support"]["status"] == "executable"
    assert adapter_record["support"]["adapter_method_id"] == "signature_score_aucell"


def test_real_corpus_unified_list_and_disambiguation(real_registry):
    composition = real_registry.get("composition")
    assert composition["kind"] == "executable_adapter"
    assert composition["support"]["mapping_quality"] == "local_adapter"

    assert real_registry.search(kinds="executable_adapter")["total"] == 4
    lit_page = real_registry.search(query="AUCell", kinds="literature_method", limit=20)
    assert lit_page["total"] > 0
    assert all(m["kind"] == "literature_method" for m in lit_page["methods"])

    aucell = real_registry.get("AUCell")
    ucell = real_registry.get("UCell")
    assert aucell["id"] != ucell["id"]
    assert real_registry.get("Seurat")["id"] != real_registry.get("Seurat FindMarkers")["id"]
    assert real_registry.get("aUcElL")["id"] == aucell["id"]

    seurat_page = real_registry.search("Seurat", limit=5)
    assert seurat_page["methods"][0]["canonical_name"] == "Seurat"
    assert seurat_page["methods"][0]["match"]["fields"] == ["name"]
    assert seurat_page["methods"][0]["match"]["name_exact"] is True
