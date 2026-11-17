"""Execute a frozen plan.

Rules this module enforces:
- there is no approval gate: a plan is frozen when it is written, and a frozen /
  running / paused plan executes. Nothing else does (a superseded or completed
  plan is not re-run without `--resume`, and an unknown status is refused);
- the object is loaded once per run and every task of the protocol shares it;
- a task's outcome is one of succeeded / not_applicable / insufficient_input /
  failed. A computed zero is `succeeded`; a missing input is never zero-filled;
- resuming skips tasks already `succeeded` and never rewrites the same artifact
  (artifact identity is task + path + checksum);
- a failed method is NOT retried with a different method: the original message
  is kept so a silent substitution cannot masquerade as success;
- the source object is verified against the frozen plan before anything runs:
  the file on disk must hash to the sha256 the plan recorded and the database
  row must agree with the plan. Checking the row alone proves nothing — the row
  is a claim about a file. A mismatch refuses the run, records why in
  `meta/source_check_<protocol>.json`, and marks every task it would have run
  `failed` with the reason instead of touching R. The protocol itself keeps its
  status: nothing ran, and restoring the object the plan froze makes the same
  frozen plan runnable again (marking it `failed` would not);
- only a `succeeded` task deposits artifacts. A `failed` / `insufficient_input`
  / `not_applicable` attempt can leave a partially written file behind; that
  file is named in the task's reason and is never registered, so a partial write
  cannot reach the report as a computed table. For the same reason a re-run
  first moves the previous attempt's files into `<task_dir>/_previous_attempt/`,
  because the R worker enumerates everything in the task directory.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from .catalog import get_method
from .db import TASK_STATUSES
from .project import (Project, canonical_json, get_or_create_project, new_id, now,
                      sha256_text)
from .protocol import (ProtocolError, RUNNABLE_STATUSES, load_protocol, set_status)
from .rbridge import run_r

# Outcomes a worker may return. `succeeded` is the only one that means "this
# table is a result"; the rest mean no result exists.
OUTCOME_STATUSES = tuple(s for s in TASK_STATUSES
                         if s in {"succeeded", "not_applicable",
                                  "insufficient_input", "failed"})
RESULT_STATUS = "succeeded"


def _environment_provenance() -> dict:
    """What actually executed, recorded per run.

    The plan asks for isolation to be stated rather than assumed: this host has
    no rootless container wired up, so the run happens in the user's own R/Python
    environment and that is written down instead of being implied.
    """
    import platform
    import shutil
    import subprocess
    env = {"python": platform.python_version(), "platform": platform.platform(),
           "isolation": "host environment (no container); versions below are what ran",
           "rscript": shutil.which("Rscript")}
    if not env["rscript"]:
        env.update({"r_version": None, "seurat_version": None,
                    "seuratobject_version": None, "aucell_version": None,
                    "probe_error": "Rscript executable not found"})
        return env
    import os
    import tempfile
    r_code = (
        "pkg_ver <- function(pkg) {\n"
        "  tryCatch(as.character(packageVersion(pkg)), error = function(e) '')\n"
        "}\n"
        "cat(paste(R.version.string, pkg_ver('Seurat'), pkg_ver('SeuratObject'),\n"
        "          pkg_ver('AUCell'), pkg_ver('infercnv'), sep = '|'))\n"
    )
    tf = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".R", delete=False) as f:
            f.write(r_code)
            tf = f.name
        probe = subprocess.run(
            [env["rscript"], tf],
            capture_output=True, text=True, timeout=120)
        if probe.returncode == 0:
            parts = (probe.stdout or "").strip().split("|")
            env.update({
                "r_version": parts[0] if parts and parts[0] else None,
                "seurat_version": parts[1] if len(parts) > 1 and parts[1] else None,
                "seuratobject_version": parts[2] if len(parts) > 2 and parts[2] else None,
                "aucell_version": parts[3] if len(parts) > 3 and parts[3] else None,
                "infercnv_version": parts[4] if len(parts) > 4 and parts[4] else None,
            })
        else:
            env.update({"r_version": None, "seurat_version": None,
                        "seuratobject_version": None, "aucell_version": None,
                        "probe_error": f"Rscript exited with code {probe.returncode}: {probe.stderr.strip()}"})
    except Exception as exc:                                  # noqa: BLE001
        env.update({"r_version": None, "seurat_version": None,
                    "seuratobject_version": None, "aucell_version": None,
                    "probe_error": f"{type(exc).__name__}: {exc}"})
    finally:
        if tf and os.path.exists(tf):
            try:
                os.remove(tf)
            except OSError:
                pass
    return env


def _file_digest(path: Path, algo: str) -> str | None:
    """Hex digest of the bytes on disk, or None when they cannot be read."""
    try:
        h = hashlib.new(algo, usedforsecurity=False)
    except (TypeError, ValueError):
        return None
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
    except OSError:
        return None
    return h.hexdigest()


def _verify_source_object(plan: dict, ds_row: sqlite3.Row | None) -> dict:
    """Check the object the plan froze, by hashing the file, not by trusting a row.

    The plan is the authority: it recorded the path and sha256 at import time
    and it cannot change. The database row is checked too, but only as a second
    opinion — a row can be rewritten by a later import, and a row that agrees
    with the plan still says nothing about what the bytes on disk are now.
    """
    data = plan.get("data") or {}
    frozen_path = data.get("source_path")
    frozen_sha = data.get("source_sha256")
    check: dict = {"ok": False, "dataset_id": data.get("dataset_id"),
                   "frozen_path": frozen_path, "frozen_sha256": frozen_sha,
                   "db_path": ds_row["source_path"] if ds_row else None,
                   "db_sha256": ds_row["source_sha256"] if ds_row else None,
                   "actual_sha256": None, "size_bytes": None,
                   "checked_utc": now(), "problems": []}
    problems = check["problems"]
    if not frozen_sha:
        problems.append("the plan records no data.source_sha256, so the frozen "
                        "source cannot be verified")
        return check
    if not frozen_path:
        problems.append("the plan records no data.source_path")
        return check
    if ds_row is None:
        problems.append("the dataset row this plan names is missing from the database")
    else:
        if str(ds_row["source_sha256"]) != str(frozen_sha):
            problems.append("the database row records a different source hash than "
                            "the frozen plan")
        if str(ds_row["source_path"]) != str(frozen_path):
            problems.append("the database row points at a different source path than "
                            "the frozen plan")
    p = Path(str(frozen_path))
    try:
        st = p.stat()
    except OSError as exc:
        problems.append(f"the frozen source object is not readable at {frozen_path} "
                        f"({type(exc).__name__}: {exc})")
        return check
    check["size_bytes"] = st.st_size
    if not p.is_file() or st.st_size == 0:
        problems.append("the frozen source object is not a non-empty regular file")
        return check
    actual = _file_digest(p, "sha256")
    check["actual_sha256"] = actual
    if actual is None:
        problems.append("the frozen source object could not be hashed")
    elif actual != str(frozen_sha):
        problems.append(f"source object sha256 {actual[:16]}... does not match the "
                        f"frozen sha256 {str(frozen_sha)[:16]}...; the object changed "
                        f"after the plan was frozen")
    check["ok"] = not problems
    return check


def _record_source_check(proj: Project, protocol_id: str, check: dict) -> Path:
    """Persist the verification, success or refusal, so it stays auditable."""
    path = proj.dirs["meta"] / f"source_check_{protocol_id}.json"
    path.write_text(json.dumps(check, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _mark_task_failed(conn: sqlite3.Connection, protocol_id: str, task: dict,
                      reason: str) -> bool:
    """Record a refusal against a task that has not succeeded.

    Returns True when a row was written. A task that already succeeded keeps
    its status: its artifacts were computed from the object the plan froze, and
    a later refusal to run again does not retroactively unmake them.
    """
    tid = task_id_for(protocol_id, task["task_key"])
    row = conn.execute("SELECT status FROM task WHERE task_id=?", (tid,)).fetchone()
    if row is not None and row["status"] == RESULT_STATUS:
        return False
    if row is None:
        stage = (get_method(task["method_id"]) or {}).get("stage") or "unknown"
        conn.execute("""INSERT INTO task(task_id, protocol_id, candidate_id, method_id,
                          stage, status, params_json, reason, finished_utc)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                     (tid, protocol_id, task.get("candidate_id"), task["method_id"],
                      stage, "failed", canonical_json(task["params"]), reason, now()))
    else:
        conn.execute("UPDATE task SET status='failed', reason=?, finished_utc=? "
                     "WHERE task_id=?", (reason, now(), tid))
    return True


