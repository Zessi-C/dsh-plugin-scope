"""Persistent research-process state for a project.

The database is the recovery boundary, not the chat transcript. One
`research_run` holds the question and its version; every hypothesis, competing
explanation, observation, gap and decision is an entry with its provenance and
typed links; every delegated subagent is a row carrying the DSH child/session
id, its attempt, lease and result. A parent that restarts reads these rows and
knows exactly what finished, what is still open, and which child owns it.

Three rules are enforced here rather than left to callers:

- Terminal and open are never conflated. `completed` is a run that closed with
  no pending/running delegation; a delegation is finished only when it carries
  a result (`completed`) or an error (`failed`, `lost`, `cancelled`); an entry
  is open until its status says otherwise. `is_open` / `is_terminal` are
  computed on every read.
- Mutations are serialised. Writes take `BEGIN IMMEDIATE` on a connection with
  a busy timeout, validate inside the transaction and commit atomically; reads
  take a deferred transaction so a recovery snapshot cannot straddle a commit.
  Foreign keys tie every row to a run, and every lookup also verifies that the
  run belongs to the project that opened the connection.
- DSH child events have a direct adapter: `start_subagent` records a
  `subagent/start` event as a running delegation bound to the parent session,
  and `settle_delegation` records a `subagent/end` event (including `lost` when
  a child vanished). `recover_run` reports what is still open and which leases
  are stale; it never guesses that a silent child succeeded.

The public functions raise domain exceptions instead of returning a failed
status: `ValidationError`, `NotFoundError`, `ConflictError`,
`InvalidTransitionError`, all subclasses of `ResearchStateError`. This module
never updates a table that existed before it: the four research tables are
additive in `db.SCHEMA`.

`as_of_utc` on the read/claim functions is an explicit clock override for
replay and tests; it never changes data other than the timestamps the call
itself writes.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .db import EVIDENCE_SOURCES
from .project import Project, get_or_create_project, new_id

RUN_STATUSES = ("running", "completed", "failed")
TERMINAL_RUN_STATUSES = ("completed", "failed")
ENTRY_KINDS = ("question", "hypothesis", "competing_explanation", "observation",
               "gap", "decision")
ENTRY_STATUSES = ("open", "supported", "refuted", "resolved", "retired",
                  "superseded")
ENTRY_TERMINAL_STATUSES = ("retired", "superseded")
LINK_RELATIONS = ("addresses", "supports", "refutes", "competes_with", "raises",
                  "answers", "informs", "supersedes", "derived_from")
DELEGATION_STATUSES = ("pending", "running", "completed", "failed", "cancelled",
                       "lost")
OPEN_DELEGATION_STATUSES = ("pending", "running")
TERMINAL_DELEGATION_STATUSES = ("completed", "failed", "cancelled", "lost")
DEFAULT_LEASE_SECONDS = 900
MAX_PAGE = 1000


class ResearchStateError(RuntimeError):
    """Base class for every rejected research-state operation."""


class ValidationError(ResearchStateError):
    """An argument is missing, of the wrong type, or outside its enum."""


class NotFoundError(ResearchStateError):
    """The addressed row does not exist in the project that was opened."""


class ConflictError(ResearchStateError):
    """A unique key or idempotency key already names a different payload."""


class InvalidTransitionError(ResearchStateError):
    """The requested transition is not legal from the current state."""


# --------------------------------------------------------------------- helpers
def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string or null")
    value = value.strip()
    return value or None


def _require_enum(value: Any, allowed: tuple[str, ...], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValidationError(f"{field} must be one of {list(allowed)}; got {value!r}")
    return value


def _require_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"{field} must be a positive integer; got {value!r}")
    return value


def _require_page(limit: Any, offset: Any) -> tuple[int, int]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE:
        raise ValidationError(f"limit must be an integer in 1..{MAX_PAGE}; got {limit!r}")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValidationError(f"offset must be a non-negative integer; got {offset!r}")
    return limit, offset


def _optional_seconds(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValidationError(f"{field} must be a non-negative number or null")
    return value


def _json_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be JSON-serializable: {exc}") from exc


def _loads(text: str | None, field: str) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ResearchStateError(f"stored {field} is corrupt JSON: {exc}") from exc


def _parse_utc(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _clock(as_of_utc: Any = None) -> datetime:
    if as_of_utc is None:
        return datetime.now(timezone.utc)
    if isinstance(as_of_utc, datetime):
        parsed = as_of_utc
    else:
        parsed = _parse_utc(_require_text(as_of_utc, "as_of_utc"))
    if parsed is None:
        raise ValidationError("as_of_utc must be an ISO-8601 timestamp")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _deadline(seconds: int, base: datetime | None = None) -> str:
    return _iso((base or datetime.now(timezone.utc)) + timedelta(seconds=seconds))


def _age_exceeds(timestamp: str | None, as_of: datetime, seconds: float) -> bool:
    parsed = _parse_utc(timestamp)
    if parsed is None:
        return True
    return (as_of - parsed).total_seconds() >= seconds


def _run_is_stale(row: sqlite3.Row, as_of: datetime,
                  stale_after_seconds: float | None) -> bool:
    if stale_after_seconds is None or row["status"] != "running":
        return False
    return _age_exceeds(row["heartbeat_utc"] or row["updated_utc"], as_of,
                        stale_after_seconds)


def _delegation_is_stale(row: sqlite3.Row, as_of: datetime,
                         stale_after_seconds: float | None) -> bool:
    if row["status"] != "running":
        return False
    lease = _parse_utc(row["lease_expires_utc"])
    if lease is None:
        # Running without a lease is unowned: reclaim must remain possible.
        return True
    if as_of >= lease:
        return True
    if stale_after_seconds is not None:
        beat = row["heartbeat_utc"] or row["started_utc"] or row["updated_utc"]
        return _age_exceeds(beat, as_of, stale_after_seconds)
    return False


@contextmanager
def _open(project_dir: str | Path) -> Iterator[tuple[Project, sqlite3.Connection, str]]:
    proj = Project(Path(project_dir)).ensure()
    conn = proj.connect()
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        project_id = get_or_create_project(conn, proj.root)
        yield proj, conn, project_id
    finally:
        conn.close()


@contextmanager
def _write(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


@contextmanager
def _read(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN")
    try:
        yield
    finally:
        conn.rollback()


def _run_row(conn: sqlite3.Connection, project_id: str,
             run_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM research_run WHERE run_id=? AND project_id=?",
        (run_id, project_id)).fetchone()
    if row is None:
        raise NotFoundError(f"research run {run_id!r} not found in this project")
    return row


def _entry_row(conn: sqlite3.Connection, project_id: str,
               entry_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT e.*, r.project_id AS project_id, r.status AS run_status "
        "FROM research_entry e JOIN research_run r ON r.run_id=e.run_id "
        "WHERE e.entry_id=?", (entry_id,)).fetchone()
    if row is None or row["project_id"] != project_id:
        raise NotFoundError(f"research entry {entry_id!r} not found in this project")
    return row


def _delegation_row(conn: sqlite3.Connection, project_id: str,
                    delegation_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT d.*, r.project_id AS project_id, r.status AS run_status, "
        "(SELECT COUNT(*) FROM research_entry e "
        " WHERE e.delegation_id=d.delegation_id) AS note_count "
        "FROM research_delegation d JOIN research_run r ON r.run_id=d.run_id "
        "WHERE d.delegation_id=?", (delegation_id,)).fetchone()
    if row is None or row["project_id"] != project_id:
        raise NotFoundError(
            f"research delegation {delegation_id!r} not found in this project")
    return row


def _require_running(run_status: str, run_id: str, action: str) -> None:
    if run_status != "running":
        raise InvalidTransitionError(
            f"cannot {action}: research run {run_id!r} is {run_status!r}, not running")


def _run_dict(row: sqlite3.Row, as_of: datetime,
              stale_after_seconds: float | None = None) -> dict:
    out = dict(row)
    out["outcome"] = _loads(out.pop("outcome_json"), "outcome_json")
    out["is_open"] = out["status"] == "running"
    out["is_terminal"] = out["status"] in TERMINAL_RUN_STATUSES
    out["is_completed"] = out["status"] == "completed"
    out["is_failed"] = out["status"] == "failed"
    out["is_stale"] = _run_is_stale(row, as_of, stale_after_seconds)
    return out


def _entry_dict(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["source"] = {
        "type": out.pop("source_type"),
        "ref": out.pop("source_ref"),
        "details": _loads(out.pop("source_json"), "source_json"),
    }
    out["details"] = _loads(out.pop("details_json"), "details_json")
    out["is_open"] = out["status"] == "open"
    out["is_terminal"] = out["status"] in ENTRY_TERMINAL_STATUSES
    return out


def _delegation_dict(row: sqlite3.Row, as_of: datetime,
                     stale_after_seconds: float | None = None) -> dict:
    out = dict(row)
    out["request"] = _loads(out.pop("request_json"), "request_json")
    out["result"] = _loads(out.pop("result_json"), "result_json")
    out["note_count"] = out.pop("note_count", None)
    out["is_open"] = out["status"] in OPEN_DELEGATION_STATUSES
    out["is_terminal"] = out["status"] in TERMINAL_DELEGATION_STATUSES
    out["is_completed"] = out["status"] == "completed"
    out["is_failed"] = out["status"] == "failed"
    out["is_lost"] = out["status"] == "lost"
    out["is_stale"] = _delegation_is_stale(row, as_of, stale_after_seconds)
    return out


def _links_for_entries(conn: sqlite3.Connection,
                       entry_ids: list[str]) -> dict[str, list[dict]]:
    links: dict[str, list[dict]] = {entry_id: [] for entry_id in entry_ids}
    if not entry_ids:
        return links
    placeholders = ",".join("?" for _ in entry_ids)
    rows = conn.execute(
        f"SELECT * FROM research_entry_link WHERE from_entry_id IN ({placeholders}) "
        f"OR to_entry_id IN ({placeholders}) ORDER BY created_utc, link_id",
        entry_ids + entry_ids).fetchall()
    for row in rows:
        for own, other, direction in ((row["from_entry_id"], row["to_entry_id"], "out"),
                                      (row["to_entry_id"], row["from_entry_id"], "in")):
            if own in links:
                links[own].append({
                    "link_id": row["link_id"],
                    "run_id": row["run_id"],
                    "relation": row["relation"],
                    "note": row["note"],
                    "created_utc": row["created_utc"],
                    "direction": direction,
                    "other_entry_id": other,
                })
    return links


def _entry_with_links(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    entry = _entry_dict(row)
    links = _links_for_entries(conn, [entry["entry_id"]])[entry["entry_id"]]
    detailed = []
    for link in links:
        other = conn.execute(
            "SELECT kind, statement FROM research_entry WHERE entry_id=?",
            (link["other_entry_id"],)).fetchone()
        link = dict(link)
        link["other_kind"] = other["kind"] if other else None
        link["other_statement"] = other["statement"] if other else None
        detailed.append(link)
    entry["links"] = detailed
    entry["run_status"] = row["run_status"]
    return entry


def _link_dict(row: sqlite3.Row) -> dict:
    return {
        "link_id": row["link_id"],
        "run_id": row["run_id"],
        "from_entry_id": row["from_entry_id"],
        "to_entry_id": row["to_entry_id"],
        "relation": row["relation"],
        "note": row["note"],
        "created_utc": row["created_utc"],
    }


def _counts(conn: sqlite3.Connection, run_id: str) -> dict:
    entries = conn.execute(
        "SELECT COUNT(*) c FROM research_entry WHERE run_id=?", (run_id,)
    ).fetchone()["c"]
    open_entries = conn.execute(
        "SELECT COUNT(*) c FROM research_entry WHERE run_id=? AND status='open'",
        (run_id,)).fetchone()["c"]
    delegations = conn.execute(
        "SELECT COUNT(*) c FROM research_delegation WHERE run_id=?", (run_id,)
    ).fetchone()["c"]
    open_delegations = conn.execute(
        "SELECT COUNT(*) c FROM research_delegation WHERE run_id=? "
        "AND status IN ('pending','running')", (run_id,)).fetchone()["c"]
    return {"entries": entries, "open_entries": open_entries,
            "delegations": delegations, "open_delegations": open_delegations}


def _delegation_outcomes(conn: sqlite3.Connection, run_id: str) -> dict:
    return {r["status"]: r["c"] for r in conn.execute(
        "SELECT status, COUNT(*) c FROM research_delegation WHERE run_id=? "
        "GROUP BY status", (run_id,))}


def _check_same_payload(existing: sqlite3.Row, payload: dict, what: str) -> None:
    for key, value in payload.items():
        if existing[key] != value:
            raise ConflictError(
                f"{what} idempotency key already names a different {key}: "
                f"existing={existing[key]!r}, requested={value!r}")


# ------------------------------------------------------------------------ runs
def start_run(project_dir: str | Path, question: str, *, version: int | None = None,
              dedup_key: str | None = None, run_id: str | None = None,
              parent_session_id: str | None = None,
              rebind_parent_session: bool = False) -> dict:
    """Open a versioned research run, or return the identical existing one.

    `dedup_key` (per project) and an explicit `run_id` are retry identities: a
    repeated call with the same question returns `created=False` instead of
    opening a second run. A different question under the same identity is a
    `ConflictError`. `parent_session_id` is the DSH session that owns the run;
    it lets `start_subagent` bind child events to the run without a model
    self-report. A restarted session that reopens the same run passes
    `rebind_parent_session=True`; without it, a different non-null parent is
    refused instead of being stolen.
    """
    question = _require_text(question, "question")
    if version is not None:
        _require_positive_int(version, "version")
    dedup_key = _optional_text(dedup_key, "dedup_key")
    run_id = _optional_text(run_id, "run_id")
    parent_session_id = _optional_text(parent_session_id, "parent_session_id")

    with _open(project_dir) as (_proj, conn, project_id):
        with _write(conn):
            if dedup_key is not None:
                existing = conn.execute(
                    "SELECT * FROM research_run WHERE project_id=? AND dedup_key=?",
                    (project_id, dedup_key)).fetchone()
                if existing is not None:
                    if existing["question"] != question:
                        raise ConflictError(
                            f"dedup_key {dedup_key!r} already belongs to run "
                            f"{existing['run_id']!r} with a different question")
                    if version is not None and existing["version"] != version:
                        raise ConflictError(
                            f"dedup_key {dedup_key!r} already belongs to version "
                            f"{existing['version']}, not {version}")
                    if run_id is not None and existing["run_id"] != run_id:
                        raise ConflictError(
                            f"dedup_key {dedup_key!r} and run_id {run_id!r} name "
                            "different runs")
                    if (parent_session_id is not None and
                            existing["parent_session_id"] is not None and
                            existing["parent_session_id"] != parent_session_id and
                            not rebind_parent_session):
                        raise ConflictError(
                            f"dedup_key {dedup_key!r} already belongs to parent "
                            f"session {existing['parent_session_id']!r}; pass "
                            "rebind_parent_session=true to move it")
                    existing = _bind_run_parent(
                        conn, project_id, existing, parent_session_id,
                        allow_rebind=rebind_parent_session)
                    out = _run_dict(existing, _clock())
                    out["counts"] = _counts(conn, existing["run_id"])
                    return {"status": "ok", "run": out, "created": False}
            if run_id is not None:
                existing = conn.execute(
                    "SELECT * FROM research_run WHERE run_id=?", (run_id,)).fetchone()
                if existing is not None:
                    if existing["project_id"] != project_id or existing["question"] != question:
                        raise ConflictError(
                            f"run_id {run_id!r} already exists in another project "
                            "or with a different question")
                    if version is not None and existing["version"] != version:
                        raise ConflictError(
                            f"run_id {run_id!r} already exists as version "
                            f"{existing['version']}, not {version}")
                    if (parent_session_id is not None and
                            existing["parent_session_id"] is not None and
                            existing["parent_session_id"] != parent_session_id and
                            not rebind_parent_session):
                        raise ConflictError(
                            f"run_id {run_id!r} already belongs to parent session "
                            f"{existing['parent_session_id']!r}; pass "
                            "rebind_parent_session=true to move it")
                    existing = _bind_run_parent(
                        conn, project_id, existing, parent_session_id,
                        allow_rebind=rebind_parent_session)
                    out = _run_dict(existing, _clock())
                    out["counts"] = _counts(conn, run_id)
                    return {"status": "ok", "run": out, "created": False}

            current_max = conn.execute(
                "SELECT COALESCE(MAX(version),0) v FROM research_run WHERE project_id=?",
                (project_id,)).fetchone()["v"]
            if version is None:
                chosen_version = current_max + 1
            else:
                if version <= current_max:
                    raise ConflictError(
                        f"version {version} is not new: project is already at "
                        f"version {current_max}")
                chosen_version = version

            chosen_id = run_id or new_id("run")
            stamp = _iso(_clock())
            try:
                conn.execute(
                    "INSERT INTO research_run(run_id, project_id, version, question, "
                    "status, created_utc, updated_utc, heartbeat_utc, dedup_key, "
                    "parent_session_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (chosen_id, project_id, chosen_version, question, "running",
                     stamp, stamp, stamp, dedup_key, parent_session_id))
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    f"research run could not be created: {exc}") from exc
            row = _run_row(conn, project_id, chosen_id)
            out = _run_dict(row, _clock())
            out["counts"] = _counts(conn, chosen_id)
            return {"status": "ok", "run": out, "created": True}


def get_run(project_dir: str | Path, run_id: str, *,
            stale_after_seconds: float | None = None,
            as_of_utc: Any = None) -> dict:
    """Read one run and its open-work counts; never mutates it."""
    run_id = _require_text(run_id, "run_id")
    stale_after_seconds = _optional_seconds(stale_after_seconds,
                                            "stale_after_seconds")
    as_of = _clock(as_of_utc)
    with _open(project_dir) as (_proj, conn, project_id):
        with _read(conn):
            row = _run_row(conn, project_id, run_id)
            out = _run_dict(row, as_of, stale_after_seconds)
            out["counts"] = _counts(conn, run_id)
            return {"status": "ok", "run": out}


def list_runs(project_dir: str | Path, *, status: str | None = None,
              limit: int = 50, offset: int = 0,
              stale_after_seconds: float | None = None,
              as_of_utc: Any = None) -> dict:
    """List a project's runs, newest version first."""
    if status is not None:
        status = _require_enum(status, RUN_STATUSES, "status")
    limit, offset = _require_page(limit, offset)
    stale_after_seconds = _optional_seconds(stale_after_seconds,
                                            "stale_after_seconds")
    as_of = _clock(as_of_utc)
    with _open(project_dir) as (_proj, conn, project_id):
        with _read(conn):
            clause = " AND status=?" if status is not None else ""
            params: list[Any] = [project_id] + ([status] if status else [])
            total = conn.execute(
                f"SELECT COUNT(*) c FROM research_run WHERE project_id=?{clause}",
                params).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM research_run WHERE project_id=?{clause} "
                "ORDER BY version DESC LIMIT ? OFFSET ?",
                params + [limit, offset]).fetchall()
            runs = []
            for row in rows:
                out = _run_dict(row, as_of, stale_after_seconds)
                out["counts"] = _counts(conn, row["run_id"])
                runs.append(out)
            return {"status": "ok", "runs": runs, "count": len(runs),
                    "total": total}


