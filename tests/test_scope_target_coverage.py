"""Every candidate is accounted for in the target output, and no comparison the
plan declares is reported as computed unless a table behind it exists.

The failure these tests pin down: a candidate whose cell-level table was never
produced used to vanish from `propose_targets`, which reads the same as a
candidate with no hits, and the plan's comparison text was attached to every
target as if that contrast had been evaluated. The claims, dossiers and report
must state the same per-candidate outcome: `reported`, `empty_result`,
`task_failed` or `no_data`, never a protocol-wide guess.

These are software-behaviour tests on a synthetic object. No number here is a
finding.
"""
from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

import pytest

from sctda import protocol as P
from sctda.execution import run_protocol
from sctda.ingest import import_dataset
from sctda.project import Project
from sctda.reasoning import propose_targets

UNIVERSE = {"Alpha", "Beta"}


def _de_artifact(conn, protocol_id: str, label: str):
    return conn.execute(
        "SELECT a.artifact_id, a.task_id, a.path FROM artifact a "
        "JOIN task t ON a.task_id=t.task_id "
        "JOIN candidate c ON c.candidate_id=t.candidate_id "
        "WHERE t.protocol_id=? AND a.kind='de_cell_level' AND c.label=?",
        (protocol_id, label)).fetchone()


def _relocate(conn, root: Path, row, name: str = "de_copy.csv") -> Path:
    """Move one artifact's file into the copied project so surgery stays local."""
    dst = root / "meta" / name
    shutil.copyfile(row["path"], dst)
    conn.execute("UPDATE artifact SET path=? WHERE artifact_id=?",
                 (str(dst), row["artifact_id"]))
    conn.commit()
    return dst


def _candidate_id(conn, label: str) -> str:
    return conn.execute("SELECT candidate_id FROM candidate WHERE label=?",
                        (label,)).fetchone()["candidate_id"]


def _candidate_claim_statements(conn, protocol_id: str,
                                candidate_id: str) -> list[str]:
    return [r["statement"] for r in conn.execute(
        "SELECT statement FROM claim WHERE protocol_id=? AND subject_type='candidate' "
        "AND subject_id=?", (protocol_id, candidate_id))]


@pytest.fixture(scope="module")
def de_run(tmp_path_factory, synthetic_object):
    """One frozen plan over the synthetic object, run once; tests copy it."""
    root = tmp_path_factory.mktemp("targets") / "proj"
    out = import_dataset(root, synthetic_object, annotation_column="celltype",
                         sample_column="orig.ident", condition_column="group")
    assert out["status"] == "imported", out
    plan = P.build_plan(root, methods=["de_vs_rest_wilcox"],
                        comparisons=["Alpha vs rest"])
    assert plan["status"] == P.FROZEN_STATUS, plan
    pid = plan["protocol_id"]
    run = run_protocol(root, pid)
    assert run["status"] == "completed", run
    return root, pid


@pytest.fixture
def run_copy(tmp_path, de_run):
    src, pid = de_run
    dst = tmp_path / "proj"
    shutil.copytree(src, dst)
    return dst, pid


# ------------------------------------------- 1. every candidate stays visible
def test_candidates_without_any_cell_level_task_are_still_listed(project):
    """A plan with no DE task must not produce an empty, candidate-less target list."""
    plan = P.build_plan(project, methods=["composition"])
    assert plan["status"] == P.FROZEN_STATUS, plan
    out = propose_targets(project, plan["protocol_id"], query_external=False)

    assert out["status"] == "ok", out
    assert out["n_targets"] == 0
    assert out["n_candidates"] == len(UNIVERSE)
    cov = {c["label"]: c for c in out["candidates"]}
    assert set(cov) == UNIVERSE
    for c in cov.values():
        assert c["status"] == "no_data", c
        assert c["direction_known"] is False
        assert c["n_targets"] == 0
        assert "not computed" in c["reason"]
        assert "unknown, not absent" in c["reason"]

    blob = json.loads(Path(out["snapshot"]).read_text(encoding="utf-8"))
    assert {c["label"] for c in blob["candidates"]} == UNIVERSE
    assert blob["candidates_by_status"]["no_data"] == len(UNIVERSE)
    assert out["candidates_by_status"] == {"reported": 0, "empty_result": 0,
                                           "task_failed": 0, "no_data": 2}


