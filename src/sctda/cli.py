"""SCTDA command line entry point."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .ingest import import_dataset, map_column
from .project import Project


def _emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


# ---------------------------------------------------------------- import / map
def cmd_import(args: argparse.Namespace) -> int:
    out = import_dataset(args.project, args.object, object_name=args.object_name,
                         annotation_column=args.annotation_column,
                         sample_column=args.sample_column,
                         condition_column=args.condition_column,
                         notes=args.notes,
                         include_unlabeled=getattr(args, "include_unlabeled", False))
    _emit(out)
    return 0 if out.get("status") in ("imported",) else (
        3 if out.get("status") == "needs_mapping" else 1)


def cmd_map(args: argparse.Namespace) -> int:
    proj = Project(Path(args.project)).ensure()
    conn = proj.connect()
    try:
        ds = conn.execute("SELECT dataset_id FROM dataset LIMIT 1").fetchone()
        if not ds:
            _emit({"status": "failed", "error": "no dataset imported yet"})
            return 1
        did = ds["dataset_id"]
        out = map_column(conn, did, args.role, args.column, notes=args.notes,
                         include_unlabeled=getattr(args, "include_unlabeled", False))
        _emit(out)
        return 0
    finally:
        conn.close()


def cmd_candidates(args: argparse.Namespace) -> int:
    """Delegates to the tool boundary so the CLI and the agent see one shape."""
    from .harness import invoke_tool
    out = invoke_tool("sctda_candidates", {"project": args.project})
    _emit(out)
    if out.get("status") == "needs_mapping":
        return 3
    return 0 if out.get("status") not in ("error", "failed") else 1


# ----------------------------------------------------------------------- plan
def cmd_catalog(args: argparse.Namespace) -> int:
    from .catalog import catalog_summary
    _emit(catalog_summary(executable_only=args.executable,
                          limit=args.limit, offset=args.offset,
                          query=args.query, stage=args.stage))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    from .protocol import build_plan
    methods = [m.strip() for m in (args.methods or "").split(",") if m.strip()] or None
    comps = None
    if args.comparisons:
        try:
            comps = json.loads(args.comparisons)
            if isinstance(comps, str):
                comps = [comps]
        except json.JSONDecodeError:
            comps = [c.strip() for c in args.comparisons.split(",") if c.strip()]
    out = build_plan(args.project, question=args.question,
                     methods=methods, comparisons=comps,
                     geneset_file=args.geneset_file)
    _emit(out)
    return 0 if out.get("status") != "failed" else 1


def cmd_run(args: argparse.Namespace) -> int:
    from .execution import run_protocol
    out = run_protocol(args.project, args.protocol,
                       resume=args.resume, stage=args.stage)
    _emit(out)
    return 0 if out.get("status") not in ("failed",) else 1


def cmd_report(args: argparse.Namespace) -> int:
    from .report import build_report
    out = build_report(args.project, protocol=args.protocol,
                       formats=args.formats)
    _emit(out)
    return 0 if out.get("status") != "failed" else 1


def cmd_derive(args: argparse.Namespace) -> int:
    from .reasoning import derive_evidence
    _emit(derive_evidence(args.project, args.protocol))
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    from .protocol import pause
    out = pause(args.project, args.protocol, reason=args.reason or "paused by operator")
    _emit(out)
    return 0 if out.get("status") != "failed" else 1


def cmd_refresh(args: argparse.Namespace) -> int:
    from .reasoning import refresh_evidence
    _emit(refresh_evidence(args.project, args.protocol))
    return 0


def cmd_dossiers(args: argparse.Namespace) -> int:
    from .reasoning import build_dossiers
    _emit(build_dossiers(args.project, args.protocol))
    return 0


def cmd_targets(args: argparse.Namespace) -> int:
    from .reasoning import propose_targets
    _emit(propose_targets(args.project, args.protocol,
                          top_per_candidate=args.top,
                          query_external=not args.offline))
    return 0


def cmd_resources(args: argparse.Namespace) -> int:
    from .resources import probe_all
    _emit(probe_all(args.project, to_jsonl=not args.no_write))
    return 0


def cmd_lit(args: argparse.Namespace) -> int:
    from . import literature
    proj = Project(Path(args.project)).ensure()
    conn = proj.connect()
    try:
        if args.action == "search":
            _emit(literature.search_pubmed(proj, conn, args.query, retmax=args.limit))
        elif args.action == "abstract":
            _emit(literature.fetch_abstracts(proj, conn, args.query.split(",")))
        elif args.action == "epmc":
            _emit(literature.europepmc_search(proj, conn, args.query,
                                              page_size=args.limit))
        elif args.action == "fulltext":
            _emit(literature.fetch_fulltext(proj, conn, args.query))
        else:
            _emit({"status": "failed", "error": f"unknown action {args.action}"})
            return 1
        return 0
    finally:
        conn.close()


def cmd_ask(args: argparse.Namespace) -> int:
    """Evidence follow-up: what is known about one candidate or target, and where."""
    from .harness import invoke_tool
    out = invoke_tool("sctda_ask", {"project": args.project, "subject": args.subject})
    _emit(out)
    return 0 if out.get("status") != "error" else 1


def cmd_tui(args: argparse.Namespace) -> int:
    from .tui import run_tui
    return run_tui(args.project)


def cmd_tools(args: argparse.Namespace) -> int:
    from .harness import tool_manifest
    _emit({"n": len(tool_manifest()), "tools": tool_manifest()})
    return 0


def cmd_call(args: argparse.Namespace) -> int:
    from .harness import invoke_tool
    payload = json.loads(args.args) if args.args else {}
    payload.setdefault("project", args.project) if args.project else None
    _emit(invoke_tool(args.tool, payload))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    # delegate to the tool boundary so the CLI and the agent see one shape
    from .harness import invoke_tool
    out = invoke_tool("sctda_status", {"project": args.project})
    _emit(out)
    return 0 if out.get("status") != "error" else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sctda", description="SCTDA research kernel")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("import", help="import a Seurat object into a project")
    pi.add_argument("object")
    pi.add_argument("--project", required=True)
    pi.add_argument("--object-name", default=None,
                    help="object name inside an .Rdata file (needed when ambiguous)")
    pi.add_argument("--annotation-column", default=None)
    pi.add_argument("--sample-column", default=None)
    pi.add_argument("--condition-column", default=None)
    pi.add_argument("--notes", default=None)
    pi.add_argument("--include-unlabeled", action="store_true", default=False,
                    help="explicitly include unannotated cells as a candidate group")
    pi.set_defaults(func=cmd_import)

    pm = sub.add_parser("map", help="set a column mapping (annotation/sample/condition)")
    pm.add_argument("--project", required=True)
    pm.add_argument("--role", required=True)
    pm.add_argument("--column", required=True)
    pm.add_argument("--notes", default=None)
    pm.add_argument("--include-unlabeled", action="store_true", default=False,
                    help="explicitly include unannotated cells as a candidate group")
    pm.set_defaults(func=cmd_map)

    pc = sub.add_parser("candidates", help="list every annotated candidate group")
    pc.add_argument("--project", required=True)
    pc.set_defaults(func=cmd_candidates)

    pcat = sub.add_parser("catalog", help="list the method catalog")
    pcat.add_argument("--executable", action="store_true")
    pcat.add_argument("--limit", type=int, default=None,
                      help="discovered page size, 1..500 (default 50)")
    pcat.add_argument("--offset", type=int, default=0)
    pcat.add_argument("--query", default=None,
                      help="filter discovered methods by name/package/operation")
    pcat.add_argument("--stage", default=None,
                      help="filter discovered methods by pipeline stage")
    pcat.set_defaults(func=cmd_catalog)

    pp = sub.add_parser("plan",
                        help="build and freeze a plan version (no approval step)")
    pp.add_argument("--project", required=True)
    pp.add_argument("--question", default=None)
    pp.add_argument("--methods", default=None, help="comma-separated method ids")
    pp.add_argument("--comparisons", default=None, help="JSON or plain text comparison spec")
    pp.add_argument("--geneset-file", default=None,
                    help="gene set snapshot JSON (defaults to newest in downloads/)")
    pp.set_defaults(func=cmd_plan)

    pr = sub.add_parser("run", help="execute a frozen plan")
    pr.add_argument("--project", required=True)
    pr.add_argument("--protocol", required=True)
    pr.add_argument("--resume", action="store_true")
    pr.add_argument("--stage", default=None)
    pr.set_defaults(func=cmd_run)

    prep = sub.add_parser("report", help="build HTML/PDF/CSV from artifacts")
    prep.add_argument("--project", required=True)
    prep.add_argument("--protocol", default=None)
    prep.add_argument("--formats", default="html,csv")
    prep.set_defaults(func=cmd_report)

    pd = sub.add_parser("derive", help="record current-data evidence and claims")
    pd.add_argument("--project", required=True)
    pd.add_argument("--protocol", required=True)
    pd.set_defaults(func=cmd_derive)

    ppz = sub.add_parser("pause", help="pause a frozen or running plan")
    ppz.add_argument("--project", required=True)
    ppz.add_argument("--protocol", required=True)
    ppz.add_argument("--reason", default=None)
    ppz.set_defaults(func=cmd_pause)

    prf = sub.add_parser("refresh", help="re-evaluate only claims whose artifacts changed")
    prf.add_argument("--project", required=True)
    prf.add_argument("--protocol", required=True)
    prf.set_defaults(func=cmd_refresh)

    pdo = sub.add_parser("dossiers", help="per-candidate evidence dossiers")
    pdo.add_argument("--project", required=True)
    pdo.add_argument("--protocol", required=True)
    pdo.set_defaults(func=cmd_dossiers)

    pt = sub.add_parser("targets", help="candidate targets and intervention chains")
    pt.add_argument("--project", required=True)
    pt.add_argument("--protocol", required=True)
    pt.add_argument("--top", type=int, default=5)
    pt.add_argument("--offline", action="store_true",
                    help="skip Open Targets / ChEMBL (no network)")
    pt.set_defaults(func=cmd_targets)

    prs = sub.add_parser("resources", help="probe external resources and record them")
    prs.add_argument("--project", required=True)
    prs.add_argument("--no-write", action="store_true")
    prs.set_defaults(func=cmd_resources)

    pl = sub.add_parser("lit", help="literature retrieval (cached, offline re-readable)")
    pl.add_argument("--project", required=True)
    pl.add_argument("action", choices=["search", "abstract", "epmc", "fulltext"])
    pl.add_argument("--query", required=True)
    pl.add_argument("--limit", type=int, default=20)
    pl.set_defaults(func=cmd_lit)

    pk = sub.add_parser("ask", help="follow up on the evidence behind a subject")
    pk.add_argument("--project", required=True)
    pk.add_argument("--subject", required=True)
    pk.set_defaults(func=cmd_ask)

    ptu = sub.add_parser("tui", help="terminal UI")
    ptu.add_argument("--project", required=True)
    ptu.set_defaults(func=cmd_tui)

    ptl = sub.add_parser("tools", help="list the unified tool boundary")
    ptl.set_defaults(func=cmd_tools)

    pc2 = sub.add_parser("call", help="invoke a tool through the unified boundary")
    pc2.add_argument("tool")
    pc2.add_argument("--project", default=None)
    pc2.add_argument("--args", default=None, help="JSON object of arguments")
    pc2.set_defaults(func=cmd_call)

    ps = sub.add_parser("status", help="project state")
    ps.add_argument("--project", required=True)
    ps.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