def finish_run(project_dir: str | Path, run_id: str, *, outcome: Any = None) -> dict:
    """Close a run as completed, refusing while a delegated child is open."""
    run_id = _require_text(run_id, "run_id")
    outcome_json = _json_text(outcome, "outcome")
    with _open(project_dir) as (_proj, conn, project_id):
        with _write(conn):
            row = _run_row(conn, project_id, run_id)
            if row["status"] != "running":
                raise InvalidTransitionError(
                    f"cannot finish research run {run_id!r}: it is {row['status']!r}")
            open_rows = conn.execute(
                "SELECT delegation_id, role, status FROM research_delegation "
                "WHERE run_id=? AND status IN ('pending','running') "
                "ORDER BY created_utc", (run_id,)).fetchall()
            if open_rows:
                detail = ", ".join(
                    f"{r['role']}:{r['delegation_id']}({r['status']})"
                    for r in open_rows)
                raise InvalidTransitionError(
                    "cannot finish research run with open delegations: " + detail)
            open_entries = conn.execute(
                "SELECT COUNT(*) c FROM research_entry WHERE run_id=? AND status='open'",
                (run_id,)).fetchone()["c"]
            stamp = _iso(_clock())
            conn.execute(
                "UPDATE research_run SET status='completed', updated_utc=?, "
                "heartbeat_utc=?, finished_utc=?, outcome_json=COALESCE(?, outcome_json), "
                "error=NULL WHERE run_id=?", (stamp, stamp, stamp, outcome_json, run_id))
            row = _run_row(conn, project_id, run_id)
            out = _run_dict(row, _clock())
            out["counts"] = _counts(conn, run_id)
            outcomes = _delegation_outcomes(conn, run_id)
            return {"status": "ok", "run": out, "changed": True,
                    "open_entries": open_entries,
                    "delegation_outcomes": outcomes,
                    "incomplete_delegations": outcomes.get("failed", 0) +
                    outcomes.get("lost", 0)}


