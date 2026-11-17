"""Import a Seurat object into a project: inspect, map columns, enumerate candidates.

Contract (from the design plan):
- the source object is opened read-only; its hash is recorded before and after
  and a mismatch is a hard error, because a "harmless" write back to the user's
  object would be a silent data corruption;
- nothing is assumed from column names. Sample / condition / annotation columns
  come from the caller. If they are absent, the real columns are listed and the
  import stops at `needs_mapping` instead of guessing;
- the candidate set is every annotated group in the configured annotation
  column. No disease-relevant subset, no marker whitelist, no fixed depth.
  Unannotated cells (NA) become their own explicitly-labelled candidate.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .project import Project, get_or_create_project, new_id, now, sha256_file
from .rbridge import inspect_object

UNLABELED = "__unlabeled__"
ROLES = ("annotation", "sample", "condition")

# a column can plausibly be an annotation / grouping column when it is discrete
MIN_LEVELS, MAX_LEVELS = 2, 300


def _column_rows(summary: dict) -> list[dict]:
    rows = []
    for c in summary.get("columns", []):
        rows.append({
            "column_name": c["column_name"],
            "dtype": c.get("dtype"),
            "n_levels": c.get("n_levels"),
            "n_na": c.get("n_na") or 0,
            "levels": c.get("levels") or [],
            "levels_truncated": bool(c.get("levels_truncated")),
            "level_counts": c.get("level_counts") or {},
            "is_character_or_factor": bool(c.get("is_character_or_factor")),
            "is_discrete": bool(c.get("is_discrete", c.get("is_character_or_factor"))),
            "n_distinct": c.get("n_distinct"),
        })
    return rows


def _propose(rows: list[dict], n_cells: int) -> dict:
    """Show the real column inventory; suggest an ordering, never a decision.

    Column names are deliberately not used to infer roles. Every discrete column
    is listed, including degenerate ones (a one-level annotation column is a
    real defect worth surfacing, not hiding). `suggested_annotation` is ordered
    coarsest-last/finest-first only because sample sheets are usually finer than
    cell-type columns; it is an ordering, not a judgement.
    """
    discrete = [r for r in rows if r["is_discrete"]]
    numeric = [r for r in rows if not r["is_discrete"]]

    def per_level(r) -> int | None:
        counts = list((r["level_counts"] or {}).values())
        if not counts:
            return None
        return sorted(counts)[len(counts) // 2]

    inventory = sorted(discrete, key=lambda r: (-(r["n_levels"] or 0), r["column_name"]))
    suggested = sorted([r for r in inventory if (r["n_levels"] or 0) >= 2],
                       key=lambda r: ((r["n_levels"] or 0), r["column_name"]))
    return {
        "columns_discrete": [
            {"column_name": r["column_name"], "dtype": r["dtype"],
             "n_levels": r["n_levels"], "n_na": r["n_na"],
             "median_cells_per_level": per_level(r),
             "degenerate": (r["n_levels"] or 0) <= 1}
            for r in inventory
        ],
        "columns_numeric": [
            {"column_name": r["column_name"], "dtype": r["dtype"],
             "n_distinct": r["n_distinct"], "n_na": r["n_na"]}
            for r in sorted(numeric, key=lambda r: r["column_name"])
        ],
        "suggested_annotation": [
            {"column_name": r["column_name"], "n_levels": r["n_levels"],
             "levels_preview": r["levels"][:30],
             "levels_truncated": r["levels_truncated"]}
            for r in suggested[:10]
        ],
        "note": ("列名不作推断：请用 --annotation-column / --sample-column / "
                 "--condition-column 指定，或用 sctda map 设置。"
                 "suggested_annotation 仅按层级数从细到粗排列，不是判定。"),
    }


def import_dataset(project_dir: str | Path, object_path: str | Path,
                   object_name: str | None = None,
                   annotation_column: str | None = None,
                   sample_column: str | None = None,
                   condition_column: str | None = None,
                   notes: str | None = None,
                   include_unlabeled: bool = False) -> dict:
    proj = Project(Path(project_dir)).ensure()
    obj = Path(object_path).expanduser().resolve()
    if not obj.exists():
        raise FileNotFoundError(f"object not found: {obj}")

    conn = proj.connect()
    try:
        pid = get_or_create_project(conn, proj.root)

        existing = conn.execute(
            "SELECT dataset_id FROM dataset WHERE source_path=? AND project_id=?",
            (str(obj), pid)).fetchone()

        before = sha256_file(obj)
        summary_path = proj.dirs["meta"] / f"inspect_{obj.stem}.json"
        res = inspect_object(obj, summary_path, object_name=object_name,
                             log_path=proj.dirs["logs"] / f"inspect_{obj.stem}.log")
        if not res.ok:
            return {"status": "failed", "stage": "inspect",
                    "error": res.payload.get("error") or res.stderr[-2000:],
                    "log": str(res.log_path)}
        summary = json.loads(summary_path.read_text())
        after = sha256_file(obj)
        if before != after:
            return {"status": "failed", "stage": "readonly_check",
                    "error": "source object hash changed during inspection; "
                             "refusing to continue"}

        did = existing["dataset_id"] if existing else new_id("ds")
        conn.execute("DELETE FROM dataset_column WHERE dataset_id=?", (did,))
        conn.execute("""
            INSERT INTO dataset(dataset_id, project_id, source_path, source_sha256,
                                imported_utc, n_cells, n_genes, assays_json,
                                reductions_json, layers_json, seurat_version,
                                r_version, read_log)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(dataset_id) DO UPDATE SET
                source_sha256=excluded.source_sha256, imported_utc=excluded.imported_utc,
                n_cells=excluded.n_cells, n_genes=excluded.n_genes,
                assays_json=excluded.assays_json, reductions_json=excluded.reductions_json,
                layers_json=excluded.layers_json, seurat_version=excluded.seurat_version,
                r_version=excluded.r_version, read_log=excluded.read_log
        """, (did, pid, str(obj), before, now(), summary["n_cells"], summary["n_genes"],
              json.dumps(summary.get("assays")), json.dumps(summary.get("reductions")),
              json.dumps([a.get("layers") for a in summary.get("assays", [])]),
              summary.get("seurat_version"), summary.get("r_version"),
              str(res.log_path)))

        rows = _column_rows(summary)
        conn.executemany("""
            INSERT INTO dataset_column(dataset_id, column_name, n_levels, n_na,
                                       levels_json, dtype)
            VALUES (?,?,?,?,?,?)
        """, [(did, r["column_name"], r["n_levels"], r["n_na"],
               json.dumps({"levels": r["levels"], "counts": r["level_counts"],
                           "truncated": r["levels_truncated"]}), r["dtype"])
              for r in rows])

        by_name = {r["column_name"]: r for r in rows}
        requested = {"annotation": annotation_column, "sample": sample_column,
                     "condition": condition_column}
        problems = []
        for role, col in requested.items():
            if col is None:
                continue
            if col not in by_name:
                problems.append(f"{role} column {col!r} is not in the object; "
                                f"available: {', '.join(sorted(by_name))}")
            elif not by_name[col]["level_counts"]:
                problems.append(f"{role} column {col!r} is not a discrete "
                                f"character/factor column ({by_name[col]['dtype']})")
        if problems:
            conn.commit()
            return {"status": "failed", "stage": "mapping", "dataset_id": did,
                    "errors": problems,
                    "columns": sorted(by_name),
                    "proposal": _propose(rows, summary["n_cells"])}

        for role, col in requested.items():
            if col is None:
                conn.execute("DELETE FROM mapping WHERE dataset_id=? AND role=?", (did, role))
                continue
            conn.execute("""
                INSERT INTO mapping(mapping_id, dataset_id, role, column_name,
                                    confirmed_by, confirmed_utc, notes)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(dataset_id, role) DO UPDATE SET
                    column_name=excluded.column_name,
                    confirmed_by=excluded.confirmed_by,
                    confirmed_utc=excluded.confirmed_utc
            """, (new_id("map"), did, role, col, "cli", now(), notes))

        conn.commit()

        out = {"status": "imported", "dataset_id": did,
               "n_cells": summary["n_cells"], "n_genes": summary["n_genes"],
               "seurat_version": summary.get("seurat_version"),
               "assays": [a["name"] for a in summary.get("assays", [])],
               "reductions": summary.get("reductions"),
               "source_sha256_16": before[:16],
               "readonly_verified": True}

        if annotation_column is None:
            out["status"] = "needs_mapping"
            out["proposal"] = _propose(rows, summary["n_cells"])
            out["next"] = ("sctda import ... --annotation-column COL "
                           "[--sample-column COL] [--condition-column COL]")
            return out
        n_na_anno = by_name[annotation_column]["n_na"] if annotation_column in by_name else 0
        out["annotation_column"] = annotation_column
        out["n_na"] = n_na_anno
        out["candidates"] = enumerate_candidates(conn, did, annotation_column,
                                                 include_unlabeled=include_unlabeled)
        out["missing_roles"] = [r for r in ("sample", "condition")
                               if r not in requested or requested[r] is None]
        return out
    finally:
        conn.close()


def map_column(conn: sqlite3.Connection, dataset_id: str, role: str, column: str,
               notes: str | None = None, confirmed_by: str = "cli",
               include_unlabeled: bool = False) -> dict:
    """Set a role->column mapping, refusing the change that would invalidate history.

    Re-pointing the annotation column after tasks were computed would leave those
    tasks attached to candidate rows that no longer describe the data. Rather
    than silently orphaning them, that remap is refused.
    """
    if role not in ROLES:
        return {"status": "failed", "error": f"unknown role {role!r}; allowed {ROLES}"}
    row = conn.execute("SELECT column_name FROM dataset_column WHERE dataset_id=? "
                       "AND column_name=?", (dataset_id, column)).fetchone()
    if not row:
        return {"status": "failed", "error": f"column {column!r} not in object",
                "columns": [r["column_name"] for r in conn.execute(
                    "SELECT column_name FROM dataset_column WHERE dataset_id=?",
                    (dataset_id,))]}
    cur = conn.execute("SELECT role, column_name FROM mapping WHERE dataset_id=? AND role=?",
                       (dataset_id, role)).fetchone()
    if role == "annotation" and cur and cur["column_name"] != column:
        # count tasks belonging to any protocol that declared this dataset: a
        # dataset-wide task (e.g. composition) has no candidate_id, so looking
        # only at candidate-linked tasks would miss exactly those runs
        n_tasks = conn.execute(
            "SELECT COUNT(*) c FROM task t JOIN protocol p ON p.protocol_id=t.protocol_id "
            "WHERE json_extract(p.plan_json, '$.data.dataset_id')=?",
            (dataset_id,)).fetchone()["c"]
        if n_tasks:
            return {"status": "failed",
                    "error": (f"annotation already used by {n_tasks} computed task(s); "
                              f"re-pointing it would invalidate that history"),
                    "hint": "start a new project for a different annotation column",
                    "current": cur["column_name"], "requested": column}
        conn.execute("DELETE FROM candidate WHERE dataset_id=? AND mapping_id IN "
                     "(SELECT mapping_id FROM mapping WHERE dataset_id=? AND role='annotation')",
                     (dataset_id, dataset_id))
    conn.execute("""
        INSERT INTO mapping(mapping_id, dataset_id, role, column_name, confirmed_by,
                            confirmed_utc, notes)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(dataset_id, role) DO UPDATE SET
            column_name=excluded.column_name,
            confirmed_by=excluded.confirmed_by,
            confirmed_utc=excluded.confirmed_utc,
            notes=excluded.notes
    """, (new_id("map"), dataset_id, role, column, confirmed_by, now(), notes))
    conn.commit()
    out = {"status": "mapped", "role": role, "column": column}
    if role == "annotation":
        col_row = conn.execute("SELECT n_na FROM dataset_column WHERE dataset_id=? AND column_name=?",
                               (dataset_id, column)).fetchone()
        out["n_na"] = col_row["n_na"] if col_row else 0
        out["candidates"] = enumerate_candidates(conn, dataset_id, column,
                                                 include_unlabeled=include_unlabeled)
    return out


def enumerate_candidates(conn: sqlite3.Connection, dataset_id: str,
                         annotation_column: str | None = None,
                         include_unlabeled: bool = False) -> list[dict]:
    """Every group in the annotation column, plus unannotated cells only if explicitly requested."""
    if annotation_column is None:
        row = conn.execute("SELECT column_name FROM mapping WHERE dataset_id=? AND role='annotation'",
                           (dataset_id,)).fetchone()
        if not row:
            return []
        annotation_column = row["column_name"]
    row = conn.execute("SELECT levels_json, n_na FROM dataset_column "
                       "WHERE dataset_id=? AND column_name=?",
                       (dataset_id, annotation_column)).fetchone()
    if not row:
        return []
    blob = json.loads(row["levels_json"] or "{}")
    counts = blob.get("counts") or {}
    mapping = conn.execute("SELECT mapping_id FROM mapping WHERE dataset_id=? AND role='annotation'",
                           (dataset_id,)).fetchone()
    mapping_id = mapping["mapping_id"]
    conn.execute("DELETE FROM candidate WHERE dataset_id=? AND mapping_id=?",
                 (dataset_id, mapping_id))
    out = []
    for label, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        cid = new_id("cand")
        conn.execute("INSERT INTO candidate(candidate_id, dataset_id, mapping_id, label,"
                     " n_cells, is_unlabeled) VALUES (?,?,?,?,?,0)",
                     (cid, dataset_id, mapping_id, label, n))
        out.append({"candidate_id": cid, "label": label, "n_cells": n,
                    "is_unlabeled": False})
    n_na = row["n_na"] or 0
    if n_na and include_unlabeled:
        cid = new_id("cand")
        conn.execute("INSERT INTO candidate(candidate_id, dataset_id, mapping_id, label,"
                     " n_cells, is_unlabeled, notes) VALUES (?,?,?,?,?,1,?)",
                     (cid, dataset_id, mapping_id, UNLABELED, n_na,
                      "cells with no annotation; explicitly requested by caller"))
        out.append({"candidate_id": cid, "label": UNLABELED, "n_cells": n_na,
                    "is_unlabeled": True})
    conn.commit()
    return out
