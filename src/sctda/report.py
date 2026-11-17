"""Build HTML / CSV / PDF deliverables from artifacts.

Everything shown is read back from the database and the artifact files: the
report cannot contain a number that was not computed. Sections that would be
empty (no evidence yet, no reasoning yet) say so explicitly instead of being
omitted, because "we did not compute this" and "we computed nothing" are
different statements.
"""
from __future__ import annotations

import csv
import html
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

from .catalog import (CatalogError, EXECUTABLE, SCOPE_NOTE, discovered_methods,
                      supported_by_detail)
from .project import Project, canonical_json, get_or_create_project, new_id, now, sha256_text

CSV_EXPORTS = ("candidates", "targets_candidates", "task_outcomes", "de_cell_level",
               "de_sample_level", "cells_per_sample", "signature_summary",
               "cnv_summary", "evidence")


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args)]


def _read_csv(path: Path, limit: int | None = None) -> tuple[list[str], list[dict]]:
    if not path.exists():
        return [], []
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        rd = csv.DictReader(fh)
        rows = []
        for i, r in enumerate(rd):
            if limit is not None and i >= limit:
                break
            rows.append(r)
        return rd.fieldnames or [], rows


# A 100-row table is unreadable in a printed PDF and makes the HTML->PDF
# converter take minutes. Tables are capped here and the cap is stated; the CSV
# export always carries every row.
MAX_TABLE_ROWS = 40


def _table(headers: list[str], rows: list[dict], cls: str = "") -> str:
    if not rows:
        return '<p class="empty">no rows</p>'
    shown = rows[:MAX_TABLE_ROWS]
    out = [f'<table class="{cls}"><thead><tr>']
    out += [f"<th>{html.escape(str(h))}</th>" for h in headers]
    out.append("</tr></thead><tbody>")
    for r in shown:
        out.append("<tr>" + "".join(
            f"<td>{html.escape(str(r.get(h, ''))[:180])}</td>" for h in headers) + "</tr>")
    out.append("</tbody></table>")
    if len(rows) > len(shown):
        out.append(f'<p class="note">showing {len(shown)} of {len(rows)} rows; '
                   f'the complete table is in the csv/ export.</p>')
    return "".join(out)


def _write_csv(path: Path, headers: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in headers})


def _aggregate_artifact(conn: sqlite3.Connection, protocol_id: str, kind: str,
                        limit: int | None = None) -> list[dict]:
    out = []
    for a in conn.execute("SELECT * FROM artifact WHERE protocol_id=? AND kind=? "
                          "ORDER BY created_utc", (protocol_id, kind)):
        _, rows = _read_csv(Path(a["path"]), limit=limit)
        for r in rows:
            # traceability columns: every exported row says which file and task
            # produced it, so a number in the CSV can be walked back to a run
            r["source_artifact"] = Path(a["path"]).name
            r["source_task"] = a["task_id"]
            out.append(r)
    return out