def fail_run(project_dir: str | Path, run_id: str, *, error: str,
             outcome: Any = None) -> dict:
    """Close a run as failed and cancel its open delegations atomically."""
    run_id = _require_text(run_id, "run_id")
    error = _require_text(error, "error")
    outcome_json = _json_text(outcome, "outcome")
    with _open(project_dir) as (_proj, conn, project_id):
        with _write(conn):
            row = _run_row(conn, project_id, run_id)
            if row["status"] != "running":
                raise InvalidTransitionError(
                    f"cannot fail research run {run_id!r}: it is {row['status']!r}")
            stamp = _iso(_clock())
            cancelled = conn.execute(
                "UPDATE research_delegation SET status='cancelled', error=?, "
                "updated_utc=?, finished_utc=?, lease_expires_utc=NULL "
                "WHERE run_id=? AND status IN ('pending','running')",
                (f"research run failed before this delegation completed: {error}",
                 stamp, stamp, run_id)).rowcount
            conn.execute(
                "UPDATE research_run SET status='failed', updated_utc=?, "
                "heartbeat_utc=?, finished_utc=?, outcome_json=COALESCE(?, outcome_json), "
                "error=? WHERE run_id=?",
                (stamp, stamp, stamp, outcome_json, error, run_id))
            row = _run_row(conn, project_id, run_id)
            out = _run_dict(row, _clock())
            out["counts"] = _counts(conn, run_id)
            return {"status": "ok", "run": out, "changed": True,
                    "cancelled_delegations": cancelled}


def heartbeat_run(project_dir: str | Path, run_id: str) -> dict:
    """Record that the parent is still advancing a running run."""
    run_id = _require_text(run_id, "run_id")
    with _open(project_dir) as (_proj, conn, project_id):
        with _write(conn):
            row = _run_row(conn, project_id, run_id)
            if row["status"] != "running":
                raise InvalidTransitionError(
                    f"cannot heartbeat research run {run_id!r}: it is {row['status']!r}")
            stamp = _iso(_clock())
            conn.execute(
                "UPDATE research_run SET heartbeat_utc=?, updated_utc=? WHERE run_id=?",
                (stamp, stamp, run_id))
            row = _run_row(conn, project_id, run_id)
            out = _run_dict(row, _clock())
            out["counts"] = _counts(conn, run_id)
            return {"status": "ok", "run": out, "changed": True}


# --------------------------------------------------------------------- entries
def append_entry(project_dir: str | Path, run_id: str, kind: str, statement: str,
                 *, source_type: str = "model_hypothesis",
                 source_ref: str | None = None, source_details: Any = None,
                 details: Any = None, status: str = "open", author: str | None = None,
                 delegation_id: str | None = None, dedup_key: str | None = None,
                 entry_id: str | None = None) -> dict:
    """Append one traceable item to a running research run.

    `dedup_key` makes a retried append idempotent: the same key with the same
    immutable payload returns the existing entry; a different payload is a
    `ConflictError`. Status is deliberately excluded from that comparison so a
    retry after a status transition still resolves to the same entry.
    `delegation_id` attributes the item to the child that produced it.
    """
    run_id = _require_text(run_id, "run_id")
    kind = _require_enum(kind, ENTRY_KINDS, "kind")
    statement = _require_text(statement, "statement")
    status = _require_enum(status, ENTRY_STATUSES, "status")
    source_type = _require_enum(source_type, EVIDENCE_SOURCES, "source_type")
    source_ref = _optional_text(source_ref, "source_ref")
    source_json = _json_text(source_details, "source_details")
    details_json = _json_text(details, "details")
    author = _optional_text(author, "author")
    delegation_id = _optional_text(delegation_id, "delegation_id")
    dedup_key = _optional_text(dedup_key, "dedup_key")
    entry_id = _optional_text(entry_id, "entry_id")
    payload = {"kind": kind, "statement": statement, "source_type": source_type,
               "source_ref": source_ref, "source_json": source_json,
               "details_json": details_json, "author": author,
               "delegation_id": delegation_id}
    with _open(project_dir) as (_proj, conn, project_id):
        with _write(conn):
            run = _run_row(conn, project_id, run_id)
            _require_running(run["status"], run_id, "append an entry")
            if delegation_id is not None:
                delegation = _delegation_row(conn, project_id, delegation_id)
                if delegation["run_id"] != run_id:
                    raise ValidationError(
                        f"delegation {delegation_id!r} belongs to another run")
            if dedup_key is not None:
                existing = conn.execute(
                    "SELECT * FROM research_entry WHERE run_id=? AND dedup_key=?",
                    (run_id, dedup_key)).fetchone()
                if existing is not None:
                    _check_same_payload(existing, payload, "entry")
                    existing = _entry_row(conn, project_id, existing["entry_id"])
                    return {"status": "ok",
                            "entry": _entry_with_links(conn, existing),
                            "created": False}
            if entry_id is not None:
                existing = conn.execute(
                    "SELECT * FROM research_entry WHERE entry_id=?",
                    (entry_id,)).fetchone()
                if existing is not None:
                    if existing["run_id"] != run_id:
                        raise ConflictError(
                            f"entry_id {entry_id!r} already exists in another run")
                    _check_same_payload(existing, payload, "entry")
                    existing = _entry_row(conn, project_id, existing["entry_id"])
                    return {"status": "ok",
                            "entry": _entry_with_links(conn, existing),
                            "created": False}
            chosen_id = entry_id or new_id("ent")
            stamp = _iso(_clock())
            try:
                conn.execute(
                    "INSERT INTO research_entry(entry_id, run_id, delegation_id, kind, "
                    "statement, status, source_type, source_ref, source_json, "
                    "details_json, author, dedup_key, created_utc, updated_utc) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (chosen_id, run_id, delegation_id, kind, statement, status,
                     source_type, source_ref, source_json, details_json, author,
                     dedup_key, stamp, stamp))
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"research entry could not be appended: {exc}") from exc
            row = _entry_row(conn, project_id, chosen_id)
            return {"status": "ok", "entry": _entry_with_links(conn, row),
                    "created": True}


def get_entry(project_dir: str | Path, entry_id: str) -> dict:
    """Read one entry with both directions of its typed links."""
    entry_id = _require_text(entry_id, "entry_id")
    with _open(project_dir) as (_proj, conn, project_id):
        with _read(conn):
            row = _entry_row(conn, project_id, entry_id)
            return {"status": "ok", "entry": _entry_with_links(conn, row)}


def list_entries(project_dir: str | Path, run_id: str, *, kind: str | None = None,
                 status: str | None = None, delegation_id: str | None = None,
                 limit: int = 100, offset: int = 0) -> dict:
    """List a run's entries, with links, in creation order."""
    run_id = _require_text(run_id, "run_id")
    if kind is not None:
        kind = _require_enum(kind, ENTRY_KINDS, "kind")
    if status is not None:
        status = _require_enum(status, ENTRY_STATUSES, "status")
    delegation_id = _optional_text(delegation_id, "delegation_id")
    limit, offset = _require_page(limit, offset)
    with _open(project_dir) as (_proj, conn, project_id):
        with _read(conn):
            run = _run_row(conn, project_id, run_id)
            if delegation_id is not None:
                delegation = _delegation_row(conn, project_id, delegation_id)
                if delegation["run_id"] != run_id:
                    raise ValidationError(
                        f"delegation {delegation_id!r} belongs to another run")
            filters = ["e.run_id=?"]
            params: list[Any] = [run_id]
            if kind is not None:
                filters.append("e.kind=?")
                params.append(kind)
            if status is not None:
                filters.append("e.status=?")
                params.append(status)
            if delegation_id is not None:
                filters.append("e.delegation_id=?")
                params.append(delegation_id)
            where = " AND ".join(filters)
            total = conn.execute(
                f"SELECT COUNT(*) c FROM research_entry e WHERE {where}",
                params).fetchone()["c"]
            rows = conn.execute(
                f"SELECT e.* FROM research_entry e WHERE {where} "
                "ORDER BY e.created_utc, e.rowid LIMIT ? OFFSET ?",
                params + [limit, offset]).fetchall()
            ids = [row["entry_id"] for row in rows]
            link_map = _links_for_entries(conn, ids)
            entries = []
            for row in rows:
                out = _entry_dict(row)
                out["links"] = link_map.get(out["entry_id"], [])
                entries.append(out)
            return {"status": "ok", "run_id": run_id, "run_status": run["status"],
                    "entries": entries, "count": len(entries), "total": total}


