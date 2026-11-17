"""Protocol lifecycle.

States: frozen -> running -> completed, plus paused/failed/cancelled and
superseded (a frozen plan that a newer version replaced before it ran).

A plan is frozen when it is written: `plan_sha256` covers the canonical plan
document and `created_utc` is the freeze time. There is no approval step and no
approver field; the kernel writes, hashes and freezes its own plan, then runs it.
Changing the comparisons, the chosen methods or any parameter never edits a
frozen row: it writes a new version pointing at its parent. The candidate
universe is fixed by the mapped annotation column - a revision may restate it
but never narrow or widen it, so a different universe needs a new plan.
`freeze_check` recomputes the hash from the stored plan and fails loudly if the
row was edited behind the system's back.

Two rules keep the frozen document honest:

- a comparison enters the plan only when a queued task computes it. The one
  contrast an adapter runs is one annotated candidate against every other cell,
  so any other contrast is refused when the plan is written, not recorded and
  left unexecuted;
- a revision names plan scope, never derived structure. Changing methods,
  parameters or the gene set file recompiles the task graph, method metadata,
  gaps and expected artifacts from that scope; the candidate universe is fixed
  by the annotation mapping and cannot be re-selected by a revision.

Superseding is deliberately narrow: writing a new version marks only older
frozen-and-never-run versions as `superseded`, so a replaced plan cannot be
executed by mistake. Running, paused and completed versions keep their status -
replacing a plan never rolls back an execution.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .catalog import (CatalogError, EXECUTABLE, get_method,
                      literature_backing)
from .project import (Project, canonical_json, get_or_create_project, new_id, now,
                      sha256_text)
from .rbridge import run_r

# a plan change in any of these keys requires a new version
MUTABLE_SCOPE_KEYS = ("candidate_universe", "methods", "comparisons", "parameters",
                      "analysis_units", "geneset_file")
# the question is part of the plan document but does not change its scope
NON_SCOPE_MUTABLE_KEYS = ("question",)
MUTABLE_KEYS = MUTABLE_SCOPE_KEYS + NON_SCOPE_MUTABLE_KEYS

FROZEN_STATUS = "frozen"
# what `run` accepts: a plan that has not started, one that is running, one the
# operator paused. Computed work is never discarded by a status change.
RUNNABLE_STATUSES = ("frozen", "running", "paused")


class ProtocolError(RuntimeError):
    pass


# How an adapter is invoked. A dataset-wide adapter computes its result once for
# the whole object and summarises every candidate in that single run; queueing it
# per candidate would recompute identical work N times. A per-candidate adapter
# reads params["candidate_label"] and needs one task per candidate. A method in
# EXECUTABLE that is in neither set is refused: guessing the scope is how
# duplicated or missing tasks get into a plan.
DATASET_WIDE_METHODS = frozenset({"composition", "signature_score_aucell",
                                  "cnv_infercnv"})
PER_CANDIDATE_METHODS = frozenset({"de_vs_rest_wilcox"})

# The only contrast an adapter computes: one annotated candidate against every
# other cell (de_vs_rest_wilcox). A declared comparison is accepted only in that
# form, for a candidate in the plan, and only when that method is chosen.
_COMPARISON_RE = re.compile(
    r"^(?P<left>.+?)\s+vs\.?\s+(?P<right>rest|all\s+other\s+cells|all\s+others)$",
    re.IGNORECASE)


def _conn(project_dir: str | Path) -> tuple[Project, sqlite3.Connection, str]:
    proj = Project(Path(project_dir)).ensure()
    conn = proj.connect()
    pid = get_or_create_project(conn, proj.root)
    return proj, conn, pid


def _dataset(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM dataset LIMIT 1").fetchone()


def _config(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT role, column_name FROM mapping").fetchall()
    cfg = {"annotation_column": None, "sample_column": None, "condition_column": None,
           "assay": None}
    for r in rows:
        cfg[f"{r['role']}_column"] = r["column_name"]
    ds = _dataset(conn)
    if ds:
        assays = json.loads(ds["assays_json"] or "[]")
        for a in assays:
            if a.get("default"):
                cfg["assay"] = a["name"]
        cfg["assay"] = cfg["assay"] or (assays[0]["name"] if assays else None)
    return cfg


def design_table(project_dir: str | Path) -> dict:
    """Metadata cross-tabs (samples, conditions, cells per candidate per sample)."""
    proj, conn, _pid = _conn(project_dir)
    try:
        ds = _dataset(conn)
        if not ds:
            raise ProtocolError("no dataset imported")
        cfg = _config(conn)
        cfg_path = proj.dirs["meta"] / "run_config.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
        out_path = proj.dirs["meta"] / "design_table.json"
        res = run_r("design_table.R",
                    ["--object", ds["source_path"], "--config", str(cfg_path),
                     "--out", str(out_path)],
                    log_path=proj.dirs["logs"] / "design_table.log")
        if not res.ok:
            raise ProtocolError(f"design_table failed: {res.payload.get('error')}")
        return json.loads(out_path.read_text())
    finally:
        conn.close()


def _find_genesets(proj: Project, explicit: str | None) -> str | None:
    if explicit:
        return explicit if Path(explicit).exists() else None
    hits = sorted((proj.dirs["downloads"]).glob("genesets_*.json"))
    return str(hits[-1]) if hits else None


# ------------------------------------------------------------ plan primitives

def _method_scope(method_id: str) -> str:
    # a catalog entry may declare its own scope; the sets below are the fallback
    declared = (get_method(method_id) or {}).get("scope")
    if declared in ("dataset", "candidate"):
        return declared
    if method_id in DATASET_WIDE_METHODS:
        return "dataset"
    if method_id in PER_CANDIDATE_METHODS:
        return "candidate"
    raise ProtocolError(
        f"{method_id}: adapter scope is undeclared in protocol.py; add it to "
        f"DATASET_WIDE_METHODS or PER_CANDIDATE_METHODS after checking whether its "
        f"adapter writes one dataset-wide result or one result per candidate")


def _method_meta(method_id: str) -> dict:
    m = EXECUTABLE[method_id]
    return {"method_id": method_id, "package": m.get("package"),
            "stage": m["stage"], "scope": _method_scope(method_id),
            "parameter_provenance": {
                "source": "catalog defaults",
                "note": "not copied from any single paper; paper params are "
                        "dataset specific and are not treated as defaults"},
            "statistical_notes": m.get("statistical_notes", [])}


def _effective_params(method_id: str, overrides: dict | None) -> dict:
    """Catalog defaults with validated overrides; unknown keys are refused."""
    m = get_method(method_id)
    if not m:
        raise ProtocolError(f"unknown method id: {method_id}")
    defaults = dict(m.get("defaults") or {})
    allowed = set(defaults) | set(m.get("params_schema") or {})
    unknown = sorted(set(overrides or {}) - allowed)
    if unknown:
        raise ProtocolError(f"{method_id}: unknown parameter(s) {unknown}; "
                            f"accepted: {sorted(allowed)}")
    params = dict(defaults)
    for key, value in (overrides or {}).items():
        default = defaults.get(key)
        ok = True
        if default is not None:
            if isinstance(default, bool):
                ok = isinstance(value, bool)
            elif isinstance(default, int):
                ok = isinstance(value, int) and not isinstance(value, bool)
            elif isinstance(default, float):
                ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            elif isinstance(default, str):
                ok = isinstance(value, str)
        if not ok:
            raise ProtocolError(f"{method_id}.{key}: expected "
                                f"{type(default).__name__}, got "
                                f"{type(value).__name__} ({value!r})")
        params[key] = value
    return params


def _compile_tasks(proj: Project, cfg: dict, cands: list[sqlite3.Row],
                   chosen: list[str], parameter_overrides: dict,
                   geneset_file: str | None,
                   discover_genesets: bool = True
                   ) -> tuple[list[dict], list[dict], dict]:
    """Tasks, recorded gaps and the effective parameters for the chosen methods.

    One task for a dataset-wide adapter, one per candidate for a per-candidate
    adapter. A missing requirement is a `gaps` entry and an `insufficient_input`
    outcome at run time, never a silently skipped task.
    """
    gaps: list[dict] = []
    tasks: list[dict] = []
    effective: dict[str, dict] = {}
    dest = proj.dirs["artifacts"]
    for mid in chosen:
        m = get_method(mid)
        req = m.get("requires", {})
        missing = []
        if req.get("annotation_column") and not cfg["annotation_column"]:
            missing.append("annotation_column")
        if req.get("sample_column") and not cfg["sample_column"]:
            missing.append("sample_column")
        gs = None
        if req.get("gene_sets"):
            if geneset_file:
                gs = geneset_file if Path(geneset_file).exists() else None
                if not gs:
                    missing.append(f"gene_sets: gene set file not found: {geneset_file}")
            elif discover_genesets:
                gs = _find_genesets(proj, None)
                if not gs:
                    missing.append("gene_sets: no snapshot found in downloads/ "
                                   "(run r/fetch_genesets.R or pass --geneset-file)")
            else:
                missing.append("gene_sets: no gene set snapshot is recorded in this "
                               "plan (pass --geneset-file to add one)")
        if missing:
            gaps.append({"method_id": mid, "missing": missing,
                         "consequence": "task will be queued as insufficient_input"})
        params = _effective_params(mid, parameter_overrides.get(mid) or {})
        if gs:
            params["geneset_file"] = gs
        effective[mid] = dict(params)
        scope = _method_scope(mid)
        if scope == "dataset":
            tasks.append({
                "task_key": mid,
                "method_id": mid,
                "candidate_id": None,
                "candidate_label": None,
                "depends_on": [],
                "params": params,
                "output_dir": str(dest / mid),
                "comparison": None,
                "expected": m["provides"],
            })
            continue
        for c in cands:
            params_c = dict(params)
            params_c["candidate_label"] = c["label"]
            tasks.append({
                "task_key": f"{mid}::{c['label']}",
                "method_id": mid,
                "candidate_id": c["candidate_id"],
                "candidate_label": c["label"],
                "depends_on": [],
                "params": params_c,
                "output_dir": str(dest / mid / _slug(c["label"])),
                # what this task actually computes, in the same words a declared
                # comparison is resolved to below
                "comparison": f"{c['label']} vs rest",
                "expected": m["provides"],
            })
    return tasks, gaps, effective


def _as_comparison_list(value: object) -> list | None:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    return None


def _parse_comparison(spec: object) -> dict | None:
    if not isinstance(spec, str):
        return None
    text = " ".join(spec.split())
    m = _COMPARISON_RE.match(text)
    if not m:
        return None
    return {"text": text, "label": m.group("left").strip()}


def _resolve_comparisons(specs: list, labels: list[str],
                         methods: list[str]) -> tuple[list[dict], list[dict]]:
    """Split declared comparisons into those a task computes and those it does not."""
    honored: list[dict] = []
    unsupported: list[dict] = []
    has_de = "de_vs_rest_wilcox" in methods
    for spec in specs:
        parsed = _parse_comparison(spec)
        if parsed is None:
            unsupported.append({
                "comparison": spec if isinstance(spec, str) else repr(spec),
                "reason": ("no adapter computes an arbitrary contrast; the only "
                           "contrast computed is one annotated candidate against "
                           "every other cell")})
        elif parsed["label"] not in labels:
            unsupported.append({
                "comparison": parsed["text"],
                "reason": (f"{parsed['label']!r} is not a candidate in this plan; "
                           f"candidates are {sorted(labels)}")})
        elif not has_de:
            unsupported.append({
                "comparison": parsed["text"],
                "reason": ("de_vs_rest_wilcox is not in the plan, so no task would "
                           "compute this contrast")})
        else:
            contrast = f"{parsed['label']} vs rest"
            if not any(h["comparison"] == contrast for h in honored):
                honored.append({"declared": parsed["text"],
                                "comparison": contrast,
                                "candidate_label": parsed["label"]})
    return honored, unsupported


def _comparisons_payload(honored: list[dict], tasks: list[dict]) -> list[dict]:
    """Which task keys compute each honoured comparison (audit trail in the plan)."""
    computed: dict[str, list[str]] = {}
    for t in tasks:
        if t.get("comparison"):
            computed.setdefault(t["comparison"], []).append(t["task_key"])
    return [{"declared": h["declared"], "comparison": h["comparison"],
             "candidate_label": h["candidate_label"],
             "computed_by": computed.get(h["comparison"], [])}
            for h in honored]


def _rejected_comparisons(unsupported: list[dict],
                          next_step: str | None = None) -> dict:
    return {
        "status": "failed",
        "error": ("refusing to freeze a comparison that no task computes: "
                  + "; ".join(f"{u['comparison']!r}: {u['reason']}"
                              for u in unsupported)),
        "unsupported_comparisons": unsupported,
        "supported_form": ("'<annotated candidate label> vs rest', with method "
                           "de_vs_rest_wilcox in the plan"),
        "why": ("a plan records the contrasts it will compute; a recorded contrast "
                "with no adapter behind it reads as a computed result"),
        "next": next_step or ("drop the comparison and state the question in "
                              "--question, add --methods de_vs_rest_wilcox and use a "
                              "candidate label, or write an adapter for the requested "
                              "contrast before planning it"),
    }


def _planned_geneset_file(plan: dict) -> str | None:
    for t in plan.get("tasks") or []:
        gs = (t.get("params") or {}).get("geneset_file")
        if gs:
            return gs
    return None


def _plan_drift(plan: dict, ds: sqlite3.Row, cfg: dict) -> dict | None:
    """Why the frozen plan no longer describes the imported project, if it does not."""
    data = plan.get("data") or {}
    if data.get("dataset_id") != ds["dataset_id"]:
        return {"detail": (f"plan dataset_id {data.get('dataset_id')!r} != imported "
                           f"{ds['dataset_id']!r}")}
    if data.get("source_sha256") and data["source_sha256"] != ds["source_sha256"]:
        return {"detail": "the object behind the plan changed (source_sha256 differs)"}
    planned = data.get("columns") or {}
    for role, key in (("annotation", "annotation_column"),
                      ("sample", "sample_column"),
                      ("condition", "condition_column")):
        if planned.get(role) != cfg[key]:
            return {"detail": (f"mapped {role} column changed from "
                               f"{planned.get(role)!r} to {cfg[key]!r}")}
    return None


def _candidate_drift(plan: dict, cands: list[sqlite3.Row]) -> dict | None:
    recorded = {c.get("candidate_id"): c
                for c in plan.get("candidate_universe") or []}
    current = {c["candidate_id"]: c for c in cands}
    if set(recorded) != set(current):
        added = sorted(set(current) - set(recorded))
        removed = sorted(set(recorded) - set(current))
        return {"detail": (f"candidate set changed (added={added}, "
                           f"removed={removed})")}
    changed = [cid for cid, c in current.items()
               if recorded[cid].get("label") != c["label"]
               or recorded[cid].get("n_cells") != c["n_cells"]]
    if changed:
        return {"detail": f"candidate label or n_cells changed for {changed}"}
    return None


def _candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT candidate_id, label, n_cells, is_unlabeled "
                        "FROM candidate ORDER BY is_unlabeled, n_cells DESC").fetchall()


# ---------------------------------------------------------------- build / write

def build_plan(project_dir: str | Path, question: str | None = None,
               methods: list[str] | None = None,
               comparisons: list[str] | None = None,
               geneset_file: str | None = None) -> dict:
    proj, conn, pid = _conn(project_dir)
    try:
        ds = _dataset(conn)
        if not ds:
            return {"status": "failed", "error": "no dataset imported"}
        cands = _candidates(conn)
        if not cands:
            return {"status": "failed",
                    "error": "no candidates: map an annotation column first",
                    "next": "sctda map --project DIR --role annotation --column COL"}
        cfg = _config(conn)

        if methods is not None and not isinstance(methods, (list, tuple)):
            return {"status": "failed",
                    "error": "methods must be a list of method ids",
                    "available": sorted(EXECUTABLE)}
        chosen = list(dict.fromkeys(methods or ["composition", "de_vs_rest_wilcox"]))
        unknown = [m for m in chosen if m not in EXECUTABLE]
        if unknown:
            return {"status": "failed", "error": f"unknown method ids: {unknown}",
                    "available": sorted(EXECUTABLE)}
        try:
            for mid in chosen:
                _method_scope(mid)
        except ProtocolError as exc:
            return {"status": "failed", "error": str(exc)}

        specs = _as_comparison_list(comparisons)
        if specs is None:
            return {"status": "failed",
                    "error": "comparisons must be a list of strings",
                    "supported_form": "'<annotated candidate label> vs rest'"}
        honored, unsupported = _resolve_comparisons(specs, [c["label"] for c in cands],
                                                    chosen)
        if unsupported:
            return _rejected_comparisons(unsupported)

        design = design_table(project_dir)
        try:
            tasks, gaps, effective = _compile_tasks(proj, cfg, cands, chosen, {},
                                                    geneset_file)
        except ProtocolError as exc:
            return {"status": "failed", "error": str(exc)}

        plan = {
            "schema": "sctda.protocol/1",
            "question": question or "(unspecified: descriptive screen of every annotated group)",
            "comparisons": [h["declared"] for h in honored],
            "comparisons_specified": bool(honored),
            "comparisons_resolved": _comparisons_payload(honored, tasks),
            "data": {
                "dataset_id": ds["dataset_id"],
                "source_path": ds["source_path"],
                "source_sha256": ds["source_sha256"],
                "n_cells": ds["n_cells"], "n_genes": ds["n_genes"],
                "assay": cfg["assay"],
                "columns": {"annotation": cfg["annotation_column"],
                            "sample": cfg["sample_column"],
                            "condition": cfg["condition_column"]},
            },
            "candidate_universe": [
                {"candidate_id": c["candidate_id"], "label": c["label"],
                 "n_cells": c["n_cells"], "is_unlabeled": bool(c["is_unlabeled"])}
                for c in cands
            ],
            "candidate_universe_rule": (
                "every group in the mapped annotation column; unannotated cells are their "
                "own candidate; no disease-relevance filter, no marker whitelist, no "
                "literature-frequency weighting"),
            "methods": [_method_meta(mid) for mid in chosen],
            "parameters": effective,
            "analysis_units": {
                "statistical_unit": "cell (primary); sample (supplementary only)",
                # The kernel applies no significance or effect-size cut of its own.
                # A plan that wants one declares it here; these stay null until a
                # rule is decided for this analysis, with a stated source.
                "cell_level_rule": None,
                "evidence_rule": None,
                "sample_level_rule": {
                    "min_samples": 3,
                    "min_cells_per_sample": 10,
                    "interval": "t-based 95%",
                    "agreement": ("direction matches the cell-level direction and the "
                                  "interval excludes zero"),
                    "supplementary": True,
                    "gates_results": False,
                    "note": ("pseudobulk describes cross-sample agreement only; it never "
                             "gates, filters or suppresses a cell-level result"),
                },
                "selection_note": (
                    "cell-level rows are reported as computed; no threshold is applied "
                    "by the kernel. To restrict a report, record a rule here "
                    "(max_p_value_adj / min_abs_log2FC) together with its source"),
            },
            "tasks": tasks,
            "gaps": gaps,
            "design": {"n_samples": len(design.get("samples") or []),
                       "condition_levels": design.get("condition_levels"),
                       "warnings": design.get("warnings")},
            "resource_budget": {"max_concurrent_tasks": 4,
                                "object_loads_per_run": 1,
                                "python_never_holds_expression_matrix": True},
            "expected_artifacts": sorted({p for mid in chosen
                                          for p in EXECUTABLE[mid]["provides"]}),
            "literature_basis": _literature_basis(chosen),
        }
        protocol_id, version, blob = _write_version(conn, pid, plan, None)
        return {"status": FROZEN_STATUS, "protocol_id": protocol_id, "version": version,
                "plan_sha256": sha256_text(blob), "n_candidates": len(cands),
                "n_tasks": len(tasks), "gaps": gaps,
                "comparisons_resolved": plan["comparisons_resolved"],
                "design_warnings": plan["design"]["warnings"],
                "next": f"sctda run --project {proj.root} --protocol {protocol_id}"}
    finally:
        conn.close()


def _supersede_older(conn: sqlite3.Connection, project_id: str,
                     protocol_id: str, version: int) -> None:
    """Mark older frozen-but-never-run versions as replaced by this one.

    Only `frozen` rows are touched: a version that is running, paused or
    completed keeps its status and its artifacts, so writing a new plan never
    undoes work that already happened.
    """
    conn.execute("UPDATE protocol SET status='superseded', superseded_by=? "
                 "WHERE project_id=? AND version < ? AND status=?",
                 (protocol_id, project_id, version, FROZEN_STATUS))


def _write_version(conn: sqlite3.Connection, project_id: str, plan: dict,
                   parent_protocol_id: str | None) -> tuple[str, int, str]:
    blob = canonical_json(plan)
    version = conn.execute("SELECT COALESCE(MAX(version),0)+1 v FROM protocol "
                           "WHERE project_id=?", (project_id,)).fetchone()["v"]
    protocol_id = new_id("prot")
    conn.execute("""INSERT INTO protocol(protocol_id, project_id, version, status,
                      created_utc, plan_json, plan_sha256, parent_protocol_id)
                    VALUES (?,?,?,?,?,?,?,?)""",
                 (protocol_id, project_id, version, FROZEN_STATUS, now(), blob,
                  sha256_text(blob), parent_protocol_id))
    _supersede_older(conn, project_id, protocol_id, version)
    conn.commit()
    return protocol_id, version, blob


def _slug(s: str) -> str:
    keep = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in s)
    return keep or "group"


def _literature_basis(methods: list[str]) -> dict:
    """What the corpus shows for each chosen method, plus its hand-checked anchor.

    The corpus counts are context for the choice, not a ranking weight: a method
    being common never makes it required, and being rare never disqualifies it.
    ``corpus_status`` keeps absence explicit (``found=false`` with the registry's
    reason) instead of an empty list that could be read as "not checked".
    """
    out: dict[str, dict] = {}
    for mid in methods:
        try:
            detail = literature_backing(mid, detail=True)
        except (CatalogError, OSError) as exc:
            detail = {
                "method_id": mid, "found": None, "quality": "unavailable",
                "reason": f"literature registry unavailable: {exc}",
                "groups": [], "instances": [], "excluded_related": [],
            }
        out[mid] = {
            "corpus_instances": detail.get("groups") or [],
            "corpus_status": {
                "found": detail.get("found"),
                "quality": detail.get("quality"),
                "reason": detail.get("reason"),
                "n_instances": detail.get("n_instances"),
                "n_papers": detail.get("n_papers"),
                "excluded_related": [
                    x.get("name") for x in (detail.get("excluded_related") or [])
                    if isinstance(x, dict)
                ],
            },
            "anchors": EXECUTABLE[mid].get("literature_refs", []),
            "note": ("corpus counts are context only and are never used as a weight "
                     "for candidate ranking or method selection"),
        }
    return out


def load_protocol(conn: sqlite3.Connection, protocol_id: str) -> dict:
    """Read a plan exactly as recorded. Columns are named explicitly because
    databases created before the approval gate was removed still carry the old
    approver columns; nothing here reads them."""
    row = conn.execute("""
        SELECT protocol_id, version, status, created_utc, plan_json, plan_sha256,
               parent_protocol_id, superseded_by
        FROM protocol WHERE protocol_id=?""", (protocol_id,)).fetchone()
    if not row:
        raise ProtocolError(f"protocol not found: {protocol_id}")
    return {"protocol_id": row["protocol_id"], "version": row["version"],
            "status": row["status"], "plan": json.loads(row["plan_json"]),
            "plan_sha256": row["plan_sha256"], "created_utc": row["created_utc"],
            "frozen": row["status"] == FROZEN_STATUS,
            "is_runnable": row["status"] in RUNNABLE_STATUSES,
            "parent_protocol_id": row["parent_protocol_id"],
            "superseded_by": row["superseded_by"]}


def freeze_check(conn: sqlite3.Connection, protocol_id: str) -> dict:
    """Recompute the frozen hash; a mismatch means the row was edited."""
    row = conn.execute("SELECT plan_json, plan_sha256, status FROM protocol "
                       "WHERE protocol_id=?", (protocol_id,)).fetchone()
    if not row:
        return {"ok": False, "error": "protocol not found"}
    actual = sha256_text(canonical_json(json.loads(row["plan_json"])))
    return {"ok": actual == row["plan_sha256"], "stored": row["plan_sha256"],
            "actual": actual, "status": row["status"]}


def pause(project_dir: str | Path, protocol_id: str,
          reason: str = "paused by operator") -> dict:
    """Stop a protocol from running further without discarding its state.

    A paused protocol keeps every recorded task and artifact; `run --resume`
    picks up only what has not succeeded. Pausing is a status change, never a
    rollback, so nothing that was computed disappears.
    """
    proj, conn, pid = _conn(project_dir)
    try:
        row = conn.execute("SELECT status FROM protocol WHERE protocol_id=?",
                           (protocol_id,)).fetchone()
        if not row:
            return {"status": "failed", "error": f"protocol not found: {protocol_id}"}
        if row["status"] not in ("frozen", "running"):
            return {"status": "failed",
                    "error": f"only a frozen or running protocol can pause "
                             f"(current: {row['status']})"}
        conn.execute("UPDATE protocol SET status='paused' WHERE protocol_id=?",
                     (protocol_id,))
        conn.execute("""INSERT INTO decision(decision_id, project_id, protocol_id, action,
                          rationale, created_utc) VALUES (?,?,?,?,?,?)""",
                     (new_id("dec"), pid, protocol_id, "protocol_paused", reason, now()))
        conn.commit()
        done = conn.execute("SELECT COUNT(*) c FROM task WHERE protocol_id=? AND "
                            "status='succeeded'", (protocol_id,)).fetchone()["c"]
        return {"status": "paused", "protocol_id": protocol_id,
                "tasks_already_succeeded": done,
                "resume_with": f"sctda run --project {proj.root} "
                               f"--protocol {protocol_id} --resume"}
    finally:
        conn.close()


def set_status(conn: sqlite3.Connection, protocol_id: str, status: str,
               expect: tuple[str, ...] | None = None) -> None:
    if expect:
        row = conn.execute("SELECT status FROM protocol WHERE protocol_id=?",
                           (protocol_id,)).fetchone()
        if row and row["status"] not in expect:
            raise ProtocolError(f"protocol {protocol_id} is {row['status']}, "
                                f"expected one of {expect}")
    conn.execute("UPDATE protocol SET status=? WHERE protocol_id=?", (status, protocol_id))
    conn.commit()


def revise(project_dir: str | Path, protocol_id: str, changes: dict) -> dict:
    """Write a new frozen version derived from an existing protocol.

    The parent row is never edited: it keeps its plan, hash and artifacts, and
    only becomes `superseded` if it had not run yet.

    `changes` names plan scope only (`MUTABLE_KEYS`). A change to methods,
    parameters or the gene set file recompiles the task graph, method metadata,
    gaps and expected artifacts together, so the new version cannot carry tasks
    that disagree with its scope. The candidate universe is fixed by the mapped
    annotation column: a revision may restate it but not narrow or widen it.
    Derived keys are refused instead of accepted verbatim.
    """
    proj, conn, pid = _conn(project_dir)
    try:
        if not isinstance(changes, dict):
            return {"status": "failed",
                    "error": "changes must be an object of plan-scope keys",
                    "revised_keys": list(MUTABLE_KEYS)}
        if not changes:
            return {"status": "failed",
                    "error": "no changes: a revision must name at least one key",
                    "revised_keys": list(MUTABLE_KEYS)}
        try:
            base = load_protocol(conn, protocol_id)
        except ProtocolError as exc:
            return {"status": "failed", "error": str(exc)}
        plan = base["plan"]

        bad_keys = sorted(set(changes) - set(MUTABLE_KEYS))
        if bad_keys:
            return {"status": "failed",
                    "error": (f"cannot revise {bad_keys}: these keys are derived, "
                              f"not plan scope"),
                    "revised_keys": list(MUTABLE_KEYS),
                    "why": ("tasks, gaps and expected artifacts are computed from "
                            "methods/parameters/candidates; accepting them verbatim "
                            "would freeze work that no adapter produces"),
                    "next": ("revise "
                             + "/".join(MUTABLE_KEYS) + ", or run "
                             f"sctda plan --project {proj.root} to write a fresh "
                             "version")}

        ds = _dataset(conn)
        if not ds:
            return {"status": "failed", "error": "no dataset imported"}
        cfg = _config(conn)
        drift = _plan_drift(plan, ds, cfg)
        if drift:
            return {"status": "failed",
                    "error": ("the imported project no longer matches the frozen "
                              "plan: " + drift["detail"]),
                    "next": (f"sctda plan --project {proj.root} --question ... "
                             f"--methods ... (a revision derives from its parent; it "
                             f"cannot switch datasets or mappings)")}

        cands = _candidates(conn)
        if not cands:
            return {"status": "failed",
                    "error": "no candidates: map an annotation column first",
                    "next": "sctda map --project DIR --role annotation --column COL"}
        cand_drift = _candidate_drift(plan, cands)
        if cand_drift:
            return {"status": "failed",
                    "error": ("the candidate universe no longer matches the frozen "
                              "plan: " + cand_drift["detail"]),
                    "next": f"sctda plan --project {proj.root} --methods ... to plan "
                            f"the current annotation"}

        # the universe is whatever the annotation column enumerates; a revision
        # may restate it but never narrow or widen it
        if "candidate_universe" in changes:
            requested = changes["candidate_universe"] or []
            if not isinstance(requested, (list, tuple)):
                return {"status": "failed",
                        "error": "candidate_universe must be a list of candidates"}
            req_ids = [c.get("candidate_id") if isinstance(c, dict) else c
                       for c in requested]
            current = {c["candidate_id"]: c for c in cands}
            unknown = [cid for cid in req_ids if cid not in current]
            if (unknown or set(req_ids) != set(current)
                    or len(req_ids) != len(set(req_ids))):
                return {"status": "failed",
                        "error": ("a revision cannot change the candidate universe: "
                                  "it is every group in the mapped annotation column"),
                        "unknown_candidate_ids": unknown,
                        "recorded": sorted(current),
                        "requested": req_ids,
                        "next": ("to analyse different groups, map a different "
                                 "annotation column and run sctda plan; excluding a "
                                 "group by hand would turn the universe into a "
                                 "selection")}

        if "methods" in changes:
            if not isinstance(changes["methods"], (list, tuple)):
                return {"status": "failed",
                        "error": "methods must be a list of method ids",
                        "available": sorted(EXECUTABLE)}
            chosen = list(dict.fromkeys(changes["methods"] or []))
        else:
            chosen = list(dict.fromkeys(
                m["method_id"] for m in plan.get("methods") or []))
        if not chosen:
            return {"status": "failed",
                    "error": "a plan needs at least one method",
                    "available": sorted(EXECUTABLE)}
        unknown = [m for m in chosen if m not in EXECUTABLE]
        if unknown:
            return {"status": "failed", "error": f"unknown method ids: {unknown}",
                    "available": sorted(EXECUTABLE)}

        param_changes = changes.get("parameters") or {}
        if not isinstance(param_changes, dict):
            return {"status": "failed",
                    "error": ("parameters must map method_id -> parameter overrides")}
        stray = [mid for mid in param_changes if mid not in chosen]
        if stray:
            return {"status": "failed",
                    "error": f"parameters given for method(s) not in the plan: {stray}",
                    "methods": chosen,
                    "next": ("add the method to methods in the same revision, or "
                             "drop its parameters")}
        malformed = [mid for mid, val in param_changes.items()
                     if val is not None and not isinstance(val, dict)]
        if malformed:
            return {"status": "failed",
                    "error": (f"parameters for {malformed} must be an object of "
                              f"name -> value")}
        overrides = {}
        recorded_params = plan.get("parameters") or {}
        for mid in chosen:
            # retain the parent's effective parameters for kept methods, then
            # apply this revision's overrides; a new method starts from defaults.
            # geneset_file is a resolved path, not a catalog parameter: it is
            # re-resolved by _compile_tasks, never fed back as an override.
            merged = {k: v for k, v in (recorded_params.get(mid) or {}).items()
                      if k != "geneset_file"}
            merged.update(param_changes.get(mid) or {})
            overrides[mid] = merged

        new_plan = dict(plan)
        if "question" in changes:
            new_plan["question"] = changes["question"]
        if "analysis_units" in changes:
            units_change = changes["analysis_units"]
            if not isinstance(units_change, dict):
                return {"status": "failed",
                        "error": "analysis_units must be an object of unit rules"}
            # reasoning reads these keys by name; a misspelled key would be
            # recorded as a rule and silently ignored, so it is refused here
            allowed_units = set(new_plan.get("analysis_units") or {})
            allowed_units.add("min_samples_per_group")     # read as a legacy alias
            unknown_units = sorted(set(units_change) - allowed_units)
            if unknown_units:
                return {"status": "failed",
                        "error": (f"unknown analysis_units key(s) {unknown_units}: a "
                                  f"recorded rule is read by key, so a misspelled key "
                                  f"would have no effect"),
                        "allowed": sorted(allowed_units)}
            units = dict(new_plan.get("analysis_units") or {})
            units.update(units_change)
            new_plan["analysis_units"] = units

        # an unchanged revision keeps the gene set the parent resolved; it never
        # silently picks up a snapshot that appeared after the parent was frozen
        if "geneset_file" in changes:
            geneset_file = changes["geneset_file"]
        else:
            geneset_file = _planned_geneset_file(plan)
        try:
            tasks, gaps, effective = _compile_tasks(proj, cfg, cands, chosen,
                                                    overrides, geneset_file,
                                                    discover_genesets=False)
        except ProtocolError as exc:
            return {"status": "failed", "error": str(exc)}

        specs = _as_comparison_list(changes.get("comparisons",
                                                plan.get("comparisons") or []))
        if specs is None:
            return {"status": "failed",
                    "error": "comparisons must be a list of strings",
                    "supported_form": "'<annotated candidate label> vs rest'",
                    "next": "pass comparisons=[] to clear a malformed inherited list"}
        honored, unsupported = _resolve_comparisons(specs, [c["label"] for c in cands],
                                                    chosen)
        if unsupported:
            return _rejected_comparisons(
                unsupported,
                next_step=("pass comparisons=[] to clear the inherited contrast, add "
                           "de_vs_rest_wilcox to methods and use a candidate label, or "
                           f"run sctda plan --project {proj.root} for a fresh version"))

        new_plan.update({
            "methods": [_method_meta(mid) for mid in chosen],
            "parameters": effective,
            "tasks": tasks,
            "gaps": gaps,
            "expected_artifacts": sorted({p for mid in chosen
                                          for p in EXECUTABLE[mid]["provides"]}),
            "literature_basis": _literature_basis(chosen),
            "comparisons": [h["declared"] for h in honored],
            "comparisons_specified": bool(honored),
            "comparisons_resolved": _comparisons_payload(honored, tasks),
        })
        new_pid, version, blob = _write_version(conn, pid, new_plan, protocol_id)
        return {"status": FROZEN_STATUS, "protocol_id": new_pid, "version": version,
                "plan_sha256": sha256_text(blob),
                "parent_protocol_id": protocol_id,
                "changed_scope_keys": [k for k in changes if k in MUTABLE_SCOPE_KEYS],
                "revised_keys": sorted(changes),
                "n_tasks": len(tasks),
                "task_graph_rebuilt": tasks != (plan.get("tasks") or []),
                "next": f"sctda run --project {proj.root} --protocol {new_pid}"}
    finally:
        conn.close()
