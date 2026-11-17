"""Tests for the literature-line tooling.

These exercise the pieces that decide whether a literature record survives:
the anchor validator, the locator repairer, and the merge. They run against the
real brief corpus because a synthetic brief would not reproduce the failure modes
(line drift, a quote that no longer exists, one sentence describing two
operations).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import repair_locators as RL  # noqa: E402

PMID = "32997960"
REAL_QUOTE = "Single cell enrichment score was run using AUCell (version 1.6.0) in R"
FAKE_QUOTE = "This sentence was never written by the authors of this paper."


@pytest.fixture(scope="module")
def index():
    idx = RL.sentence_index(PMID)
    if not idx:
        pytest.skip("brief corpus unavailable")
    return idx


def test_repair_moves_a_drifted_locator_to_the_real_line(index):
    rec = {"pmid": PMID, "locator": "methods:L1:P1:S1", "verbatim": REAL_QUOTE}
    kept, log = RL.repair_rows([rec], index)
    assert len(kept) == 1
    loc = kept[0]["locator"]
    assert loc != "methods:L1:P1:S1"
    assert REAL_QUOTE in dict(index)[loc]
    assert any(x.get("new") == loc for x in log)


def test_repair_drops_a_record_whose_quote_does_not_exist(index):
    rec = {"pmid": PMID, "locator": "methods:L1:P1:S1", "verbatim": FAKE_QUOTE}
    kept, log = RL.repair_rows([rec], index)
    assert kept == []
    assert any(x.get("reason") == "quote not found in brief" for x in log)


def test_repair_keeps_the_workflow_and_drops_only_the_bad_claim(index):
    """One unverifiable claim must not cost the whole reasoning path."""
    wf = {
        "workflow_id": f"{PMID}:wf1",
        "pmid": PMID,
        "question": {"text": "q", "evidence": [{"locator": "abstract:L5:P1:S2",
                                                "verbatim": "To identify determinants of bone marrow failure"}]},
        "comparison": "FA vs healthy",
        "cell_level": "HSPC clusters",
        "steps": [{"observation": "o", "locator": "methods:L1:P1:S1",
                   "verbatim": REAL_QUOTE, "next_analysis": "n", "rationale": "r"}],
        "claims": [
            {"claim": "good", "locator": "methods:L1:P1:S1", "verbatim": REAL_QUOTE,
             "evidence_type": "expression", "direction": "supports"},
            {"claim": "fabricated", "locator": "methods:L1:P1:S1",
             "verbatim": FAKE_QUOTE, "evidence_type": "expression",
             "direction": "supports"},
        ],
        "gaps": [],
        "author_focus_groups": [],
        "no_prior_warning": "author_focus_groups are recorded for context only; they "
                            "MUST NOT seed SCTDA candidate sets, marker whitelists or "
                            "ranking weights",
    }
    kept, log = RL.repair_rows([wf], index)
    assert len(kept) == 1, "the workflow must survive"
    claims = kept[0]["claims"]
    assert len(claims) == 1 and claims[0]["claim"] == "good"
    assert any(x.get("dropped_item") for x in log)


def test_repair_refuses_to_repoint_a_short_quote(index):
    """A 3-word quote matches everywhere; pointing it is a guess, not a repair."""
    rec = {"pmid": PMID, "locator": "methods:L1:P1:S1", "verbatim": "Principal"}
    kept, log = RL.repair_rows([rec], index)
    assert kept == []
    assert any(x.get("reason") == "quote not found in brief" for x in log)


def test_merge_discriminates_two_operations_from_one_sentence(tmp_path):
    """A sentence describing two operations must yield two records, not one."""
    sys.path.insert(0, str(ROOT / "tools"))
    import merge_research as MR
    shard = tmp_path / "shards" / "bXX"
    shard.mkdir(parents=True)
    (shard / "pmids.txt").write_text(PMID + "\n")
    (shard / "done.txt").write_text(PMID + "\n")
    base = {"pmid": PMID, "locator": "methods:L1:P1:S1", "verbatim": REAL_QUOTE,
            "in_scope": True, "pipeline_stage": "dimred", "analysis_object": "cells",
            "method_class": "computational", "scope_reason": "computational analysis",
            "rerunnable_role": "rerunnable"}
    rows = [dict(base, instance_id=f"{PMID}:methods:L1:P1:S1", canonical_name="zeta"),
            dict(base, instance_id=f"{PMID}:methods:L1:P1:S1", canonical_name="alpha")]
    (shard / "methods.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n")
    (shard / "workflows.jsonl").touch()
    (shard / "code_sources.jsonl").touch()
    for name in ("methods.jsonl", "workflows.jsonl", "code_sources.jsonl"):
        shutil_target = ROOT / "research" / name
        assert shutil_target.exists()
    # drive the merge in-process over just this shard
    MR.BAD_RECORDS.clear()
    per_file: dict[str, dict[str, list[dict]]] = {f: {} for f in MR.KEYS}
    for rec in MR.read_jsonl(shard / "methods.jsonl"):
        per_file["methods.jsonl"].setdefault(rec["instance_id"], []).append(rec)
    keys = per_file["methods.jsonl"][f"{PMID}:methods:L1:P1:S1"]
    ordered = sorted(keys, key=lambda r: str(r.get("canonical_name")))
    ids = [r["instance_id"] if i == 0 else f'{r["instance_id"]}#{i}'
           for i, r in enumerate(ordered)]
    assert len(set(ids)) == 2, ids
    assert ids[0].endswith(":S1") and ids[1].endswith("#1")