def _pdf_from_html(html_path: Path, pdf_path: Path) -> dict:
    """Render PDF from the HTML with whatever converter this host actually has."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return {"status": "unavailable",
                "reason": "no HTML->PDF converter on PATH (soffice/libreoffice/chromium)"}
    # A shared LibreOffice user profile deadlocks headless conversions that run
    # close together (the second instance waits on the first one's lock forever).
    # A throwaway profile per conversion is what makes this reliable.
    import tempfile
    # a lock left behind by a killed conversion makes the next one refuse to
    # write, which looks identical to a converter failure
    for stale in pdf_path.parent.glob(".~lock.*#"):
        stale.unlink(missing_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="sctda_lo_"))
    cmd = [soffice, f"-env:UserInstallation=file://{profile}", "--headless",
           "--norestore", "--nolockcheck", "--convert-to", "pdf",
           "--outdir", str(pdf_path.parent), str(html_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {"status": "failed",
                "reason": "converter timed out after 300 s; HTML is still available"}
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    if pdf_path.exists():
        return {"status": "rendered", "path": str(pdf_path),
                "bytes": pdf_path.stat().st_size}
    return {"status": "failed", "returncode": proc.returncode,
            "stderr": (proc.stderr or proc.stdout)[-2000:]}


def build_report(project_dir: str | Path, protocol: str | None = None,
                 formats: str = "html,csv") -> dict:
    proj = Project(Path(project_dir)).ensure()
    conn = proj.connect()
    try:
        get_or_create_project(conn, proj.root)
        if protocol is None:
            row = conn.execute("SELECT protocol_id FROM protocol WHERE status IN "
                               "('completed','running','paused','frozen') "
                               "ORDER BY version DESC LIMIT 1"
                               ).fetchone() or conn.execute(
                               "SELECT protocol_id FROM protocol ORDER BY version DESC LIMIT 1"
                               ).fetchone()
            if not row:
                return {"status": "failed", "error": "no protocol to report on"}
            protocol = row["protocol_id"]
        prow = conn.execute("SELECT * FROM protocol WHERE protocol_id=?",
                            (protocol,)).fetchone()
        if not prow:
            return {"status": "failed", "error": f"protocol not found: {protocol}"}
        plan = json.loads(prow["plan_json"])

        stamp = now().replace(":", "").replace("-", "").replace("+0000", "Z")
        outdir = proj.dirs["reports"] / f"{protocol}_{stamp}"
        outdir.mkdir(parents=True, exist_ok=True)
        csvdir = outdir / "csv"
        wanted = {f.strip() for f in formats.split(",") if f.strip()}

        # ---------------- data ------------------------------------------------
        tasks = _rows(conn, "SELECT t.*, c.label AS candidate_label FROM task t "
                            "LEFT JOIN candidate c ON c.candidate_id = t.candidate_id "
                            "WHERE t.protocol_id=? ORDER BY t.method_id, "
                            "c.n_cells DESC", (protocol,))
        arts = _rows(conn, "SELECT * FROM artifact WHERE protocol_id=?", (protocol,))
        candidates = plan.get("candidate_universe", [])
        claims = _rows(conn, "SELECT * FROM claim WHERE protocol_id=? ORDER BY created_utc",
                       (protocol,))
        decisions = _rows(conn, "SELECT decision_id, claim_id, action, rationale, "
                                "created_utc FROM decision WHERE protocol_id=? "
                                "ORDER BY created_utc", (protocol,))
        evidence = _rows(conn, "SELECT * FROM evidence WHERE claim_id IN "
                               "(SELECT claim_id FROM claim WHERE protocol_id=?) "
                               "ORDER BY source_type", (protocol,))
        de_cell = _aggregate_artifact(conn, protocol, "de_cell_level")
        de_sample = _aggregate_artifact(conn, protocol, "de_sample_level")
        sig = _aggregate_artifact(conn, protocol, "signature_summary")
        comp = _aggregate_artifact(conn, protocol, "composition_by_sample")
        per_sample = _aggregate_artifact(conn, protocol, "de_per_sample")
        cnv_summary = _aggregate_artifact(conn, protocol, "cnv_summary_by_group")
        # ---- dossiers and target chains ----------------------------------
        from .reasoning import build_dossiers
        try:
            doss = build_dossiers(proj.root, protocol)
        except Exception as exc:                              # noqa: BLE001
            doss = {"status": "failed", "error": str(exc), "dossiers": []}
        dossier_rows = []
        for d in doss.get("dossiers", []):
            ex = (d["dimensions"] or {}).get("expression_cell_level") or {}
            sl = (d["dimensions"] or {}).get("expression_sample_level") or {}

            def _gene_txt(g: dict) -> str:
                lfc = "n/a" if g.get("avg_log2FC") is None else g["avg_log2FC"]
                p = "n/a" if g.get("p_val_adj") is None else f"{g['p_val_adj']:.2g}"
                if g.get("declared_rule") is None:
                    verdict = "no rule"
                elif g.get("passes_declared_rule") is None:
                    verdict = "not evaluable"
                else:
                    verdict = "pass" if g["passes_declared_rule"] else "fail"
                return f"{g['gene']}({g['direction'][0]},{lfc},FDR{p},{verdict})"

            dossier_rows.append({
                "label": d["label"], "n_cells": d["n_cells"],
                "classification": d["classification"],
                "n_genes_reported": d.get("n_genes_reported"),
                "n_passing_rule": ("none declared"
                                   if d.get("n_genes_passing_declared_rule") is None
                                   else d["n_genes_passing_declared_rule"]),
                "n_sample_corroborated": d.get("n_genes_sample_corroborated"),
                "n_sample_discordant": d.get("n_genes_sample_discordant"),
                "top_genes": ", ".join(_gene_txt(g)
                                       for g in (ex.get("top_genes") or [])[:5]),
                "sample_level": (f"{sl.get('n_corroborates')} corroborate / "
                                 f"{sl.get('n_discordant')} discordant / "
                                 f"{sl.get('n_not_corroborated')} not corroborated / "
                                 f"{sl.get('n_not_computed')} not computed"),
                "reason": d["classification_reason"],
            })
        snap = proj.dirs["meta"] / f"targets_{protocol}.json"
        tgt_rows, tgt_note, tgt_candidate_rows = [], "", []
        if snap.exists():
            blob = json.loads(snap.read_text(encoding="utf-8"))
            for t in blob.get("targets", []):
                cl = t.get("cell_level") or {}
                sl = t.get("sample_level") or {}
                ci = sl.get("ci")
                sel_rule = (t.get("selection") or {}).get("declared_rule")
                if sel_rule is None:
                    verdict = "no rule"
                elif t.get("passes_declared_rule") is None:
                    verdict = "not evaluable"
                else:
                    verdict = "pass" if t["passes_declared_rule"] else "fail"
                tgt_rows.append({
                    "cell_group": t["cell_group"], "gene": t["gene"],
                    "observed_direction": t["observed_direction"],
                    "cell_LFC": cl.get("avg_log2FC"),
                    "cell_FDR": cl.get("p_val_adj"),
                    "passes_rule": verdict,
                    "sample_level": sl.get("status"),
                    "sample_ci": (f"[{ci[0]}, {ci[1]}]" if ci else "not computed"),
                    "n_samples": sl.get("n_samples"),
                    "target_class": t["target_class"],
                    "chain_complete": t["chain_complete"],
                    "compound_mechanisms": "; ".join(
                        f"{m.get('molecule_chembl_id')}({m.get('action_type')})"
                        for m in (t.get("intervention") or [])[:3]) or "-",
                    "stop_reason": t.get("stop_reason") or "",
                })
            for c in blob.get("candidates") or []:
                tgt_candidate_rows.append({
                    "candidate": c.get("label"),
                    "n_cells": (c.get("n_cells") if c.get("n_cells") is not None
                                else "-"),
                    "status": c.get("status") or "not recorded",
                    "direction_known": c.get("direction_known"),
                    "rows_reported": (c.get("n_rows_reported")
                                      if c.get("n_rows_reported") is not None else "-"),
                    "targets": (c.get("n_targets")
                                if c.get("n_targets") is not None else "-"),
                    "task_status": c.get("task_status") or "-",
                    "reason": c.get("reason") or c.get("note") or "",
                })
            sel = blob.get("selection") or {}
            tgt_note = (f"snapshot built {blob.get('built_utc')}; "
                        f"{len(blob.get('external_calls') or [])} external calls recorded; "
                        f"selection: {('declared rule ' + str(sel.get('declared_rule'))) if sel.get('significance_gated') else 'no threshold declared (top rows by |log2FC| only)'}. "
                        "Observed expression direction does not establish pharmacology "
                        "direction.")
        else:
            tgt_note = ("no target snapshot for this protocol yet; run "
                        "`sctda targets --protocol ...`")
        if not tgt_candidate_rows:
            # a snapshot without coverage cannot say which candidate was never
            # analysed, so say that instead of showing only candidates with hits
            why = (("this snapshot records no per-candidate coverage; rerun "
                    "`sctda targets` for a status per candidate")
                   if snap.exists() else
                   "no target snapshot for this protocol yet; run "
                   "`sctda targets --protocol ...`")
            tgt_candidate_rows = [{
                "candidate": c["label"], "n_cells": c["n_cells"],
                "status": "not recorded", "direction_known": "unknown",
                "rows_reported": "-", "targets": "-", "task_status": "-",
                "reason": why} for c in candidates]

        # ---------------- csv -------------------------------------------------
        csv_paths = {}
        if "csv" in wanted:
            csv_paths["candidates"] = csvdir / "candidates.csv"
            _write_csv(csv_paths["candidates"],
                       ["candidate_id", "label", "n_cells", "is_unlabeled"], candidates)
            csv_paths["targets_candidates"] = csvdir / "targets_candidates.csv"
            _write_csv(csv_paths["targets_candidates"],
                       ["candidate", "n_cells", "status", "direction_known",
                        "rows_reported", "targets", "task_status", "reason"],
                       tgt_candidate_rows)
            csv_paths["task_outcomes"] = csvdir / "task_outcomes.csv"
            _write_csv(csv_paths["task_outcomes"],
                       ["task_id", "method_id", "stage", "candidate_label", "status",
                        "reason", "started_utc", "finished_utc"], tasks)
            csv_paths["de_cell_level"] = csvdir / "de_cell_level.csv"
            hdr = list(de_cell[0].keys()) if de_cell else ["gene", "candidate"]
            _write_csv(csv_paths["de_cell_level"], hdr, de_cell)
            csv_paths["de_sample_level"] = csvdir / "de_sample_level.csv"
            hdr = list(de_sample[0].keys()) if de_sample else ["gene", "candidate"]
            _write_csv(csv_paths["de_sample_level"], hdr, de_sample)
            if cnv_summary:
                csv_paths["cnv_summary"] = csvdir / "cnv_summary.csv"
                hdr = list(cnv_summary[0].keys()) if cnv_summary else ["group", "mean_cnv_score"]
                _write_csv(csv_paths["cnv_summary"], hdr, cnv_summary)
            csv_paths["cells_per_sample"] = csvdir / "cells_per_sample.csv"
            hdr = list(comp[0].keys()) if comp else ["candidate", "sample"]
            _write_csv(csv_paths["cells_per_sample"], hdr, comp)
            csv_paths["signature_summary"] = csvdir / "signature_summary.csv"
            hdr = list(sig[0].keys()) if sig else ["candidate", "gene_set"]
            _write_csv(csv_paths["signature_summary"], hdr, sig)
            csv_paths["evidence"] = csvdir / "evidence.csv"
            _write_csv(csv_paths["evidence"],
                       ["evidence_id", "claim_id", "source_type", "locator", "pmid",
                        "payload_json"], evidence)

        # ---------------- html ------------------------------------------------
        status_counts: dict[str, int] = {}
        for t in tasks:
            status_counts[t["status"]] = status_counts.get(t["status"], 0) + 1
        outcome_rows = [{"status": k, "n_tasks": v} for k, v in sorted(status_counts.items())]

        def rel(p) -> str:
            try:
                return str(Path(p).relative_to(outdir))
            except ValueError:
                return str(p)

        art_rows = [{"table": Path(a["path"]).name, "task": a["task_id"],
                     "rows": a["n_rows"], "checksum": a["checksum"],
                     "link": rel(a["path"])} for a in arts]

        cand_rows = []
        for c in candidates:
            mine = [t for t in tasks if t.get("candidate_label") == c["label"]]
            cand_rows.append({
                "label": c["label"], "n_cells": c["n_cells"],
                "unlabeled": c["is_unlabeled"],
                "tasks": ", ".join(sorted({t["method_id"] for t in mine}))
                         or "composition only (dataset-wide)",
                "outcomes": ", ".join(sorted({t["status"] for t in mine})) or "-",
            })

        gs_prov = plan.get("parameters", {}).get("signature_score_aucell", {}).get(
            "geneset_file")
        prov_rows = [
            {"what": "source object", "value": plan["data"]["source_path"]},
            {"what": "source sha256", "value": plan["data"]["source_sha256"]},
            {"what": "cells x genes", "value": f"{plan['data']['n_cells']} x "
                                               f"{plan['data']['n_genes']}"},
            {"what": "assay", "value": plan["data"]["assay"]},
            {"what": "annotation column", "value": plan["data"]["columns"]["annotation"]},
            {"what": "sample column", "value": plan["data"]["columns"]["sample"]},
            {"what": "condition column", "value": plan["data"]["columns"]["condition"]},
            {"what": "gene set snapshot", "value": gs_prov or "not used"},
            {"what": "execution environment",
             "value": (lambda e: (f"{e.get('r_version')} / Seurat {e.get('seurat_version')}"
                                  f"; {e.get('isolation')}") if e else "not recorded")(
                 (json.loads((proj.dirs["meta"] / f"env_{protocol}.json").read_text())
                  if (proj.dirs["meta"] / f"env_{protocol}.json").exists() else None))},
            {"what": "flow cell / batch", "value": "not recorded by this object"},
        ]
        prov_rows = [r for r in prov_rows if r["value"] not in (None, "None")]

        def section(title: str, body: str, note: str = "") -> str:
            n = f'<p class="note">{html.escape(note)}</p>' if note else ""
            return f"<section><h2>{html.escape(title)}</h2>{n}{body}</section>"

        body = []
        body.append(section(
            "Protocol",
            _table(["what", "value"], [
                {"what": "protocol", "value": protocol},
                {"what": "version", "value": prow["version"]},
                {"what": "status", "value": prow["status"]},
                {"what": "plan sha256 (frozen)", "value": prow["plan_sha256"]},
                {"what": "frozen at", "value": prow["created_utc"]},
                {"what": "statistical unit",
                 "value": (plan.get("analysis_units") or {}).get("statistical_unit")},
                {"what": "declared cell-level rule",
                 "value": ((plan.get("analysis_units") or {}).get("cell_level_rule")
                           or "none declared (the kernel applies no threshold)")},
                {"what": "declared evidence rule",
                 "value": ((plan.get("analysis_units") or {}).get("evidence_rule")
                           or "none declared (no sufficient/insufficient call)")},
                {"what": "question", "value": plan.get("question")},
                {"what": "comparisons", "value": ", ".join(plan.get("comparisons") or []) or
                                                   "none specified"},
            ]),
            note=("A plan is frozen when it is written (hash above); a change produces "
                  "a new version instead of editing this one.")))
        body.append(section("Provenance", _table(["what", "value"], prov_rows)))
        body.append(section(
            "Candidate universe",
            _table(["label", "n_cells", "unlabeled", "tasks", "outcomes"], cand_rows),
            note=plan.get("candidate_universe_rule", "")))
        body.append(section("Task outcomes", _table(["status", "n_tasks"], outcome_rows),
                            note="`insufficient_input` and `not_applicable` are not "
                                 "failures and are not zero results."))
        body.append(section("Per-task detail",
                            _table(["method_id", "candidate_label", "status", "reason"],
                                   tasks)))
        if de_cell:
            body.append(section(
                "Differential expression (cell-level, primary)",
                _table(["candidate", "gene", "avg_log2FC", "p_val", "p_val_adj",
                        "pct.1", "pct.2", "independence_warning"], de_cell[:60]),
                note="Cell-level Wilcoxon is the primary literature-comparable result; "
                     "cells are treated as non-independent replicates (anti-conservative)."))
        if de_sample:
            body.append(section(
                "Differential expression (sample-level corroboration, supplementary)",
                _table(["candidate", "gene", "n_samples", "mean_log2FC_sample_level",
                        "ci_low", "ci_high", "frac_samples_positive", "basis",
                        "corroboration", "caveat"], de_sample[:60]),
                note="Sample-level pseudobulk provides supplementary cross-sample corroboration with 95% intervals "
                     "(<10 cells excluded); literature mainstream is cell-level."))
        if cnv_summary:
            body.append(section(
                "Copy number variation (CNV inference)",
                _table(["group", "n_cells", "mean_cnv_score", "median_cnv_score", "is_reference", "status"], cnv_summary[:60]),
                note="Large-scale copy number variation scores relative to stated reference cells."))
        if sig:
            body.append(section(
                "Gene set activity (AUCell, per-cell mean by group)",
                _table(["candidate", "gene_set", "n_cells", "mean_score", "median_score"],
                       sig[:60]),
                note="AUCell scores are relative within a cell and are not comparable "
                     "across cells as absolute activity."))
        # computed evidence must point at the file it came from, otherwise the
        # number in the report is not traceable to a row
        art_paths = {a["artifact_id"]: Path(a["path"]).name
                     for a in conn.execute("SELECT artifact_id, path FROM artifact")}
        ev_rows = []
        for e in evidence:
            try:
                pay = json.loads(e.get("payload_json") or "{}")
            except json.JSONDecodeError:
                pay = {}
            what = (pay.get("gene") or pay.get("gene_set")
                    or pay.get("molecule_chembl_id") or pay.get("disease")
                    or pay.get("query_gene") or e.get("origin") or "")
            where = (e.get("locator")
                     or (f"PMID {e['pmid']}" if e.get("pmid") else None)
                     or art_paths.get(e.get("artifact_id"))
                     or (e.get("origin") or "-"))
            cl = pay.get("cell_level") or {}
            sl = pay.get("sample_level") or {}
            if isinstance(cl.get("avg_log2FC"), (int, float)):
                value = f"log2FC {cl['avg_log2FC']:.2f}"
                if isinstance(cl.get("p_val_adj"), (int, float)):
                    value += f" (FDR {cl['p_val_adj']:.2g})"
                if sl.get("status"):
                    value += f"; sample: {sl['status']}"
            elif isinstance(pay.get("mean_score"), (int, float)):
                value = f"score {pay['mean_score']:.3f}"
            else:
                value = pay.get("action_type") or pay.get("direction") or ""
            ev_rows.append({
                "source_type": e["source_type"],
                "what": what,
                "where": where,
                "value": value,
                "curation": pay.get("curation") or "",
                "claim": e["claim_id"],
            })
        body.append(section("Evidence", _table(
            ["source_type", "what", "where", "value", "curation", "claim"], ev_rows)
            if evidence else
            '<p class="empty">no evidence recorded yet: this report contains computed '
            'data only. Paper observations, database records and perturbation evidence '
            'are added by the evidence stage.</p>'))
        body.append(section(
            "Decisions and superseded conclusions",
            _table(["created_utc", "action", "claim_id", "rationale"], decisions)
            if decisions else
            '<p class="empty">no decisions recorded: no earlier conclusion has been '
            'revised in this protocol.</p>',
            note=("A revised conclusion is marked superseded and its reason logged; "
                  "the earlier statement is never deleted.")))
        body.append(section("Claims", _table(
            ["claim_id", "subject_type", "subject_id", "statement", "status"], claims)
            if claims else '<p class="empty">no claims asserted yet.</p>'))
        body.append(section("Artifacts", _table(
            ["table", "task", "rows", "checksum", "link"], art_rows)))
        if dossier_rows:
            body.append(section(
                "Candidate dossiers",
                _table(["label", "n_cells", "classification", "n_genes_reported",
                        "n_passing_rule", "n_sample_corroborated",
                        "n_sample_discordant", "top_genes", "sample_level", "reason"],
                       dossier_rows),
                note=doss.get("note", "")))
        body.append(section(
            "Cell -> target -> direction -> intervention",
            "<h3>Every candidate in the universe</h3>"
            + _table(["candidate", "n_cells", "status", "direction_known",
                      "rows_reported", "targets", "task_status", "reason"],
                     tgt_candidate_rows)
            + "<h3>Gene-level targets</h3>"
            + (_table(["cell_group", "gene", "observed_direction", "cell_LFC",
                       "cell_FDR", "passes_rule", "sample_level", "sample_ci",
                       "n_samples", "target_class", "chain_complete",
                       "compound_mechanisms", "stop_reason"], tgt_rows)
               if tgt_rows else f'<p class="empty">{html.escape(tgt_note)}</p>'),
            note=((tgt_note + " ") if tgt_note else "")
                 + ("Every candidate in the plan's universe is listed with its own "
                    "status and reason; a candidate with no cell-level table is not a "
                    "candidate without signal.")))
        gaps = plan.get("gaps") or []
        body.append(section("Gaps and warnings", _table(["method_id", "missing"],
                                                        gaps) if gaps else
                            f'<p class="empty">no method gaps. Design warnings: '
                            f'{html.escape("; ".join(plan.get("design", {}).get("warnings") or []) or "none")}</p>'))

        try:
            disc = discovered_methods()
            disc_error = None
        except (CatalogError, OSError) as exc:
            disc = []
            disc_error = f"literature registry unavailable: {exc}"
        exe_rows = [{"method_id": m, "package": EXECUTABLE[m].get("package") or "-",
                     "stage": EXECUTABLE[m]["stage"], "status": "wired adapter"}
                    for m in sorted(EXECUTABLE)]
        body.append(section(
            "Methods: supported vs discovered",
            "<h3>Executable in this run</h3>" + _table(["method_id", "package", "stage",
                                                        "status"], exe_rows) +
            f"<h3>Discovered in the literature corpus ({len(disc)} methods; "
            f"first {min(40, len(disc))} shown)</h3>" +
            (f'<p class="empty">{html.escape(disc_error)}</p>' if disc_error else "") +
            _table(["canonical_name", "n_papers", "stages", "support"],
                   [{"canonical_name": d["canonical_name"], "n_papers": d["n_papers"],
                     "stages": ", ".join(d["stages"]),
                     "support": ", ".join(
                         f"{h['method_id']} ({h['quality']}: {h['wired_operation']})"
                         for h in supported_by_detail(d["canonical_name"])) or
                                "none - literature only"}
                    for d in disc[:40]]),
            note="A discovered method is not a supported method. Support requires a "
                 "wired adapter that has actually run. " + SCOPE_NOTE))

        html_doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>SCTDA report {html.escape(protocol)}</title>
<style>
 body {{ font-family: -apple-system, "Segoe UI", Roboto, sans-serif; margin: 2rem auto;
        max-width: 1100px; color: #1b1b1b; line-height: 1.45; }}
 h1 {{ font-size: 1.5rem; border-bottom: 2px solid #333; padding-bottom: .4rem; }}
 h2 {{ font-size: 1.15rem; margin-top: 2rem; border-bottom: 1px solid #ccc; }}
 h3 {{ font-size: 1rem; margin-top: 1.2rem; }}
 table {{ border-collapse: collapse; width: 100%; font-size: .82rem; margin: .6rem 0; }}
 th, td {{ border: 1px solid #d8d8d8; padding: .3rem .45rem; text-align: left;
           vertical-align: top; word-break: break-word; }}
 th {{ background: #f4f4f4; }}
 .note {{ color: #555; font-size: .82rem; font-style: italic; }}
 .empty {{ color: #777; font-size: .85rem; }}
 .meta {{ color: #555; font-size: .8rem; }}
</style></head><body>
<h1>SCTDA report</h1>
<p class="meta">protocol {html.escape(protocol)} · built {html.escape(now())} ·
frozen plan hash {html.escape(prow['plan_sha256'][:16])}…</p>
{''.join(body)}
</body></html>"""
        html_path = outdir / "index.html"
        html_path.write_text(html_doc, encoding="utf-8")

        paths = {"html": str(html_path)}
        paths.update({k: str(v) for k, v in csv_paths.items()})
        if "pdf" in wanted:
            res = _pdf_from_html(html_path, outdir / "index.pdf")
            paths["pdf"] = res.get("path") or f"unavailable: {res.get('reason')}"

        spec = canonical_json({"protocol": protocol, "plan_sha256": prow["plan_sha256"],
                               "tasks": status_counts, "formats": sorted(wanted)})
        conn.execute("""INSERT INTO report(report_id, project_id, protocol_id, built_utc,
                          spec_sha256, paths_json) VALUES (?,?,?,?,?,?)""",
                     (new_id("rep"), prow["project_id"], protocol, now(),
                      sha256_text(spec), json.dumps(paths)))
        conn.commit()
        return {"status": "ok", "protocol": protocol, "outdir": str(outdir),
                "paths": paths, "n_tasks": len(tasks), "outcomes": status_counts,
                "n_artifacts": len(arts)}
    finally:
        conn.close()