def test_a_declared_comparison_is_not_computed_without_a_table(project):
    """The plan freezes 'Alpha vs rest', but nothing ran: not computed, not implied."""
    plan = P.build_plan(project, methods=["de_vs_rest_wilcox"],
                        comparisons=["Alpha vs rest"])
    assert plan["status"] == P.FROZEN_STATUS, plan
    out = propose_targets(project, plan["protocol_id"], query_external=False)

    scope = out["comparison_scope"]
    assert scope["declared_in_plan"] == ["Alpha vs rest"]
    assert scope["declared_comparisons_computed"] == {
        "Alpha vs rest": "not_computed"}
    assert scope["computed_tables"] == []
    assert "without a computed result" in out["warning"]
    assert out["n_targets"] == 0


# ------------------------------------- 2. only the computed contrast is attributed
def test_a_computed_run_attributes_only_the_contrast_it_computed(run_copy):
    root, pid = run_copy
    out = propose_targets(root, pid, query_external=False)

    assert out["n_targets"] > 0, out
    assert out["n_candidates"] == len(UNIVERSE)
    assert out["comparison_scope"]["declared_comparisons_computed"] == {
        "Alpha vs rest": "computed"}
    tables = {t["candidate_label"]: t
              for t in out["comparison_scope"]["computed_tables"]}
    assert set(tables) == UNIVERSE
    assert all(t["contrast"].endswith(" vs rest") for t in tables.values())

    for t in out["targets"]:
        ctx = t["comparison_context"]
        assert ctx["computed"]["kind"] == "candidate_vs_rest"
        assert ctx["computed"]["contrast"] == f"{t['cell_group']} vs rest"
        assert ctx["computed"]["rest"].startswith("every cell not labelled")
        assert ctx["declared_in_plan"] == ["Alpha vs rest"]
        assert ctx["declared_comparisons_computed"] == {"Alpha vs rest": "computed"}
        if t["cell_group"] == "Alpha":
            assert ctx["declared_for_this_group"] == ["Alpha vs rest"]
        else:
            assert ctx["declared_for_this_group"] == []
            assert "no declared comparison is computed for this group" in ctx["note"]

    conn = Project(root).connect()
    try:
        statements = [r["statement"] for r in conn.execute(
            "SELECT statement FROM claim WHERE protocol_id=? AND subject_type='target'",
            (pid,))]
    finally:
        conn.close()
    assert statements
    for s in statements:
        assert "remaining cells" in s
        assert "Alpha vs rest" not in s


def test_a_legacy_plan_string_is_never_attributed_to_a_target(run_copy):
    """A plan that only declares 'treat vs ctrl' computed no such contrast."""
    root, pid = run_copy
    conn = Project(root).connect()
    try:
        row = conn.execute("SELECT plan_json FROM protocol WHERE protocol_id=?",
                           (pid,)).fetchone()
        plan = json.loads(row["plan_json"])
        plan.pop("comparisons_resolved", None)       # pre-resolution frozen plan
        plan["comparisons"] = ["treat vs ctrl"]      # a two-condition contrast
        plan["comparisons_specified"] = True
        conn.execute("UPDATE protocol SET plan_json=? WHERE protocol_id=?",
                     (json.dumps(plan), pid))
        conn.commit()
    finally:
        conn.close()

    out = propose_targets(root, pid, query_external=False)
    scope = out["comparison_scope"]
    assert scope["declared_in_plan"] == ["treat vs ctrl"]
    assert scope["declared_comparisons_computed"] == {"treat vs ctrl": "not_computed"}
    assert "without a computed result" in out["warning"]
    assert out["n_targets"] > 0
    for t in out["targets"]:
        ctx = t["comparison_context"]
        assert ctx["declared_comparisons_computed"] == {
            "treat vs ctrl": "not_computed"}
        assert ctx["declared_for_this_group"] == []
        assert ctx["computed"]["kind"] == "candidate_vs_rest"
        assert ctx["computed"]["contrast"] == f"{t['cell_group']} vs rest"


