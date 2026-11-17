"""Reasoning: turn artifacts into located, evidence-backed statements.

Deliberate limits, matching the design:
- the cell-level comparison is the reported result. The sample-level table
  (pseudobulk) is supplementary corroboration: it never gates, filters or
  suppresses a cell-level result, and when the two disagree both are reported;
- a gene that fails the cell-level rule is not "absent from the data". A
  missing statistic and a non-significant one are different states, and a
  sample-level table that was never computed is recorded as
  `not_computed`, never as a zero effect;
- every candidate is explained, including ones with no usable evidence;
- no single weighted score ranks candidates. Dimensions are reported side by
  side and candidates are grouped into sufficient / disputed / insufficient.
  Where evidence is not comparable, they stay tied;
- a target candidate must be bound to (cell group, comparison) and carry the
  direction actually observed in this data;
- an observed direction of gene expression is NOT evidence that a drug in that
  direction will work, so the intervention chain stops at the target whenever the
  pharmacology direction is unknown;
- literature frequency never enters a score.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from . import literature
from .evidence import add_evidence, ensure_claim, stale_claims
from .project import Project, get_or_create_project, new_id, now

# The kernel declares no significance or effect-size threshold: a fixed cut would
# be a scientific decision nobody made. A plan may record one in
# `analysis_units.cell_level_rule` together with its source; when it does, each
# row is annotated with whether it passes that rule. Rows are never dropped for a
# rule the analysis did not declare.
#
# The sample-level block is descriptive, not a gate: what `corroborates` means is
# fixed here because it is a property of the table, and it never filters the
# cell-level result.
SAMPLE_LEVEL_DEFAULT = {"min_samples": 3, "min_cells_per_sample": 10}
NO_RULE_SOURCE = "none declared (no kernel default applied)"


def _load(conn: sqlite3.Connection, protocol_id: str, kind: str) -> list[dict]:
    out = []
    for a in conn.execute("SELECT * FROM artifact WHERE protocol_id=? AND kind=?",
                          (protocol_id, kind)):
        p = Path(a["path"])
        if not p.exists():
            continue
        with p.open(newline="", encoding="utf-8", errors="replace") as fh:
            for r in csv.DictReader(fh):
                r["_artifact_id"] = a["artifact_id"]
                r["_path"] = str(p)
                r["_task_id"] = a["task_id"]
                out.append(r)
    return out


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v, default=0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _declared_rule(raw) -> dict | None:
    """A rule only counts as declared when the plan carries the numbers."""
    if not isinstance(raw, dict):
        return None
    rule: dict = {}
    for key in ("max_p_value_adj", "min_abs_log2FC"):
        v = raw.get(key)
        if isinstance(v, (int, float)):
            rule[key] = float(v)
    if not rule:
        return None
    rule["source"] = raw.get("source") or "declared in the analysis plan"
    return rule


def rule_from_plan(plan: dict | None) -> tuple[dict | None, dict, dict | None, str]:
    """What this protocol declared, and nothing more.

    Returns (cell_level_rule, sample_level_rule, evidence_rule, source). With no
    rule recorded the cell-level rule is `None` and the source says so: the
    kernel does not substitute a default threshold of its own.
    """
    au = (plan or {}).get("analysis_units") or {}
    cell = _declared_rule(au.get("cell_level_rule"))
    evidence = au.get("evidence_rule") if isinstance(au.get("evidence_rule"), dict) else None
    sample = dict(SAMPLE_LEVEL_DEFAULT)
    srule = au.get("sample_level_rule") or {}
    for k in sample:
        if isinstance(srule.get(k), (int, float)):
            sample[k] = int(srule[k])
    # legacy plans recorded the sample floor under a different key
    if "min_samples" not in srule and isinstance(au.get("min_samples_per_group"),
                                                 (int, float)):
        sample["min_samples"] = int(au["min_samples_per_group"])
    source = "plan" if cell else NO_RULE_SOURCE
    return cell, sample, evidence, source


def _row_passes(padj: float | None, lfc: float | None,
                rule: dict | None) -> bool | None:
    """Whether one row satisfies the declared rule.

    `None` means "not evaluated": no rule is declared, or the statistic the rule
    needs is missing. A missing statistic is never recorded as failing the rule.
    """
    if rule is None:
        return None
    if "max_p_value_adj" in rule:
        if padj is None:
            return None
        if padj > rule["max_p_value_adj"]:
            return False
    if "min_abs_log2FC" in rule:
        if lfc is None:
            return None
        if abs(lfc) < rule["min_abs_log2FC"]:
            return False
    return True


def cell_level_rows(de_rows: list[dict], rule: dict | None) -> list[dict]:
    """Every cell-level row the adapter wrote, annotated with the declared rule.

    No row is dropped: a row without a statistic is kept and marked as such, and
    when no rule is declared `passes_declared_rule` is None rather than False.
    A missing statistic is not read as a zero effect.
    """
    out = []
    for r in de_rows:
        if not r.get("gene"):
            continue
        lfc = _f(r.get("avg_log2FC"))
        out.append({"gene": r["gene"], "candidate": r.get("candidate"),
                    "direction": ("higher" if (lfc or 0) > 0 else
                                  "lower" if (lfc or 0) < 0 else "undetermined"),
                    "avg_log2FC": lfc, "p_val": _f(r.get("p_val")),
                    "p_val_adj": _f(r.get("p_val_adj")),
                    "pct_candidate": _f(r.get("pct.1")),
                    "pct_rest": _f(r.get("pct.2")),
                    "n_cells_candidate": _int(r.get("n_cells_candidate")),
                    "test": r.get("test") or "wilcox_cell_level",
                    "independence_warning": r.get("independence_warning") or
                    "cells treated as independent replicates; p-values are "
                    "anti-conservative",
                    "passes_declared_rule": _row_passes(_f(r.get("p_val_adj")), lfc,
                                                        rule),
                    "declared_rule": rule,
                    "artifact_id": r.get("_artifact_id"), "_path": r.get("_path"),
                    # the task that wrote the row: a contrast can only be
                    # attributed to the adapter that actually produced it
                    "_task_id": r.get("_task_id")})
    return out


def n_passing(rows: list[dict]) -> int | None:
    """How many rows pass the declared rule; None when no rule was declared."""
    if not rows or rows[0].get("declared_rule") is None:
        return None
    return sum(1 for g in rows if g.get("passes_declared_rule") is True)


def sample_level_status(row: dict | None, direction: str, rule: dict) -> dict:
    """Supplementary sample-level reading for one cell-level hit.

    Never a gate: the caller reports a cell-level hit whether this is
    `corroborates`, `discordant`, `not_corroborated` or `not_computed`.
    """
    min_n = int(rule.get("min_samples", SAMPLE_LEVEL_DEFAULT["min_samples"]))
    if row is None:
        return {"status": "not_computed", "n_samples": 0,
                "mean_log2FC_sample_level": None, "ci": None, "basis": None,
                "is_supplementary": True,
                "reason": ("no sample-level row for this gene: the sample column "
                           "was not mapped, or too few cells per sample were "
                           "available to aggregate")}
    n = _int(row.get("n_samples"))
    m = _f(row.get("mean_log2FC_sample_level"))
    lo, hi = _f(row.get("ci_low")), _f(row.get("ci_high"))
    base = {"n_samples": n, "mean_log2FC_sample_level": m,
            "ci": ([lo, hi] if lo is not None and hi is not None else None),
            "basis": row.get("basis"), "is_supplementary": True}
    if m is None or lo is None or hi is None:
        return {**base, "status": "not_corroborated",
                "reason": "sample-level effect or interval is missing"}
    if n < min_n:
        return {**base, "status": "not_corroborated",
                "reason": f"fewer than {min_n} samples contribute"}
    if (m > 0) != (direction == "higher"):
        return {**base, "status": "discordant",
                "reason": ("sample-level direction is opposite to the cell-level "
                           "direction")}
    if lo <= 0 <= hi:
        return {**base, "status": "not_corroborated",
                "reason": "sample-level 95% interval includes zero"}
    return {**base, "status": "corroborates", "reason": None}


def _sample_index(de_sample_rows: list[dict]) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for r in de_sample_rows:
        gene = r.get("gene")
        cand = r.get("candidate")
        if gene and cand:
            out[(cand, gene)] = r
    return out


def _attach_sample_level(genes: list[dict], de_sample_rows: list[dict],
                         rule: dict) -> list[dict]:
    idx = _sample_index(de_sample_rows)
    for g in genes:
        g["sample_level"] = sample_level_status(
            idx.get((g["candidate"], g["gene"])), g["direction"], rule)
    return genes


def _sample_tally(genes: list[dict]) -> dict:
    tally: dict[str, int] = defaultdict(int)
    n_samples = 0
    for g in genes:
        sl = g.get("sample_level") or {}
        tally[sl.get("status") or "not_computed"] += 1
        n_samples = max(n_samples, _int(sl.get("n_samples")))
    return {"n_corroborates": tally.get("corroborates", 0),
            "n_discordant": tally.get("discordant", 0),
            "n_not_corroborated": tally.get("not_corroborated", 0),
            "n_not_computed": tally.get("not_computed", 0),
            "max_n_samples": n_samples,
            "is_supplementary": True,
            "gates_results": False}


def _de_files(conn: sqlite3.Connection, protocol_id: str) -> tuple[list[dict], list[dict]]:
    return (_load(conn, protocol_id, "de_cell_level"),
            _load(conn, protocol_id, "de_sample_level"))


def _cell_statement(label: str, n_cells: int, genes: list[dict], cell_rule: dict | None,
                    n_pass: int | None, sample_rule: dict, sample_table_present: bool,
                    n_reported: int, top_n: int | None,
                    state: dict | None = None) -> str:
    """One candidate's cell-level statement, from that candidate's own state.

    `state` is the per-candidate outcome (`reported` / `empty_result` /
    `task_failed` / `no_data`). A protocol-wide table says nothing about this
    candidate: a failed task is not an empty result, and an absent table is not
    a zero effect.
    """
    state = state or {}
    status = state.get("status") or ("reported" if n_reported else "no_data")
    reason = state.get("reason")
    if status == "task_failed":
        return (f"No cell-level table exists for {label} ({n_cells} cells): "
                f"{reason or 'the task failed and no result exists'}.")
    if status == "no_data":
        detail = reason or ("the comparison was not computed, so its direction is "
                            "unknown, not absent")
        return (f"No cell-level table is recorded for {label} ({n_cells} cells): "
                f"{detail}.")
    if n_reported == 0:
        return (f"The cell-level table holds no gene rows for {label} ({n_cells} cells): "
                f"an empty result, not an absent comparison.")
    higher = sum(1 for g in genes if g["direction"] == "higher")
    lower = sum(1 for g in genes if g["direction"] == "lower")
    lfcs = [abs(g["avg_log2FC"]) for g in genes if g["avg_log2FC"] is not None]
    padjs = [g["p_val_adj"] for g in genes if g["p_val_adj"] is not None]
    tally = _sample_tally(genes)
    if not sample_table_present:
        supp = (" Sample-level corroboration was not computed for this candidate, so "
                "the sample-level direction is unknown, not absent.")
    else:
        supp = (f" Sample-level corroboration (supplementary, never a gate): "
                f"{tally['n_corroborates']} corroborate, {tally['n_discordant']} "
                f"discordant, {tally['n_not_corroborated']} not corroborated over "
                f">= {int(sample_rule.get('min_samples', 3))} samples.")
    cap = (f"; the adapter table is cut at its top {int(top_n)} by adjusted p"
           if top_n else "")
    head = (f"In {label} ({n_cells} cells), {higher} "
            f"gene{'s' if higher != 1 else ''} "
            f"{'are' if higher != 1 else 'is'} higher and {lower} lower than the "
            f"remaining cells, among the {n_reported} genes the adapter reported")
    if cell_rule is None:
        facts = []
        if padjs:
            facts.append(f"lowest adjusted p {min(padjs):.3g}")
        if lfcs:
            facts.append(f"largest |log2FC| {max(lfcs):.2f}")
        return (head + " (cell-level Wilcoxon; cells are not independent replicates, so "
                f"these p-values are anti-conservative){cap}. "
                f"This plan declares no significance or effect-size rule, so the kernel "
                f"applies none: every reported row is kept and none is called "
                f"significant or excluded"
                + (f" ({'; '.join(facts)})" if facts else "") + "." + supp)
    bits = []
    if "max_p_value_adj" in cell_rule:
        bits.append(f"FDR <= {cell_rule['max_p_value_adj']:g}")
    if "min_abs_log2FC" in cell_rule:
        bits.append(f"|log2FC| >= {cell_rule['min_abs_log2FC']:g}")
    return (head + f" (cell-level Wilcoxon{cap}; cells are not independent replicates, "
            f"so these p-values are anti-conservative). "
            f"{n_pass} of {n_reported} rows pass the rule this plan declares "
            f"({'; '.join(bits)}; source: {cell_rule.get('source')}); the other rows "
            f"are reported, not deleted." + supp)


def derive_evidence(project_dir: str | Path, protocol_id: str,
                    only_candidates: set[str] | None = None) -> dict:
    """Record current_data evidence and claims for every candidate.

    The cell-level table produces the claim; the sample-level table only adds a
    supplementary sentence, so a candidate whose pseudobulk table is empty or
    absent still gets its cell-level result recorded.
    """
    proj = Project(Path(project_dir)).ensure()
    conn = proj.connect()
    try:
        pid = get_or_create_project(conn, proj.root)
        proto = conn.execute("SELECT * FROM protocol WHERE protocol_id=?",
                             (protocol_id,)).fetchone()
        if not proto:
            return {"status": "failed", "error": f"protocol not found: {protocol_id}"}
        plan = json.loads(proto["plan_json"])
        cell_rule, sample_rule, _evidence_rule, rule_source = rule_from_plan(plan)
        de_cell, de_sample = _de_files(conn, protocol_id)
        # per-candidate table state: a protocol having a table somewhere says
        # nothing about this candidate, so presence is read from its own task
        de_by_cand = _de_tasks_by_candidate(conn, protocol_id)
        sig = _load(conn, protocol_id, "signature_summary")
        comp = _load(conn, protocol_id, "composition_by_sample")
        # how many rows the adapter wrote per candidate: the claim's denominator,
        # because the adapter truncates its table and a count without its
        # denominator would read as a genome-wide count
        reported: dict[str, int] = defaultdict(int)
        for r in de_cell:
            if r.get("candidate"):
                reported[r["candidate"]] += 1
        top_n = ((plan.get("parameters") or {}).get("de_vs_rest_wilcox") or {}).get("top_n")

        observed = _attach_sample_level(cell_level_rows(de_cell, cell_rule),
                                        de_sample, sample_rule)
        by_cand: dict[str, list[dict]] = defaultdict(list)
        for g in observed:
            by_cand[g["candidate"]].append(g)

        n_claims = n_ev = 0
        touched_claims: set[str] = set()
        for cand in plan.get("candidate_universe", []):
            if only_candidates is not None and cand["label"] not in only_candidates:
                continue
            label = cand["label"]
            genes = by_cand.get(label, [])
            n_pass = n_passing(genes)
            de_tasks = de_by_cand.get(cand["candidate_id"], [])
            state = _candidate_state(de_tasks, reported.get(label, 0))
            statement = _cell_statement(label, cand["n_cells"], genes,
                                        cell_rule, n_pass, sample_rule,
                                        _has_sample_table(de_tasks),
                                        reported.get(label, 0),
                                        int(top_n) if top_n else None,
                                        state=state)
            cid = ensure_claim(conn, pid, "candidate", cand["candidate_id"],
                               statement, protocol_id)
            touched_claims.add(cid)
            n_claims += 1
            # every reported row is recorded: a cap here would itself be a selection
            # rule the analysis never declared
            for g in genes:
                r = add_evidence(conn, pid, cid, "current_data",
                                 origin=f"{g['candidate']}:{g['gene']}:{g['direction']}",
                                 artifact_id=g["artifact_id"],
                                 payload={"gene": g["gene"],
                                          "direction": g["direction"],
                                          "cell_level": {
                                              "avg_log2FC": g["avg_log2FC"],
                                              "p_val": g["p_val"],
                                              "p_val_adj": g["p_val_adj"],
                                              "pct_candidate": g["pct_candidate"],
                                              "pct_rest": g["pct_rest"],
                                              "n_cells_candidate": g["n_cells_candidate"],
                                              "test": g["test"],
                                              "independence_warning":
                                                  g["independence_warning"]},
                                          "passes_declared_rule":
                                              g["passes_declared_rule"],
                                          "declared_rule": g["declared_rule"],
                                          "sample_level": g["sample_level"],
                                          "path": g["_path"]})
                n_ev += r["status"] == "added"

            # pathway-level: gene sets whose mean score departs most in this group
            sets = [r for r in sig if r.get("candidate") == label]
            if sets:
                vals = []
                for r in sets:
                    ms = _f(r.get("mean_score"))
                    if ms is None:
                        continue
                    vals.append((r["gene_set"], ms, int(_f(r.get("n_cells"), 0) or 0)))
                if vals:
                    vals.sort(key=lambda x: -x[1])
                    top = vals[:3]
                    st2 = (f"In {label}, the highest scoring gene sets are "
                           + ", ".join(f"{n} ({m:.3f})" for n, m, _ in top))
                    cid2 = ensure_claim(conn, pid, "candidate", cand["candidate_id"],
                                        st2, protocol_id)
                    touched_claims.add(cid2)
                    n_claims += 1
                    art = sets[0]["_artifact_id"]
                    for n, m, nc in top:
                        r = add_evidence(conn, pid, cid2, "current_data",
                                         origin=f"{label}:{n}", artifact_id=art,
                                         payload={"gene_set": n, "mean_score": m,
                                                  "n_cells": nc})
                        n_ev += r["status"] == "added"

            # abundance: share of cells per sample
            rows = [r for r in comp if r.get("candidate") == label]
            props = [_f(r.get("proportion")) for r in rows]
            props = [p for p in props if p is not None]
            if props:
                st3 = (f"{label} accounts for a median {sorted(props)[len(props)//2]:.3f} "
                       f"of cells per sample across {len(props)} samples")
                cid3 = ensure_claim(conn, pid, "candidate", cand["candidate_id"],
                                    st3, protocol_id)
                touched_claims.add(cid3)
                n_claims += 1
                r = add_evidence(conn, pid, cid3, "current_data",
                                 origin=f"{label}:abundance",
                                 artifact_id=rows[0]["_artifact_id"],
                                 payload={"median_proportion": sorted(props)[len(props)//2],
                                          "n_samples": len(props)})
                n_ev += r["status"] == "added"
        return {"status": "ok", "protocol": protocol_id, "n_claims": n_claims,
                "claim_ids": sorted(touched_claims),
                "n_evidence_added": n_ev,
                "n_cell_level_reported": len(observed),
                "n_passing_declared_rule": n_passing(observed),
                "sample_level": _sample_tally(observed),
                "statistical_unit": "cell (primary); sample (supplementary only)",
                "thresholds": {"cell_level_rule": cell_rule,
                               "sample_level_rule": sample_rule,
                               "source": rule_source}}
    finally:
        conn.close()


def refresh_evidence(project_dir: str | Path, protocol_id: str) -> dict:
    """Re-derive only the claims whose supporting artifact bytes changed.

    A new artifact (or a re-run that produced different bytes) marks exactly the
    claims that depended on it. Untouched claims keep their existing evidence
    rows; superseded claims are marked, never deleted, and the reason is written
    to the decision log so the old conclusion and why it changed both survive.
    """
    proj = Project(Path(project_dir)).ensure()
    conn = proj.connect()
    try:
        pid = get_or_create_project(conn, proj.root)
        stale = stale_claims(conn)
        if not stale:
            total = conn.execute("SELECT COUNT(*) c FROM claim WHERE protocol_id=?",
                                 (protocol_id,)).fetchone()["c"]
            return {"status": "ok", "recomputed": 0, "evidence_added": 0,
                    "stale_claims_detected": 0, "claims_superseded": 0,
                    "unchanged_claims": total,
                    "candidates_reevaluated": [],
                    "message": "no claim depends on a changed artifact"}
        subjects = {}
        for cid in stale:
            row = conn.execute("SELECT subject_id, subject_type FROM claim WHERE claim_id=?",
                               (cid,)).fetchone()
            if row:
                subjects[cid] = (row["subject_type"], row["subject_id"])
        labels = set()
        for _t, sid in subjects.values():
            for r in conn.execute("SELECT label FROM candidate WHERE candidate_id=?",
                                  (sid,)):
                labels.add(r["label"])
        # only claims belonging to the affected candidates are candidates for
        # supersession; every other claim is out of scope for this refresh
        affected_cids = {r["claim_id"] for r in conn.execute(
            "SELECT claim_id FROM claim WHERE protocol_id=? AND subject_id IN "
            "(SELECT candidate_id FROM candidate WHERE label IN (%s))"
            % ",".join("?" * len(labels)), (protocol_id, *sorted(labels)))}
        before = {c["claim_id"]: c["statement"] for c in
                  conn.execute("SELECT claim_id, statement FROM claim WHERE protocol_id=?",
                               (protocol_id,)) if c["claim_id"] in affected_cids}
        out = derive_evidence(project_dir, protocol_id, only_candidates=labels)
        reaffirmed = set(out.get("claim_ids") or [])
        superseded = 0
        for cid, statement in before.items():
            if cid in reaffirmed:
                continue
            conn.execute("UPDATE claim SET status='superseded', updated_utc=? "
                         "WHERE claim_id=?", (now(), cid))
            conn.execute("""INSERT INTO decision(decision_id, project_id, claim_id,
                              protocol_id, action, rationale, created_utc)
                            VALUES (?,?,?,?,?,?,?)""",
                         (new_id("dec"), pid, cid, protocol_id, "claim_superseded",
                          "supporting artifact changed: " +
                          "; ".join(stale.get(cid, ["recomputation changed the statement"])),
                          now()))
            superseded += 1
        conn.commit()
        return {"status": "ok", "recomputed": out.get("n_claims", 0),
                "evidence_added": out.get("n_evidence_added", 0),
                "stale_claims_detected": len(stale),
                "claims_superseded": superseded,
                "unchanged_claims": len(reaffirmed),
                "candidates_reevaluated": sorted(labels)}
    finally:
        conn.close()


def build_dossiers(project_dir: str | Path, protocol_id: str) -> dict:
    """Every candidate gets a dossier; grouping is by evidence quality, not by a score."""
    proj = Project(Path(project_dir)).ensure()
    conn = proj.connect()
    try:
        get_or_create_project(conn, proj.root)
        proto = conn.execute("SELECT * FROM protocol WHERE protocol_id=?",
                             (protocol_id,)).fetchone()
        if not proto:
            return {"status": "failed", "error": f"protocol not found: {protocol_id}"}
        plan = json.loads(proto["plan_json"])
        cell_rule, sample_rule, evidence_rule, rule_source = rule_from_plan(plan)
        de_cell, de_sample = _de_files(conn, protocol_id)
        # per-candidate table state, not a protocol-wide flag: one candidate's
        # failed or absent table must not change another candidate's reading
        de_by_cand = _de_tasks_by_candidate(conn, protocol_id)
        sig = _load(conn, protocol_id, "signature_summary")
        comp = _load(conn, protocol_id, "composition_by_sample")
        reported: dict[str, int] = defaultdict(int)
        for r in de_cell:
            if r.get("candidate"):
                reported[r["candidate"]] += 1
        observed = _attach_sample_level(cell_level_rows(de_cell, cell_rule),
                                        de_sample, sample_rule)
        by_cand: dict[str, list[dict]] = defaultdict(list)
        for g in observed:
            by_cand[g["candidate"]].append(g)

        dossiers = []
        for cand in plan.get("candidate_universe", []):
            label = cand["label"]
            genes = by_cand.get(label, [])
            n_pass = n_passing(genes)
            tally = _sample_tally(genes)
            de_tasks = de_by_cand.get(cand["candidate_id"], [])
            state = _candidate_state(de_tasks, reported.get(label, 0))
            sets = [r for r in sig if r.get("candidate") == label]
            props = [_f(r.get("proportion")) for r in comp if r.get("candidate") == label]
            props = [p for p in props if p is not None]
            top = sorted(genes, key=lambda g: -abs(g["avg_log2FC"] or 0))[:10]

            dims = {
                "abundance": ({"median_proportion_per_sample":
                               sorted(props)[len(props) // 2],
                               "n_samples": len(props)} if props else None),
                "expression_cell_level": ({
                    "n_genes_reported": reported.get(label, 0),
                    "n_passing_declared_rule": n_pass,
                    "declared_rule": cell_rule,
                    "thresholds_source": rule_source,
                    "n_higher": sum(1 for g in genes if g["direction"] == "higher"),
                    "n_lower": sum(1 for g in genes if g["direction"] == "lower"),
                    "top_genes": [{"gene": g["gene"], "direction": g["direction"],
                                   "avg_log2FC": (round(g["avg_log2FC"], 3)
                                                  if g["avg_log2FC"] is not None else None),
                                   "p_val_adj": g["p_val_adj"],
                                   "pct_candidate": g["pct_candidate"],
                                   "pct_rest": g["pct_rest"],
                                   "passes_declared_rule": g["passes_declared_rule"],
                                   "sample_level": g["sample_level"]["status"]}
                                  for g in top]} if genes else None),
                "expression_sample_level": tally,
                "expression_table_state": state,
                "pathway": ({"n_gene_sets_scored": len({r["gene_set"] for r in sets}),
                             "top_gene_sets": sorted(
                                 [{"gene_set": r["gene_set"],
                                   "mean_score": round(_f(r.get("mean_score")) or 0, 4)}
                                  for r in sets[:5]],
                                 key=lambda x: -x["mean_score"])} if sets else None),
            }
            min_genes = (evidence_rule or {}).get("min_genes_passing_cell_rule")
            evaluable = sum(1 for g in genes if g["passes_declared_rule"] is not None)
            if state["status"] != "reported":
                # the candidate's own outcome decides this, never the protocol's:
                # a failed task and an empty table are different statements, and
                # neither is "0 genes were tested and none passed"
                klass, reason = "evidence_not_adjudicated", (
                    f"{reported.get(label, 0)} cell-level rows are reported; "
                    f"{state['reason']}; evidence cannot be called sufficient or "
                    f"insufficient")
            elif not isinstance(min_genes, (int, float)) or cell_rule is None or not genes:
                if cell_rule is None:
                    why = "no cell-level selection rule is declared"
                elif not genes:
                    why = "the table holds no rows for this candidate"
                else:
                    why = ("no evidence rule is declared "
                           "(evidence_rule.min_genes_passing_cell_rule)")
                klass, reason = "evidence_not_adjudicated", (
                    f"{reported.get(label, 0)} cell-level rows are reported; {why}, so "
                    f"the kernel makes no sufficient/insufficient call")
            elif evaluable == 0:
                klass, reason = "evidence_not_adjudicated", (
                    f"none of the {reported.get(label, 0)} reported rows carries the "
                    f"statistics the declared rule needs, so the rule cannot be "
                    f"evaluated here")
            elif n_pass >= min_genes:
                klass, reason = "evidence_sufficient", (
                    f"{n_pass} of {reported.get(label, 0)} reported rows pass the "
                    f"declared rule (>= {int(min_genes)}); "
                    f"{tally['n_corroborates']} also corroborated at the sample level, "
                    f"{tally['n_discordant']} discordant")
            elif n_pass:
                klass, reason = "evidence_thin", (
                    f"only {n_pass} of {reported.get(label, 0)} reported rows pass the "
                    f"declared rule (fewer than {int(min_genes)})")
            else:
                klass, reason = "evidence_insufficient", (
                    f"no row of {reported.get(label, 0)} passes the declared rule; a "
                    "technical explanation (ambient RNA, low cell number, batch) is "
                    "equally consistent with the data")
            dossiers.append({"candidate_id": cand["candidate_id"], "label": label,
                             "n_cells": cand["n_cells"],
                             "is_unlabeled": cand["is_unlabeled"],
                             "classification": klass, "classification_reason": reason,
                             "dimensions": dims,
                             "n_genes_reported": reported.get(label, 0),
                             "n_genes_passing_declared_rule": n_pass,
                             "classification_rule_source": (
                                 "declared in plan" if isinstance(min_genes, (int, float))
                                 and n_pass is not None else NO_RULE_SOURCE),
                             "n_genes_sample_corroborated": tally["n_corroborates"],
                             "n_genes_sample_discordant": tally["n_discordant"],
                             "sample_level_note": (
                                 "supplementary only; a cell-level result is never "
                                 "filtered, suppressed or upgraded by it")})

        order = {"evidence_sufficient": 0, "evidence_thin": 1, "evidence_insufficient": 2,
                 "evidence_not_adjudicated": 3}
        dossiers.sort(key=lambda d: (order.get(d["classification"], 3),
                                     -d["n_genes_reported"], -d["n_cells"]))
        return {"status": "ok", "protocol": protocol_id,
                "note": ("grouping uses only the rule this plan declares; with no rule "
                         "nothing is called sufficient or insufficient, and no weighted "
                         "score is computed, so candidates inside a group remain tied"),
                "cell_level_rule": cell_rule,
                "evidence_rule": evidence_rule,
                "thresholds_source": rule_source,
                "dossiers": dossiers,
                "groups": {k: [d["label"] for d in dossiers
                               if d["classification"] == k] for k in order}}
    finally:
        conn.close()


def _de_method_ids() -> set[str]:
    """Methods whose adapter writes the cell-level differential-expression table."""
    from .catalog import EXECUTABLE
    return {mid for mid, m in EXECUTABLE.items()
            if "de_cell_level.csv" in (m.get("provides") or [])}


def _protocol_tasks(conn: sqlite3.Connection, protocol_id: str) -> list[dict]:
    """Per-candidate tasks of this protocol with the artifacts registered for them.

    Ordered by attempt, so the last entry per task is the newest attempt. A task
    counts as a cell-level differential-expression task when its method is known
    to produce that table or when it actually registered one: a frozen plan may
    name a method this build no longer ships.
    """
    de_ids = _de_method_ids()
    tasks: dict[str, dict] = {}
    for r in conn.execute(
            "SELECT t.task_id, t.candidate_id, t.method_id, t.status, t.reason, "
            "t.attempt, a.artifact_id, a.kind, a.path, a.n_rows "
            "FROM task t LEFT JOIN artifact a ON a.task_id=t.task_id "
            "WHERE t.protocol_id=? AND t.candidate_id IS NOT NULL "
            "ORDER BY t.attempt, t.task_id", (protocol_id,)):
        t = tasks.setdefault(r["task_id"], {
            "task_id": r["task_id"], "candidate_id": r["candidate_id"],
            "method_id": r["method_id"], "status": r["status"],
            "reason": r["reason"], "attempt": r["attempt"], "artifacts": []})
        if r["artifact_id"]:
            t["artifacts"].append({"artifact_id": r["artifact_id"], "kind": r["kind"],
                                   "path": r["path"], "n_rows": r["n_rows"]})
    out = []
    for t in tasks.values():
        has_table = any(a["kind"] == "de_cell_level" for a in t["artifacts"])
        t["is_de"] = bool(has_table or t["method_id"] in de_ids)
        out.append(t)
    return out


def _candidate_state(de_tasks: list[dict], n_rows: int) -> dict:
    """Whether this candidate's cell-level table is reported, empty, failed or absent.

    A missing table is never read as a zero effect. `empty_result` is a computed
    table with no gene rows; `task_failed` is a task that ran and errored;
    `no_data` covers everything else (not queued, not run, inapplicable, or a
    table registered but missing on disk). Only `reported` has a known direction.
    """
    if n_rows > 0:
        return {"status": "reported", "direction_known": True, "reason": None}
    if not de_tasks:
        return {"status": "no_data", "direction_known": False,
                "reason": ("no differential-expression task was queued for this "
                           "candidate: the comparison was not computed, so its "
                           "direction is unknown, not absent")}
    latest = de_tasks[-1]
    if latest["status"] == "failed":
        why = latest.get("reason") or "no reason recorded"
        return {"status": "task_failed", "direction_known": False,
                "reason": (f"the cell-level task failed ({why}); no result exists, so "
                           f"the direction is unknown, not absent")}
    present = [a for a in latest["artifacts"]
               if a["kind"] == "de_cell_level" and Path(a["path"]).exists()]
    if present:
        return {"status": "empty_result", "direction_known": False,
                "reason": ("the task succeeded and wrote a cell-level table with no "
                           "gene rows: an empty result, not an absent comparison")}
    if latest["status"] == "succeeded":
        return {"status": "no_data", "direction_known": False,
                "reason": ("the task is recorded as succeeded but no cell-level table "
                           "is registered for it: the table is missing, so the "
                           "direction is unknown, not absent")}
    why = latest.get("reason")
    return {"status": "no_data", "direction_known": False,
            "reason": (f"the cell-level task is {latest['status']}"
                       + (f" ({why})" if why else "")
                       + "; the comparison was not computed, so its direction is "
                         "unknown, not absent")}


def _has_table(tasks: list[dict]) -> bool:
    """Whether any of these tasks has a cell-level table that exists on disk."""
    return any(a["kind"] == "de_cell_level" and Path(a["path"]).exists()
               for t in tasks for a in t["artifacts"])


def _de_tasks_by_candidate(conn: sqlite3.Connection,
                           protocol_id: str) -> dict[str, list[dict]]:
    """Per-candidate cell-level tasks, keyed by candidate_id, newest attempt last."""
    out: dict[str, list[dict]] = defaultdict(list)
    for t in _protocol_tasks(conn, protocol_id):
        if t["is_de"]:
            out[t["candidate_id"]].append(t)
    return out


def _has_sample_table(tasks: list[dict]) -> bool:
    """Whether these tasks registered a sample-level table that exists on disk."""
    return any(a["kind"] == "de_sample_level" and Path(a["path"]).exists()
               for t in tasks for a in t["artifacts"])


def propose_targets(project_dir: str | Path, protocol_id: str,
                    top_per_candidate: int = 5,
                    query_external: bool = True) -> dict:
    """Bind observed genes to (cell group, comparison), then look them up.

    Distinguishes marker / regulatory / intervention evidence. The observed
    direction comes from the cell-level comparison (primary); the sample-level
    reading travels with it as supplementary and never changes the ranking.
    No gene is filtered by a threshold the plan did not declare: the declared
    rule, when present, is carried in `selection` and marked per target.
    Refuses to translate an expression direction into a recommended drug
    direction.

    Every candidate in the plan's universe is accounted for, whether or not it
    has a table: `candidates[]` records `reported` / `empty_result` /
    `task_failed` / `no_data` per candidate, so a candidate with no table is
    never silently read as a candidate with no signal. Every comparison the plan
    declares is resolved against the tasks and tables that exist:
    `declared_comparisons_computed` maps each declaration to `computed` /
    `empty_result` / `not_computed`, and a target's direction is attributed only
    to the contrast its own adapter computed, never to a declaration standing
    alone.
    """
    proj = Project(Path(project_dir)).ensure()
    conn = proj.connect()
    try:
        pid = get_or_create_project(conn, proj.root)
        proto = conn.execute("SELECT * FROM protocol WHERE protocol_id=?",
                             (protocol_id,)).fetchone()
        if not proto:
            return {"status": "failed", "error": f"protocol not found: {protocol_id}"}
        plan = json.loads(proto["plan_json"])
        cell_rule, sample_rule, _evidence_rule, rule_source = rule_from_plan(plan)
        de_cell, de_sample = _de_files(conn, protocol_id)
        observed = _attach_sample_level(cell_level_rows(de_cell, cell_rule),
                                        de_sample, sample_rule)
        # A row without a candidate label cannot be bound to a group, so it never
        # becomes a target group: it is counted as unbound and reported as such.
        by_cand: dict[str, list[dict]] = defaultdict(list)
        for g in observed:
            if g["candidate"]:
                by_cand[g["candidate"]].append(g)

        selection = {
            "declared_rule": cell_rule,
            "thresholds_source": rule_source,
            "significance_gated": cell_rule is not None,
            "ranked_by": "abs(avg_log2FC)",
            "cap_per_candidate": top_per_candidate,
            "note": ("every reported row is eligible; the cap is how many are listed, "
                     "not a significance filter"),
            "candidate_scope": ("every candidate in the plan's universe is accounted "
                                "for in `candidates`, including candidates whose "
                                "cell-level table is empty, failed or absent"),
        }
        # A comparison is a claim about what was computed, so each one is resolved
        # here against the tasks and the tables that exist. A declaration with no
        # resolving task, or with no table behind it, is carried as not computed and
        # is never attributed to a target.
        universe = plan.get("candidate_universe", [])
        cand_id_by_label = {c["label"]: c["candidate_id"] for c in universe}
        label_by_cand_id = {c["candidate_id"]: c["label"] for c in universe}
        tasks = _protocol_tasks(conn, protocol_id)
        method_by_task = {t["task_id"]: t["method_id"] for t in tasks}
        de_by_cand: dict[str, list[dict]] = defaultdict(list)
        for t in tasks:
            if t["is_de"]:
                de_by_cand[t["candidate_id"]].append(t)
        tables_present = {cid for cid, ts in de_by_cand.items() if _has_table(ts)}
        planned_contrast = {t.get("candidate_id"): t.get("comparison")
                            for t in (plan.get("tasks") or [])
                            if t.get("candidate_id")}
        raw_reported: dict[str, int] = defaultdict(int)
        for r in de_cell:
            if r.get("candidate"):
                raw_reported[r["candidate"]] += 1
        # A row without a candidate label cannot be bound to a group, so it never
        # becomes a target group: it is counted and reported as unbound.
        unbound_rows = sum(1 for g in observed if not g["candidate"])

        def _declared_state(cid, label, computed_by) -> str:
            """computed / empty_result / not_computed, from tasks and tables alone."""
            if not cid or not computed_by or cid not in tables_present:
                return "not_computed"
            return ("computed" if raw_reported.get(label, 0) > 0
                    else "empty_result")

        declared_entries: list[dict] = []
        seen_declared: set = set()
        for r in (plan.get("comparisons_resolved") or []):
            if not isinstance(r, dict):
                continue
            label = r.get("candidate_label")
            cid = cand_id_by_label.get(label)
            computed_by = r.get("computed_by") or []
            declared_entries.append({
                "declared": r.get("declared"),
                "contrast": (r.get("comparison")
                             or planned_contrast.get(cid)
                             or (f"{label} vs rest" if label else None)),
                "candidate_label": label,
                "status": _declared_state(cid, label, computed_by),
                "rows_reported": raw_reported.get(label, 0) if label else 0,
                "computed_by_tasks": computed_by})
            seen_declared.add(r.get("declared"))
        for text in (plan.get("comparisons") or []):
            if text in seen_declared:
                continue
            # a plan written before comparisons were resolved keeps bare text, and
            # no task in it resolves that text to a computed contrast
            declared_entries.append({
                "declared": text, "contrast": None, "candidate_label": None,
                "status": "not_computed", "rows_reported": 0,
                "computed_by_tasks": [],
                "reason": ("recorded in the plan but no task resolves it to a "
                           "computed contrast")})
        declared_texts = [d["declared"] for d in declared_entries]
        declared_computed = {d["declared"]: d["status"] for d in declared_entries}
        computed_tables = []
        for cid in sorted(tables_present, key=lambda c: str(label_by_cand_id.get(c))):
            label = label_by_cand_id.get(cid)
            de_tasks = de_by_cand.get(cid) or []
            computed_tables.append({
                "candidate_label": label,
                "contrast": (planned_contrast.get(cid)
                             or (f"{label} vs rest" if label else None)),
                "method_id": de_tasks[-1]["method_id"] if de_tasks else None,
                "rows_reported": raw_reported.get(label, 0),
                "rest": ("every cell not labelled this group; unlabelled cells fall "
                         "into the rest set")})
        unbacked = [f"{d['declared']} ({d['status']})" for d in declared_entries
                    if d["status"] != "computed"]
        if declared_entries:
            scope_note = ("a declared comparison is reported as computed only when a "
                          "task resolves it and a cell-level table with rows backs it")
            if unbacked:
                scope_note += ("; declared without a result here (never attributed to "
                               "a target): " + "; ".join(str(u) for u in unbacked))
        else:
            scope_note = ("this plan declares no comparison; the contrast computed is "
                          "one annotated group against the remaining cells")
        comparison_scope = {
            "declared_in_plan": declared_texts,
            "declared_comparisons_computed": declared_computed,
            "declarations": declared_entries,
            "computed_tables": computed_tables,
            "unbound_candidate_rows": unbound_rows,
            "note": scope_note,
        }

        # Every candidate is accounted for before any target is built: a candidate
        # with no table is not a candidate with no signal.
        coverage: dict[str, dict] = {}
        for cand in universe:
            label = cand["label"]
            de_tasks = de_by_cand.get(cand["candidate_id"], [])
            n_rows = raw_reported.get(label, 0)
            # the task that actually wrote this candidate's rows when there are rows
            row_task = next((g["_task_id"] for g in by_cand.get(label, [])
                             if g.get("_task_id")), None)
            chosen = next((t for t in de_tasks if t["task_id"] == row_task),
                          de_tasks[-1] if de_tasks else None)
            cov = {"candidate_id": cand["candidate_id"], "label": label,
                   "n_cells": cand["n_cells"],
                   "is_unlabeled": bool(cand.get("is_unlabeled")),
                   "in_candidate_universe": True,
                   "de_task_id": (chosen or {}).get("task_id"),
                   "de_method_id": (chosen or {}).get("method_id"),
                   "task_status": (chosen or {}).get("status"),
                   "task_reason": (chosen or {}).get("reason"),
                   "n_rows_reported": n_rows,
                   "declared_comparison": [
                       d["declared"] for d in declared_entries
                       if d["candidate_label"] == label],
                   "n_targets": 0}
            cov.update(_candidate_state(de_tasks, n_rows))
            coverage[label] = cov
        # A label that has rows but is not in the universe (a revised annotation
        # label, say) keeps its targets and is flagged.
        extra_labels = [l for l in by_cand if l not in coverage]
        for label in extra_labels:
            n_rows = raw_reported.get(label, 0)
            cov = {"candidate_id": None, "label": label,
                   "n_cells": None, "is_unlabeled": None,
                   "in_candidate_universe": False,
                   "de_task_id": None, "de_method_id": None, "task_status": None,
                   "task_reason": None,
                   "n_rows_reported": n_rows, "n_targets": 0,
                   "note": ("label is not in the plan's candidate universe; its rows "
                            "are reported because they exist, and no universe entry is "
                            "invented for it")}
            cov.update(_candidate_state([], n_rows))
            coverage[label] = cov
        targets, external_calls = [], []
        by_target_cand: dict[str, int] = defaultdict(int)
        ordered = ([c["label"] for c in universe if c["label"] in by_cand]
                   + extra_labels)
        for label in ordered:
            genes = by_cand.get(label) or []
            genes.sort(key=lambda g: -abs(g["avg_log2FC"] or 0))
            for g in genes[:max(0, top_per_candidate)]:
                sl = g["sample_level"]
                method_id = method_by_task.get(g.get("_task_id"))
                in_universe = coverage[label]["in_candidate_universe"]
                plan_contrast = planned_contrast.get(cand_id_by_label.get(label))
                if not in_universe:
                    # the row's label has no entry in the plan, so no contrast can
                    # be attributed to it: the label is reported, the contrast is not
                    computed = {"kind": "not asserted", "contrast": None,
                                "why": ("this row's label is not in the plan's "
                                        "candidate universe, so the contrast behind "
                                        "the row is not attributed to it")}
                elif method_id == "de_vs_rest_wilcox":
                    computed = {"kind": "candidate_vs_rest",
                                "contrast": plan_contrast or f"{label} vs rest",
                                "rest": ("every cell not labelled this group; "
                                         "unlabelled cells fall into the rest set")}
                else:
                    computed = {"kind": "not asserted",
                                "contrast": None,
                                "why": (f"the adapter that wrote this row (method "
                                        f"{method_id}) is not this build's group-vs-rest "
                                        f"method, so the shape of its contrast is not "
                                        f"claimed here")}
                computed.update({"cell_group": label, "method_id": method_id,
                                 "artifact_id": g["artifact_id"]})
                honored_here = [d["declared"] for d in declared_entries
                                if d["candidate_label"] == label
                                and d["status"] == "computed"]
                t = {"gene": g["gene"], "cell_group": label,
                     "in_candidate_universe": coverage[label]["in_candidate_universe"],
                     "comparison_context": {
                         "computed": computed,
                         "declared_in_plan": declared_texts,
                         "declared_comparisons_computed": declared_computed,
                         "declared_for_this_group": honored_here,
                         "note": ("the direction below is the contrast recorded in "
                                  "`computed`; a declaration counts as computed only "
                                  "when a table with rows backs it"
                                  + ("" if honored_here else
                                     "; no declared comparison is computed for this "
                                     "group, so none is attributed to this target"))},
                     "observed_direction": g["direction"],
                     "selection": selection,
                     "passes_declared_rule": g["passes_declared_rule"],
                     "cell_level": {
                         "avg_log2FC": (round(g["avg_log2FC"], 3)
                                        if g["avg_log2FC"] is not None else None),
                         "p_val": g["p_val"], "p_val_adj": g["p_val_adj"],
                         "pct_candidate": g["pct_candidate"],
                         "pct_rest": g["pct_rest"],
                         "n_cells_candidate": g["n_cells_candidate"],
                         "test": g["test"],
                         "independence_warning": g["independence_warning"],
                         "thresholds_source": rule_source},
                     "sample_level": {
                         "status": sl["status"], "reason": sl.get("reason"),
                         "n_samples": sl["n_samples"],
                         "mean_log2FC_sample_level": sl["mean_log2FC_sample_level"],
                         "ci": sl["ci"], "basis": sl["basis"],
                         "is_supplementary": True, "gates_results": False,
                         "thresholds": sample_rule},
                     "evidence": {"source_type": "current_data",
                                  "artifact_id": g["artifact_id"]},
                     "target_class": "marker_candidate",
                     "intervention": None,
                     "chain_complete": False,
                     "stop_reason": "no external target or compound evidence queried yet"}
                if query_external:
                    res = literature.resolve_target_symbol(proj, conn, g["gene"])
                    external_calls.append({"gene": g["gene"], "step": "resolve",
                                           "status": res.get("status")})
                    if res.get("status") == "ok" and res.get("ensembl_id"):
                        t["ensembl_id"] = res["ensembl_id"]
                        assoc = literature.target_associations(proj, conn,
                                                              res["ensembl_id"])
                        mech = literature.chembl_compounds_for_target(proj, conn, g["gene"])
                        external_calls += [
                            {"gene": g["gene"], "step": "associations",
                             "status": assoc.get("status")},
                            {"gene": g["gene"], "step": "chembl",
                             "status": mech.get("status")}]
                        t["disease_associations"] = (assoc.get("associations") or [])[:5]
                        t["n_disease_associations"] = assoc.get("n_associations")
                        if mech.get("status") == "ok" and mech.get("mechanisms"):
                            t["target_class"] = "target_with_intervention_evidence"
                            t["intervention"] = mech["mechanisms"][:5]
                            directed = [m for m in mech["mechanisms"]
                                        if (m.get("action_type") or "").upper() in
                                        ("INHIBITOR", "ANTAGONIST", "AGONIST", "ACTIVATOR")]
                            if directed:
                                t["chain_complete"] = True
                                t["stop_reason"] = None
                                t["pharmacology_direction"] = directed[0]["action_type"]
                                t["direction_caveat"] = (
                                    "this is the direction of the recorded compound "
                                    "mechanism only; it is NOT implied by the observed "
                                    "expression direction and must not be read as a "
                                    "therapeutic recommendation")
                            else:
                                t["stop_reason"] = ("compounds exist but no directional "
                                                    "mechanism recorded")
                        elif t.get("n_disease_associations"):
                            t["target_class"] = "target_with_disease_association"
                            t["stop_reason"] = ("disease association exists; no compound "
                                                "mechanism found, so the chain stops at "
                                                "the target")
                        else:
                            t["target_class"] = "marker_candidate"
                            t["stop_reason"] = ("no disease association and no compound "
                                                "mechanism; expression marker only")
                    else:
                        t["stop_reason"] = f"target symbol not resolved ({res.get('status')})"

                    # literature leads: a keyword hit is a paper to read, not
                    # evidence of a direction, so it is recorded with an explicit
                    # curation flag and never counted as support
                    search = literature.search_pubmed(
                        proj, conn,
                        f'("{g["gene"]}"[Title/Abstract]) AND (single cell OR scRNA-seq)',
                        retmax=5)
                    external_calls.append({"gene": g["gene"], "step": "pubmed",
                                           "status": search.get("status")})
                    if search.get("status") == "ok":
                        t["literature_leads"] = search.get("count")
                        t["literature_lead_pmids"] = (search.get("pmids") or [])[:5]
                targets.append(t)
                by_target_cand[label] += 1
                if cell_rule is None:
                    rule_text = ("no selection rule declared; reported, not adjudicated")
                else:
                    rule_text = ("passes the declared rule"
                                 if g["passes_declared_rule"] else
                                 "does not pass the declared rule")
                p_txt = (f"FDR={g['p_val_adj']:.3g}"
                         if g["p_val_adj"] is not None else "FDR not reported")
                lfc_txt = (f"log2FC={g['avg_log2FC']:.2f}"
                           if g["avg_log2FC"] is not None else "log2FC not reported")
                tail = (f"(cell-level Wilcoxon, {lfc_txt}, {p_txt}, "
                        f"n={g['n_cells_candidate']} cells; {rule_text}; "
                        f"sample-level: {sl['status']})")
                if g["direction"] == "undetermined":
                    # a missing or zero log2FC is not a direction: saying "is
                    # undetermined than" would assert a comparison that has none
                    statement = (f"{g['gene']} has no established direction in {label} "
                                 f"versus the remaining cells {tail}")
                else:
                    statement = (f"{g['gene']} is {g['direction']} in {label} than in "
                                 f"the remaining cells {tail}")
                cid = ensure_claim(conn, pid, "target", g["gene"], statement,
                                   protocol_id)
                add_evidence(conn, pid, cid, "current_data",
                             origin=f"{label}:{g['gene']}",
                             artifact_id=g["artifact_id"],
                             payload={"direction": g["direction"],
                                      "passes_declared_rule":
                                          g["passes_declared_rule"],
                                      "declared_rule": g["declared_rule"],
                                      "cell_level": t["cell_level"],
                                      "sample_level": t["sample_level"]})
                for m in (t.get("intervention") or []):
                    add_evidence(conn, pid, cid, "database_record",
                                 origin=f"chembl:{m.get('molecule_chembl_id')}",
                                 payload=m)
                for lead in (t.get("literature_lead_pmids") or []):
                    add_evidence(conn, pid, cid, "paper_observation",
                                 origin=f"pubmed:keyword:{lead}", pmid=lead,
                                 payload={"curation": "keyword_match_uncurated",
                                          "query_gene": t["gene"],
                                          "note": ("abstract-level keyword hit for this "
                                                   "symbol; not read for direction, so it "
                                                   "is not counted as support")})
        for label, cov in coverage.items():
            cov["n_targets"] = by_target_cand.get(label, 0)
        status_order = ("reported", "empty_result", "task_failed", "no_data")
        by_status = {k: [l for l, c in coverage.items() if c["status"] == k]
                     for k in status_order}
        warning = ("expression direction does not establish pharmacology direction; "
                   "chains stop at the target when direction is unknown")
        uncomputed = [f"{d['declared']} ({d['status']})" for d in declared_entries
                      if d["status"] != "computed"]
        if uncomputed:
            warning += ("; declared without a computed result here (never attributed "
                        "to a target): " + "; ".join(uncomputed))
        # persist the snapshot so a report can show the chain without re-querying
        # (and without the risk of showing a fresher answer than was adjudicated)
        snap = proj.dirs["meta"] / f"targets_{protocol_id}.json"
        snap.write_text(json.dumps({"protocol_id": protocol_id, "built_utc": now(),
                                    "statistical_unit": ("cell (primary); sample "
                                                         "(supplementary only)"),
                                    "selection": selection,
                                    "thresholds": {"cell_level_rule": cell_rule,
                                                   "sample_level_rule": sample_rule,
                                                   "source": rule_source},
                                    "comparison_scope": comparison_scope,
                                    "targets": targets,
                                    "candidates": list(coverage.values()),
                                    "candidates_by_status": {
                                        k: len(v) for k, v in by_status.items()},
                                    "unbound_candidate_rows": unbound_rows,
                                    "external_calls": external_calls,
                                    "warning": warning},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
        return {"status": "ok", "protocol": protocol_id, "snapshot": str(snap),
                "n_targets": len(targets),
                "n_candidates": len(coverage),
                "n_candidates_with_targets": sum(1 for c in coverage.values()
                                                 if c["n_targets"]),
                "n_candidates_without_targets": sum(1 for c in coverage.values()
                                                    if not c["n_targets"]),
                "statistical_unit": "cell (primary); sample (supplementary only)",
                "selection": selection,
                "thresholds": {"cell_level_rule": cell_rule,
                               "sample_level_rule": sample_rule,
                               "source": rule_source},
                "by_class": {k: sum(1 for t in targets if t["target_class"] == k)
                             for k in sorted({t["target_class"] for t in targets})},
                "candidates_by_status": {k: len(v) for k, v in by_status.items()},
                "candidates": list(coverage.values()),
                "unbound_candidate_rows": unbound_rows,
                "comparison_scope": comparison_scope,
                "targets": targets,
                "external_calls": external_calls,
                "warning": warning}
    finally:
        conn.close()