def set_entry_status(project_dir: str | Path, entry_id: str, status: str, *,
                     note: str | None = None) -> dict:
    """Transition an entry's status; retired/superseded are terminal."""
    entry_id = _require_text(entry_id, "entry_id")
    status = _require_enum(status, ENTRY_STATUSES, "status")
    note = _optional_text(note, "note")
    with _open(project_dir) as (_proj, conn, project_id):
        with _write(conn):
            row = _entry_row(conn, project_id, entry_id)
            _require_running(row["run_status"], row["run_id"], "update an entry")
            if row["status"] in ENTRY_TERMINAL_STATUSES and row["status"] != status:
                raise InvalidTransitionError(
                    f"entry {entry_id!r} is {row['status']!r}, a terminal status; "
                    f"cannot transition to {status!r}")
            if row["status"] == status:
                if note is not None and note != row["status_note"]:
                    conn.execute(
                        "UPDATE research_entry SET status_note=?, updated_utc=? "
                        "WHERE entry_id=?", (note, _iso(_clock()), entry_id))
                    row = _entry_row(conn, project_id, entry_id)
                    return {"status": "ok", "entry": _entry_with_links(conn, row),
                            "changed": True}
                return {"status": "ok", "entry": _entry_with_links(conn, row),
                        "changed": False}
            conn.execute(
                "UPDATE research_entry SET status=?, status_note=?, updated_utc=? "
                "WHERE entry_id=?", (status, note, _iso(_clock()), entry_id))
            row = _entry_row(conn, project_id, entry_id)
            return {"status": "ok", "entry": _entry_with_links(conn, row),
                    "changed": True}


def link_entries(project_dir: str | Path, run_id: str, from_entry_id: str,
                 to_entry_id: str, relation: str, *, note: str | None = None) -> dict:
    """Add a typed directed relation between two entries of the same run."""
    run_id = _require_text(run_id, "run_id")
    from_entry_id = _require_text(from_entry_id, "from_entry_id")
    to_entry_id = _require_text(to_entry_id, "to_entry_id")
    relation = _require_enum(relation, LINK_RELATIONS, "relation")
    note = _optional_text(note, "note")
    if from_entry_id == to_entry_id:
        raise ValidationError("an entry cannot link to itself")
    with _open(project_dir) as (_proj, conn, project_id):
        with _write(conn):
            run = _run_row(conn, project_id, run_id)
            _require_running(run["status"], run_id, "link entries")
            source = _entry_row(conn, project_id, from_entry_id)
            target = _entry_row(conn, project_id, to_entry_id)
            if source["run_id"] != run_id or target["run_id"] != run_id:
                raise ValidationError(
                    "both entries must belong to the run being linked")
            existing = conn.execute(
                "SELECT * FROM research_entry_link WHERE from_entry_id=? "
                "AND to_entry_id=? AND relation=?",
                (from_entry_id, to_entry_id, relation)).fetchone()
            if existing is not None:
                return {"status": "ok", "link": _link_dict(existing), "created": False}
            link_id = new_id("lnk")
            try:
                conn.execute(
                    "INSERT INTO research_entry_link(link_id, run_id, from_entry_id, "
                    "to_entry_id, relation, note, created_utc) VALUES (?,?,?,?,?,?,?)",
                    (link_id, run_id, from_entry_id, to_entry_id, relation, note,
                     _iso(_clock())))
            except sqlite3.IntegrityError as exc:
                existing = conn.execute(
                    "SELECT * FROM research_entry_link WHERE from_entry_id=? "
                    "AND to_entry_id=? AND relation=?",
                    (from_entry_id, to_entry_id, relation)).fetchone()
                if existing is not None:
                    return {"status": "ok", "link": _link_dict(existing),
                            "created": False}
                raise ConflictError(f"research link could not be created: {exc}") from exc
            row = conn.execute(
                "SELECT * FROM research_entry_link WHERE link_id=?",
                (link_id,)).fetchone()
            return {"status": "ok", "link": _link_dict(row), "created": True}


# ----------------------------------------------------------------- delegations
def create_delegation(project_dir: str | Path, run_id: str, role: str, task: str,
                      *, dedup_key: str | None = None, request: Any = None,
                      delegation_id: str | None = None,
                      parent_session_id: str | None = None,
                      dsh_run_id: str | None = None, provider: str | None = None,
                      label: str | None = None,
                      task_prompt: str | None = None,
                      rebind_parent_session: bool = False) -> dict:
    """Record a pending subagent delegation before its DSH child starts.

    The durable row identity is `delegation_id`; `(run_id, dedup_key)` makes a
    retried spawn return the same delegation instead of starting a second child.
    DSH metadata can be supplied here or later through `attach_subagent`.
    """
    run_id = _require_text(run_id, "run_id")
    role = _require_text(role, "role")
    task = _require_text(task, "task")
    dedup_key = _optional_text(dedup_key, "dedup_key")
    delegation_id = _optional_text(delegation_id, "delegation_id")
    parent_session_id = _optional_text(parent_session_id, "parent_session_id")
    dsh_run_id = _optional_text(dsh_run_id, "dsh_run_id")
    provider = _optional_text(provider, "provider")
    label = _optional_text(label, "label")
    task_prompt = _optional_text(task_prompt, "task_prompt")
    request_json = _json_text(request, "request")
    payload = {"role": role, "task": task, "request_json": request_json,
               "provider": provider, "label": label, "task_prompt": task_prompt}
    with _open(project_dir) as (_proj, conn, project_id):
        with _write(conn):
            run = _run_row(conn, project_id, run_id)
            _require_running(run["status"], run_id, "delegate work")
            run = _bind_run_parent(conn, project_id, run, parent_session_id,
                                   allow_rebind=rebind_parent_session)
            if dedup_key is not None:
                existing = conn.execute(
                    "SELECT * FROM research_delegation WHERE run_id=? AND dedup_key=?",
                    (run_id, dedup_key)).fetchone()
                if existing is not None:
                    _check_same_payload(existing, payload, "delegation")
                    row = _rebind_delegation_parent(
                        conn, project_id, existing, parent_session_id,
                        allow_rebind=rebind_parent_session)
                    return {"status": "ok",
                            "delegation": _delegation_dict(row, _clock()),
                            "created": False}
            if delegation_id is not None:
                existing = conn.execute(
                    "SELECT * FROM research_delegation WHERE delegation_id=?",
                    (delegation_id,)).fetchone()
                if existing is not None:
                    if existing["run_id"] != run_id:
                        raise ConflictError(
                            f"delegation_id {delegation_id!r} already exists in "
                            "another run")
                    _check_same_payload(existing, payload, "delegation")
                    row = _rebind_delegation_parent(
                        conn, project_id, existing, parent_session_id,
                        allow_rebind=rebind_parent_session)
                    return {"status": "ok",
                            "delegation": _delegation_dict(row, _clock()),
                            "created": False}
            chosen_id = delegation_id or new_id("del")
            stamp = _iso(_clock())
            try:
                conn.execute(
                    "INSERT INTO research_delegation(delegation_id, run_id, role, "
                    "task, status, parent_session_id, dsh_run_id, provider, label, "
                    "task_prompt, request_json, dedup_key, created_utc, updated_utc) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (chosen_id, run_id, role, task, "pending", parent_session_id,
                     dsh_run_id, provider, label, task_prompt, request_json,
                     dedup_key, stamp, stamp))
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    f"research delegation could not be created: {exc}") from exc
            row = _delegation_row(conn, project_id, chosen_id)
            return {"status": "ok",
                    "delegation": _delegation_dict(row, _clock()),
                    "created": True}


def _bind_run_parent(conn: sqlite3.Connection, project_id: str,
                     row: sqlite3.Row, parent_session_id: str | None, *,
                     allow_rebind: bool) -> sqlite3.Row:
    """Bind a running run to a session, refusing an implicit steal.

    A null parent may be filled by the first child event. A non-null parent is
    only replaced when the caller sets `rebind_parent_session=True`; a closed
    run is never rebound.
    """
    if parent_session_id is None or row["parent_session_id"] == parent_session_id:
        return row
    if row["parent_session_id"] is not None and not allow_rebind:
        raise ConflictError(
            f"research run {row['run_id']!r} belongs to parent session "
            f"{row['parent_session_id']!r}, not {parent_session_id!r}; "
            "pass rebind_parent_session=true to move it")
    if row["status"] != "running":
        raise InvalidTransitionError(
            f"cannot rebind research run {row['run_id']!r}: it is "
            f"{row['status']!r}, not running")
    stamp = _iso(_clock())
    conn.execute(
        "UPDATE research_run SET parent_session_id=?, heartbeat_utc=?, "
        "updated_utc=? WHERE run_id=?",
        (parent_session_id, stamp, stamp, row["run_id"]))
    return _run_row(conn, project_id, row["run_id"])


