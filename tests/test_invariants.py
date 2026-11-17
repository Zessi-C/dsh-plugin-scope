"""Permanent behaviour tests for the invariants that matter.

Six things must not silently break:
  1. a plan is frozen when it is written and runs without an approval step;
     replacing it writes a new version and never edits the old row;
  2. every candidate keeps its own outcome;
  3. a missing input is not a zero result, and a missing sample-level table is
     `not_computed`, not "no effect";
  4. resuming does not duplicate artifacts or re-run completed work;
  5. evidence derived from our own computation never counts as independent
     external support;
  6. the cell-level comparison is the reported result and the sample-level one
     only supplements it.

These test software behaviour on a synthetic object. They are not a biological
benchmark and no result here is a finding.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sctda import protocol as P
from sctda.evidence import add_evidence, ensure_claim, independence
from sctda.execution import run_protocol
from sctda.ingest import enumerate_candidates
from sctda.project import Project, canonical_json, new_id, now, sha256_text


# --------------------------------------------------------------- 0. import contract
def test_import_is_readonly_and_keeps_source_hash(project, synthetic_object):
    conn = Project(project).connect()
    ds = conn.execute("SELECT * FROM dataset").fetchone()
    assert Path(ds["source_path"]) == synthetic_object
    # recorded hash must equal the file's current hash: nothing wrote to it
    from sctda.project import sha256_file
    assert ds["source_sha256"] == sha256_file(synthetic_object)
    conn.close()


def test_candidates_partition_the_annotated_groups(project, conn):
    """Candidates are exactly the annotated groups, and group counts sum to the dataset cell count."""
    cands = enumerate_candidates(conn, conn.execute(
        "SELECT dataset_id FROM dataset").fetchone()["dataset_id"], "celltype")
    labels = {c["label"] for c in cands}
    assert labels == {"Alpha", "Beta"}
    # every cell is accounted for by the annotated group candidates
    total = sum(c["n_cells"] for c in cands)
    assert total == conn.execute("SELECT n_cells FROM dataset").fetchone()["n_cells"]

def test_unlabeled_cells_are_not_candidates_by_default_unless_explicitly_requested(project, conn):
    from sctda.ingest import map_column
    ds = conn.execute("SELECT dataset_id FROM dataset").fetchone()["dataset_id"]
    # Default behavior: NAs reported as data quality metric (n_na), candidates are annotated groups only
    out = map_column(conn, ds, "annotation", "unlabeled_probe", confirmed_by="test")
    assert out["status"] == "mapped", out
    assert out.get("n_na", 0) > 0, "n_na must be reported in mapping output"
    cands = out["candidates"]
    unl = [c for c in cands if c["is_unlabeled"]]
    assert len(unl) == 0, "unannotated cells must not become candidates by default"
    assert {c["label"] for c in cands} == {"Alpha", "Beta"}

    # Explicit request: caller explicitly asks for the NA group to be a candidate
    out_explicit = map_column(conn, ds, "annotation", "unlabeled_probe", confirmed_by="test", include_unlabeled=True)
    assert out_explicit["status"] == "mapped", out_explicit
    cands_exp = out_explicit["candidates"]
    unl_exp = [c for c in cands_exp if c["is_unlabeled"]]
    assert len(unl_exp) == 1 and unl_exp[0]["label"] == "__unlabeled__" and unl_exp[0]["n_cells"] > 0
    assert {c["label"] for c in cands_exp if not c["is_unlabeled"]} == {"Alpha", "Beta"}

def test_ambiguous_columns_are_listed_not_guessed(bare_project):
    out = json.loads((Project(bare_project).db_path.parent / "meta")
                     .glob("inspect_*.json").__next__().read_text())
    names = {c["column_name"] for c in out["columns"]}
    # the real columns are all visible, including the one-level degenerate ones
    assert {"celltype", "orig.ident", "group", "unlabeled_probe"} <= names
    conn = Project(bare_project).connect()
    assert conn.execute("SELECT COUNT(*) c FROM mapping").fetchone()["c"] == 0
    conn.close()


# ------------------------------------------------------- 1. protocol immutability
def test_a_plan_is_frozen_when_written_and_runs_without_an_approval_step(project, conn):
    """There is no gate: writing the plan is the freeze, and run executes it."""
    plan = P.build_plan(project, methods=["composition"])
    pid = plan["protocol_id"]
    assert plan["status"] == "frozen", plan
    assert plan["next"].startswith("sctda run")
    row = conn.execute("SELECT status, plan_sha256 FROM protocol WHERE protocol_id=?",
                       (pid,)).fetchone()
    assert row["status"] == "frozen"
    assert row["plan_sha256"] == plan["plan_sha256"]
    assert P.freeze_check(conn, pid)["ok"] is True
    assert P.load_protocol(conn, pid)["is_runnable"] is True
    out = run_protocol(project, pid)
    assert out["status"] == "completed", out


def test_tampering_with_a_frozen_plan_is_detected(project):
    plan = P.build_plan(project, methods=["composition"])
    pid = plan["protocol_id"]
    conn = Project(project).connect()
    assert P.freeze_check(conn, pid)["ok"] is True
    # forge the stored plan without updating the hash
    blob = conn.execute("SELECT plan_json FROM protocol WHERE protocol_id=?",
                        (pid,)).fetchone()["plan_json"]
    doc = json.loads(blob)
    doc["candidate_universe"] = doc["candidate_universe"][:1]
    conn.execute("UPDATE protocol SET plan_json=? WHERE protocol_id=?",
                 (canonical_json(doc), pid))
    conn.commit()
    check = P.freeze_check(conn, pid)
    assert check["ok"] is False
    assert check["stored"] != check["actual"]
    conn.close()


def test_a_new_version_supersedes_a_replaced_plan_without_editing_it(project, conn):
    """A replaced frozen plan keeps its hash and version, and stops being runnable."""
    first = P.build_plan(project, methods=["composition"])
    pid = first["protocol_id"]
    second = P.build_plan(project, methods=["composition", "de_vs_rest_wilcox"])
    row = conn.execute("SELECT status, plan_sha256, version, superseded_by FROM protocol "
                       "WHERE protocol_id=?", (pid,)).fetchone()
    assert row["status"] == "superseded"
    assert row["superseded_by"] == second["protocol_id"]
    assert row["plan_sha256"] == first["plan_sha256"]
    assert row["version"] == first["version"]
    out = run_protocol(project, pid)
    assert out["status"] == "failed" and "superseded" in out["error"], out


def test_revision_writes_a_new_frozen_version_and_leaves_the_old_one_intact(project):
    plan = P.build_plan(project, methods=["composition"])
    pid = plan["protocol_id"]
    before = P.load_protocol(Project(project).connect(), pid)
    rev = P.revise(project, pid, {"question": "does Alpha differ from Beta?"})
    assert rev["status"] == "frozen" and rev["parent_protocol_id"] == pid
    after = P.load_protocol(Project(project).connect(), pid)
    assert after["plan_sha256"] == before["plan_sha256"]
    # the parent never ran, so it is marked replaced rather than left runnable
    assert after["status"] == "superseded"
    new = P.load_protocol(Project(project).connect(), rev["protocol_id"])
    assert new["version"] == before["version"] + 1
    assert new["plan"]["question"] == "does Alpha differ from Beta?"


def test_a_comparison_no_task_computes_is_refused_not_recorded(project, conn):
    """A condition contrast has no adapter; freezing it would read as a result."""
    before = conn.execute("SELECT COUNT(*) c FROM protocol").fetchone()["c"]
    out = P.build_plan(project, methods=["composition", "de_vs_rest_wilcox"],
                       comparisons=["treat vs ctrl"])
    assert out["status"] == "failed", out
    assert out["unsupported_comparisons"]
    assert "no task computes" in out["error"]
    assert out["supported_form"].startswith("'<annotated candidate label> vs rest'")
    assert conn.execute("SELECT COUNT(*) c FROM protocol").fetchone()["c"] == before, \
        "a refused comparison must not freeze a plan"


def test_a_declared_comparison_names_the_task_that_computes_it(project, conn):
    plan = P.build_plan(project, methods=["de_vs_rest_wilcox"],
                        comparisons=["Alpha vs rest"])
    assert plan["status"] == "frozen", plan
    doc = P.load_protocol(conn, plan["protocol_id"])["plan"]
    assert doc["comparisons"] == ["Alpha vs rest"]
    assert doc["comparisons_specified"] is True
    assert doc["comparisons_resolved"] == [{
        "declared": "Alpha vs rest", "comparison": "Alpha vs rest",
        "candidate_label": "Alpha", "computed_by": ["de_vs_rest_wilcox::Alpha"]}]
    # the same declaration without the method that computes it is refused
    refused = P.build_plan(project, methods=["composition"],
                           comparisons=["Alpha vs rest"])
    assert refused["status"] == "failed", refused
    assert "de_vs_rest_wilcox is not in the plan" in refused["error"]
    # so is a label that is not a candidate
    refused2 = P.build_plan(project, methods=["de_vs_rest_wilcox"],
                            comparisons=["Gamma vs rest"])
    assert refused2["status"] == "failed", refused2
    assert "not a candidate" in refused2["error"]


def test_dataset_wide_methods_queue_one_task_not_one_per_candidate(project, conn):
    plan = P.build_plan(project, methods=["signature_score_aucell", "composition",
                                          "de_vs_rest_wilcox"])
    doc = P.load_protocol(conn, plan["protocol_id"])["plan"]
    n_cands = len(doc["candidate_universe"])
    tasks = doc["tasks"]
    assert [t["task_key"] for t in tasks
            if t["method_id"] == "signature_score_aucell"] == ["signature_score_aucell"]
    assert [t["task_key"] for t in tasks
            if t["method_id"] == "composition"] == ["composition"]
    assert len([t for t in tasks if t["method_id"] == "de_vs_rest_wilcox"]) == n_cands
    assert len(tasks) == 2 + n_cands, tasks
    assert plan["n_tasks"] == len(tasks)


def test_revising_methods_rebuilds_the_task_graph(project, conn):
    plan = P.build_plan(project, methods=["de_vs_rest_wilcox"])
    rev = P.revise(project, plan["protocol_id"], {"methods": ["composition"]})
    assert rev["status"] == "frozen", rev
    assert rev["task_graph_rebuilt"] is True
    new = P.load_protocol(conn, rev["protocol_id"])["plan"]
    assert [m["method_id"] for m in new["methods"]] == ["composition"]
    assert [t["task_key"] for t in new["tasks"]] == ["composition"]
    assert new["expected_artifacts"] == sorted({p for p in
                                                P.EXECUTABLE["composition"]["provides"]})
    assert set(new["literature_basis"]) == {"composition"}
    assert new["parameters"] == {"composition": P.EXECUTABLE["composition"]["defaults"]}
    assert new["gaps"] == [], new["gaps"]


def test_revising_parameters_reaches_every_task(project, conn):
    plan = P.build_plan(project, methods=["de_vs_rest_wilcox"])
    rev = P.revise(project, plan["protocol_id"],
                   {"parameters": {"de_vs_rest_wilcox": {"min_cells": 7}}})
    assert rev["status"] == "frozen", rev
    new = P.load_protocol(conn, rev["protocol_id"])["plan"]
    assert new["parameters"]["de_vs_rest_wilcox"]["min_cells"] == 7
    de_tasks = [t for t in new["tasks"] if t["method_id"] == "de_vs_rest_wilcox"]
    assert de_tasks and all(t["params"]["min_cells"] == 7 for t in de_tasks)
    # a later revision that keeps the method keeps the declared parameter
    later = P.revise(project, rev["protocol_id"],
                     {"methods": ["de_vs_rest_wilcox", "composition"]})
    assert later["status"] == "frozen", later
    kept = P.load_protocol(conn, later["protocol_id"])["plan"]
    assert kept["parameters"]["de_vs_rest_wilcox"]["min_cells"] == 7
    assert kept["parameters"]["composition"] == {}
    # a misspelled parameter (or one for a method not in the plan) is refused
    bad = P.revise(project, plan["protocol_id"],
                   {"parameters": {"de_vs_rest_wilcox": {"min_cels": 7}}})
    assert bad["status"] == "failed" and "unknown parameter" in bad["error"]
    stray = P.revise(project, plan["protocol_id"],
                     {"parameters": {"composition": {"min_cells": 7}}})
    assert stray["status"] == "failed" and "not in the plan" in stray["error"]


def test_revise_refuses_derived_keys_and_candidate_selection(project, conn):
    plan = P.build_plan(project, methods=["composition"])
    pid = plan["protocol_id"]
    n_before = conn.execute("SELECT COUNT(*) c FROM protocol").fetchone()["c"]
    tasks = P.revise(project, pid, {"tasks": []})
    assert tasks["status"] == "failed" and "derived" in tasks["error"]
    universe = P.load_protocol(conn, pid)["plan"]["candidate_universe"]
    narrowed = P.revise(project, pid, {"candidate_universe": universe[:1]})
    assert narrowed["status"] == "failed" and "candidate universe" in narrowed["error"]
    misspelled = P.revise(project, pid, {"analysis_units": {"cell_level_rul": {}}})
    assert misspelled["status"] == "failed" and "unknown analysis_units" in misspelled["error"]
    assert conn.execute("SELECT COUNT(*) c FROM protocol").fetchone()["c"] == n_before


def test_a_revision_cannot_add_an_uncomputed_comparison(project, conn):
    plan = P.build_plan(project, methods=["composition"])
    pid = plan["protocol_id"]
    out = P.revise(project, pid, {"comparisons": ["treat vs ctrl"]})
    assert out["status"] == "failed", out
    assert out["unsupported_comparisons"]
    # the supported form is accepted only together with the method that computes it
    out2 = P.revise(project, pid, {"methods": ["composition", "de_vs_rest_wilcox"],
                                   "comparisons": ["Alpha vs rest"]})
    assert out2["status"] == "frozen", out2
    doc = P.load_protocol(conn, out2["protocol_id"])["plan"]
    assert doc["comparisons_resolved"][0]["computed_by"] == ["de_vs_rest_wilcox::Alpha"]


def test_a_running_plan_is_not_superseded_by_writing_a_new_version(project, conn):
    """Replacing a plan must not invalidate an execution that already started."""
    first = P.build_plan(project, methods=["composition"])
    pid = first["protocol_id"]
    out = run_protocol(project, pid)
    assert out["status"] == "completed"
    conn.execute("UPDATE protocol SET status='paused' WHERE protocol_id=?", (pid,))
    conn.commit()
    P.build_plan(project, methods=["composition", "de_vs_rest_wilcox"])
    status = conn.execute("SELECT status FROM protocol WHERE protocol_id=?",
                          (pid,)).fetchone()["status"]
    assert status == "paused"


# ------------------------------------------------- 2. every candidate keeps a status
def test_every_candidate_gets_its_own_task_and_outcome(project):
    plan = P.build_plan(project, methods=["de_vs_rest_wilcox"])
    pid = plan["protocol_id"]
    out = run_protocol(project, pid)
    assert out["status"] == "completed", out
    conn = Project(project).connect()
    rows = conn.execute("SELECT candidate_id, status FROM task WHERE protocol_id=? "
                        "AND candidate_id IS NOT NULL", (pid,)).fetchall()
    n_cands = conn.execute("SELECT COUNT(*) c FROM candidate").fetchone()["c"]
    assert len(rows) == n_cands
    assert all(r["status"] == "succeeded" for r in rows)
    conn.close()


# --------------------------------------------------- 3. missing input is not zero
def test_method_without_its_input_is_insufficient_not_zero(project, conn):
    """AUCell without a gene set snapshot must not produce a zero score."""
    plan = P.build_plan(project, methods=["signature_score_aucell"])
    pid = plan["protocol_id"]
    assert any(g["method_id"] == "signature_score_aucell" for g in plan["gaps"]), \
        "missing gene sets must be recorded as a plan gap"
    out = run_protocol(project, pid)
    assert out["outcomes"].get("insufficient_input", 0) > 0
    row = conn.execute("SELECT status, reason FROM task WHERE protocol_id=?",
                       (pid,)).fetchone()
    assert row["status"] == "insufficient_input"
    assert "no gene sets" in (row["reason"] or "")
    n_art = conn.execute("""SELECT COUNT(*) c FROM artifact WHERE protocol_id=?""",
                         (pid,)).fetchone()["c"]
    assert n_art == 0, "a missing input must not deposit artifacts"


def test_annotation_remap_after_a_run_is_refused(project, conn):
    """Re-pointing the annotation column would orphan computed tasks."""
    from sctda.ingest import map_column
    plan = P.build_plan(project, methods=["composition"])
    run_protocol(project, plan["protocol_id"])
    ds = conn.execute("SELECT dataset_id FROM dataset").fetchone()["dataset_id"]
    out = map_column(conn, ds, "annotation", "unlabeled_probe")
    assert out["status"] == "failed"
    assert "invalidate" in out["error"]
    assert conn.execute("SELECT column_name FROM mapping WHERE role='annotation'"
                        ).fetchone()["column_name"] == "celltype"


def test_no_condition_column_means_no_group_statement(project, conn):
    conn.execute("DELETE FROM mapping WHERE role='condition'")
    conn.commit()
    design = P.design_table(project)
    assert any("no condition column" in w for w in design["warnings"])
    assert not design["condition_levels"]


# ------------------------------------------------------------ 3b. pause / resume
def test_pause_requires_a_frozen_or_running_plan(project):
    plan = P.build_plan(project, methods=["composition"])
    pid = plan["protocol_id"]
    paused = P.pause(project, pid, reason="operator stopped it")
    assert paused["status"] == "paused"
    assert paused["resume_with"].endswith("--resume")
    # a completed plan cannot be paused
    run_protocol(project, pid, resume=True)
    conn = Project(project).connect()
    conn.execute("UPDATE protocol SET status='completed' WHERE protocol_id=?", (pid,))
    conn.commit()
    conn.close()
    again = P.pause(project, pid)
    assert again["status"] == "failed" and "completed" in again["error"]


def test_pause_keeps_state_and_resume_finishes(project):
    plan = P.build_plan(project, methods=["composition"])
    pid = plan["protocol_id"]
    first = run_protocol(project, pid)
    assert first["status"] == "completed"
    # already completed: a pause is refused (covered by the previous test)
    conn = Project(project).connect()
    n_tasks = conn.execute("SELECT COUNT(*) c FROM task WHERE protocol_id=?",
                           (pid,)).fetchone()["c"]
    n_art = conn.execute("SELECT COUNT(*) c FROM artifact WHERE protocol_id=?",
                         (pid,)).fetchone()["c"]
    # simulate a pause that happened mid-run
    conn.execute("UPDATE protocol SET status='paused' WHERE protocol_id=?", (pid,))
    conn.execute("UPDATE task SET status='pending' WHERE protocol_id=?", (pid,))
    conn.commit()
    conn.close()
    out = run_protocol(project, pid, resume=True)
    assert out["status"] == "completed"
    conn = Project(project).connect()
    assert conn.execute("SELECT COUNT(*) c FROM task WHERE protocol_id=?",
                        (pid,)).fetchone()["c"] == n_tasks
    assert conn.execute("SELECT COUNT(*) c FROM artifact WHERE protocol_id=?",
                        (pid,)).fetchone()["c"] == n_art, "resume must not duplicate artifacts"
    conn.close()


# ------------------------------------------------------- 4. resume is idempotent
def test_resume_does_not_duplicate_artifacts_or_rerun(project):
    plan = P.build_plan(project, methods=["composition", "de_vs_rest_wilcox"])
    pid = plan["protocol_id"]
    first = run_protocol(project, pid)
    assert first["status"] == "completed"
    conn = Project(project).connect()
    n_art = conn.execute("SELECT COUNT(*) c FROM artifact WHERE protocol_id=?",
                         (pid,)).fetchone()["c"]
    assert n_art > 0
    conn.close()

    second = run_protocol(project, pid, resume=True)
    assert second["status"] == "completed"
    assert second["ran"] == 0 and second["skipped"] == first["ran"]
    conn = Project(project).connect()
    n_art2 = conn.execute("SELECT COUNT(*) c FROM artifact WHERE protocol_id=?",
                          (pid,)).fetchone()["c"]
    assert n_art2 == n_art, "resume must not add artifact rows"
    conn.close()


def test_artifact_rows_are_restored_when_the_db_lost_them(project):
    plan = P.build_plan(project, methods=["composition"])
    pid = plan["protocol_id"]
    run_protocol(project, pid)
    conn = Project(project).connect()
    had = conn.execute("SELECT COUNT(*) c FROM artifact WHERE protocol_id=?",
                       (pid,)).fetchone()["c"]
    assert had > 0
    conn.execute("DELETE FROM artifact WHERE protocol_id=?", (pid,))
    conn.commit()
    conn.close()
    out = run_protocol(project, pid, resume=True)
    assert out["ran"] == 0
    conn = Project(project).connect()
    now_n = conn.execute("SELECT COUNT(*) c FROM artifact WHERE protocol_id=?",
                         (pid,)).fetchone()["c"]
    assert now_n == had, "resume must repair missing artifact index rows"
    conn.close()


# ------------------------------------------------ 3c. dependency enforcement
def test_task_with_a_failed_dependency_does_not_run(project):
    """A task whose prerequisite did not succeed must not produce output.

    Build a frozen plan, then hand-edit its stored task graph so the DE tasks
    depend on the gene-set scoring task. `revise` refuses derived keys on
    purpose, so the fixture writes the plan row directly: this test is about the
    execution-side dependency gate, not about plan revision.
    """
    plan = P.build_plan(project, methods=["signature_score_aucell",
                                          "de_vs_rest_wilcox"])
    conn = Project(project).connect()
    try:
        row = conn.execute("SELECT plan_json FROM protocol WHERE protocol_id=?",
                           (plan["protocol_id"],)).fetchone()
        doc = json.loads(row["plan_json"])
        scoring = [t["task_key"] for t in doc["tasks"]
                   if t["method_id"] == "signature_score_aucell"]
        # AUCell scores the whole object once, so there is exactly one scoring task
        assert scoring == ["signature_score_aucell"], scoring
        for t in doc["tasks"]:
            if t["method_id"] == "de_vs_rest_wilcox":
                t["depends_on"] = scoring
        # write the edited graph as a properly frozen version (hash matches), so the
        # fixture stays valid if execution starts verifying the frozen hash too
        blob = canonical_json(doc)
        version = conn.execute("SELECT COALESCE(MAX(version),0)+1 v FROM protocol "
                               "WHERE project_id=(SELECT project_id FROM protocol "
                               "WHERE protocol_id=?)",
                               (plan["protocol_id"],)).fetchone()["v"]
        pid = new_id("prot")
        conn.execute("""INSERT INTO protocol(protocol_id, project_id, version, status,
                          created_utc, plan_json, plan_sha256, parent_protocol_id)
                        SELECT ?, project_id, ?, 'frozen', ?, ?, ?, protocol_id
                        FROM protocol WHERE protocol_id=?""",
                     (pid, version, now(), blob, sha256_text(blob),
                      plan["protocol_id"]))
        conn.commit()
    finally:
        conn.close()
    out = run_protocol(project, pid)
    assert out["status"] == "completed", out
    conn = Project(project).connect()
    rows = {r["method_id"]: r["status"] for r in conn.execute(
        "SELECT method_id, status FROM task WHERE protocol_id=?", (pid,))}
    assert rows["signature_score_aucell"] == "insufficient_input"
    assert rows["de_vs_rest_wilcox"] == "insufficient_input", rows
    reason = conn.execute("SELECT reason FROM task WHERE protocol_id=? AND "
                          "method_id='de_vs_rest_wilcox'", (pid,)).fetchone()["reason"]
    assert "dependency" in reason
    n_art = conn.execute("SELECT COUNT(*) c FROM artifact WHERE protocol_id=?",
                         (pid,)).fetchone()["c"]
    assert n_art == 0, "a blocked task must not deposit artifacts"
    conn.close()


# ------------------------------------------- 4b. incremental re-evaluation
def _artifact_for(conn, protocol_id: str, kind: str, like: str) -> dict:
    row = conn.execute("SELECT * FROM artifact WHERE protocol_id=? AND kind=? "
                       "AND path LIKE ?", (protocol_id, kind, f"%{like}%")).fetchone()
    assert row, f"no {kind} artifact for {like}"
    return dict(row)


def test_refresh_reevaluates_only_claims_with_changed_artifacts(project, conn):
    """A changed artifact must re-derive its own claims and leave the rest alone."""
    from sctda.reasoning import derive_evidence, refresh_evidence
    plan = P.build_plan(project, methods=["composition", "de_vs_rest_wilcox"])
    pid = plan["protocol_id"]
    run_protocol(project, pid)
    derive_evidence(project, pid)

    claims_before = {r["claim_id"]: r["statement"] for r in
                     conn.execute("SELECT claim_id, statement FROM claim WHERE protocol_id=?",
                                  (pid,))}
    assert claims_before
    no_change = refresh_evidence(project, pid)
    assert no_change["recomputed"] == 0 and no_change["claims_superseded"] == 0

    # simulate a re-run that produced different bytes for ONE candidate
    art = _artifact_for(conn, pid, "de_cell_level", "")
    path = Path(art["path"])
    lines = path.read_text().splitlines()
    # drop every gene row so the derived gene counts change
    path.write_text("\n".join([lines[0]]) + "\n")
    import hashlib
    conn.execute("UPDATE artifact SET checksum=? WHERE artifact_id=?",
                 (hashlib.md5(path.read_bytes()).hexdigest(), art["artifact_id"]))
    conn.commit()

    out = refresh_evidence(project, pid)
    assert out["stale_claims_detected"] > 0
    assert len(out["candidates_reevaluated"]) == 1, out["candidates_reevaluated"]
    affected = out["candidates_reevaluated"][0]
    # the other candidate's claims are untouched
    untouched = conn.execute(
        "SELECT COUNT(*) c FROM claim WHERE protocol_id=? AND subject_id NOT IN "
        "(SELECT candidate_id FROM candidate WHERE label=?)",
        (pid, affected)).fetchone()["c"]
    assert untouched > 0
    # a changed statement leaves the old claim marked and logged, not deleted
    assert out["claims_superseded"] >= 1
    old = conn.execute("SELECT status FROM claim WHERE protocol_id=? AND status='superseded'",
                       (pid,)).fetchone()
    assert old is not None
    dec = conn.execute("SELECT action, rationale FROM decision WHERE protocol_id=? "
                       "AND action='claim_superseded'", (pid,)).fetchone()
    assert dec is not None and "artifact" in dec["rationale"]


# --------------------------------- 4c. cell level primary, sample level supplementary
def test_cell_level_is_primary_and_sample_level_only_supplements(project, conn):
    """The claim is the cell-level result; the sample-level table adds a sentence."""
    from sctda.reasoning import NO_RULE_SOURCE, build_dossiers, derive_evidence
    plan = P.build_plan(project, methods=["de_vs_rest_wilcox"])
    pid = plan["protocol_id"]
    units = P.load_protocol(Project(project).connect(), pid)["plan"]["analysis_units"]
    assert units["statistical_unit"].startswith("cell"), units
    assert units["sample_level_rule"]["note"].startswith("pseudobulk")
    # no threshold is decided by the kernel; the plan records none
    assert units["cell_level_rule"] is None, units
    assert units["evidence_rule"] is None, units
    out = run_protocol(project, pid)
    assert out["status"] == "completed", out

    der = derive_evidence(project, pid)
    assert der["statistical_unit"].startswith("cell")
    assert der["thresholds"]["source"] == NO_RULE_SOURCE
    assert der["n_cell_level_reported"] > 0, der
    assert der["n_passing_declared_rule"] is None, der

    statements = [r["statement"] for r in conn.execute(
        "SELECT statement FROM claim WHERE protocol_id=? AND subject_type='candidate'",
        (pid,))]
    assert statements
    for s in statements:
        assert "cell-level Wilcoxon" in s
        assert "not independent replicates" in s
        assert "supplementary" in s
        assert "no significance or effect-size rule" in s
        assert "using the sample as the unit" not in s

    payloads = [json.loads(r["payload_json"]) for r in conn.execute(
        "SELECT e.payload_json FROM evidence e JOIN claim c ON c.claim_id=e.claim_id "
        "WHERE c.protocol_id=? AND e.source_type='current_data' "
        "AND e.payload_json LIKE '%cell_level%'", (pid,))]
    assert payloads
    for pay in payloads:
        assert pay["cell_level"]["test"] == "wilcox_cell_level"
        assert pay["cell_level"]["independence_warning"]
        assert pay["passes_declared_rule"] is None
        assert pay["declared_rule"] is None
        assert pay["sample_level"]["is_supplementary"] is True
        assert pay["sample_level"]["status"] in (
            "corroborates", "discordant", "not_corroborated", "not_computed")

    doss = build_dossiers(project, pid)
    for d in doss["dossiers"]:
        assert "expression_cell_level" in d["dimensions"]
        assert d["dimensions"]["expression_sample_level"]["gates_results"] is False
        assert d["n_genes_passing_declared_rule"] is None
        assert d["classification"] == "evidence_not_adjudicated"
        assert "makes no sufficient/insufficient call" in d["classification_reason"]


def test_a_rule_declared_in_the_plan_annotates_rows_without_dropping_them(project, conn):
    """A declared rule marks rows; every reported row is still recorded and kept."""
    from sctda.reasoning import build_dossiers, derive_evidence
    plan = P.build_plan(project, methods=["de_vs_rest_wilcox"])
    base = P.load_protocol(Project(project).connect(), plan["protocol_id"])
    units = dict(base["plan"]["analysis_units"])
    units["cell_level_rule"] = {"max_p_value_adj": 0.01, "min_abs_log2FC": 1.0,
                                "source": "declared for this analysis"}
    units["evidence_rule"] = {"min_genes_passing_cell_rule": 5}
    rev = P.revise(project, plan["protocol_id"], {"analysis_units": units})
    pid = rev["protocol_id"]
    assert run_protocol(project, pid)["status"] == "completed"

    der = derive_evidence(project, pid)
    assert der["thresholds"]["source"] == "plan"
    assert der["n_passing_declared_rule"] is not None
    payloads = [json.loads(r["payload_json"]) for r in conn.execute(
        "SELECT e.payload_json FROM evidence e JOIN claim c ON c.claim_id=e.claim_id "
        "WHERE c.protocol_id=? AND e.source_type='current_data' "
        "AND e.payload_json LIKE '%cell_level%'", (pid,))]
    # one evidence row per reported gene: nothing is dropped by the rule
    assert len(payloads) == der["n_cell_level_reported"], (
        len(payloads), der["n_cell_level_reported"])
    verdicts = [p["passes_declared_rule"] for p in payloads]
    assert all(v in (True, False, None) for v in verdicts), verdicts
    assert any(v is not None for v in verdicts), verdicts
    assert sum(1 for v in verdicts if v) == der["n_passing_declared_rule"]
    assert any(p["declared_rule"]["source"] == "declared for this analysis"
               for p in payloads)

    doss = build_dossiers(project, pid)
    d = doss["dossiers"][0]
    assert d["classification"] in ("evidence_sufficient", "evidence_thin",
                                    "evidence_insufficient")
    assert d["classification_rule_source"] == "declared in plan"
    assert d["dimensions"]["expression_cell_level"]["declared_rule"] is not None


def test_no_sample_column_means_sample_level_not_computed_never_zero(tmp_path,
                                                                    synthetic_object):
    """Without a sample column there is no pseudobulk table; that is not `no effect`."""
    from sctda.ingest import import_dataset
    from sctda.reasoning import build_dossiers, derive_evidence
    root = tmp_path / "nosample"
    out = import_dataset(root, synthetic_object, annotation_column="celltype",
                         condition_column="group")
    assert out["status"] == "imported", out
    plan = P.build_plan(root, methods=["de_vs_rest_wilcox"])
    pid = plan["protocol_id"]
    run = run_protocol(root, pid)
    assert run["status"] == "completed", run

    conn = Project(root).connect()
    try:
        n_sample_tables = conn.execute(
            "SELECT COUNT(*) c FROM artifact WHERE protocol_id=? AND kind=?",
            (pid, "de_sample_level")).fetchone()["c"]
        assert n_sample_tables == 0, "no sample column must produce no sample-level table"
        der = derive_evidence(root, pid)
        assert der["sample_level"]["n_not_computed"] == der["n_cell_level_reported"]
        assert der["sample_level"]["n_corroborates"] == 0
        statement = conn.execute(
            "SELECT statement FROM claim WHERE protocol_id=? AND subject_type='candidate'",
            (pid,)).fetchone()["statement"]
        assert "cell-level Wilcoxon" in statement
        assert "not computed" in statement
        assert "unknown, not absent" in statement
        doss = build_dossiers(root, pid)
        d = doss["dossiers"][0]
        assert d["dimensions"]["expression_sample_level"]["n_not_computed"] == \
            d["n_genes_reported"]
        assert d["n_genes_sample_corroborated"] == 0
    finally:
        conn.close()


def test_a_plan_without_a_declared_rule_records_none_and_applies_none(project):
    """No kernel default is substituted: the recorded rule is null and says so."""
    from sctda.reasoning import NO_RULE_SOURCE, rule_from_plan
    cell, sample, evidence, source = rule_from_plan({"analysis_units": {
        "statistical_unit": "sample (donor)", "min_samples_per_group": 4}})
    assert cell is None
    assert evidence is None
    assert sample["min_samples"] == 4          # legacy key still honored
    assert source == NO_RULE_SOURCE

    plan = P.build_plan(project, methods=["composition"])
    stored = P.load_protocol(Project(project).connect(), plan["protocol_id"])["plan"]
    cell2, _s2, ev2, src2 = rule_from_plan(stored)
    assert cell2 is None and ev2 is None and src2 == NO_RULE_SOURCE


# ------------------------------------------------ 4d. autonomy at the tool boundary
def test_tool_surface_has_no_approval_verb_and_a_plan_runs_directly(project):
    from sctda.harness import invoke_tool, tool_manifest
    names = {t["name"] for t in tool_manifest()}
    assert "sctda_approve" not in names, sorted(names)
    refused = invoke_tool("sctda_approve", {"project": str(project), "protocol": "x"})
    assert refused["status"] == "error"
    planned = invoke_tool("sctda_plan", {"project": str(project),
                                         "methods": ["composition"]})
    assert planned["status"] == "frozen", planned
    ran = invoke_tool("sctda_run", {"project": str(project),
                                    "protocol": planned["protocol_id"]})
    assert ran["status"] == "completed", ran


def test_records_written_before_the_gate_removal_migrate_to_frozen(project, conn):
    """An old `approved` row keeps its version and hash and becomes runnable."""
    from sctda.db import migrate
    plan = P.build_plan(project, methods=["composition"])
    pid = plan["protocol_id"]
    before = conn.execute("SELECT version, plan_sha256 FROM protocol WHERE protocol_id=?",
                          (pid,)).fetchone()
    conn.execute("UPDATE protocol SET status='approved' WHERE protocol_id=?", (pid,))
    conn.commit()
    applied = migrate(conn)
    assert any("frozen" in a for a in applied), applied
    row = conn.execute("SELECT status, version, plan_sha256 FROM protocol "
                       "WHERE protocol_id=?", (pid,)).fetchone()
    assert row["status"] == "frozen"
    assert row["version"] == before["version"]
    assert row["plan_sha256"] == before["plan_sha256"]
    assert run_protocol(project, pid)["status"] == "completed"


# ---------------------------------------------------- 5. evidence does not upgrade
def test_identical_paper_evidence_is_stored_once(project, conn):
    pid = conn.execute("SELECT project_id FROM project").fetchone()["project_id"]
    cid = ensure_claim(conn, pid, "target", "ACKR1", "ACKR1 is higher in Alpha")
    a = add_evidence(conn, pid, cid, "paper_observation", origin="paper:12345",
                     pmid="12345", locator="methods:L1:P1:S1")
    b = add_evidence(conn, pid, cid, "paper_observation", origin="paper:12345",
                     pmid="12345", locator="methods:L1:P1:S1")
    assert a["status"] == "added" and b["status"] == "duplicate"
    ind = independence(conn, cid)
    assert ind["n_evidence_rows"] == 1
    assert ind["independent_external_sources"] == 1


def _fake_artifact(conn, project_id: str, name: str) -> str:
    """A real artifact row (FK needs a real one) standing in for computed output."""
    aid = f"art_{name}"
    conn.execute("""INSERT OR REPLACE INTO artifact(artifact_id, task_id, protocol_id,
                      kind, path, checksum, checksum_algo, created_utc)
                    VALUES (?,NULL,'test',?,?,?,'md5','now')""",
                 (aid, name, f"/tmp/{name}.csv", f"ck-{name}"))
    conn.commit()
    return aid


def test_own_computation_never_counts_as_external_support(project, conn):
    pid = conn.execute("SELECT project_id FROM project").fetchone()["project_id"]
    cid = ensure_claim(conn, pid, "target", "MMRN1", "MMRN1 is lower in Beta")
    for i in range(5):          # five rows, all from our own computation
        add_evidence(conn, pid, cid, "current_data", origin=f"artifact-{i}",
                     artifact_id=_fake_artifact(conn, pid, f"a{i}"))
    ind = independence(conn, cid)
    assert ind["n_evidence_rows"] == 5
    assert ind["independent_external_sources"] == 0
    assert ind["own_computation_only"] is True


def test_current_data_evidence_without_a_source_is_refused(project, conn):
    pid = conn.execute("SELECT project_id FROM project").fetchone()["project_id"]
    cid = ensure_claim(conn, pid, "target", "TFPI", "TFPI is lower in Alpha")
    with pytest.raises(ValueError, match="artifact row"):
        add_evidence(conn, pid, cid, "current_data", origin="no-source")


def test_model_hypothesis_does_not_become_an_observation(project, conn):
    pid = conn.execute("SELECT project_id FROM project").fetchone()["project_id"]
    cid = ensure_claim(conn, pid, "target", "FLT1", "FLT1 may act through VEGFA")
    add_evidence(conn, pid, cid, "model_hypothesis", origin="inference-1")
    ind = independence(conn, cid)
    assert ind["hypothesis_only"] is True
    assert ind["independent_external_sources"] == 0
    assert "paper_observation" not in ind["by_source_type"]
