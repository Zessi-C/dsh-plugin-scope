"""Guards that keep unverified work out of the scientific record.

Two ways a table can enter the record without a computation behind it:

  1. an attempt that did not succeed still leaves files in its task directory,
     and the R worker indexes every file it finds there — so the Python side
     must refuse to register artifacts of a `failed` / `insufficient_input` /
     `not_applicable` attempt, and must not re-index them on a later attempt;
  2. the object loaded at run time is not the object the plan froze, which the
     database row can no longer prove because a re-import rewrites it — so the
     file itself is hashed before anything runs.

The worker is scripted here: these are tests of the Python contract, not of R,
and no result in this file is a biological finding.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from sctda import execution as E
from sctda import protocol as P
from sctda.execution import run_protocol, task_id_for
from sctda.ingest import import_dataset
from sctda.project import Project, sha256_file
from sctda.rbridge import RResult


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()


def _script_worker(monkeypatch, behaviour) -> dict:
    """Replace the R batch with a scripted outcome.

    `behaviour(index, task, odir)` returns `(status, reason, declared_paths)` or
    None to leave the task unreported. Declared paths mirror `r/run_tasks.R`,
    which indexes every non-directory file left in the task directory.
    """
    calls = {"n": 0}

    def fake_run_r(script, args, log_path=None, timeout=None):
        calls["n"] += 1
        tasks = json.loads(Path(args[args.index("--tasks") + 1]).read_text())
        out_dir = Path(args[args.index("--out-dir") + 1])
        results = []
        for i, t in enumerate(tasks):
            odir = out_dir / t["task_id"]
            odir.mkdir(parents=True, exist_ok=True)
            scripted = behaviour(i, t, odir)
            if scripted is None:
                continue
            status, reason, declared = scripted
            rec = {
                "task_id": t["task_id"], "method_id": t["method_id"],
                "status": status, "reason": reason,
                "finished_utc": "2026-01-01T00:00:00Z",
                "artifacts": [{"path": str(p), "checksum": _md5(p),
                               "checksum_algo": "md5", "n_rows": 1}
                              for p in declared if Path(p).exists()] +
                             [{"path": str(p), "checksum": "0" * 32,
                               "checksum_algo": "md5", "n_rows": 1}
                              for p in declared if not Path(p).exists()],
            }
            # r/run_tasks.R leaves a result.json per task; so does this
            (odir / "result.json").write_text(json.dumps(rec), encoding="utf-8")
            results.append(rec)
        return RResult(ok=True, payload={"ok": True, "results": results},
                       stdout="", stderr="", returncode=0, log_path=log_path)

    monkeypatch.setattr(E, "run_r", fake_run_r)
    return calls


def _forbid_r(monkeypatch) -> None:
    def explode(script, args, log_path=None, timeout=None):
        raise AssertionError("R must not be invoked when the run is refused")

    monkeypatch.setattr(E, "run_r", explode)


def _plan_tasks(protocol_dir: Path, protocol_id: str) -> list[dict]:
    return _plan_doc(protocol_dir, protocol_id)["tasks"]


def _plan_doc(protocol_dir: Path, protocol_id: str) -> dict:
    conn = Project(protocol_dir).connect()
    try:
        return P.load_protocol(conn, protocol_id)["plan"]
    finally:
        conn.close()


def _rows(project_dir: Path, sql: str, params=()) -> list:
    conn = Project(project_dir).connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ------------------------------------------------- artifacts need a success
def test_partial_artifacts_of_a_failed_task_are_not_registered(project,
                                                               monkeypatch):
    """A file written before the failure is named and left unregistered."""
    plan = P.build_plan(project, methods=["de_vs_rest_wilcox"])
    pid = plan["protocol_id"]
    tasks = _plan_tasks(project, pid)
    assert len(tasks) == 2, tasks

    def behaviour(i, task, odir):
        if i == 0:
            partial = odir / "de_cell_level.csv"
            partial.write_text("gene,avg_log2FC\nG1,0.4\n", encoding="utf-8")
            return ("failed", "FindMarkers: boom after the table was written",
                    [partial])
        good = odir / "de_cell_level.csv"
        good.write_text("gene,avg_log2FC\nG2,-0.4\n", encoding="utf-8")
        return ("succeeded", "1 gene tested", [good])

    _script_worker(monkeypatch, behaviour)
    out = run_protocol(project, pid)
    assert out["status"] == "failed", out
    assert out["outcomes"] == {"failed": 1, "succeeded": 1}, out["outcomes"]

    failed_id = task_id_for(pid, tasks[0]["task_key"])
    ok_id = task_id_for(pid, tasks[1]["task_key"])
    arts = _rows(project, "SELECT task_id, path FROM artifact WHERE protocol_id=?",
                 (pid,))
    assert len(arts) == 1, arts
    assert arts[0]["task_id"] == ok_id

    row = _rows(project, "SELECT status, reason FROM task WHERE task_id=?",
                (failed_id,))[0]
    assert row["status"] == "failed"
    assert "NOT registered" in (row["reason"] or "")
    assert "de_cell_level.csv" in (row["reason"] or "")
    # the partial file itself is kept as evidence of what the attempt did
    assert Path(arts[0]["path"]).exists()
    partial = Path(_rows(project, "SELECT output_dir FROM task WHERE task_id=?",
                         (failed_id,))[0]["output_dir"]) / "de_cell_level.csv"
    assert partial.exists()


def test_unrecognised_worker_status_is_failed_and_not_registered(project,
                                                                 monkeypatch):
    plan = P.build_plan(project, methods=["composition"])
    pid = plan["protocol_id"]

    def behaviour(i, task, odir):
        f = odir / "composition_overall.csv"
        f.write_text("candidate,n_cells\nAlpha,10\n", encoding="utf-8")
        return ("ok", "adapter typo", [f])       # not one of the four outcomes

    _script_worker(monkeypatch, behaviour)
    out = run_protocol(project, pid)
    assert out["status"] == "failed", out
    assert _rows(project, "SELECT COUNT(*) c FROM artifact WHERE protocol_id=?",
                 (pid,))[0]["c"] == 0
    row = _rows(project, "SELECT status, reason FROM task WHERE protocol_id=?",
                (pid,))[0]
    assert row["status"] == "failed"
    assert "unrecognised status" in (row["reason"] or "")


def test_a_task_the_worker_never_reports_is_failed_not_left_pending(project,
                                                                    monkeypatch):
    plan = P.build_plan(project, methods=["de_vs_rest_wilcox"])
    pid = plan["protocol_id"]
    tasks = _plan_tasks(project, pid)

    def behaviour(i, task, odir):
        if i == 1:
            return None                            # batch ends without a record
        f = odir / "de_cell_level.csv"
        f.write_text("gene,avg_log2FC\nG1,0.4\n", encoding="utf-8")
        return ("succeeded", "1 gene tested", [f])

    _script_worker(monkeypatch, behaviour)
    out = run_protocol(project, pid)
    assert out["status"] == "failed", out
    assert out["tasks_without_outcome"] == [task_id_for(pid, tasks[1]["task_key"])]
    row = _rows(project, "SELECT status, reason FROM task WHERE task_id=?",
                (task_id_for(pid, tasks[1]["task_key"]),))[0]
    assert row["status"] == "failed"
    assert "no outcome" in (row["reason"] or "")


def test_a_success_whose_artifact_is_missing_on_disk_is_not_a_success(project,
                                                                      monkeypatch):
    plan = P.build_plan(project, methods=["composition"])
    pid = plan["protocol_id"]

    def behaviour(i, task, odir):
        ghost = odir / "composition_overall.csv"    # declared, never written
        return ("succeeded", "claims a table", [ghost])

    _script_worker(monkeypatch, behaviour)
    out = run_protocol(project, pid)
    assert out["status"] == "failed", out
    assert _rows(project, "SELECT COUNT(*) c FROM artifact WHERE protocol_id=?",
                 (pid,))[0]["c"] == 0
    row = _rows(project, "SELECT status, reason FROM task WHERE protocol_id=?",
                (pid,))[0]
    assert row["status"] == "failed"
    assert "missing on disk" in (row["reason"] or "")


# ------------------------------------------------- the source object is checked
def _imported_project(tmp_path, synthetic_object) -> tuple[Path, Path]:
    obj = tmp_path / "object.rds"
    shutil.copy2(synthetic_object, obj)
    root = tmp_path / "guard_proj"
    out = import_dataset(root, obj, annotation_column="celltype",
                         sample_column="orig.ident", condition_column="group")
    assert out["status"] == "imported", out
    return root, obj


def test_changed_object_is_refused_even_though_the_row_still_agrees(tmp_path,
                                                                    synthetic_object,
                                                                    monkeypatch):
    """The hash is computed from the file; the row is not accepted as proof."""
    root, obj = _imported_project(tmp_path, synthetic_object)
    plan = P.build_plan(root, methods=["composition"])
    pid = plan["protocol_id"]
    doc = _plan_doc(root, pid)

    ds = _rows(root, "SELECT source_path, source_sha256 FROM dataset")[0]
    # at this point row and plan agree, so a row-only check would pass
    assert ds["source_sha256"] == doc["data"]["source_sha256"]

    obj.write_bytes(obj.read_bytes() + b"\n# appended after the plan was frozen\n")
    assert sha256_file(obj) != doc["data"]["source_sha256"]

    _forbid_r(monkeypatch)                      # R must never learn about this
    out = run_protocol(root, pid)
    assert out["status"] == "failed", out
    assert out["stage"] == "source_verification", out
    assert "sha256" in out["error"]
    assert out["source_check"]["actual_sha256"] == sha256_file(obj)
    assert out["source_check"]["frozen_sha256"] == doc["data"]["source_sha256"]

    recorded = json.loads(Path(out["source_check_record"]).read_text(encoding="utf-8"))
    assert recorded["ok"] is False and recorded["problems"]

    tasks = _plan_tasks(root, pid)
    assert out["tasks_marked_failed"] == len(tasks)
    for t in tasks:
        row = _rows(root, "SELECT status, reason FROM task WHERE task_id=?",
                    (task_id_for(pid, t["task_key"]),))[0]
        assert row["status"] == "failed"
        assert "refused before any task ran" in (row["reason"] or "")
    assert _rows(root, "SELECT COUNT(*) c FROM artifact WHERE protocol_id=?",
                 (pid,))[0]["c"] == 0


def test_a_database_row_that_disagrees_with_the_plan_is_refused(project,
                                                               monkeypatch):
    plan = P.build_plan(project, methods=["composition"])
    pid = plan["protocol_id"]
    doc = _plan_doc(project, pid)
    conn = Project(project).connect()
    try:
        conn.execute("UPDATE dataset SET source_sha256=?", ("f" * 64,))
        conn.commit()
    finally:
        conn.close()

    _forbid_r(monkeypatch)
    out = run_protocol(project, pid)
    assert out["status"] == "failed", out
    assert out["stage"] == "source_verification"
    assert "database row" in out["error"]
    assert out["source_check"]["actual_sha256"] == doc["data"]["source_sha256"]


def test_missing_object_file_is_refused(tmp_path, synthetic_object, monkeypatch):
    root, obj = _imported_project(tmp_path, synthetic_object)
    plan = P.build_plan(root, methods=["composition"])
    pid = plan["protocol_id"]
    obj.unlink()

    _forbid_r(monkeypatch)
    out = run_protocol(root, pid)
    assert out["status"] == "failed", out
    assert out["stage"] == "source_verification"
    assert "not readable" in out["error"]


# ------------------------------------------- leftovers of an earlier attempt
def test_previous_attempt_files_are_quarantined_before_a_rerun(project,
                                                               monkeypatch):
    """A stale partial must not be re-indexed as if the new attempt wrote it."""
    plan = P.build_plan(project, methods=["composition"])
    pid = plan["protocol_id"]
    tasks = _plan_tasks(project, pid)
    tid = task_id_for(pid, tasks[0]["task_key"])

    def failing(i, task, odir):
        partial = odir / "composition_overall.csv"
        partial.write_text("candidate,n_cells\nAlpha,10\n", encoding="utf-8")
        return ("failed", "died after writing the table", [partial])

    _script_worker(monkeypatch, failing)
    first = run_protocol(project, pid)
    assert first["status"] == "failed", first

    odir = Path(_rows(project, "SELECT output_dir FROM task WHERE task_id=?",
                      (tid,))[0]["output_dir"])
    assert (odir / "composition_overall.csv").exists()
    # an operator re-runs the same frozen plan after a failed batch
    conn = Project(project).connect()
    try:
        conn.execute("UPDATE protocol SET status='frozen' WHERE protocol_id=?", (pid,))
        conn.commit()
    finally:
        conn.close()

    def succeeding(i, task, odir):
        # mirror r/run_tasks.R: every non-directory file in the task directory
        # is declared, whatever wrote it
        f = odir / "composition_by_sample.csv"
        f.write_text("candidate,sample,n_cells\nAlpha,S1,5\n", encoding="utf-8")
        declared = sorted(p for p in odir.iterdir() if p.is_file())
        return ("succeeded", "1 sample", declared)

    _script_worker(monkeypatch, succeeding)
    second = run_protocol(project, pid)
    assert second["status"] == "completed", second
    assert tid in second["quarantined_previous_attempts"]
    moved = Path(second["quarantined_previous_attempts"][tid][0])
    assert moved.parent.name == "_previous_attempt"
    assert moved.name == "composition_overall.csv"
    assert not (odir / "composition_overall.csv").exists()

    arts = _rows(project, "SELECT path FROM artifact WHERE protocol_id=?", (pid,))
    assert len(arts) == 1, arts
    assert Path(arts[0]["path"]).name == "composition_by_sample.csv"


# --------------------------------------------------------- index repair rules
def _successful_run(project, monkeypatch) -> tuple[str, str]:
    plan = P.build_plan(project, methods=["composition"])
    pid = plan["protocol_id"]
    tid = task_id_for(pid, _plan_tasks(project, pid)[0]["task_key"])

    def behaviour(i, task, odir):
        f = odir / "composition_overall.csv"
        f.write_text("candidate,n_cells\nAlpha,10\n", encoding="utf-8")
        return ("succeeded", "1 candidate", [f])

    _script_worker(monkeypatch, behaviour)
    run = run_protocol(project, pid)
    assert run["status"] == "completed", run
    return pid, tid


def _drop_artifact_rows(project: Path) -> None:
    conn = Project(project).connect()
    try:
        conn.execute("DELETE FROM artifact")
        conn.commit()
    finally:
        conn.close()


def test_repair_does_not_index_a_result_json_that_is_not_a_success(project,
                                                                   monkeypatch):
    pid, tid = _successful_run(project, monkeypatch)
    _drop_artifact_rows(project)

    regard = Path(_rows(project, "SELECT output_dir FROM task WHERE task_id=?",
                        (tid,))[0]["output_dir"]) / "result.json"
    rec = json.loads(regard.read_text(encoding="utf-8"))
    rec["status"] = "failed"
    regard.write_text(json.dumps(rec), encoding="utf-8")

    out = run_protocol(project, pid, resume=True)
    assert out["ran"] == 0, out
    assert _rows(project, "SELECT COUNT(*) c FROM artifact WHERE protocol_id=?",
                 (pid,))[0]["c"] == 0
    repaired = [s for s in out["skipped_tasks"] if s["task_id"] == tid][0]
    assert repaired["artifacts_repaired"] == 0
    assert "not 'succeeded'" in repaired["artifact_repair"]["skipped"]


def test_repair_does_not_index_a_file_whose_bytes_changed(project, monkeypatch):
    pid, tid = _successful_run(project, monkeypatch)
    _drop_artifact_rows(project)

    csv = Path(_rows(project, "SELECT output_dir FROM task WHERE task_id=?",
                     (tid,))[0]["output_dir"]) / "composition_overall.csv"
    csv.write_text("candidate,n_cells\nAlpha,9999\n", encoding="utf-8")

    out = run_protocol(project, pid, resume=True)
    assert out["ran"] == 0, out
    assert _rows(project, "SELECT COUNT(*) c FROM artifact WHERE protocol_id=?",
                 (pid,))[0]["c"] == 0
    repaired = [s for s in out["skipped_tasks"] if s["task_id"] == tid][0]
    assert repaired["artifacts_repaired"] == 0
    assert "composition_overall.csv" in repaired["artifact_repair"]["checksum_mismatch"]