def _resolve_run_for_child(conn: sqlite3.Connection, project_id: str,
                           run_id: str | None,
                           parent_session_id: str | None,
                           *, rebind_parent_session: bool = False) -> sqlite3.Row:
    """Find the research run a child event belongs to.

    Explicit `run_id` wins. Otherwise the parent session must own exactly one
    running run; with neither selector the project must have exactly one
    running run. Ambiguity is an error, never a guess. A different non-null
    parent is only replaced when `rebind_parent_session` is true (a restarted
    session that learned the run id); without it the call is refused.
    """
    if run_id is not None:
        row = _run_row(conn, project_id, run_id)
        _require_running(row["status"], run_id, "delegate work")
        return _bind_run_parent(conn, project_id, row, parent_session_id,
                                allow_rebind=rebind_parent_session)
    if parent_session_id is not None:
        rows = conn.execute(
            "SELECT * FROM research_run WHERE project_id=? AND parent_session_id=? "
            "AND status='running' ORDER BY version DESC",
            (project_id, parent_session_id)).fetchall()
        if not rows:
            # A lead may have opened the run without a session id. Adopt only
            # an unbound run; never steal a run owned by another session.
            all_rows = conn.execute(
                "SELECT * FROM research_run WHERE project_id=? AND status='running' "
                "AND parent_session_id IS NULL ORDER BY version DESC",
                (project_id,)).fetchall()
            if len(all_rows) > 1:
                raise ConflictError(
                    "no run is bound to parent session "
                    f"{parent_session_id!r} and {len(all_rows)} unbound runs are "
                    f"running: {[r['run_id'] for r in all_rows]}; pass run_id")
            if all_rows:
                rows = all_rows
            elif rebind_parent_session:
                # A restarted session may adopt the only running run even if
                # the old session still owns it; the explicit flag is the
                # authorisation, and ambiguity is still refused.
                candidates = conn.execute(
                    "SELECT * FROM research_run WHERE project_id=? "
                    "AND status='running' ORDER BY version DESC",
                    (project_id,)).fetchall()
                if len(candidates) > 1:
                    raise ConflictError(
                        "rebind_parent_session is ambiguous: "
                        f"{[r['run_id'] for r in candidates]} are all running; "
                        "pass run_id")
                rows = candidates
            if not rows:
                raise NotFoundError(
                    f"no running research run for parent session "
                    f"{parent_session_id!r}")
            if len(rows) > 1:
                raise ConflictError(
                    "multiple running research runs for parent session "
                    f"{parent_session_id!r}: {[r['run_id'] for r in rows]}; "
                    "pass run_id")
            rows = [_bind_run_parent(conn, project_id, rows[0], parent_session_id,
                                     allow_rebind=True)]
        if len(rows) > 1:
            raise ConflictError(
                "multiple running research runs for parent session "
                f"{parent_session_id!r}: {[r['run_id'] for r in rows]}; pass run_id")
        return rows[0]
    rows = conn.execute(
        "SELECT * FROM research_run WHERE project_id=? AND status='running' "
        "ORDER BY version DESC", (project_id,)).fetchall()
    if not rows:
        raise NotFoundError("no running research run in this project")
    if len(rows) > 1:
        raise ConflictError(
            "multiple running research runs in this project: "
            f"{[r['run_id'] for r in rows]}; pass run_id")
    return rows[0]


def _find_delegation_row(conn: sqlite3.Connection, project_id: str, *,
                         delegation_id: str | None = None,
                         child_id: str | None = None,
                         dsh_run_id: str | None = None,
                         session_id: str | None = None,
                         run_id: str | None = None) -> sqlite3.Row:
    selectors = {"delegation_id": delegation_id, "child_id": child_id,
                 "dsh_run_id": dsh_run_id, "session_id": session_id,
                 "run_id": run_id}
    provided = {key: value for key, value in selectors.items() if value is not None}
    if not provided:
        raise ValidationError(
            "at least one of delegation_id, child_id, dsh_run_id, session_id, "
            "run_id is required")
    clauses = ["r.project_id=?"]
    params: list[Any] = [project_id]
    for column, value in provided.items():
        clauses.append(f"d.{column}=?")
        params.append(value)
    rows = conn.execute(
        "SELECT d.delegation_id FROM research_delegation d "
        "JOIN research_run r ON r.run_id=d.run_id WHERE " + " AND ".join(clauses) +
        " ORDER BY d.created_utc, d.rowid", params).fetchall()
    if not rows:
        raise NotFoundError(
            "no research delegation matches " +
            ", ".join(f"{key}={value!r}" for key, value in provided.items()))
    if len(rows) > 1:
        raise ConflictError(
            "multiple research delegations match " +
            ", ".join(f"{key}={value!r}" for key, value in provided.items()) +
            f": {[r['delegation_id'] for r in rows]}; add a selector")
    return _delegation_row(conn, project_id, rows[0]["delegation_id"])


def _rebind_delegation_parent(conn: sqlite3.Connection, project_id: str,
                              row: sqlite3.Row, parent_session_id: str | None, *,
                              allow_rebind: bool) -> sqlite3.Row:
    """Move a delegation row to a new parent session under an explicit rebind.

    Used by the idempotent retry paths, which must leave the row exactly as it
    was when the caller did not ask for a move.
    """
    if (parent_session_id is None or row["parent_session_id"] == parent_session_id
            or row["parent_session_id"] is None):
        return _delegation_row(conn, project_id, row["delegation_id"])
    if not allow_rebind:
        raise ConflictError(
            f"delegation {row['delegation_id']!r} belongs to parent session "
            f"{row['parent_session_id']!r}, not {parent_session_id!r}; pass "
            "rebind_parent_session=true to move it")
    if row["status"] in TERMINAL_DELEGATION_STATUSES:
        raise InvalidTransitionError(
            f"delegation {row['delegation_id']!r} is {row['status']!r}; it cannot "
            "be rebound")
    conn.execute(
        "UPDATE research_delegation SET parent_session_id=?, updated_utc=? "
        "WHERE delegation_id=?",
        (parent_session_id, _iso(_clock()), row["delegation_id"]))
    return _delegation_row(conn, project_id, row["delegation_id"])


def _locate_delegation(conn: sqlite3.Connection, project_id: str,
                       selectors: dict) -> sqlite3.Row:
    """Locate a delegation for attach/end and reject conflicting selectors.

    The strongest selector is used for the lookup (`delegation_id`, then
    `dsh_run_id`, `session_id`, `child_id`); the others are validated against
    the row. `child_id` may be null on the row because attach is what binds it.
    """
    lookup = None
    for key in ("delegation_id", "dsh_run_id", "session_id", "child_id"):
        if selectors[key] is not None:
            lookup = {key: selectors[key]}
            break
    if lookup is None:
        raise ValidationError("a delegation selector is required")
    row = _find_delegation_row(conn, project_id, **lookup)
    for key in ("delegation_id", "dsh_run_id", "session_id", "child_id"):
        value = selectors[key]
        if value is not None and row[key] not in (None, value):
            raise ConflictError(
                f"delegation {row['delegation_id']!r} already has "
                f"{key}={row[key]!r}, not {value!r}")
    return row


def get_delegation(project_dir: str | Path, delegation_id: str) -> dict:
    """Read one delegation with its DSH ids, lease, result and note count."""
    delegation_id = _require_text(delegation_id, "delegation_id")
    with _open(project_dir) as (_proj, conn, project_id):
        with _read(conn):
            row = _delegation_row(conn, project_id, delegation_id)
            return {"status": "ok",
                    "delegation": _delegation_dict(row, _clock())}


def find_delegation(project_dir: str | Path, *, delegation_id: str | None = None,
                    child_id: str | None = None, dsh_run_id: str | None = None,
                    session_id: str | None = None,
                    run_id: str | None = None) -> dict:
    """Resolve a delegation from whichever DSH id an event carried."""
    selectors = {key: _optional_text(value, key) for key, value in {
        "delegation_id": delegation_id, "child_id": child_id,
        "dsh_run_id": dsh_run_id, "session_id": session_id,
        "run_id": run_id}.items()}
    with _open(project_dir) as (_proj, conn, project_id):
        with _read(conn):
            row = _find_delegation_row(conn, project_id, **selectors)
            return {"status": "ok",
                    "delegation": _delegation_dict(row, _clock())}