# ------------------------------ 3. failed task / empty table / no data stay apart
def test_a_failed_cell_level_task_is_task_failed_not_no_data(run_copy):
    root, pid = run_copy
    conn = Project(root).connect()
    try:
        row = _de_artifact(conn, pid, "Alpha")
        assert row is not None
        conn.execute("UPDATE task SET status='failed', reason='FindMarkers: boom' "
                     "WHERE task_id=?", (row["task_id"],))
        conn.execute("DELETE FROM artifact WHERE task_id=?", (row["task_id"],))
        conn.commit()
    finally:
        conn.close()

    out = propose_targets(root, pid, query_external=False)
    cov = {c["label"]: c for c in out["candidates"]}
    assert cov["Alpha"]["status"] == "task_failed", cov["Alpha"]
    assert cov["Alpha"]["direction_known"] is False
    assert cov["Alpha"]["task_status"] == "failed"
    assert "boom" in cov["Alpha"]["reason"]
    assert cov["Alpha"]["n_targets"] == 0
    assert not [t for t in out["targets"] if t["cell_group"] == "Alpha"]
    assert cov["Beta"]["status"] == "reported"
    assert out["candidates_by_status"]["task_failed"] == 1
    assert out["candidates_by_status"]["reported"] == 1


def test_a_header_only_table_is_an_empty_result_not_an_absent_one(run_copy):
    root, pid = run_copy
    conn = Project(root).connect()
    try:
        row = _de_artifact(conn, pid, "Alpha")
        target = _relocate(conn, root, row, "de_alpha_empty.csv")
        header = target.read_text(encoding="utf-8").splitlines()[0]
        target.write_text(header + "\n", encoding="utf-8")
        conn.execute("UPDATE artifact SET n_rows=0 WHERE artifact_id=?",
                     (row["artifact_id"],))
        conn.commit()
    finally:
        conn.close()

    out = propose_targets(root, pid, query_external=False)
    cov = {c["label"]: c for c in out["candidates"]}
    assert cov["Alpha"]["status"] == "empty_result", cov["Alpha"]
    assert cov["Alpha"]["n_rows_reported"] == 0
    assert cov["Alpha"]["direction_known"] is False
    assert "empty result, not an absent comparison" in cov["Alpha"]["reason"]
    assert not [t for t in out["targets"] if t["cell_group"] == "Alpha"]
    assert cov["Beta"]["status"] == "reported"
    assert out["candidates_by_status"]["empty_result"] == 1
    # the declaration resolves to a task, but the table it wrote has no rows
    assert out["comparison_scope"]["declared_comparisons_computed"] == {
        "Alpha vs rest": "empty_result"}


def test_a_succeeded_task_with_no_registered_table_is_no_data(run_copy):
    """Succeeded + no artifact on disk is a missing table, not an empty result."""
    root, pid = run_copy
    conn = Project(root).connect()
    try:
        row = _de_artifact(conn, pid, "Alpha")
        conn.execute("DELETE FROM artifact WHERE artifact_id=?", (row["artifact_id"],))
        conn.commit()
    finally:
        conn.close()

    out = propose_targets(root, pid, query_external=False)
    cov = {c["label"]: c for c in out["candidates"]}
    assert cov["Alpha"]["status"] == "no_data", cov["Alpha"]
    assert cov["Alpha"]["task_status"] == "succeeded"
    assert "missing" in cov["Alpha"]["reason"]


# ------------------------------ 4. claims and dossiers read the candidate's own task
def test_a_plan_without_a_cell_level_task_says_so_for_each_candidate(project):
    """No DE task at all: the candidate claim states absence, never an empty result."""
    from sctda.reasoning import derive_evidence

    plan = P.build_plan(project, methods=["composition"])
    pid = plan["protocol_id"]
    conn = Project(project).connect()
    try:
        alpha_id = _candidate_id(conn, "Alpha")
    finally:
        conn.close()
    out = derive_evidence(project, pid)
    assert out["status"] == "ok", out
    conn = Project(project).connect()
    try:
        statements = _candidate_claim_statements(conn, pid, alpha_id)
    finally:
        conn.close()
    assert statements
    for s in statements:
        assert "No cell-level table is recorded" in s
        assert "unknown, not absent" in s
        assert "empty result" not in s


