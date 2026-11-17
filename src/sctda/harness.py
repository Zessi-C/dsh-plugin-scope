"""Unified tool boundary.

Every capability of the kernel - CLI, TUI, DSH plugin, or programmatic batch -
goes through `invoke_tool`. That is what keeps a batch orchestration from
bypassing the plan freeze or editing the evidence store directly: there is one
door, and it enforces the same rules for all callers.

Tools declare `mutating`. A mutating tool cannot change a frozen plan's scope:
`plan` and `revise` write a *new* frozen version, and `run` only moves a plan
through running/completed. There is no approval verb - nothing waits for a
human to release a plan. Research-process tools (`sctda_research_*`,
`sctda_entry_*`, `sctda_delegation_*`) write the additive research tables, not
the protocol; their descriptions state the call order, and none of them starts
or waits on a DSH subagent.

Two further distinctions keep the method surface honest:

* A *registry* record (from a frozen paper extraction) is not a *project*
  record. `sctda_method_*` reads the registry; `sctda_method_ledger_*` writes
  and reads what this project proposed or checked. Neither registers an
  adapter, installs a package, or runs an analysis.
* A real write can still be forbidden to create a project.
  `requires_initialised_project` marks a tool that records into an existing
  project (ledger rows, source snapshots); `invoke_tool` and the DSH plugin both
  refuse it unless `project.db` already exists, for every role. The ledger's
  process probe is deliberately not registered at all: accepting an argv from a
  model would put process execution back into a preset that has no shell.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import research_state as rs
from .catalog import catalog_summary
from . import method_search as ms
from .db import EVIDENCE_SOURCES
from .evidence import independence
from .execution import run_protocol
from .ingest import import_dataset, map_column
from .project import Project
from .protocol import build_plan, freeze_check, pause, revise
from .reasoning import (build_dossiers, derive_evidence, propose_targets,
                        refresh_evidence)
from .report import build_report
from .resources import probe_all

TOOL_DIRECTIVE = (
    "SCTDA tools operate on an imported project. Screening all annotated groups "
    "runs after the kernel freezes its own plan: `sctda_plan` writes a frozen "
    "version, `sctda_run` executes it, and no tool waits for user approval. "
    "Changing scope means writing a new version, never editing a frozen one. A "
    "missing input is never treated as a zero result. Research-process state is "
    "separate from analysis protocols: `sctda_research_start` opens a versioned "
    "run, entry tools record questions, hypotheses, competing explanations, "
    "observations, gaps and decisions with their provenance and typed links. "
    "Delegated children are recorded by the DSH host: the plugin writes rows "
    "through `sctda_subagent_start`, `sctda_subagent_attach` and "
    "`sctda_subagent_end`, while the manual `sctda_delegation_create`, "
    "`sctda_delegation_claim`, `sctda_delegation_complete` and "
    "`sctda_delegation_fail` bookkeeping is plugin/operator-only and is not "
    "registered with a model. No research tool starts, waits for, or cancels a "
    "DSH subagent: the caller starts the child with the DSH subagent tool, and "
    "the host records the returned child and session ids. After a restart, read "
    "`sctda_resume`; it reports stale children, and reconciliation is the "
    "host's `scope_delegation_status` path, never a manual claim. Method "
    "discovery is separate from execution: `sctda_method_search`, "
    "`sctda_method_get` and `sctda_method_stats` read the local registry, "
    "`sctda_method_external_search` queries public literature and keeps its "
    "request provenance. A literature hit or a descriptor match (for example "
    "UCell against AUCell) is never support; only the exact adapter ids in "
    "`sctda.catalog.EXECUTABLE` are executable, and only a task outcome shows "
    "that an execution succeeded. The PROJECT method ledger is a different "
    "store: `sctda_method_ledger_register`, `sctda_method_ledger_alias_add` "
    "and `sctda_method_ledger_source_add` write what this project proposed or "
    "checked, `sctda_method_ledger_list`, `sctda_method_ledger_get` and "
    "`sctda_method_ledger_sources` read it back. Those writes are real writes "
    "that never create a project; their answers keep wiring, installation and "
    "execution derived (catalog, recorded probe, succeeded protocol task), so "
    "no tool text can assert them. Source snapshots are immutable: "
    "`sctda_source_fetch` and `sctda_lit_fulltext` create one, "
    "`sctda_source_read` pages it, `sctda_source_verify` re-hashes it, and "
    "`sctda_source_list` / `sctda_code_sources` index them. Reading a source "
    "establishes what the document says, never that a method was used, "
    "correct, installed, or executed."
)


@dataclass
class ToolSpec:
    name: str
    summary: str
    mutating: bool
    requires_project: bool
    handler: Callable[..., dict]
    parameters: dict
    # Registration metadata for the DSH plugin, not an authorization boundary:
    # `internal` tools are never registered with a model, and `roles` narrows
    # which agent preset may see the tool. The kernel itself does not
    # authenticate a caller's role; a caller that reaches `invoke_tool`
    # directly bypasses both fields.
    internal: bool = False
    roles: tuple[str, ...] = ("lead", "worker")
    # A real write that is never allowed to create a project. `mutating` says
    # the call changes project state; this flag additionally says the project
    # must already exist, so neither a lead nor a worker can turn a method
    # registration, a snapshot fetch, or an external lookup into `sctda_import`.
    requires_initialised_project: bool = False


def _status(args: dict) -> dict:
    proj = Project(Path(args["project"])).ensure()
    conn = proj.connect()
    try:
        out: dict = {"project": str(proj.root)}
        ds = conn.execute("SELECT * FROM dataset LIMIT 1").fetchone()
        if ds:
            out["dataset"] = {"path": ds["source_path"], "n_cells": ds["n_cells"],
                              "n_genes": ds["n_genes"], "seurat": ds["seurat_version"]}
        out["mapping"] = [dict(r) for r in conn.execute(
            "SELECT role, column_name FROM mapping")]
        out["n_candidates"] = conn.execute("SELECT COUNT(*) c FROM candidate").fetchone()["c"]
        out["protocols"] = [dict(r) for r in conn.execute(
            "SELECT protocol_id, version, status, plan_sha256 FROM protocol ORDER BY version")]
        out["task_outcomes"] = {r["status"]: r["c"] for r in conn.execute(
            "SELECT status, COUNT(*) c FROM task GROUP BY status")}
        out["counts"] = {
            "artifacts": conn.execute("SELECT COUNT(*) c FROM artifact").fetchone()["c"],
            "claims": conn.execute("SELECT COUNT(*) c FROM claim").fetchone()["c"],
            "evidence": conn.execute("SELECT COUNT(*) c FROM evidence").fetchone()["c"],
            "reports": conn.execute("SELECT COUNT(*) c FROM report").fetchone()["c"],
        }
        return out
    finally:
        conn.close()


def _candidates(args: dict) -> dict:
    proj = Project(Path(args["project"])).ensure()
    conn = proj.connect()
    try:
        ds = conn.execute("SELECT dataset_id FROM dataset LIMIT 1").fetchone()
        if not ds:
            return {"status": "failed", "error": "no dataset imported yet"}
        did = ds["dataset_id"]
        m = conn.execute("SELECT column_name FROM mapping WHERE dataset_id=? AND role='annotation'",
                         (did,)).fetchone()
        if not m:
            return {"status": "needs_mapping",
                    "error": "no annotation column mapped; call sctda_map_column first",
                    "columns": [r[0] for r in conn.execute(
                        "SELECT column_name FROM dataset_column WHERE dataset_id=?",
                        (did,))]}
        rows = [dict(r) for r in conn.execute(
            "SELECT candidate_id, label, n_cells, is_unlabeled FROM candidate "
            "WHERE dataset_id=? ORDER BY is_unlabeled, n_cells DESC", (did,))]
        return {"annotation_column": m["column_name"], "n_candidates": len(rows),
                "candidates": rows}
    finally:
        conn.close()


def _ask(args: dict) -> dict:
    proj = Project(Path(args["project"])).ensure()
    conn = proj.connect()
    try:
        subject = args["subject"]
        rows = conn.execute("SELECT * FROM claim WHERE subject_id=? OR statement LIKE ?",
                            (subject, f"%{subject}%")).fetchall()
        claims = []
        for c in rows:
            ev = [dict(e) for e in conn.execute(
                "SELECT source_type, origin, locator, pmid, artifact_id, payload_json "
                "FROM evidence WHERE claim_id=?", (c["claim_id"],))]
            claims.append({"claim_id": c["claim_id"], "statement": c["statement"],
                           "status": c["status"],
                           "independence": independence(conn, c["claim_id"]),
                           "evidence": ev})
        return {"subject": subject, "n_claims": len(claims), "claims": claims}
    finally:
        conn.close()


def _bool_arg(args: dict, name: str, default: bool) -> bool:
    value = args.get(name)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _method_search(args: dict) -> dict:
    return ms.search_methods(
        query=args.get("query"), page=args.get("page", 1),
        page_size=args.get("page_size", ms.DEFAULT_PAGE_SIZE),
        stage=args.get("stage"), package=args.get("package"),
        support=args.get("support"),
        in_scope=(None if args.get("in_scope") is None
                  else _bool_arg(args, "in_scope", True)))


def _method_get(args: dict) -> dict:
    return ms.get_method(
        args["method_id"],
        anchor_page=args.get("anchor_page", 1),
        anchor_page_size=args.get("anchor_page_size", ms.DEFAULT_ANCHOR_PAGE_SIZE))


def _method_stats(args: dict) -> dict:
    return ms.method_stats()


def _method_external_search(args: dict) -> dict:
    sc_filter = args.get("single_cell_filter")
    if sc_filter is not None and not isinstance(sc_filter, bool):
        raise ValueError("single_cell_filter must be a boolean")
    include_abstracts = args.get("include_abstracts", False)
    if not isinstance(include_abstracts, bool):
        raise ValueError("include_abstracts must be a boolean")
    return ms.external_search(
        args["project"], args["query"],
        scope=args.get("scope") or "both", page=args.get("page", 1),
        page_size=args.get("page_size", ms.DEFAULT_EXTERNAL_PAGE_SIZE),
        include_abstracts=include_abstracts,
        single_cell_filter=True if sc_filter is None else sc_filter)


# ------------------------------------------- project method ledger and sources
# Two different things share the word "method" here. The read-only registry
# (`sctda_method_search/get/stats/external_search`) is a frozen literature
# index: it reports what papers mention and which ids are wired. The ledger
# below is the *project's own* record of methods a worker proposed or checked:
# name, premises, limitations, and the sources it was checked against. Writing a
# ledger row never wires an adapter, installs a package, or runs anything; the
# derived wiring/installation/execution fields in a ledger answer come from the
# catalog and from real recorded protocol rows, never from the caller's prose.


def _ledger_module():
    from . import method_ledger
    return method_ledger


def _sources_module():
    from . import method_sources
    return method_sources


def _project_ledger_answer(name: str, call: Callable[[Any], dict]) -> dict:
    """Call the ledger, turning its typed errors into statuses.

    A missing module is `unavailable`, not an empty ledger: the worker must not
    read "no records" out of an install that cannot answer at all.
    """
    try:
        module = _ledger_module()
    except ImportError as exc:                                  # pragma: no cover
        return {"status": "unavailable", "tool": name,
                "error": "the project method ledger module is not installed",
                "detail": str(exc)}
    try:
        return call(module)
    except module.NotFoundError as exc:
        return {"status": "not_found", "tool": name, "found": False,
                "error": str(exc)}
    except module.ConflictError as exc:
        return {"status": "conflict", "tool": name, "error": str(exc)}
    except module.ValidationError as exc:
        return {"status": "error", "tool": name, "error_kind": "validation",
                "error": str(exc)}
    except module.MethodLedgerError as exc:
        return {"status": "error", "tool": name,
                "error": f"{type(exc).__name__}: {exc}"}


def _method_ledger_register(args: dict) -> dict:
    def call(mod):
        out = mod.register_method(
            args["project"], args["name"],
            aliases=args.get("aliases"), purpose=args.get("purpose"),
            inputs=args.get("inputs"), prerequisites=args.get("prerequisites"),
            limitations=args.get("limitations"),
            adapter_method_id=args.get("adapter_method_id"),
            dedup_key=args.get("dedup_key"))
        return {"status": "created" if out.get("created") else "existing", **out}
    return _project_ledger_answer("sctda_method_ledger_register", call)


def _method_ledger_list(args: dict) -> dict:
    def call(mod):
        out = mod.search_methods(
            args["project"], args.get("query"),
            adapter_method_id=args.get("adapter_method_id"),
            limit=args.get("limit", 50), offset=args.get("offset", 0))
        return {"status": "ok", **out}
    return _project_ledger_answer("sctda_method_ledger_list", call)


def _method_ledger_get(args: dict) -> dict:
    def call(mod):
        out = mod.get_method(
            args["project"], args["identifier"],
            verify_snapshots=_bool_arg(args, "verify_snapshots", True),
            source_limit=args.get("source_limit", 50),
            source_offset=args.get("source_offset", 0))
        return {"status": "ok", "found": True, **out}
    return _project_ledger_answer("sctda_method_ledger_get", call)


def _method_ledger_alias_add(args: dict) -> dict:
    def call(mod):
        out = mod.add_alias(args["project"], args["ledger_id"], args["alias"])
        return {"status": "created" if out.get("created") else "existing", **out}
    return _project_ledger_answer("sctda_method_ledger_alias_add", call)


def _method_ledger_source_add(args: dict) -> dict:
    def call(mod):
        out = mod.add_source(
            args["project"], args["ledger_id"],
            source_kind=args["source_kind"],
            external_id=args.get("external_id"), title=args.get("title"),
            version=args.get("version"), query=args.get("query"),
            endpoint=args.get("endpoint"), locator=args.get("locator"),
            snapshot_path=args.get("snapshot_path"),
            expected_sha256=args.get("expected_sha256"),
            quote=args.get("quote"), match_kind=args.get("match_kind", "metadata"),
            notes=args.get("notes"), dedup_key=args.get("dedup_key"),
            retrieved_utc=args.get("retrieved_utc"))
        return {"status": "created" if out.get("created") else "existing", **out}
    return _project_ledger_answer("sctda_method_ledger_source_add", call)


def _method_ledger_sources(args: dict) -> dict:
    def call(mod):
        out = mod.list_sources(
            args["project"], args["ledger_id"],
            limit=args.get("limit", 50), offset=args.get("offset", 0),
            verify_snapshots=_bool_arg(args, "verify_snapshots", True))
        return {"status": "ok", **out}
    return _project_ledger_answer("sctda_method_ledger_sources", call)


def _source_module_answer(name: str, call: Callable[[Any], dict]) -> dict:
    try:
        module = _sources_module()
    except ImportError as exc:                                  # pragma: no cover
        return {"status": "unavailable", "tool": name,
                "error": "the method source snapshot module is not installed",
                "detail": str(exc)}
    try:
        return call(module)
    except ValueError as exc:
        return {"status": "error", "tool": name, "error_kind": "validation",
                "error": str(exc)}


def _source_fetch(args: dict) -> dict:
    return _source_module_answer("sctda_source_fetch", lambda mod: mod.fetch_source(
        args["project"], args["url"], args["kind"],
        refresh=_bool_arg(args, "refresh", False),
        timeout=args.get("timeout", 20)))


def _lit_fulltext(args: dict) -> dict:
    return _source_module_answer("sctda_lit_fulltext", lambda mod: mod.fetch_paper(
        args["project"], args["pmcid"], refresh=_bool_arg(args, "refresh", False)))


def _source_read(args: dict) -> dict:
    return _source_module_answer("sctda_source_read", lambda mod: mod.read_snapshot(
        args["project"], args["snapshot_id"],
        offset=args.get("offset", 0), limit=args.get("limit", 4000)))


def _source_verify(args: dict) -> dict:
    return _source_module_answer("sctda_source_verify", lambda mod: mod.verify_snapshot(
        args["project"], args["snapshot_id"]))


def _source_list(args: dict) -> dict:
    return _source_module_answer("sctda_source_list", lambda mod: mod.list_snapshots(
        args["project"], kind=args.get("kind"), status=args.get("status"),
        query=args.get("query"), limit=args.get("limit", 20),
        offset=args.get("offset", 0)))


def _code_sources(args: dict) -> dict:
    return _source_module_answer("sctda_code_sources", lambda mod: mod.code_sources(
        args["pmid"], page=args.get("page", 1),
        page_size=args.get("page_size", 20), path=args.get("path")))


def _import(args: dict) -> dict:
    return import_dataset(args["project"], args["object"],
                          object_name=args.get("object_name"),
                          annotation_column=args.get("annotation_column"),
                          sample_column=args.get("sample_column"),
                          condition_column=args.get("condition_column"))


# ------------------------------------------------------------ research runs
# These handlers do not re-implement state rules: `sctda.research_state` owns
# validation, transactions and transitions, and lets its domain exceptions
# reach `invoke_tool`, so a research tool fails in the same envelope as every
# other kernel tool (`{"status": "error", "error": "ValidationError: ..."}`).

def _research_start(args: dict) -> dict:
    return rs.start_run(args["project"], args["question"],
                        version=args.get("version"),
                        dedup_key=args.get("dedup_key"),
                        run_id=args.get("run_id"),
                        parent_session_id=args.get("parent_session_id"),
                        rebind_parent_session=bool(
                            args.get("rebind_parent_session", False)))


def _research_get(args: dict) -> dict:
    return rs.get_run(args["project"], args["run_id"],
                      stale_after_seconds=args.get("stale_after_seconds"),
                      as_of_utc=args.get("as_of_utc"))


def _research_list(args: dict) -> dict:
    return rs.list_runs(args["project"], status=args.get("status"),
                        limit=args.get("limit", 50), offset=args.get("offset", 0),
                        stale_after_seconds=args.get("stale_after_seconds"),
                        as_of_utc=args.get("as_of_utc"))


def _research_finish(args: dict) -> dict:
    return rs.finish_run(args["project"], args["run_id"],
                         outcome=args.get("outcome"))


def _research_fail(args: dict) -> dict:
    return rs.fail_run(args["project"], args["run_id"], error=args["error"],
                       outcome=args.get("outcome"))


def _research_heartbeat(args: dict) -> dict:
    return rs.heartbeat_run(args["project"], args["run_id"])


# ---------------------------------------------------------------- entries
def _entry_add(args: dict) -> dict:
    return rs.append_entry(
        args["project"], args["run_id"], args["kind"], args["statement"],
        source_type=args.get("source_type", "model_hypothesis"),
        source_ref=args.get("source_ref"),
        source_details=args.get("source_details"),
        details=args.get("details"),
        status=args.get("status", "open"),
        author=args.get("author"),
        delegation_id=args.get("delegation_id"),
        dedup_key=args.get("dedup_key"),
        entry_id=args.get("entry_id"))


def _entry_get(args: dict) -> dict:
    return rs.get_entry(args["project"], args["entry_id"])


def _entries(args: dict) -> dict:
    return rs.list_entries(args["project"], args["run_id"],
                           kind=args.get("kind"), status=args.get("status"),
                           delegation_id=args.get("delegation_id"),
                           limit=args.get("limit", 100), offset=args.get("offset", 0))


def _entry_set_status(args: dict) -> dict:
    return rs.set_entry_status(args["project"], args["entry_id"], args["status"],
                               note=args.get("note"))


def _entry_link(args: dict) -> dict:
    return rs.link_entries(args["project"], args["run_id"],
                           args["from_entry_id"], args["to_entry_id"],
                           args["relation"], note=args.get("note"))


# ------------------------------------------------------------ delegations
def _delegation_create(args: dict) -> dict:
    return rs.create_delegation(args["project"], args["run_id"], args["role"],
                                args["task"], dedup_key=args.get("dedup_key"),
                                request=args.get("request"),
                                delegation_id=args.get("delegation_id"),
                                parent_session_id=args.get("parent_session_id"),
                                dsh_run_id=args.get("dsh_run_id"),
                                provider=args.get("provider"),
                                label=args.get("label"),
                                task_prompt=args.get("task_prompt"),
                                rebind_parent_session=bool(
                                    args.get("rebind_parent_session", False)))


def _delegation_claim(args: dict) -> dict:
    return rs.claim_delegation(
        args["project"], args["delegation_id"], child_id=args["child_id"],
        session_id=args.get("session_id"),
        lease_seconds=args.get("lease_seconds", rs.DEFAULT_LEASE_SECONDS),
        stale_after_seconds=args.get("stale_after_seconds"),
        as_of_utc=args.get("as_of_utc"))


def _delegation_heartbeat(args: dict) -> dict:
    return rs.heartbeat_delegation(
        args["project"], args["delegation_id"],
        lease_seconds=args.get("lease_seconds", rs.DEFAULT_LEASE_SECONDS))


def _delegation_complete(args: dict) -> dict:
    return rs.complete_delegation(
        args["project"], args["delegation_id"], result=args["result"],
        child_id=args.get("child_id"), session_id=args.get("session_id"))


def _delegation_fail(args: dict) -> dict:
    return rs.fail_delegation(
        args["project"], args["delegation_id"], error=args["error"],
        child_id=args.get("child_id"), session_id=args.get("session_id"))


def _delegations(args: dict) -> dict:
    return rs.list_delegations(
        args["project"], args.get("run_id") or None, status=args.get("status"),
        include_finished=bool(args.get("include_finished", True)),
        limit=args.get("limit", 100), offset=args.get("offset", 0),
        stale_after_seconds=args.get("stale_after_seconds"),
        as_of_utc=args.get("as_of_utc"))


def _delegation_find(args: dict) -> dict:
    return rs.find_delegation(
        args["project"], delegation_id=args.get("delegation_id"),
        child_id=args.get("child_id"), dsh_run_id=args.get("dsh_run_id"),
        session_id=args.get("session_id"), run_id=args.get("run_id"))


# -- DSH lifecycle handlers: called by the plugin, never registered with a model
def _subagent_start(args: dict) -> dict:
    return rs.start_subagent(
        args["project"], parent_session_id=args["parent_session_id"],
        provider=args["provider"], dsh_run_id=args["dsh_run_id"],
        child_id=args["child_id"], label=args["label"],
        task_prompt=args.get("task_prompt"), session_id=args.get("session_id"),
        run_id=args.get("run_id"), role=args.get("role"), task=args.get("task"),
        request=args.get("request"), dedup_key=args.get("dedup_key"),
        delegation_id=args.get("delegation_id"),
        rebind_parent_session=bool(args.get("rebind_parent_session", False)),
        lease_seconds=args.get("lease_seconds", rs.DEFAULT_LEASE_SECONDS),
        as_of_utc=args.get("as_of_utc"))


def _subagent_attach(args: dict) -> dict:
    return rs.attach_subagent(
        args["project"], delegation_id=args.get("delegation_id"),
        child_id=args.get("child_id"), dsh_run_id=args.get("dsh_run_id"),
        session_id=args.get("session_id"),
        parent_session_id=args.get("parent_session_id"),
        provider=args.get("provider"), label=args.get("label"),
        task_prompt=args.get("task_prompt"),
        rebind_parent_session=bool(args.get("rebind_parent_session", False)),
        lease_seconds=args.get("lease_seconds", rs.DEFAULT_LEASE_SECONDS),
        as_of_utc=args.get("as_of_utc"))


def _subagent_end(args: dict) -> dict:
    return rs.end_subagent(
        args["project"], delegation_id=args.get("delegation_id"),
        child_id=args.get("child_id"), dsh_run_id=args.get("dsh_run_id"),
        session_id=args.get("session_id"), status=args.get("status", "completed"),
        stop_reason=args.get("stop_reason"), result=args.get("result"),
        error=args.get("error"), output_sha256=args.get("output_sha256"),
        output_excerpt=args.get("output_excerpt"), provider=args.get("provider"),
        label=args.get("label"), task_prompt=args.get("task_prompt"))


# ---------------------------------------------------------------- recovery
def _resume(args: dict) -> dict:
    return rs.recover_run(
        args["project"], args.get("run_id") or None,
        stale_after_seconds=args.get("stale_after_seconds"),
        include_finished=bool(args.get("include_finished", False)),
        entry_limit=args.get("entry_limit", 200),
        delegation_limit=args.get("delegation_limit", 200),
        as_of_utc=args.get("as_of_utc"))


TOOLS: dict[str, ToolSpec] = {
    "sctda_status": ToolSpec(
        "sctda_status", "Project state: dataset, mappings, candidates, protocols, "
        "task outcomes, artifact/claim/evidence counts.", False, True, _status, {}),
    "sctda_candidates": ToolSpec(
        "sctda_candidates", "List every annotated candidate group and its cell count, "
        "plus unannotated cells as their own entry.", False, True, _candidates, {}),
    "sctda_ask": ToolSpec(
        "sctda_ask", "Follow up on the evidence behind a candidate or target: the claims "
        "made about it and the located evidence for each, with an independence count.",
        False, True, _ask,
        {"subject": {"type": "string", "required": True,
                     "description": "Candidate label, gene symbol or claim subject."}}),
    "sctda_catalog": ToolSpec(
        "sctda_catalog", "Methods discovered in the literature, kept separate from methods "
        "that actually have a working adapter. The discovered list is a bounded page: "
        "pass limit/offset, and optionally query or stage, instead of pulling every name. "
        "The registry already applies the authoritative 422-PMID scope and retraction "
        "exclusion; this tool does not re-filter it.", False, False,
        lambda a: catalog_summary(
            executable_only=bool(a.get("executable_only")),
            limit=a.get("limit", 50), offset=a.get("offset", 0),
            query=a.get("query"), stage=a.get("stage")),
        {"executable_only": {"type": "boolean",
                             "description": "list only wired adapters"},
         "limit": {"type": "integer",
                   "description": "discovered page size 1..500; default 50."},
         "offset": {"type": "integer", "description": "non-negative; default 0."},
         "query": {"type": "string", "description": "filter discovered names."},
         "stage": {"type": "string", "description": "filter by pipeline stage."}}),

    # -- method discovery: local registry and corpus-external literature ------
    # These are read-only. `sctda_method_search/get/stats` never need a
    # project; the external lookup keeps its HTTP provenance in the project's
    # literature_cache, so it does.
    "sctda_method_search": ToolSpec(
        "sctda_method_search",
        "Search the local method registry (the same 422-PMID scope as the "
        "catalog, retraction-excluded) with deterministic paging. Every hit "
        "carries its corpus provenance (PMIDs, locators, examples) and exactly "
        "one support status: executable (an exact wired adapter exists), "
        "corpus_only (in the literature, no adapter), external_only (only from "
        "a public search; never used here), or not_found (no local record). "
        "Frequency is provenance, never a ranking or admission filter. An empty "
        "answer is a gap in a known-incomplete extraction, not proof that no "
        "method exists; use sctda_method_external_search for a public lookup.",
        False, False, _method_search,
        {"query": {"type": "string",
                   "description": "Terms matched against ids, names, aliases, packages, stages and operations."},
         "page": {"type": "integer", "description": ">= 1; default 1."},
         "page_size": {"type": "integer", "description": "1..100; default 20."},
         "stage": {"type": "string", "description": "Exact stage filter (normalized)."},
         "package": {"type": "string", "description": "Package filter (normalized)."},
         "support": {"type": "string", "enum": list(ms.SUPPORT_STATUSES),
                     "description": "Only the computed support status; registry claims do not override it."},
         "in_scope": {"type": "boolean",
                      "description": "Omit for both; true/false filters records with a known flag; adapter records with unknown corpus scope are never excluded."}},
        roles=("worker", "lead")),
    "sctda_method_get": ToolSpec(
        "sctda_method_get",
        "Read one local method record by exact method_id or exact canonical "
        "name: support status, exact adapter wiring when it exists, corpus "
        "PMIDs, fuzzy catalog relations explicitly marked "
        "counts_as_executable=false, and every evidence anchor as a page. "
        "Anchor paging is separate from method lookup: method.provenance omits "
        "the full anchor list, and anchors carries page/page_size/total/pages. "
        "An unknown id returns found=false with near_matches and status "
        "not_found; no record is invented.",
        False, False, _method_get,
        {"method_id": {"type": "string", "required": True,
                       "description": "Registry id (for example lit:<slug>-<hex> or adapter:composition) or exact canonical name."},
         "anchor_page": {"type": "integer",
                         "description": "Evidence-anchor page >= 1; default 1."},
         "anchor_page_size": {"type": "integer",
                              "description": "Evidence anchors per page 1..100; default 20."}},
        roles=("worker", "lead")),
    "sctda_method_stats": ToolSpec(
        "sctda_method_stats",
        "Report the local registry's size by support/kind/stage/package, the "
        "four-status vocabulary, the four exact adapters, corpus scope "
        "metadata and known incompleteness. Counts are descriptive, never a "
        "ranking. It does not probe R package installation or claim that "
        "PubMed is reachable now.",
        False, False, _method_stats, {},
        roles=("worker", "lead")),
    "sctda_method_external_search": ToolSpec(
        "sctda_method_external_search",
        "Search PubMed and/or Europe PMC for a method when the local corpus is "
        "insufficient. Requests go through the project's cached literature "
        "client, and every source reports endpoint, request time, HTTP status, "
        "body SHA-256 and cache path. Hits are external_only pointers: "
        "usable_as_evidence=false, method_support=not_asserted, anchor not "
        "checked. A failed source is unreachable/partial, never an empty "
        "negative; a zero-hit ok response is not a claim of nonexistence.",
        False, True, _method_external_search,
        {"query": {"type": "string", "required": True,
                   "description": "Method/tool query; <= 500 printable characters."},
         "scope": {"type": "string", "enum": ["pubmed", "europepmc", "both"],
                   "description": "Which public source(s) to query; default both."},
         "page": {"type": "integer", "description": ">= 1; default 1."},
         "page_size": {"type": "integer", "description": "1..25; default 10."},
         "include_abstracts": {"type": "boolean",
                               "description": "Include abstract text in hits; default false."},
         "single_cell_filter": {"type": "boolean",
                                "description": "AND a single-cell scope filter; its exact text is echoed per source."}},
        roles=("worker", "lead")),

    # -- project method ledger: the project's own method records --------------
    # Distinct from the registry above: this is what *this project* proposed or
    # checked, with its sources. Writes are real writes, and every one of them
    # requires an already-initialised project. None of them wires an adapter or
    # grants execution: `wiring`, `installation` and `execution` in an answer are
    # derived by the ledger from the catalog and from real protocol rows.
    "sctda_method_ledger_register": ToolSpec(
        "sctda_method_ledger_register",
        "Register one method in THIS project's method ledger: name, aliases, "
        "purpose, required inputs, prerequisites, limitations and the optional "
        "exact adapter method id. Writes project state; the project must "
        "already be initialised (project.db), because this is not an import. "
        "Idempotent on the normalized name (or dedup_key): an identical repeat "
        "returns created=false with the existing row, and a conflicting payload "
        "is refused. Registration never makes a method executable, never "
        "installs a package and never runs an analysis; support is derived "
        "later from the catalog and from real protocol task rows.",
        True, True, _method_ledger_register,
        {"name": {"type": "string", "required": True,
                  "description": "Method name as used in the source; one record per canonical name."},
         "aliases": {"type": "array", "items": {"type": "string"},
                     "description": "Other names the same method appears under."},
         "purpose": {"type": "string",
                     "description": "What the method is for, in the worker's own words, traceable to a source."},
         "inputs": {"type": "array", "items": {"type": "string"},
                    "description": "Required inputs, one string each, e.g. 'expression matrix', 'annotation column', 'sample column', 'gene set'."},
         "prerequisites": {"type": "array", "items": {"type": "string"},
                           "description": "Preconditions and assumptions, one string each, e.g. 'non-malignant reference group', '>=10 cells per sample'."},
         "limitations": {"type": "array", "items": {"type": "string"},
                         "description": "Known limits, failure modes and what the source does not establish."},
         "adapter_method_id": {"type": "string",
                               "description": "Only when an exact wired adapter id fits; never a similar name. Wiring is re-derived, not trusted."},
         "dedup_key": {"type": "string",
                       "description": "Retry identity; a repeated identical call returns the same record."}},
        roles=("worker", "lead"), requires_initialised_project=True),
    "sctda_method_ledger_list": ToolSpec(
        "sctda_method_ledger_list",
        "List this project's method-ledger records, newest first, with paging "
        "and an optional substring query or exact adapter filter. Compact rows: "
        "read one with `sctda_method_ledger_get` for premises, sources and the "
        "derived wiring/installation/execution states. An empty page means this "
        "project has registered nothing, not that the literature has no method.",
        False, True, _method_ledger_list,
        {"query": {"type": "string",
                   "description": "Substring over name, aliases, purpose and limitations."},
         "adapter_method_id": {"type": "string",
                               "description": "Only records bound to this exact adapter id."},
         "limit": {"type": "integer", "description": "Page size; default 50."},
         "offset": {"type": "integer", "description": "Zero-based offset; default 0."}},
        roles=("worker", "lead")),
    "sctda_method_ledger_get": ToolSpec(
        "sctda_method_ledger_get",
        "Read one project method-ledger record by ledger_id, canonical name or "
        "alias: premise fields, aliases, the attached sources with their "
        "snapshot check status, and the DERIVED wiring / installation / "
        "execution states. Derived means the ledger, not the model, decides: "
        "`wired` comes from the exact catalog id, `installation` stays "
        "`unprobed` unless a real probe was recorded, and `execution` needs a "
        "succeeded protocol task with its artifact. `missing` lists what was "
        "never recorded. Unknown identifiers answer not_found.",
        False, True, _method_ledger_get,
        {"identifier": {"type": "string", "required": True,
                        "description": "ledger_id, exact canonical name, or alias."},
         "verify_snapshots": {"type": "boolean",
                              "description": "Re-hash attached snapshots; default true."},
         "source_limit": {"type": "integer",
                          "description": "Sources per page; default 50."},
         "source_offset": {"type": "integer",
                           "description": "Zero-based source offset; default 0."}},
        roles=("worker", "lead")),
    "sctda_method_ledger_alias_add": ToolSpec(
        "sctda_method_ledger_alias_add",
        "Attach one alias to an existing project method-ledger record. Writes "
        "project state; the project must already be initialised. An identical "
        "alias returns created=false; an alias held by another record is "
        "refused rather than merged. It changes no support status.",
        True, True, _method_ledger_alias_add,
        {"ledger_id": {"type": "string", "required": True,
                       "description": "Target ledger record id."},
         "alias": {"type": "string", "required": True,
                   "description": "Alias text exactly as the source spells it."}},
        roles=("worker", "lead"), requires_initialised_project=True),
    "sctda_method_ledger_source_add": ToolSpec(
        "sctda_method_ledger_source_add",
        "Attach one checked source to a project method-ledger record: a paper "
        "locator/PMID, a documentation or web page, a repository path, or a "
        "database record, plus the optional immutable snapshot and a verbatim "
        "quote. "
        "Writes project state and requires an initialised project. The ledger "
        "hashes a supplied snapshot and checks expected_sha256; it appends a new "
        "record version and never rewrites earlier sources. At least one "
        "checkable reference is required: external_id, endpoint, locator or "
        "snapshot_path; a title or query alone is refused, and a failed fetch "
        "has no snapshot_path (pass its endpoint instead). match_kind records "
        "how the claim was matched (metadata, text_match, snapshot_bytes); "
        "semantic_verification stays not_performed, so a source is never "
        "evidence that the method was used or is correct.",
        True, True, _method_ledger_source_add,
        {"ledger_id": {"type": "string", "required": True},
         "source_kind": {"type": "string", "required": True,
                         "enum": ["paper", "preprint", "package", "repository",
                                  "documentation", "database", "web", "other"],
                         "description": "Ledger source class. A fetched tool doc is documentation/web, author code is repository, a paper is paper."},
         "external_id": {"type": "string", "description": "PMID, DOI, database accession."},
         "title": {"type": "string"},
         "version": {"type": "string", "description": "Package/tool version when the source states one."},
         "query": {"type": "string", "description": "The query that found this source, when it was a search."},
         "endpoint": {"type": "string", "description": "HTTP endpoint or API URL actually requested."},
         "locator": {"type": "string", "description": "Section, line or file locator inside the source."},
         "snapshot_path": {"type": "string",
                           "description": "Existing immutable snapshot path, e.g. sctda_source_fetch's body_path."},
         "expected_sha256": {"type": "string",
                             "description": "Hash the snapshot must match; a mismatch is refused."},
         "quote": {"type": "string", "description": "Verbatim excerpt; never a paraphrase."},
         "match_kind": {"type": "string", "enum": ["metadata", "text_match", "snapshot_bytes"],
                        "description": "How the claim was matched; default metadata."},
         "notes": {"type": "string"},
         "dedup_key": {"type": "string"},
         "retrieved_utc": {"type": "string", "description": "ISO-8601 time the source was read."}},
        roles=("worker", "lead"), requires_initialised_project=True),
    "sctda_method_ledger_sources": ToolSpec(
        "sctda_method_ledger_sources",
        "Page one ledger record's attached sources with their snapshot check "
        "status (absent / missing / not_checked / verified / drift / "
        "unreadable) and appended record version. Read-only; it does not fetch "
        "anything and does not add a source. A verified hash means the bytes "
        "still match; it does not mean the source supports the method.",
        False, True, _method_ledger_sources,
        {"ledger_id": {"type": "string", "required": True},
         "limit": {"type": "integer", "description": "Page size; default 50."},
         "offset": {"type": "integer", "description": "Zero-based offset; default 0."},
         "verify_snapshots": {"type": "boolean",
                              "description": "Re-hash snapshots; default true."}},
        roles=("worker", "lead")),

    # -- immutable source snapshots ------------------------------------------
    # `blocked`, `too_large`, `unreachable` and `not_found` are answers, not
    # empty results. A snapshot is what the document says; it is never proof
    # that a method was used, correct, or installed.
    "sctda_source_fetch": ToolSpec(
        "sctda_source_fetch",
        "Fetch one public tool-document or author-code page into an immutable "
        "project snapshot: https only, fixed host allowlists, redirects "
        "refused, DNS must resolve to a public address, and a byte cap. Writes "
        "the snapshot plus sidecar metadata and requires an initialised "
        "project. Answers status=ok|blocked|too_large|failed|unreachable; ok "
        "carries the snapshot id, endpoint, request time, HTTP status, body "
        "SHA-256 and body_path, and claims_verified=false. Attach it to the "
        "ledger with `sctda_method_ledger_source_add`; fetching is not checking.",
        True, True, _source_fetch,
        {"url": {"type": "string", "required": True,
                 "description": "https URL on an allowed documentation or code host."},
         "kind": {"type": "string", "required": True,
                  "enum": ["tool_doc", "author_code"],
                  "description": "What the page is; doc/code are accepted aliases."},
         "refresh": {"type": "boolean",
                     "description": "Re-fetch even if a cached snapshot exists; default false."},
         "timeout": {"type": "integer", "description": "Per-request seconds; default 20."}},
        roles=("worker", "lead"), requires_initialised_project=True),
    "sctda_lit_fulltext": ToolSpec(
        "sctda_lit_fulltext",
        "Fetch one Europe PMC open-access full text into an immutable project "
        "snapshot with all sections, the abstract and tables preserved. Writes "
        "the snapshot plus sidecar metadata and requires an initialised "
        "project. Answers status=ok|not_available|failed|unreachable; "
        "not_available means no open-access full text, never an empty paper. "
        "Read it with `sctda_source_read` and quote it as the paper's text, not "
        "as support for a claim.",
        True, True, _lit_fulltext,
        {"pmcid": {"type": "string", "required": True,
                   "description": "PMCID as PMC123 or 123."},
         "refresh": {"type": "boolean", "description": "Re-fetch a cached copy; default false."}},
        roles=("worker", "lead"), requires_initialised_project=True),
    "sctda_source_read": ToolSpec(
        "sctda_source_read",
        "Page through one stored source snapshot as characters, re-hashing the "
        "file on every read. Read-only. status=ok returns text, total_chars, "
        "next_offset and sha256_verified; integrity_failed refuses to return "
        "text when the file no longer matches its record; not_found names an "
        "unknown snapshot, including a failed fetch's attempt_id (which is not "
        "a snapshot id). claims_verified is always false: this is what the "
        "document says, not evidence that it supports a method.",
        False, True, _source_read,
        {"snapshot_id": {"type": "string", "required": True,
                         "description": "Snapshot id from a fetch or source listing."},
         "offset": {"type": "integer", "description": "Character offset; default 0."},
         "limit": {"type": "integer",
                   "description": "Characters per page 1..20000; default 4000."}},
        roles=("worker", "lead")),
    "sctda_source_verify": ToolSpec(
        "sctda_source_verify",
        "Re-read one stored snapshot and compare its SHA-256 and byte size with "
        "the recorded metadata. Read-only: it never re-fetches and writes "
        "nothing. status=ok returns integrity_verified with sha256_match and "
        "size_match; not_found for an unknown id (an attempt_id from a failed "
        "fetch included); integrity_failed for a "
        "mismatch (with recorded and actual hashes). integrity_verified proves "
        "file integrity only - it is not claim verification, and "
        "claims_verified stays false.",
        False, True, _source_verify,
        {"snapshot_id": {"type": "string", "required": True}},
        roles=("worker", "lead")),
    "sctda_source_list": ToolSpec(
        "sctda_source_list",
        "List stored source snapshots newest first, with metadata only and "
        "optional kind/status/query filters. Read-only. Use it before "
        "re-fetching; a listed snapshot still needs `sctda_source_read` before "
        "it can be quoted.",
        False, True, _source_list,
        {"kind": {"type": "string",
                  "description": "Snapshot kind, e.g. tool_doc, author_code, paper_fulltext."},
         "status": {"type": "string", "description": "Only snapshots with this stored status."},
         "query": {"type": "string", "description": "Substring over title, URL and snapshot id."},
         "limit": {"type": "integer", "description": "Page size; default 20."},
         "offset": {"type": "integer", "description": "Zero-based offset; default 0."}},
        roles=("worker", "lead")),
    "sctda_code_sources": ToolSpec(
        "sctda_code_sources",
        "List the frozen author-code records for one PMID from "
        "research/code_sources.jsonl, paged, with the file SHA-256. Read-only "
        "and project-free. Every record carries verified=false and "
        "verification=unchecked (or recorded_check): a repository or script "
        "link is a lead, not a confirmed implementation or parameter source.",
        False, False, _code_sources,
        {"pmid": {"type": "string", "required": True},
         "page": {"type": "integer", "description": ">= 1; default 1."},
         "page_size": {"type": "integer", "description": "1..100; default 20."},
         "path": {"type": "string",
                  "description": "Omit; only the checkout's frozen research/code_sources.jsonl is readable. Any other path answers failed."}},
        roles=("worker", "lead")),

    "sctda_import": ToolSpec(
        "sctda_import", "Import a Seurat object into a project and enumerate candidates. "
        "Column roles are never guessed: pass them or the real columns are listed.",
        True, True, _import,
        {"project": {"type": "string", "required": True},
         "object": {"type": "string", "required": True},
         "annotation_column": {"type": "string"},
         "sample_column": {"type": "string"},
         "condition_column": {"type": "string"}}),
    "sctda_map_column": ToolSpec(
        "sctda_map_column", "Map a metadata column to a role (annotation/sample/condition). "
        "Re-pointing the annotation column after a run is refused.",
        True, True,
        lambda a: _with_conn(a, lambda conn: map_column(
            conn, _dataset_id(conn), a["role"], a["column"])),
        {"role": {"type": "string", "required": True},
         "column": {"type": "string", "required": True}}),
    "sctda_plan": ToolSpec(
        "sctda_plan", "Build and freeze a plan version: candidate universe, task graph, "
        "parameter provenance, statistical unit (cell primary / sample supplementary), "
        "expected artifacts, design warnings. No significance threshold is applied "
        "unless the plan declares one. The written plan is frozen by hash; running it "
        "needs no approval.", True, True,
        lambda a: build_plan(a["project"], question=a.get("question"),
                             methods=a.get("methods"), comparisons=a.get("comparisons")),
        {"question": {"type": "string"},
         "methods": {"type": "array", "description": "method ids, e.g. ['composition']"},
         "comparisons": {"type": "array", "description": "only '<candidate label> vs rest'; group-condition contrasts are not supported"}}),
    "sctda_revise": ToolSpec(
        "sctda_revise", "Write a NEW frozen version from an existing plan instead of "
        "mutating it; the parent keeps its hash and artifacts.", True, True,
        lambda a: revise(a["project"], a["protocol"], a.get("changes") or {}),
        {"protocol": {"type": "string", "required": True},
         "changes": {"type": "object"}}),
    "sctda_run": ToolSpec(
        "sctda_run", "Execute a frozen plan (frozen/running/paused; no approval gate). "
        "Records one outcome per (candidate, method). Resuming never duplicates "
        "artifacts.", True, True,
        lambda a: run_protocol(a["project"], a["protocol"], resume=bool(a.get("resume"))),
        {"protocol": {"type": "string", "required": True},
         "resume": {"type": "boolean"}}),
    "sctda_pause": ToolSpec(
        "sctda_pause", "Pause a frozen or running plan without discarding computed "
        "state; resume with sctda_run and resume=true.", True, True,
        lambda a: pause(a["project"], a["protocol"], a.get("reason") or "paused by operator"),
        {"protocol": {"type": "string", "required": True},
         "reason": {"type": "string"}}),
    "sctda_derive": ToolSpec(
        "sctda_derive", "Record current-data evidence and claims for every candidate.",
        True, True, lambda a: derive_evidence(a["project"], a["protocol"]),
        {"protocol": {"type": "string", "required": True}}),
    "sctda_refresh": ToolSpec(
        "sctda_refresh", "Re-derive only the claims whose supporting artifact changed; "
        "superseded claims are marked and logged, never deleted.",
        True, True, lambda a: refresh_evidence(a["project"], a["protocol"]),
        {"protocol": {"type": "string", "required": True}}),
    "sctda_dossiers": ToolSpec(
        "sctda_dossiers", "Per-candidate dossiers grouped by evidence quality, with no "
        "single weighted score.", False, True,
        lambda a: build_dossiers(a["project"], a["protocol"]),
        {"protocol": {"type": "string", "required": True}}),
    "sctda_targets": ToolSpec(
        "sctda_targets", "Bind observed genes to a cell group and comparison, then look up "
        "disease associations and compound mechanisms. Stops at the target when direction "
        "is unknown.", True, True,
        lambda a: propose_targets(a["project"], a["protocol"],
                                  top_per_candidate=int(a.get("top", 5)),
                                  query_external=not a.get("offline", False)),
        {"protocol": {"type": "string", "required": True},
         "top": {"type": "number"}, "offline": {"type": "boolean"}}),
    "sctda_report": ToolSpec(
        "sctda_report", "Build HTML/CSV/PDF deliverables from computed artifacts.",
        True, True,
        lambda a: build_report(a["project"], protocol=a.get("protocol"),
                              formats=a.get("formats", "html,csv")),
        {"protocol": {"type": "string"}, "formats": {"type": "string"}}),
    "sctda_probe_resources": ToolSpec(
        "sctda_probe_resources", "Contact each external resource and record what came back, "
        "including failure.", True, True,
        lambda a: probe_all(a["project"]), {}),
    "sctda_check_freeze": ToolSpec(
        "sctda_check_freeze", "Verify a frozen plan still matches its recorded hash "
        "(detects out-of-band edits).", False, True,
        lambda a: _with_conn(a, lambda conn: freeze_check(conn, a["protocol"])),
        {"protocol": {"type": "string", "required": True}}),

    # -- research-process state: runs, entries, delegations, recovery ---------
    "sctda_research_start": ToolSpec(
        "sctda_research_start",
        "Open or resume a versioned research run for a question; this is the "
        "bookkeeping root for entries and delegations, not an analysis protocol. "
        "Call it first, record questions and observations with `sctda_entry_add`, "
        "and close it with `sctda_research_finish` or `sctda_research_fail`; "
        "delegated children are recorded by the DSH host, not by the model. It "
        "runs no analysis. Retrying with the same "
        "dedup_key returns the same run instead of opening a second one.",
        True, True, _research_start,
        {"question": {"type": "string", "required": True,
                      "description": "The research question this run exists to answer."},
         "version": {"type": "integer",
                     "description": "Positive run version; omit for max+1."},
         "dedup_key": {"type": "string",
                       "description": "Retry identity: same key and question returns the existing run."},
         "run_id": {"type": "string",
                    "description": "Caller-chosen run id for retry idempotency."},
         "parent_session_id": {"type": "string",
                               "description": "DSH session that owns this run."},
         "rebind_parent_session": {"type": "boolean",
                                   "description": "Allow a restarted session to take over an existing run; off by default."}},
        roles=("lead",)),
    "sctda_research_get": ToolSpec(
        "sctda_research_get",
        "Read one research run: question, version, status, heartbeat and "
        "open/total counts. `stale_after_seconds` and `as_of_utc` report a run "
        "whose parent stopped advancing. Read-only; it changes no state.",
        False, True, _research_get,
        {"run_id": {"type": "string", "required": True},
         "stale_after_seconds": {"type": "number",
                                 "description": "Report staleness past this many seconds."},
         "as_of_utc": {"type": "string",
                        "description": "ISO-8601 clock override for replay and tests."}},
        roles=("lead", "worker")),
    "sctda_research_list": ToolSpec(
        "sctda_research_list",
        "List research runs newest version first with open/total counts, "
        "optionally filtered by status; paginate with limit/offset. Read-only.",
        False, True, _research_list,
        {"status": {"type": "string", "enum": list(rs.RUN_STATUSES)},
         "limit": {"type": "integer", "description": "1..1000; default 50."},
         "offset": {"type": "integer", "description": "Non-negative; default 0."},
         "stale_after_seconds": {"type": "number"},
         "as_of_utc": {"type": "string"}},
        roles=("lead", "worker")),
    "sctda_research_finish": ToolSpec(
        "sctda_research_finish",
        "Close a running research run as completed with an optional JSON "
        "outcome. It refuses while a pending or running delegation exists: "
        "complete or fail those first. Open entries do not block closing and "
        "their count is returned.",
        True, True, _research_finish,
        {"run_id": {"type": "string", "required": True},
         "outcome": {"type": "object", "description": "JSON-serialisable closing record."}},
        roles=("lead",)),
    "sctda_research_fail": ToolSpec(
        "sctda_research_fail",
        "Close a research run as failed with the actual error text; pending and "
        "running delegations are cancelled in the same transaction. Do not use "
        "this to hide a run whose work could still be resumed.",
        True, True, _research_fail,
        {"run_id": {"type": "string", "required": True},
         "error": {"type": "string", "required": True},
         "outcome": {"type": "object"}},
        roles=("lead",)),
    "sctda_research_heartbeat": ToolSpec(
        "sctda_research_heartbeat",
        "Record that the parent is still advancing a running research run. It "
        "affects staleness reporting only and changes no scientific result.",
        True, True, _research_heartbeat,
        {"run_id": {"type": "string", "required": True}},
        roles=("lead",)),

    "sctda_entry_add": ToolSpec(
        "sctda_entry_add",
        "Append one traceable entry to a running research run: kind, statement, "
        "evidence source class, optional JSON details and provenance. Call after "
        "`sctda_research_start`; link it to other entries with "
        "`sctda_entry_link`. `dedup_key` makes a retried append return the same "
        "entry instead of duplicating it.",
        True, True, _entry_add,
        {"run_id": {"type": "string", "required": True},
         "kind": {"type": "string", "required": True, "enum": list(rs.ENTRY_KINDS)},
         "statement": {"type": "string", "required": True},
         "source_type": {"type": "string", "enum": list(EVIDENCE_SOURCES),
                         "description": "Defaults to model_hypothesis; own computation is current_data."},
         "source_ref": {"type": "string", "description": "PMID, artifact id or other locator."},
         "source_details": {"type": "object"},
         "details": {"type": "object"},
         "status": {"type": "string", "enum": list(rs.ENTRY_STATUSES),
                    "description": "Defaults to open."},
         "author": {"type": "string"},
         "delegation_id": {"type": "string",
                           "description": "Attribute the entry to the DSH child that produced it."},
         "dedup_key": {"type": "string"},
         "entry_id": {"type": "string"}},
        roles=("lead", "worker")),
    "sctda_entry_get": ToolSpec(
        "sctda_entry_get",
        "Read one research entry with both directions of its typed links and "
        "its run status. Read-only.",
        False, True, _entry_get,
        {"entry_id": {"type": "string", "required": True}},
        roles=("lead", "worker")),
    "sctda_entries": ToolSpec(
        "sctda_entries",
        "List a research run's entries in creation order with their links, "
        "filtered by kind, status and producing delegation; paginate with "
        "limit/offset. Read-only.",
        False, True, _entries,
        {"run_id": {"type": "string", "required": True},
         "kind": {"type": "string", "enum": list(rs.ENTRY_KINDS)},
         "status": {"type": "string", "enum": list(rs.ENTRY_STATUSES)},
         "delegation_id": {"type": "string",
                           "description": "Only entries attributed to this child."},
         "limit": {"type": "integer", "description": "1..1000; default 100."},
         "offset": {"type": "integer", "description": "Non-negative; default 0."}},
        roles=("lead", "worker")),
    "sctda_entry_set_status": ToolSpec(
        "sctda_entry_set_status",
        "Transition an entry's status inside a running run (for example open -> "
        "supported, refuted or resolved). Retired and superseded are terminal. "
        "It records an optional note and does not edit the statement.",
        True, True, _entry_set_status,
        {"entry_id": {"type": "string", "required": True},
         "status": {"type": "string", "required": True, "enum": list(rs.ENTRY_STATUSES)},
         "note": {"type": "string"}},
        roles=("lead",)),
    "sctda_entry_link": ToolSpec(
        "sctda_entry_link",
        "Add a typed directed link between two entries of the same run. "
        "Self-links are refused and a duplicate edge is returned rather than "
        "duplicated.",
        True, True, _entry_link,
        {"run_id": {"type": "string", "required": True},
         "from_entry_id": {"type": "string", "required": True},
         "to_entry_id": {"type": "string", "required": True},
         "relation": {"type": "string", "required": True, "enum": list(rs.LINK_RELATIONS)},
         "note": {"type": "string"}},
        roles=("lead",)),

    "sctda_delegation_create": ToolSpec(
        "sctda_delegation_create",
        "Internal/operator bookkeeping: record a pending delegation before a "
        "DSH child exists, with its run, worker role, task and optional DSH "
        "metadata. It does not start, resume or await a subagent, and it is "
        "never registered with a model. DSH hosts create the row through "
        "`sctda_subagent_start`; retrying with the same dedup_key returns the "
        "same delegation.",
        True, True, _delegation_create,
        {"run_id": {"type": "string", "required": True},
         "role": {"type": "string", "required": True,
                  "description": "Worker role label, e.g. explorer or reviewer."},
         "task": {"type": "string", "required": True},
         "dedup_key": {"type": "string",
                       "description": "Spawn-retry identity within the run."},
         "request": {"type": "object", "description": "What the child was asked to do, as JSON."},
         "delegation_id": {"type": "string"},
         "parent_session_id": {"type": "string",
                               "description": "DSH session that owns the run; recorded if unset."},
         "dsh_run_id": {"type": "string"},
         "provider": {"type": "string"},
         "label": {"type": "string"},
         "task_prompt": {"type": "string"},
         "rebind_parent_session": {"type": "boolean",
                                   "description": "Allow a restarted session to claim the run; off by default."}},
        internal=True, roles=("lead",)),
    "sctda_delegation_claim": ToolSpec(
        "sctda_delegation_claim",
        "Internal/operator bookkeeping: bind a pending delegation to the DSH "
        "child and session it actually produced and start its lease; re-claiming "
        "with the same child extends the lease. A different child may take over "
        "only after the previous lease expired, which increments attempt and "
        "returns reclaimed=true. The takeover window uses the same staleness "
        "predicate as the read tools: pass `stale_after_seconds` to include the "
        "heartbeat window, otherwise only the lease is checked. It does not "
        "contact or start the child and is never registered with a model; DSH "
        "hosts bind ids with `sctda_subagent_start` or `sctda_subagent_attach`.",
        True, True, _delegation_claim,
        {"delegation_id": {"type": "string", "required": True},
         "child_id": {"type": "string", "required": True,
                      "description": "DSH child activation id returned by tool-subagent."},
         "session_id": {"type": "string", "description": "DSH session id when known."},
         "lease_seconds": {"type": "integer", "description": "Lease length; default 900."},
         "stale_after_seconds": {"type": "number",
                                 "description": "Also honor heartbeat age when deciding takeover."},
         "as_of_utc": {"type": "string"}},
        internal=True, roles=("lead",)),
    "sctda_delegation_heartbeat": ToolSpec(
        "sctda_delegation_heartbeat",
        "Internal bookkeeping: extend a running delegation's lease while its "
        "child is alive. The plugin calls it on a timer; it affects staleness "
        "detection only and is never registered with a model.",
        True, True, _delegation_heartbeat,
        {"delegation_id": {"type": "string", "required": True},
         "lease_seconds": {"type": "integer", "description": "Lease length; default 900."}},
        internal=True, roles=("lead",)),
    "sctda_delegation_complete": ToolSpec(
        "sctda_delegation_complete",
        "Internal/operator bookkeeping: record a delegation's actual result "
        "(required, non-null JSON) and mark it completed. Repeating with the "
        "identical result is safe; a different result is refused. DSH hosts "
        "record end events with `sctda_subagent_end`; this is never registered "
        "with a model.",
        True, True, _delegation_complete,
        {"delegation_id": {"type": "string", "required": True},
         "result": {"type": "json", "required": True},
         "child_id": {"type": "string"},
         "session_id": {"type": "string"}},
        internal=True, roles=("lead",)),
    "sctda_delegation_fail": ToolSpec(
        "sctda_delegation_fail",
        "Internal/operator bookkeeping: record a delegation's actual error text "
        "and mark it failed; repeating with the identical error is safe. A "
        "missing result is never recorded as success. DSH hosts record end "
        "events with `sctda_subagent_end`; this is never registered with a model.",
        True, True, _delegation_fail,
        {"delegation_id": {"type": "string", "required": True},
         "error": {"type": "string", "required": True},
         "child_id": {"type": "string"},
         "session_id": {"type": "string"}},
        internal=True, roles=("lead",)),
    "sctda_delegations": ToolSpec(
        "sctda_delegations",
        "List delegations with status, attempt, child/session ids, lease "
        "staleness and parsed request/result, filtered by status. With no run_id "
        "every delegation in the project is listed, newest run first; finished "
        "rows are included unless include_finished=false. Read-only.",
        False, True, _delegations,
        {"run_id": {"type": "string",
                    "description": "Omit to cover every run in the project."},
         "status": {"type": "string", "enum": list(rs.DELEGATION_STATUSES)},
         "include_finished": {"type": "boolean"},
         "limit": {"type": "integer", "description": "1..1000; default 100."},
         "offset": {"type": "integer"},
         "stale_after_seconds": {"type": "number"},
         "as_of_utc": {"type": "string"}},
        roles=("lead",)),

    "sctda_delegation_find": ToolSpec(
        "sctda_delegation_find",
        "Resolve one delegation from whichever DSH id a restart or end event "
        "left: delegation_id, child_id, dsh_run_id, session_id or run_id. An "
        "ambiguous match is an error, never a guess. Read-only.",
        False, True, _delegation_find,
        {"delegation_id": {"type": "string"},
         "child_id": {"type": "string"},
         "dsh_run_id": {"type": "string"},
         "session_id": {"type": "string"},
         "run_id": {"type": "string"}},
        roles=("lead",)),

    "sctda_subagent_start": ToolSpec(
        "sctda_subagent_start",
        "Internal DSH lifecycle handler: record a subagent/start event as a "
        "running delegation, binding an existing pending delegation by "
        "delegation_id or dedup_key, or creating one. Every argument is an event "
        "fact (parent session, provider, DSH run id, child id, label), not a "
        "model self-report. The plugin calls it; it is not registered with a "
        "model and it does not spawn anything.",
        True, True, _subagent_start,
        {"parent_session_id": {"type": "string", "required": True},
         "provider": {"type": "string", "required": True},
         "dsh_run_id": {"type": "string", "required": True},
         "child_id": {"type": "string", "required": True},
         "label": {"type": "string", "required": True},
         "task_prompt": {"type": "string"},
         "session_id": {"type": "string"},
         "run_id": {"type": "string"},
         "role": {"type": "string"},
         "task": {"type": "string"},
         "request": {"type": "object"},
         "dedup_key": {"type": "string"},
         "delegation_id": {"type": "string"},
         "rebind_parent_session": {"type": "boolean",
                                   "description": "Let a restarted session take over the run; off by default."},
         "lease_seconds": {"type": "integer", "description": "Lease length; default 900."},
         "as_of_utc": {"type": "string"}},
        internal=True, roles=("lead",)),
    "sctda_subagent_attach": ToolSpec(
        "sctda_subagent_attach",
        "Internal DSH lifecycle handler: bind later-discovered DSH ids (child, "
        "session, run, parent session, provider, label) to an existing "
        "delegation without changing its result. A pending row that gains a "
        "child id starts running; conflicting non-null ids are refused. The "
        "plugin calls it; it is not registered with a model.",
        True, True, _subagent_attach,
        {"delegation_id": {"type": "string"},
         "child_id": {"type": "string"},
         "dsh_run_id": {"type": "string"},
         "session_id": {"type": "string"},
         "parent_session_id": {"type": "string"},
         "provider": {"type": "string"},
         "label": {"type": "string"},
         "task_prompt": {"type": "string"},
         "rebind_parent_session": {"type": "boolean",
                                   "description": "Let a restarted session take over the owning run; off by default."},
         "lease_seconds": {"type": "integer", "description": "Lease length; default 900."},
         "as_of_utc": {"type": "string"}},
        internal=True, roles=("lead",)),
    "sctda_subagent_end": ToolSpec(
        "sctda_subagent_end",
        "Internal DSH lifecycle handler: record a subagent/end event by "
        "whichever id it carries, writing status, result/output hash/excerpt, or "
        "the error. completed needs a result or output_excerpt; failed needs an "
        "error; lost and cancelled keep the reason. The plugin calls it; it is "
        "not registered with a model.",
        True, True, _subagent_end,
        {"delegation_id": {"type": "string"},
         "child_id": {"type": "string"},
         "dsh_run_id": {"type": "string"},
         "session_id": {"type": "string"},
         "status": {"type": "string",
                    "enum": ["settled", "completed", "failed", "lost", "cancelled"],
                    "description": "Defaults to completed."},
         "stop_reason": {"type": "string"},
         "result": {"type": "json"},
         "error": {"type": "string"},
         "output_sha256": {"type": "string"},
         "output_excerpt": {"type": "string"},
         "provider": {"type": "string"},
         "label": {"type": "string"},
         "task_prompt": {"type": "string"}},
        internal=True, roles=("lead",)),

    "sctda_resume": ToolSpec(
        "sctda_resume",
        "Recovery query: return every running research run (all runs with "
        "include_finished) with its open entries and open delegations, marking a "
        "delegation stale when its lease or heartbeat exceeded "
        "stale_after_seconds. Read this first after a restart. It only reports: "
        "a stale child is reconciled by the DSH host's delegation path "
        "(`scope_delegation_status` with reconcile=true), never by re-creating "
        "or manually claiming a row. It never mutates state or restarts work.",
        False, True, _resume,
        {"run_id": {"type": "string",
                    "description": "Omit to cover every running run."},
         "stale_after_seconds": {"type": "number"},
         "include_finished": {"type": "boolean"},
         "entry_limit": {"type": "integer", "description": "1..1000; default 200."},
         "delegation_limit": {"type": "integer", "description": "1..1000; default 200."},
         "as_of_utc": {"type": "string"}},
        roles=("lead",)),
}


def _with_conn(args: dict, fn) -> dict:
    proj = Project(Path(args["project"])).ensure()
    conn = proj.connect()
    try:
        return fn(conn)
    finally:
        conn.close()


def _dataset_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT dataset_id FROM dataset LIMIT 1").fetchone()
    if not row:
        raise RuntimeError("no dataset imported")
    return row["dataset_id"]


def tool_manifest() -> list[dict]:
    return [{"name": t.name, "summary": t.summary, "mutating": t.mutating,
             "requires_project": t.requires_project,
             "requires_initialised_project": t.requires_initialised_project,
             "parameters": t.parameters,
             "internal": t.internal, "roles": list(t.roles)}
            for t in TOOLS.values()]


def invoke_tool(name: str, arguments: dict[str, Any] | None = None) -> dict:
    """The single entry point every caller uses."""
    args = dict(arguments or {})
    spec = TOOLS.get(name)
    if spec is None:
        return {"status": "error", "error": f"unknown tool {name!r}",
                "available": sorted(TOOLS)}
    for pname, pspec in spec.parameters.items():
        if pspec.get("required") and pname not in args:
            return {"status": "error", "error": f"{name}: missing required {pname!r}"}
    if spec.requires_project and not args.get("project"):
        return {"status": "error", "error": f"{name}: 'project' is required"}
    # Defense in depth for the plugin's `requires_initialised_project` policy:
    # a project-opening tool would otherwise create `project.db` on its way in.
    # This check never creates the project it is testing.
    if spec.requires_initialised_project and args.get("project"):
        root = Path(str(args["project"]))
        if not (root / "project.db").exists():
            return {"status": "error", "tool": name,
                    "error_kind": "uninitialised_project",
                    "error": f"{name}: project {str(root)!r} is not initialised "
                             "(no project.db); this tool records into an existing "
                             "project and never creates one. Run sctda_import first."}
    try:
        out = spec.handler(args)
    except Exception as exc:                                  # noqa: BLE001
        return {"status": "error", "tool": name, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(out, dict):
        out = {"status": "ok", "result": out}
    out.setdefault("_tool", name)
    out.setdefault("_mutating", spec.mutating)
    return out