def start_subagent(project_dir: str | Path, *, parent_session_id: str,
                   provider: str, dsh_run_id: str, child_id: str,
                   label: str, task_prompt: str | None = None,
                   session_id: str | None = None, run_id: str | None = None,
                   role: str | None = None, task: str | None = None,
                   request: Any = None, dedup_key: str | None = None,
                   delegation_id: str | None = None,
                   rebind_parent_session: bool = False,
                   lease_seconds: int = DEFAULT_LEASE_SECONDS,
                   as_of_utc: Any = None) -> dict:
    """Record a DSH `subagent/start` event as a running delegation.

    Binds to an existing pending delegation selected by `delegation_id` or
    `dedup_key`, otherwise creates one. `parent_session_id`, `provider`,
    `dsh_run_id`, `child_id` and `label` come from the event; `task_prompt` and
    `role` from the spawn call. No model self-report is involved.

    After a restart the new session may pass the learned `run_id` plus
    `rebind_parent_session=True`; the run and the delegation row then carry the
    new session. Without the flag, a different non-null parent is refused.
    """
    parent_session_id = _require_text(parent_session_id, "parent_session_id")
    provider = _require_text(provider, "provider")
    dsh_run_id = _require_text(dsh_run_id, "dsh_run_id")
    child_id = _require_text(child_id, "child_id")
    label = _require_text(label, "label")
    task_prompt = _optional_text(task_prompt, "task_prompt")
    session_id = _optional_text(session_id, "session_id")
    run_id = _optional_text(run_id, "run_id")
    role = _optional_text(role, "role")
    task = _optional_text(task, "task")
    create_role = role or "subagent"
    create_task = task or task_prompt or label
    request_json = _json_text(request, "request")
    dedup_key = _optional_text(dedup_key, "dedup_key")
    delegation_id = _optional_text(delegation_id, "delegation_id")
    _require_positive_int(lease_seconds, "lease_seconds")
    as_of = _clock(as_of_utc)
    stamp = _iso(as_of)
    with _open(project_dir) as (_proj, conn, project_id):
        with _write(conn):
            run = _resolve_run_for_child(conn, project_id, run_id,
                                         parent_session_id,
                                         rebind_parent_session=rebind_parent_session)
            existing = None
            if delegation_id is not None:
                existing = _find_delegation_row(
                    conn, project_id, delegation_id=delegation_id,
                    run_id=run["run_id"])
            if dedup_key is not None:
                found = conn.execute(
                    "SELECT delegation_id FROM research_delegation "
                    "WHERE run_id=? AND dedup_key=?",
                    (run["run_id"], dedup_key)).fetchone()
                if found is not None:
                    via_dedup = _delegation_row(conn, project_id,
                                                found["delegation_id"])
                    if (existing is not None and
                            existing["delegation_id"] != via_dedup["delegation_id"]):
                        raise ConflictError(
                            f"delegation_id {delegation_id!r} and dedup_key "
                            f"{dedup_key!r} name different delegations")
                    existing = via_dedup
            # A retried start event carries the DSH ids even when the caller
            # did not keep delegation_id/dedup_key; resolve those too. Child id
            # is precise; fall back to the subagent run id only when it misses.
            if existing is None:
                try:
                    existing = _find_delegation_row(conn, project_id,
                                                    child_id=child_id)
                except NotFoundError:
                    pass
            if existing is None:
                try:
                    existing = _find_delegation_row(conn, project_id,
                                                    dsh_run_id=dsh_run_id)
                except NotFoundError:
                    pass
            if existing is None:
                chosen_id = delegation_id or new_id("del")
                try:
                    conn.execute(
                        "INSERT INTO research_delegation(delegation_id, run_id, role, "
                        "task, status, child_id, session_id, dsh_run_id, "
                        "parent_session_id, provider, label, task_prompt, "
                        "request_json, dedup_key, created_utc, updated_utc, "
                        "started_utc, heartbeat_utc, lease_expires_utc) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (chosen_id, run["run_id"], create_role, create_task,
                         "running", child_id, session_id, dsh_run_id,
                         parent_session_id, provider, label, task_prompt,
                         request_json, dedup_key, stamp, stamp, stamp, stamp,
                         _deadline(lease_seconds, as_of)))
                except sqlite3.IntegrityError as exc:
                    raise ConflictError(
                        f"research delegation could not be created: {exc}") from exc
                row = _delegation_row(conn, project_id, chosen_id)
                return {"status": "ok",
                        "delegation": _delegation_dict(row, as_of),
                        "created": True, "reclaimed": False}
            if existing["run_id"] != run["run_id"]:
                raise ValidationError(
                    f"delegation {existing['delegation_id']!r} belongs to another run")
            if existing["status"] in TERMINAL_DELEGATION_STATUSES:
                raise InvalidTransitionError(
                    f"delegation {existing['delegation_id']!r} is "
                    f"{existing['status']!r}; a start event cannot reopen it")
            if (existing["parent_session_id"] is not None and
                    existing["parent_session_id"] != parent_session_id and
                    not rebind_parent_session):
                raise ConflictError(
                    f"delegation {existing['delegation_id']!r} belongs to parent "
                    f"session {existing['parent_session_id']!r}; pass "
                    "rebind_parent_session=true to move it")
            for column, value in (
                    ("provider", provider), ("label", label),
                    ("task_prompt", task_prompt),
                    ("role", role), ("task", task)):
                if value is not None and existing[column] not in (None, value):
                    raise ConflictError(
                        f"delegation {existing['delegation_id']!r} already has "
                        f"{column}={existing[column]!r}, not {value!r}")
            if existing["child_id"] not in (None, child_id):
                if not _delegation_is_stale(existing, as_of, None):
                    raise ConflictError(
                        f"delegation {existing['delegation_id']!r} is running under "
                        f"child {existing['child_id']!r} with a live lease")
                conn.execute(
                    "UPDATE research_delegation SET child_id=?, "
                    "session_id=COALESCE(?, session_id), "
                    "dsh_run_id=COALESCE(?, dsh_run_id), "
                    "parent_session_id=?, "
                    "provider=COALESCE(?, provider), label=COALESCE(?, label), "
                    "task_prompt=COALESCE(?, task_prompt), "
                    "role=COALESCE(?, role), task=COALESCE(?, task), "
                    "status='running', attempt=attempt+1, started_utc=?, "
                    "heartbeat_utc=?, lease_expires_utc=?, updated_utc=?, error=NULL "
                    "WHERE delegation_id=?",
                    (child_id, session_id, dsh_run_id, parent_session_id, provider,
                     label, task_prompt, role, task, stamp, stamp,
                     _deadline(lease_seconds, as_of), stamp,
                     existing["delegation_id"]))
                row = _delegation_row(conn, project_id, existing["delegation_id"])
                return {"status": "ok", "delegation": _delegation_dict(row, as_of),
                        "created": False, "reclaimed": True}
            conn.execute(
                "UPDATE research_delegation SET status='running', child_id=?, "
                "session_id=COALESCE(?, session_id), "
                "dsh_run_id=COALESCE(?, dsh_run_id), "
                "parent_session_id=?, "
                "provider=COALESCE(?, provider), label=COALESCE(?, label), "
                "task_prompt=COALESCE(?, task_prompt), role=COALESCE(?, role), "
                "task=COALESCE(?, task), started_utc=COALESCE(started_utc, ?), "
                "heartbeat_utc=?, lease_expires_utc=?, updated_utc=? "
                "WHERE delegation_id=?",
                (child_id, session_id, dsh_run_id, parent_session_id, provider,
                 label, task_prompt, role, task, stamp, stamp,
                 _deadline(lease_seconds, as_of), stamp,
                 existing["delegation_id"]))
            row = _delegation_row(conn, project_id, existing["delegation_id"])
            return {"status": "ok", "delegation": _delegation_dict(row, as_of),
                    "created": False, "reclaimed": False}


def attach_subagent(project_dir: str | Path, *, delegation_id: str | None = None,
                    child_id: str | None = None, dsh_run_id: str | None = None,
                    session_id: str | None = None,
                    parent_session_id: str | None = None,
                    provider: str | None = None, label: str | None = None,
                    task_prompt: str | None = None,
                    rebind_parent_session: bool = False,
                    lease_seconds: int = DEFAULT_LEASE_SECONDS,
                    as_of_utc: Any = None) -> dict:
    """Bind later-discovered DSH ids to a delegation without changing its result.

    A pending delegation with a child id becomes running. Conflicting non-null
    ids are refused rather than overwritten. A restarted session may move the
    owning run and the row to itself with `rebind_parent_session=True`.
    """
    selectors = {"delegation_id": _optional_text(delegation_id, "delegation_id"),
                 "child_id": _optional_text(child_id, "child_id"),
                 "dsh_run_id": _optional_text(dsh_run_id, "dsh_run_id"),
                 "session_id": _optional_text(session_id, "session_id")}
    new_values = {
        "parent_session_id": _optional_text(parent_session_id, "parent_session_id"),
        "provider": _optional_text(provider, "provider"),
        "label": _optional_text(label, "label"),
        "task_prompt": _optional_text(task_prompt, "task_prompt"),
        "dsh_run_id": selectors["dsh_run_id"],
        "session_id": selectors["session_id"],
    }
    _require_positive_int(lease_seconds, "lease_seconds")
    as_of = _clock(as_of_utc)
    stamp = _iso(as_of)
    with _open(project_dir) as (_proj, conn, project_id):
        with _write(conn):
            row = _locate_delegation(conn, project_id, selectors)
            if row["status"] in TERMINAL_DELEGATION_STATUSES:
                raise InvalidTransitionError(
                    f"delegation {row['delegation_id']!r} is {row['status']!r}; "
                    "it cannot be attached")
            _require_running(row["run_status"], row["run_id"], "attach a delegation")
            if new_values["parent_session_id"] is not None:
                run = _run_row(conn, project_id, row["run_id"])
                run = _bind_run_parent(conn, project_id, run,
                                       new_values["parent_session_id"],
                                       allow_rebind=rebind_parent_session)
            if (row["parent_session_id"] is not None and
                    new_values["parent_session_id"] is not None and
                    row["parent_session_id"] != new_values["parent_session_id"]):
                if not rebind_parent_session:
                    raise ConflictError(
                        f"delegation {row['delegation_id']!r} belongs to parent "
                        f"session {row['parent_session_id']!r}; pass "
                        "rebind_parent_session=true to move it")
            updates = {}
            for column, value in new_values.items():
                if value is None:
                    continue
                if column == "parent_session_id" and rebind_parent_session:
                    updates[column] = value
                    continue
                if row[column] not in (None, value):
                    raise ConflictError(
                        f"delegation {row['delegation_id']!r} already has "
                        f"{column}={row[column]!r}, not {value!r}")
                updates[column] = value
            if selectors["child_id"] is not None:
                updates["child_id"] = selectors["child_id"]
            start_now = row["status"] == "pending" and (
                updates.get("child_id") or updates.get("session_id") or
                row["child_id"]) is not None
            if start_now:
                updates.update({"status": "running", "started_utc": stamp,
                                "heartbeat_utc": stamp,
                                "lease_expires_utc": _deadline(lease_seconds, as_of)})
            updates["updated_utc"] = stamp
            assignments = ", ".join(f"{column}=?" for column in updates)
            conn.execute(
                f"UPDATE research_delegation SET {assignments} WHERE delegation_id=?",
                list(updates.values()) + [row["delegation_id"]])
            row = _delegation_row(conn, project_id, row["delegation_id"])
            return {"status": "ok", "delegation": _delegation_dict(row, as_of),
                    "started": start_now}