def test_a_failed_task_is_claimed_as_failed_not_as_an_empty_result(run_copy):
    """The protocol has other tables; this candidate's failed task is still failed."""
    from sctda.reasoning import build_dossiers, derive_evidence

    root, pid = run_copy
    conn = Project(root).connect()
    try:
        row = _de_artifact(conn, pid, "Alpha")
        alpha_id = _candidate_id(conn, "Alpha")
        conn.execute("UPDATE task SET status='failed', reason='FindMarkers: boom' "
                     "WHERE task_id=?", (row["task_id"],))
        conn.execute("DELETE FROM artifact WHERE task_id=?", (row["task_id"],))
        conn.commit()
    finally:
        conn.close()

    der = derive_evidence(root, pid)
    assert der["status"] == "ok", der
    conn = Project(root).connect()
    try:
        statements = _candidate_claim_statements(conn, pid, alpha_id)
    finally:
        conn.close()
    joined = " ".join(statements)
    assert "boom" in joined
    assert "failed" in joined
    assert "empty result" not in joined

    doss = build_dossiers(root, pid)
    by_label = {d["label"]: d for d in doss["dossiers"]}
    assert by_label["Alpha"]["dimensions"]["expression_table_state"]["status"] == \
        "task_failed"
    assert "failed" in by_label["Alpha"]["classification_reason"]
    assert by_label["Beta"]["dimensions"]["expression_table_state"]["status"] == \
        "reported"


def test_the_report_lists_every_candidate_and_keeps_gene_targets(run_copy):
    from sctda.report import build_report

    root, pid = run_copy
    conn = Project(root).connect()
    try:
        row = _de_artifact(conn, pid, "Alpha")
        target = _relocate(conn, root, row, "de_alpha_empty.csv")
        header = target.read_text(encoding="utf-8").splitlines()[0]
        target.write_text(header + "\n", encoding="utf-8")
        conn.execute("UPDATE artifact SET n_rows=0 WHERE artifact_id=?",
                     (row["artifact_id"],))
        conn.commit()
    finally:
        conn.close()
    propose_targets(root, pid, query_external=False)

    out = build_report(root, protocol=pid, formats="html,csv")
    assert out["status"] == "ok", out
    page = Path(out["paths"]["html"]).read_text(encoding="utf-8")
    assert "Every candidate in the universe" in page
    assert "<td>empty_result</td>" in page
    assert "empty result, not an absent comparison" in page
    assert "<td>reported</td>" in page
    # the gene-level table is still rendered for the candidate that has rows
    assert "Gene-level targets" in page

    by_label = {r["candidate"]: r
                for r in csv.DictReader(Path(out["paths"]["targets_candidates"])
                                        .open(encoding="utf-8"))}
    assert set(by_label) == UNIVERSE
    assert by_label["Alpha"]["status"] == "empty_result"
    assert "empty result" in by_label["Alpha"]["reason"]
    assert by_label["Beta"]["status"] == "reported"


# -------------------- 5. rows that cannot be bound to a group stay visible
def test_rows_outside_the_universe_keep_targets_and_unbound_rows_are_counted(run_copy):
    root, pid = run_copy
    conn = Project(root).connect()
    try:
        row = _de_artifact(conn, pid, "Alpha")
        path = _relocate(conn, root, row, "de_alpha_extra.csv")
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            fields = reader.fieldnames
            rows = list(reader)
        extra = dict(rows[0])
        extra.update({"gene": "GENE399", "candidate": "Gamma"})
        unbound = dict(rows[0])
        unbound.update({"gene": "GENE398", "candidate": ""})
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows + [extra, unbound])
    finally:
        conn.close()

    out = propose_targets(root, pid, query_external=False)
    cov = {c["label"]: c for c in out["candidates"]}
    assert cov["Gamma"]["in_candidate_universe"] is False
    assert cov["Gamma"]["status"] == "reported"
    gamma = [t for t in out["targets"] if t["cell_group"] == "Gamma"]
    assert gamma and gamma[0]["in_candidate_universe"] is False
    # an off-universe label has no resolved contrast, so none is asserted for it
    assert gamma[0]["comparison_context"]["computed"]["kind"] == "not asserted"
    # a row with no candidate label cannot be bound to a group: counted, not a target
    assert out["unbound_candidate_rows"] == 1
    assert not [t for t in out["targets"] if not t["cell_group"]]
    assert set(cov) == UNIVERSE | {"Gamma"}