def _quarantine_previous_attempt(odir: Path) -> list[str]:
    """Move files left by an earlier attempt of this task out of the way.

    The R worker indexes every non-directory file in the task directory as an
    artifact of the attempt now running. Left alone, a partial file from a
    failed attempt would be re-indexed by a later successful attempt as if that
    attempt had produced it. Moving them into a subdirectory keeps the evidence
    on disk and keeps them out of the index (directories are skipped).
    """
    if not odir.is_dir():
        return []
    stale = sorted(p for p in odir.iterdir() if p.is_file())
    if not stale:
        return []
    dest = odir / "_previous_attempt"
    dest.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for p in stale:
        target = dest / p.name
        n = 1
        while target.exists():
            n += 1
            target = dest / f"{p.name}.{n}"
        p.replace(target)
        moved.append(str(target))
    return moved


def _repair_artifacts(conn: sqlite3.Connection, task_id: str,
                      protocol_id: str) -> dict:
    """Rebuild artifact rows from result.json, only for a verified success.

    Re-registering an index is not a second chance for a task that did not
    succeed, and a hash record is only worth as much as the file it describes:
    the result must say `succeeded`, every artifact file must still exist, and
    its bytes must still hash to the recorded checksum. Anything else is
    reported and left unregistered.
    """
    out: dict = {"registered": 0, "missing": [], "checksum_mismatch": [],
                 "skipped": None}
    n_have = conn.execute("SELECT COUNT(*) c FROM artifact WHERE task_id=?",
                          (task_id,)).fetchone()["c"]
    row = conn.execute("SELECT output_dir FROM task WHERE task_id=?",
                       (task_id,)).fetchone()
    if n_have:
        out["skipped"] = "artifact rows already present"
        return out
    if not row or not row["output_dir"]:
        out["skipped"] = "no output directory recorded"
        return out
    rj = Path(row["output_dir"]) / "result.json"
    if not rj.exists():
        out["skipped"] = "no result.json"
        return out
    try:
        rec = json.loads(rj.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        out["skipped"] = f"result.json unreadable ({type(exc).__name__}: {exc})"
        return out
    if rec.get("status") != RESULT_STATUS:
        out["skipped"] = (f"result.json records {rec.get('status')!r}, not "
                          f"{RESULT_STATUS!r}")
        return out
    verified = []
    for art in rec.get("artifacts") or []:
        p = Path(str(art.get("path") or ""))
        if not art.get("path") or not p.is_file():
            out["missing"].append(p.name or str(art.get("path")))
            continue
        actual = _file_digest(p, str(art.get("checksum_algo") or "md5").lower())
        if actual is None or actual != art.get("checksum"):
            out["checksum_mismatch"].append(p.name)
            continue
        verified.append(art)
    out["registered"] = _register_artifacts(conn, task_id, protocol_id, verified)
    return out


def task_id_for(protocol_id: str, task_key: str) -> str:
    return "task_" + sha256_text(f"{protocol_id}|{task_key}")[:16]


def _register_artifacts(conn: sqlite3.Connection, task_id: str, protocol_id: str,
                        artifacts: list[dict]) -> int:
    """Insert artifact rows idempotently; returns how many were new."""
    n = 0
    for art in artifacts or []:
        path = str(Path(art["path"]))
        if conn.execute("SELECT 1 FROM artifact WHERE task_id=? AND path=? AND checksum=?",
                        (task_id, path, art["checksum"])).fetchone():
            continue
        conn.execute("""INSERT INTO artifact(artifact_id, task_id, protocol_id, kind,
                            path, checksum, checksum_algo, n_rows, n_cols, created_utc)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (new_id("art"), task_id, protocol_id,
                      Path(path).name.split(".")[0], path, art["checksum"],
                      art.get("checksum_algo", "md5"), art.get("n_rows"), None, now()))
        n += 1
    return n


def run_protocol(project_dir: str | Path, protocol_id: str,
                 resume: bool = False, stage: str | None = None) -> dict:
    proj = Project(Path(project_dir)).ensure()
    conn = proj.connect()
    try:
        pid = get_or_create_project(conn, proj.root)
        try:
            proto = load_protocol(conn, protocol_id)
        except ProtocolError as exc:
            return {"status": "failed", "error": str(exc)}
        allowed = RUNNABLE_STATUSES + (("completed",) if resume else ())
        if proto["status"] not in allowed:
            return {"status": "failed",
                    "error": (f"protocol is {proto['status']}; only a frozen plan "
                              f"runs (frozen/running/paused)"),
                    "hint": (f"sctda plan --project {proj.root} --methods ... "
                             f"writes a new frozen version"
                             if proto["status"] in ("superseded", "cancelled", "failed")
                             else f"sctda run --project {proj.root} "
                                  f"--protocol {protocol_id} --resume")}
        plan = proto["plan"]

        ds = conn.execute("SELECT * FROM dataset WHERE dataset_id=?",
                          (plan["data"]["dataset_id"],)).fetchone()

        tasks = plan["tasks"]
        if stage:
            tasks = [t for t in tasks if t["method_id"] == stage]
            if not tasks:
                return {"status": "failed", "error": f"no tasks for stage {stage}"}

        # ---- frozen source verification --------------------------------------
        # The plan recorded the object it was frozen against; the file on disk
        # must still be that object. A row that says so is not evidence — the
        # row is what a previous import claimed. Hash the file, and refuse to
        # run when it differs, marking the tasks with the reason so the refusal
        # is visible in the task table instead of being an empty run.
        check = _verify_source_object(plan, ds)
        check_path = _record_source_check(proj, protocol_id, check)
        if not check["ok"]:
            reason = ("execution refused before any task ran: "
                      + "; ".join(check["problems"]))
            marked = sum(1 for t in tasks
                         if _mark_task_failed(conn, protocol_id, t, reason))
            conn.commit()
            return {"status": "failed", "stage": "source_verification",
                    "protocol_id": protocol_id, "error": reason,
                    "source_check": check,
                    "source_check_record": str(check_path),
                    "tasks_marked_failed": marked,
                    "tasks_left_succeeded": len(tasks) - marked,
                    "hint": ("restore the object the plan froze, or import the "
                             "current object and write a new plan")}

        env = _environment_provenance()
        cfg = {"annotation_column": plan["data"]["columns"]["annotation"],
               "sample_column": plan["data"]["columns"]["sample"],
               "condition_column": plan["data"]["columns"]["condition"],
               "assay": plan["data"]["assay"]}

        # the verified path the plan froze, not the path the row happens to hold
        source_path = check["frozen_path"]

        # ---- dependency gate -------------------------------------------------
        # A task whose dependency did not succeed must not run: producing output
        # from a missing prerequisite is how a fabricated result gets in.
        key_to_task = {t["task_key"]: t for t in plan["tasks"]}
        blocked: dict[str, str] = {}
        for t in tasks:
            for dep in t.get("depends_on") or []:
                dep_task = key_to_task.get(dep)
                if dep_task is None:
                    blocked[t["task_key"]] = f"declared dependency {dep!r} is not in this plan"
                    continue
                dep_tid = task_id_for(protocol_id, dep)
                row = conn.execute("SELECT status FROM task WHERE task_id=?",
                                   (dep_tid,)).fetchone()
                if row is None or row["status"] != "succeeded":
                    blocked[t["task_key"]] = (
                        f"dependency {dep!r} is {row['status'] if row else 'not run'}")

        # ---- plan / resume --------------------------------------------------
        todo, skipped = [], []
        for t in tasks:
            tid = task_id_for(protocol_id, t["task_key"])
            row = conn.execute("SELECT status, attempt FROM task WHERE task_id=?",
                               (tid,)).fetchone()
            if row and row["status"] == "succeeded":
                # a previous run may have died between "task succeeded" and
                # "artifacts registered"; repair from the on-disk result before
                # declaring the task done, otherwise the report would silently
                # miss results that exist on disk.
                repaired = _repair_artifacts(conn, tid, protocol_id)
                entry = {"task_id": tid, "task_key": t["task_key"],
                         "reason": "already succeeded",
                         "artifacts_repaired": repaired["registered"]}
                blocked_repair = {k: v for k, v in repaired.items()
                                  if k != "registered" and v}
                if blocked_repair:
                    entry["artifact_repair"] = blocked_repair
                skipped.append(entry)
                continue
            if t["task_key"] in blocked:
                reason = blocked[t["task_key"]]
                if row is None:
                    conn.execute("""INSERT INTO task(task_id, protocol_id, candidate_id,
                                      method_id, stage, status, params_json, reason,
                                      finished_utc) VALUES (?,?,?,?,?,?,?,?,?)""",
                                 (tid, protocol_id, t.get("candidate_id"), t["method_id"],
                                  get_method(t["method_id"])["stage"], "insufficient_input",
                                  canonical_json(t["params"]), reason, now()))
                else:
                    conn.execute("UPDATE task SET status='insufficient_input', reason=?, "
                                 "finished_utc=? WHERE task_id=?", (reason, now(), tid))
                skipped.append({"task_id": tid, "task_key": t["task_key"],
                                "reason": reason})
                continue
            if row is None:
                conn.execute("""INSERT INTO task(task_id, protocol_id, candidate_id,
                                  method_id, stage, status, params_json)
                                VALUES (?,?,?,?,?,?,?)""",
                             (tid, protocol_id, t.get("candidate_id"), t["method_id"],
                              get_method(t["method_id"])["stage"], "pending",
                              canonical_json(t["params"])))
            else:
                # a task that was attempted before keeps the count of attempts:
                # re-running it is a new attempt, not a silent overwrite
                conn.execute("UPDATE task SET status='pending', "
                             "attempt=COALESCE(attempt, 0) + 1, reason=NULL "
                             "WHERE task_id=?", (tid,))
            todo.append({"task_id": tid, "method_id": t["method_id"],
                         "params": t["params"], "outdir_key": t["task_key"]})
        conn.commit()

        if not todo:
            set_status(conn, protocol_id, "completed",
                       expect=allowed)
            return {"status": "completed", "ran": 0, "skipped": len(skipped),
                    "skipped_tasks": skipped,
                    "message": "all tasks already succeeded"}

        set_status(conn, protocol_id, "running", expect=allowed)
        (proj.dirs["meta"] / f"env_{protocol_id}.json").write_text(
            json.dumps(env, ensure_ascii=False, indent=1), encoding="utf-8")

        # ---- hand the batch to R -------------------------------------------
        metadir = proj.dirs["meta"]
        cfg_path = metadir / f"run_config_{protocol_id}.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))

        out_root = proj.dirs["artifacts"] / protocol_id
        out_root.mkdir(parents=True, exist_ok=True)
        log_path = proj.dirs["logs"] / f"run_{protocol_id}.log"

        # each task directory must belong to the attempt now running; files an
        # earlier attempt left there are moved aside before the batch is built
        quarantined: dict[str, list[str]] = {}
        stuck: dict[str, str] = {}
        for t in todo:
            odir = out_root / t["task_id"]
            try:
                moved = _quarantine_previous_attempt(odir)
            except OSError as exc:
                stuck[t["task_id"]] = f"{type(exc).__name__}: {exc}"
                continue
            if moved:
                quarantined[t["task_id"]] = moved
            conn.execute("UPDATE task SET started_utc=?, output_dir=?, log_path=? "
                         "WHERE task_id=?",
                         (now(), str(odir), str(log_path), t["task_id"]))
        for tid, why in stuck.items():
            conn.execute("UPDATE task SET status='failed', reason=?, finished_utc=? "
                         "WHERE task_id=?",
                         ("refusing to run this task: files from an earlier attempt "
                          f"could not be moved aside ({why})", now(), tid))
        conn.commit()
        if stuck:
            todo = [t for t in todo if t["task_id"] not in stuck]
            if not todo:
                set_status(conn, protocol_id, "failed")
                return {"status": "failed", "stage": "quarantine",
                        "error": "no task could run: files from an earlier attempt "
                                 "could not be moved aside",
                        "quarantine_blocked": stuck, "log": str(log_path)}

        tasks_payload = []
        for t in todo:
            params = dict(t["params"])
            gs_file = params.pop("geneset_file", None)
            if gs_file:
                blob = json.loads(Path(gs_file).read_text(encoding="utf-8"))
                params["gene_sets"] = blob["gene_sets"]
                params["geneset_provenance"] = {
                    "file": gs_file, "name": blob.get("name"),
                    "source": blob.get("source"),
                    "source_version": blob.get("source_version"),
                    "retrieved_utc": blob.get("retrieved_utc"),
                    "n_sets": blob.get("n_sets"),
                }
            tasks_payload.append({"task_id": t["task_id"], "method_id": t["method_id"],
                                  "params": params})
        tasks_path = metadir / f"tasks_{protocol_id}.json"
        tasks_path.write_text(json.dumps(tasks_payload, ensure_ascii=False, indent=2))

        res = run_r("run_tasks.R",
                    ["--object", source_path, "--config", str(cfg_path),
                     "--tasks", str(tasks_path), "--out-dir", str(out_root)],
                    log_path=log_path)

        if not res.ok:
            set_status(conn, protocol_id, "failed")
            detail = str(res.payload.get("error") or res.stderr[-2000:]
                         or "R batch failed")[:2000]
            for t in todo:
                conn.execute("UPDATE task SET status='failed', reason=?, finished_utc=? "
                             "WHERE task_id=?", (detail, now(), t["task_id"]))
            conn.commit()
            out = {"status": "failed", "stage": "r_batch",
                   "error": res.payload.get("error") or res.stderr[-3000:],
                   "log": str(log_path)}
            if quarantined:
                out["quarantined_previous_attempts"] = quarantined
            if stuck:
                out["quarantine_blocked"] = stuck
            return out

        results = res.payload.get("results") or []
        todo_ids = {t["task_id"] for t in todo}
        # tasks whose leftovers could not be moved aside already failed
        counts: dict[str, int] = {"failed": len(stuck)} if stuck else {}
        refused: list[dict] = []
        answered: set[str] = set()
        for r in results:
            tid = r.get("task_id")
            if tid not in todo_ids:
                # the worker reported a task this run never queued: registering
                # its files would attach an unrequested result to the protocol
                refused.append({"task_id": tid,
                                "why": "task_id is not part of this batch"})
                continue
            answered.add(tid)
            status = str(r.get("status") or "")
            reason = r.get("reason")
            if status not in OUTCOME_STATUSES:
                reason = ((f"{reason}; " if reason else "")
                          + f"worker returned unrecognised status {status!r}; "
                            f"recorded as failed")
                status = "failed"
            listed = [a for a in (r.get("artifacts") or []) if isinstance(a, dict)]
            present, absent = [], []
            for a in listed:
                (present if a.get("path") and Path(str(a["path"])).is_file()
                 else absent).append(a)
            if status == RESULT_STATUS and not absent:
                _register_artifacts(conn, tid, protocol_id, present)
            elif status == RESULT_STATUS:
                # a claimed artifact that is not on disk makes the claim false;
                # a success index built on it would describe files that do not
                # exist, so the outcome is not a success
                status = "failed"
                names = ", ".join(Path(str(a.get("path") or "?")).name for a in absent)
                reason = ((f"{reason}; " if reason else "")
                          + f"artifact(s) recorded by the worker are missing on disk: "
                            f"{names}")
            else:
                if present or absent:
                    names = [Path(str(a.get("path") or "?")).name
                             for a in present + absent]
                    note = (f"{len(names)} partial artifact(s) written before the "
                            f"task became {status} were NOT registered: "
                            + ", ".join(names[:10])
                            + (" ..." if len(names) > 10 else ""))
                    reason = f"{reason}; {note}" if reason else note
            counts[status] = counts.get(status, 0) + 1
            conn.execute("UPDATE task SET status=?, reason=?, finished_utc=? "
                         "WHERE task_id=?",
                         (status, reason, r.get("finished_utc"), tid))

        # a queued task the worker never reported must not stay `pending` with
        # an empty result: that reads as "not run yet" when the batch is over
        silent = sorted(todo_ids - answered)
        for tid in silent:
            conn.execute("UPDATE task SET status='failed', reason=?, finished_utc=? "
                         "WHERE task_id=?",
                         ("the R batch returned no outcome for this task; nothing "
                          "about it is a result", now(), tid))
            counts["failed"] = counts.get("failed", 0) + 1
        conn.commit()

        failed = counts.get("failed", 0)
        final = "completed" if failed == 0 else "failed"
        set_status(conn, protocol_id, final, expect=("running", "completed"))
        out = {"status": final, "protocol_id": protocol_id,
               "ran": len(results), "skipped": len(skipped),
               "skipped_tasks": skipped,
               "outcomes": counts, "artifacts_root": str(out_root),
               "log": str(log_path),
               "source_check": {"ok": check["ok"],
                                "record": str(check_path)},
               "next": f"sctda report --project {proj.root} --protocol {protocol_id}"}
        if quarantined:
            out["quarantined_previous_attempts"] = quarantined
        if stuck:
            out["quarantine_blocked"] = stuck
        if refused:
            out["unmatched_results"] = refused
        if silent:
            out["tasks_without_outcome"] = silent
        return out
    finally:
        conn.close()