def end_subagent(project_dir: str | Path, *,
                 delegation_id: str | None = None, child_id: str | None = None,
                 dsh_run_id: str | None = None, session_id: str | None = None,
                 status: str = "completed", stop_reason: str | None = None,
                 result: Any = None, error: str | None = None,
                 output_sha256: str | None = None,
                 output_excerpt: str | None = None,
                 provider: str | None = None, label: str | None = None,
                 task_prompt: str | None = None) -> dict:
    """Record a DSH `subagent/end` event and its actual output.

    `status` accepts the plugin vocabulary: `settled`/`completed` becomes
    `completed`, `failed` stays `failed`, `lost` records a child that vanished
    without an end event, `cancelled` records a deliberate stop. A completion
    needs a structured result or the child's last message; a lost child keeps
    the reason instead of pretending to be complete. Metadata that only became
    known at the end (`provider`, `label`, `task_prompt`, `dsh_run_id`) is
    written only where the column is still null.
    """
    selectors = {"delegation_id": _optional_text(delegation_id, "delegation_id"),
                 "child_id": _optional_text(child_id, "child_id"),
                 "dsh_run_id": _optional_text(dsh_run_id, "dsh_run_id"),
                 "session_id": _optional_text(session_id, "session_id")}
    status = _require_text(status, "status")
    if status in ("settled", "completed"):
        status = "completed"
    elif status not in ("failed", "lost", "cancelled"):
        raise ValidationError(
            "status must be one of ['settled', 'completed', 'failed', 'lost', "
            f"'cancelled']; got {status!r}")
    stop_reason = _optional_text(stop_reason, "stop_reason")
    error = _optional_text(error, "error")
    output_sha256 = _optional_text(output_sha256, "output_sha256")
    output_excerpt = _optional_text(output_excerpt, "output_excerpt")
    provider = _optional_text(provider, "provider")
    label = _optional_text(label, "label")
    task_prompt = _optional_text(task_prompt, "task_prompt")
    if status == "completed" and result is None and output_excerpt is None:
        raise ValidationError(
            "a completed subagent needs a result or an output_excerpt")
    if status == "failed" and error is None:
        raise ValidationError("a failed subagent needs an error")
    result_json = _json_text(result, "result")
    if status == "completed" and result is None:
        result_json = _json_text({"last_assistant_message": output_excerpt},
                                 "result")
    if status in ("lost", "cancelled"):
        error = error or f"{status}: {stop_reason or 'no_end_event'}"
    with _open(project_dir) as (_proj, conn, project_id):
        with _write(conn):
            row = _locate_delegation(conn, project_id, selectors)
            if row["status"] == "completed":
                same_result = row["result_json"] == result_json
                same_output = (output_sha256 is None or
                               row["output_sha256"] in (None, output_sha256)) and \
                              (output_excerpt is None or
                               row["output_excerpt"] in (None, output_excerpt))
                if status == "completed" and same_result and same_output:
                    return {"status": "ok",
                            "delegation": _delegation_dict(row, _clock()),
                            "changed": False}
                raise ConflictError(
                    f"delegation {row['delegation_id']!r} already ended as completed "
                    "with a different result/output")
            if row["status"] in TERMINAL_DELEGATION_STATUSES:
                same_error = row["error"] == error and \
                    row["stop_reason"] == stop_reason
                if row["status"] == status and same_error:
                    return {"status": "ok",
                            "delegation": _delegation_dict(row, _clock()),
                            "changed": False}
                raise ConflictError(
                    f"delegation {row['delegation_id']!r} already ended as "
                    f"{row['status']!r} with a different reason")
            _require_running(row["run_status"], row["run_id"], "end a delegation")
            for column, value in (("provider", provider), ("label", label),
                                  ("task_prompt", task_prompt)):
                if value is not None and row[column] not in (None, value):
                    raise ConflictError(
                        f"delegation {row['delegation_id']!r} already has "
                        f"{column}={row[column]!r}, not {value!r}")
            as_of = _clock()
            stamp = _iso(as_of)
            conn.execute(
                "UPDATE research_delegation SET status=?, child_id=COALESCE(child_id, ?), "
                "session_id=COALESCE(session_id, ?), dsh_run_id=COALESCE(dsh_run_id, ?), "
                "provider=COALESCE(provider, ?), label=COALESCE(label, ?), "
                "task_prompt=COALESCE(task_prompt, ?), "
                "result_json=?, error=?, stop_reason=?, output_sha256=?, "
                "output_excerpt=?, started_utc=COALESCE(started_utc, ?), "
                "finished_utc=?, heartbeat_utc=?, lease_expires_utc=NULL, "
                "updated_utc=? WHERE delegation_id=?",
                (status, selectors["child_id"], selectors["session_id"],
                 selectors["dsh_run_id"], provider, label, task_prompt,
                 result_json, error, stop_reason, output_sha256, output_excerpt,
                 stamp, stamp, stamp, stamp, row["delegation_id"]))
            row = _delegation_row(conn, project_id, row["delegation_id"])
            return {"status": "ok", "delegation": _delegation_dict(row, as_of),
                    "changed": True}


def settle_delegation(project_dir: str | Path, delegation_id: str, *,
                      status: str, stop_reason: str | None = None,
                      result: Any = None, error: str | None = None,
                      output_sha256: str | None = None,
                      output_excerpt: str | None = None,
                      dsh_run_id: str | None = None,
                      provider: str | None = None, label: str | None = None,
                      task_prompt: str | None = None,
                      child_id: str | None = None,
                      session_id: str | None = None) -> dict:
    """Settle one delegation by id, writing the DSH end-event fields.

    Thin, explicit entry point for the plugin's `subagent/end` handler; the
    semantics and idempotency are those of `end_subagent`.
    """
    delegation_id = _require_text(delegation_id, "delegation_id")
    return end_subagent(project_dir, delegation_id=delegation_id, status=status,
                        stop_reason=stop_reason, result=result, error=error,
                        output_sha256=output_sha256,
                        output_excerpt=output_excerpt, dsh_run_id=dsh_run_id,
                        provider=provider, label=label, task_prompt=task_prompt,
                        child_id=child_id, session_id=session_id)


def claim_delegation(project_dir: str | Path, delegation_id: str, *,
                     child_id: str, session_id: str | None = None,
                     lease_seconds: int = DEFAULT_LEASE_SECONDS,
                     stale_after_seconds: float | None = None,
                     as_of_utc: Any = None) -> dict:
    """Bind a pending delegation to its DSH child and start its lease.

    Re-claiming with the same child extends the lease. A different child may
    take over only after the previous lease expired, or after
    `stale_after_seconds` passed since its heartbeat when that window is given
    (the same rule `recover_run` reports as `is_stale`); that increments
    `attempt` and is reported as `reclaimed=true`. A live delegation held by
    another child is a `ConflictError`.
    """
    delegation_id = _require_text(delegation_id, "delegation_id")
    child_id = _require_text(child_id, "child_id")
    session_id = _optional_text(session_id, "session_id")
    _require_positive_int(lease_seconds, "lease_seconds")
    stale_after_seconds = _optional_seconds(stale_after_seconds,
                                            "stale_after_seconds")
    as_of = _clock(as_of_utc)
    stamp = _iso(as_of)
    with _open(project_dir) as (_proj, conn, project_id):
        with _write(conn):
            row = _delegation_row(conn, project_id, delegation_id)
            if row["status"] in TERMINAL_DELEGATION_STATUSES:
                raise InvalidTransitionError(
                    f"delegation {delegation_id!r} is {row['status']!r}, a terminal "
                    "status; it cannot be claimed")
            _require_running(row["run_status"], row["run_id"], "claim a delegation")
            if row["status"] == "pending":
                if session_id is not None and row["session_id"] not in (None, session_id):
                    raise ConflictError(
                        f"delegation {delegation_id!r} already belongs to session "
                        f"{row['session_id']!r}, not {session_id!r}")
                conn.execute(
                    "UPDATE research_delegation SET status='running', child_id=?, "
                    "session_id=?, started_utc=?, heartbeat_utc=?, "
                    "lease_expires_utc=?, updated_utc=? WHERE delegation_id=?",
                    (child_id, session_id, stamp, stamp,
                     _deadline(lease_seconds, as_of), stamp, delegation_id))
                row = _delegation_row(conn, project_id, delegation_id)
                return {"status": "ok",
                        "delegation": _delegation_dict(row, as_of),
                        "changed": True, "reclaimed": False}
            if row["child_id"] == child_id:
                if session_id is not None and row["session_id"] not in (None, session_id):
                    raise ConflictError(
                        f"delegation {delegation_id!r} already belongs to session "
                        f"{row['session_id']!r}, not {session_id!r}")
                conn.execute(
                    "UPDATE research_delegation SET session_id=COALESCE(?, session_id), "
                    "heartbeat_utc=?, lease_expires_utc=?, updated_utc=? "
                    "WHERE delegation_id=?",
                    (session_id, stamp, _deadline(lease_seconds, as_of), stamp,
                     delegation_id))
                row = _delegation_row(conn, project_id, delegation_id)
                return {"status": "ok",
                        "delegation": _delegation_dict(row, as_of),
                        "changed": False, "reclaimed": False}
            if not _delegation_is_stale(row, as_of, stale_after_seconds):
                raise ConflictError(
                    f"delegation {delegation_id!r} is running under child "
                    f"{row['child_id']!r} with a live lease; refusing child "
                    f"{child_id!r}")
            conn.execute(
                "UPDATE research_delegation SET child_id=?, session_id=?, "
                "attempt=attempt+1, started_utc=?, heartbeat_utc=?, "
                "lease_expires_utc=?, updated_utc=?, error=NULL "
                "WHERE delegation_id=?",
                (child_id, session_id, stamp, stamp,
                 _deadline(lease_seconds, as_of), stamp, delegation_id))
            row = _delegation_row(conn, project_id, delegation_id)
            return {"status": "ok", "delegation": _delegation_dict(row, as_of),
                    "changed": True, "reclaimed": True}


def heartbeat_delegation(project_dir: str | Path, delegation_id: str, *,
                         lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict:
    """Extend a running delegation's lease while its child is alive."""
    delegation_id = _require_text(delegation_id, "delegation_id")
    _require_positive_int(lease_seconds, "lease_seconds")
    with _open(project_dir) as (_proj, conn, project_id):
        with _write(conn):
            row = _delegation_row(conn, project_id, delegation_id)
            if row["status"] != "running":
                raise InvalidTransitionError(
                    f"cannot heartbeat delegation {delegation_id!r}: it is "
                    f"{row['status']!r}, not running")
            _require_running(row["run_status"], row["run_id"], "heartbeat a delegation")
            as_of = _clock()
            stamp = _iso(as_of)
            conn.execute(
                "UPDATE research_delegation SET heartbeat_utc=?, "
                "lease_expires_utc=?, updated_utc=? WHERE delegation_id=?",
                (stamp, _deadline(lease_seconds, as_of), stamp, delegation_id))
            row = _delegation_row(conn, project_id, delegation_id)
            return {"status": "ok", "delegation": _delegation_dict(row, as_of),
                    "changed": True}


def complete_delegation(project_dir: str | Path, delegation_id: str, *,
                        result: Any, child_id: str | None = None,
                        session_id: str | None = None) -> dict:
    """Mark a delegation completed with its actual result.

    A completion without a JSON result is rejected: "completed with nothing"
    would erase the distinction between finished work and missing output.
    """
    delegation_id = _require_text(delegation_id, "delegation_id")
    if result is None:
        raise ValidationError("result is required and cannot be null")
    result_json = _json_text(result, "result")
    child_id = _optional_text(child_id, "child_id")
    session_id = _optional_text(session_id, "session_id")
    with _open(project_dir) as (_proj, conn, project_id):
        with _write(conn):
            row = _delegation_row(conn, project_id, delegation_id)
            if row["status"] == "completed":
                if row["result_json"] != result_json:
                    raise ConflictError(
                        f"delegation {delegation_id!r} is already completed with a "
                        "different result")
                if (child_id is not None and row["child_id"] not in (None, child_id)) or \
                   (session_id is not None and row["session_id"] not in (None, session_id)):
                    raise ConflictError(
                        f"delegation {delegation_id!r} is already completed by a "
                        "different child/session")
                return {"status": "ok",
                        "delegation": _delegation_dict(row, _clock()),
                        "changed": False}
            if row["status"] in TERMINAL_DELEGATION_STATUSES:
                raise InvalidTransitionError(
                    f"delegation {delegation_id!r} is {row['status']!r}; it cannot "
                    "become completed")
            _require_running(row["run_status"], row["run_id"], "complete a delegation")
            if child_id is not None and row["child_id"] not in (None, child_id):
                raise ConflictError(
                    f"delegation {delegation_id!r} belongs to child "
                    f"{row['child_id']!r}, not {child_id!r}")
            if session_id is not None and row["session_id"] not in (None, session_id):
                raise ConflictError(
                    f"delegation {delegation_id!r} belongs to session "
                    f"{row['session_id']!r}, not {session_id!r}")
            as_of = _clock()
            stamp = _iso(as_of)
            conn.execute(
                "UPDATE research_delegation SET status='completed', "
                "child_id=COALESCE(child_id, ?), session_id=COALESCE(session_id, ?), "
                "result_json=?, error=NULL, started_utc=COALESCE(started_utc, ?), "
                "finished_utc=?, heartbeat_utc=?, lease_expires_utc=NULL, "
                "updated_utc=? WHERE delegation_id=?",
                (child_id, session_id, result_json, stamp, stamp, stamp, stamp,
                 delegation_id))
            row = _delegation_row(conn, project_id, delegation_id)
            return {"status": "ok", "delegation": _delegation_dict(row, as_of),
                    "changed": True}


def fail_delegation(project_dir: str | Path, delegation_id: str, *, error: str,
                    child_id: str | None = None,
                    session_id: str | None = None) -> dict:
    """Mark a delegation failed with the actual error text."""
    delegation_id = _require_text(delegation_id, "delegation_id")
    error = _require_text(error, "error")
    child_id = _optional_text(child_id, "child_id")
    session_id = _optional_text(session_id, "session_id")
    with _open(project_dir) as (_proj, conn, project_id):
        with _write(conn):
            row = _delegation_row(conn, project_id, delegation_id)
            if row["status"] == "failed":
                if row["error"] != error:
                    raise ConflictError(
                        f"delegation {delegation_id!r} already failed with a different "
                        "error")
                return {"status": "ok",
                        "delegation": _delegation_dict(row, _clock()),
                        "changed": False}
            if row["status"] in TERMINAL_DELEGATION_STATUSES:
                raise InvalidTransitionError(
                    f"delegation {delegation_id!r} is {row['status']!r}; it cannot "
                    "become failed")
            _require_running(row["run_status"], row["run_id"], "fail a delegation")
            if child_id is not None and row["child_id"] not in (None, child_id):
                raise ConflictError(
                    f"delegation {delegation_id!r} belongs to child "
                    f"{row['child_id']!r}, not {child_id!r}")
            if session_id is not None and row["session_id"] not in (None, session_id):
                raise ConflictError(
                    f"delegation {delegation_id!r} belongs to session "
                    f"{row['session_id']!r}, not {session_id!r}")
            as_of = _clock()
            stamp = _iso(as_of)
            conn.execute(
                "UPDATE research_delegation SET status='failed', "
                "child_id=COALESCE(child_id, ?), session_id=COALESCE(session_id, ?), "
                "error=?, result_json=NULL, started_utc=COALESCE(started_utc, ?), "
                "finished_utc=?, heartbeat_utc=?, lease_expires_utc=NULL, "
                "updated_utc=? WHERE delegation_id=?",
                (child_id, session_id, error, stamp, stamp, stamp, stamp,
                 delegation_id))
            row = _delegation_row(conn, project_id, delegation_id)
            return {"status": "ok", "delegation": _delegation_dict(row, as_of),
                    "changed": True}


def list_delegations(project_dir: str | Path, run_id: str | None = None, *,
                     status: str | None = None, include_finished: bool = True,
                     limit: int = 100, offset: int = 0,
                     stale_after_seconds: float | None = None,
                     as_of_utc: Any = None) -> dict:
    """List delegations with open/stale markers; finished ones only on request.

    With no `run_id`, every delegation in the project is listed (newest run
    first); the plugin can then reconcile all children after a restart.
    """
    run_id = _optional_text(run_id, "run_id")
    if status is not None:
        status = _require_enum(status, DELEGATION_STATUSES, "status")
    limit, offset = _require_page(limit, offset)
    stale_after_seconds = _optional_seconds(stale_after_seconds,
                                            "stale_after_seconds")
    as_of = _clock(as_of_utc)
    with _open(project_dir) as (_proj, conn, project_id):
        with _read(conn):
            run = None
            filters = ["r.project_id=?"]
            params: list[Any] = [project_id]
            if run_id is not None:
                run = _run_row(conn, project_id, run_id)
                filters.append("d.run_id=?")
                params.append(run_id)
            if status is not None:
                filters.append("d.status=?")
                params.append(status)
            elif not include_finished:
                filters.append("d.status IN ('pending','running')")
            where = " AND ".join(filters)
            total = conn.execute(
                "SELECT COUNT(*) c FROM research_delegation d "
                "JOIN research_run r ON r.run_id=d.run_id WHERE " + where,
                params).fetchone()["c"]
            rows = conn.execute(
                "SELECT d.*, (SELECT COUNT(*) FROM research_entry e "
                " WHERE e.delegation_id=d.delegation_id) AS note_count "
                "FROM research_delegation d "
                "JOIN research_run r ON r.run_id=d.run_id "
                f"WHERE {where} "
                "ORDER BY d.created_utc, d.rowid LIMIT ? OFFSET ?",
                params + [limit, offset]).fetchall()
            delegations = [_delegation_dict(row, as_of, stale_after_seconds)
                           for row in rows]
            return {"status": "ok", "run_id": run_id,
                    "run_status": run["status"] if run is not None else None,
                    "delegations": delegations, "count": len(delegations),
                    "total": total}


# -------------------------------------------------------------------- recovery
def recover_run(project_dir: str | Path, run_id: str | None = None, *,
                stale_after_seconds: float | None = None,
                include_finished: bool = False, entry_limit: int = 200,
                delegation_limit: int = 200, as_of_utc: Any = None) -> dict:
    """Read what a restarted parent must resume, without changing any state.

    With no `run_id`, every running run is returned (or every run when
    `include_finished` is true). Each run carries its open entries and open
    delegations. A running delegation past its lease, or past
    `stale_after_seconds` since its heartbeat, is marked `is_stale` so the
    parent can reclaim it with `claim_delegation`; it is never mutated here.
    """
    if run_id is not None:
        run_id = _require_text(run_id, "run_id")
    stale_after_seconds = _optional_seconds(stale_after_seconds,
                                            "stale_after_seconds")
    entry_limit = _require_page(entry_limit, 0)[0]
    delegation_limit = _require_page(delegation_limit, 0)[0]
    as_of = _clock(as_of_utc)
    with _open(project_dir) as (_proj, conn, project_id):
        with _read(conn):
            if run_id is not None:
                rows = [_run_row(conn, project_id, run_id)]
            else:
                clause = "" if include_finished else " AND status='running'"
                rows = conn.execute(
                    f"SELECT * FROM research_run WHERE project_id=?{clause} "
                    "ORDER BY version DESC", (project_id,)).fetchall()
            runs = []
            for row in rows:
                entries = conn.execute(
                    "SELECT * FROM research_entry WHERE run_id=? AND status='open' "
                    "ORDER BY created_utc, rowid LIMIT ?",
                    (row["run_id"], entry_limit)).fetchall()
                entry_ids = [entry["entry_id"] for entry in entries]
                link_map = _links_for_entries(conn, entry_ids)
                open_entries = []
                for entry in entries:
                    out = _entry_dict(entry)
                    out["links"] = link_map.get(out["entry_id"], [])
                    open_entries.append(out)
                delegations = conn.execute(
                    "SELECT d.*, (SELECT COUNT(*) FROM research_entry e "
                    " WHERE e.delegation_id=d.delegation_id) AS note_count "
                    "FROM research_delegation d WHERE d.run_id=? "
                    "AND d.status IN ('pending','running') "
                    "ORDER BY d.created_utc, d.rowid LIMIT ?",
                    (row["run_id"], delegation_limit)).fetchall()
                open_delegations = [
                    _delegation_dict(delegation, as_of, stale_after_seconds)
                    for delegation in delegations]
                run_out = _run_dict(row, as_of, stale_after_seconds)
                run_out["counts"] = _counts(conn, row["run_id"])
                runs.append({
                    "run": run_out,
                    "open_entries": open_entries,
                    "open_delegations": open_delegations,
                    "counts": {
                        "open_entries": run_out["counts"]["open_entries"],
                        "open_delegations": run_out["counts"]["open_delegations"],
                        "stale_delegations": sum(
                            1 for delegation in open_delegations
                            if delegation["is_stale"]),
                    },
                })
            return {"status": "ok", "runs": runs, "n_runs": len(runs),
                    "as_of_utc": _iso(as_of)}
