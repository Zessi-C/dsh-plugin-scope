"""Project-level persistent method ledger.

The ledger is a project artifact, not a research-run artifact: it survives a
``research_run`` and can be read while no run exists. One row records a method's
identity (canonical name, aliases), what it is for, what it needs and what it
cannot do, plus an optional binding to an adapter that this build ships.

Three states are derived, never written by the caller:

- *wiring* comes from ``catalog.EXECUTABLE``. A ledger row cannot assert that a
  method is wired; a binding that is missing from the current build reads back
  as ``stale_binding``.
- *installation* is ``unprobed`` until ``probe_installation`` actually starts a
  process and records its return code and output hashes. A source quote or a
  model statement never changes this state.
- *execution* is derived only from the project's ``protocol``/``task`` rows with
  ``task.status='succeeded'`` and at least one ``artifact`` row. A ledger row
  saying "we ran this" is not evidence of a run.

Source records are append-only. Each append gets a per-ledger
``record_version`` and keeps the external source's own ``version``, the query,
the locator and the retrieval time. When a snapshot path is supplied the file
must exist and this module computes its sha256; an optional ``expected_sha256``
must match. At read time the snapshot is re-checked and can read back as
``verified``, ``drift`` or ``missing``. A text match is recorded as a quote and
a locator, but every source is reported with
``semantic_verification='not_performed'``: byte equality and text presence do
not establish that the method does what its description says.

The corpus under ``research/`` is never written. Mutations take
``BEGIN IMMEDIATE`` on a connection with a busy timeout, so concurrent writers
serialise; reads use a deferred transaction. Every row is project-scoped and
every public lookup also filters on the project that opened the connection.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .project import (Project, get_or_create_project, new_id, now, sha256_file,
                      sha256_text)

DEFAULT_PAGE = 50
MAX_PAGE = 200
DEFAULT_SOURCE_PAGE = 50
MAX_PROBE_SECONDS = 3600

SOURCE_KINDS = ("paper", "preprint", "package", "repository", "documentation",
                "database", "web", "other")
MATCH_KINDS = ("metadata", "text_match", "snapshot_bytes")


class MethodLedgerError(RuntimeError):
    """Base class for every rejected method-ledger operation."""


class ValidationError(MethodLedgerError):
    """An argument is missing, of the wrong type, or outside its enum."""


class NotFoundError(MethodLedgerError):
    """The addressed ledger row does not exist in the opened project."""


class ConflictError(MethodLedgerError):
    """A name, alias, dedup key or idempotent payload conflicts."""


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
    return value.strip() or None


def _normalize(value: str) -> str:
    """Casefold and collapse punctuation so names compare without spelling drift."""
    text = unicodedata.normalize("NFKC", value).casefold()
    text = re.sub(r"[\W_]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{field} must be a list of strings")
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _require_text(item, f"{field} item")
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _json_list(text: str | None, field: str) -> list[str]:
    if not text:
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MethodLedgerError(f"stored {field} is corrupt JSON: {exc}") from exc
    if not isinstance(value, list):
        raise MethodLedgerError(f"stored {field} is not a JSON list")
    return [str(v) for v in value]


def _page(limit: Any, offset: Any, field: str = "limit") -> tuple[int, int]:
    if limit is None:
        limit = DEFAULT_PAGE
    if isinstance(limit, bool) or not isinstance(limit, int) \
            or not 1 <= limit <= MAX_PAGE:
        raise ValidationError(
            f"{field} must be an integer in 1..{MAX_PAGE}; got {limit!r}")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValidationError(
            f"offset must be a non-negative integer; got {offset!r}")
    return limit, offset


def _parse_utc(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError(
                f"{field} must be an ISO-8601 timestamp; got {value!r}") from exc
    else:
        raise ValidationError(f"{field} must be an ISO-8601 timestamp")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _excerpt(data: bytes, limit: int = 2000) -> str:
    return data.decode("utf-8", "replace")[:limit]


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


# ------------------------------------------------------------------ row lookup
def _ledger_row(conn: sqlite3.Connection, project_id: str,
                identifier: str) -> sqlite3.Row:
    text = _require_text(identifier, "identifier")
    row = conn.execute(
        "SELECT * FROM method_ledger WHERE project_id=? AND ledger_id=?",
        (project_id, text)).fetchone()
    if row is not None:
        return row
    normalized = _normalize(text)
    if not normalized:
        raise NotFoundError(f"method ledger entry {identifier!r} not found")
    row = conn.execute(
        "SELECT * FROM method_ledger WHERE project_id=? AND normalized_name=?",
        (project_id, normalized)).fetchone()
    if row is not None:
        return row
    row = conn.execute(
        """SELECT m.* FROM method_ledger m
           JOIN method_ledger_alias a
             ON a.project_id=m.project_id AND a.ledger_id=m.ledger_id
           WHERE a.project_id=? AND a.normalized_alias=?""",
        (project_id, normalized)).fetchone()
    if row is not None:
        return row
    raise NotFoundError(
        f"method ledger entry {identifier!r} not found in this project")


def _aliases(conn: sqlite3.Connection, project_id: str,
             ledger_id: str) -> list[str]:
    rows = conn.execute(
        """SELECT alias FROM method_ledger_alias
           WHERE project_id=? AND ledger_id=? ORDER BY rowid""",
        (project_id, ledger_id)).fetchall()
    return [row["alias"] for row in rows]


def _aliases_many(conn: sqlite3.Connection, project_id: str,
                  ledger_ids: Sequence[str]) -> dict[str, list[str]]:
    ids = sorted({i for i in ledger_ids if i})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""SELECT ledger_id, alias FROM method_ledger_alias
            WHERE project_id=? AND ledger_id IN ({placeholders})
            ORDER BY rowid""",
        (project_id, *ids)).fetchall()
    out: dict[str, list[str]] = {}
    for row in rows:
        out.setdefault(row["ledger_id"], []).append(row["alias"])
    return out


# ------------------------------------------------------------------- derivation
def _wiring(adapter_method_id: str | None) -> dict:
    """Wiring is a property of the current build, not of the ledger row."""
    if not adapter_method_id:
        return {
            "status": "not_wired",
            "adapter_method_id": None,
            "source": "catalog.EXECUTABLE",
            "reason": "no catalog adapter is bound to this ledger entry",
        }
    from .catalog import EXECUTABLE
    spec = EXECUTABLE.get(adapter_method_id)
    if spec is None:
        return {
            "status": "stale_binding",
            "adapter_method_id": adapter_method_id,
            "source": "catalog.EXECUTABLE",
            "reason": ("the bound id is absent from catalog.EXECUTABLE in this "
                       "build; the ledger entry is descriptive only"),
        }
    return {
        "status": "wired",
        "adapter_method_id": adapter_method_id,
        "source": "catalog.EXECUTABLE",
        "adapter": spec.get("adapter"),
        "stage": spec.get("stage"),
        "package": spec.get("package"),
        "provides": list(spec.get("provides") or []),
        "adapter_status": spec.get("status"),
    }


def _execution_counts(conn: sqlite3.Connection, project_id: str,
                      adapter_ids: Sequence[str]) -> dict[str, dict]:
    """Count succeeded tasks and their artifact rows, scoped to the project."""
    ids = sorted({i for i in adapter_ids if i})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""SELECT t.method_id AS method_id,
                   COUNT(*) AS n_succeeded,
                   COALESCE(SUM((SELECT COUNT(*) FROM artifact a
                                 WHERE a.task_id=t.task_id)), 0) AS n_artifacts,
                   COALESCE(SUM(CASE WHEN (SELECT COUNT(*) FROM artifact a
                                         WHERE a.task_id=t.task_id) > 0
                                     THEN 1 ELSE 0 END), 0) AS n_with_artifacts
            FROM task t
            JOIN protocol p ON p.protocol_id=t.protocol_id
            WHERE p.project_id=? AND t.status='succeeded'
              AND t.method_id IN ({placeholders})
            GROUP BY t.method_id""",
        (project_id, *ids)).fetchall()
    return {
        row["method_id"]: {
            "n_succeeded_tasks": int(row["n_succeeded"]),
            "n_succeeded_with_artifacts": int(row["n_with_artifacts"]),
            "n_artifacts": int(row["n_artifacts"]),
        }
        for row in rows
    }


def _execution_status(adapter_method_id: str | None, counts: dict | None) -> str:
    if not adapter_method_id:
        return "not_linked"
    counts = counts or {}
    n_succeeded = int(counts.get("n_succeeded_tasks") or 0)
    n_with_artifacts = int(counts.get("n_succeeded_with_artifacts") or 0)
    if n_succeeded == 0:
        return "not_run"
    if n_with_artifacts == 0:
        return "no_artifact"
    return "ran"


def _execution_default(adapter_method_id: str | None) -> dict:
    return {
        "status": _execution_status(adapter_method_id, None),
        "adapter_method_id": adapter_method_id,
        "n_succeeded_tasks": 0,
        "n_succeeded_with_artifacts": 0,
        "n_artifacts": 0,
        "last_success": None,
        "artifacts": [],
        "artifacts_truncated": False,
        "derived_from": "protocol.task(status='succeeded') + artifact rows",
        "artifact_file_check": "not_performed",
        "reason": ("no catalog adapter is bound, so task rows cannot be "
                   "attributed to this entry" if not adapter_method_id
                   else "no succeeded task with an artifact is recorded for "
                        "this adapter in this project"),
    }


def _execution_compact(adapter_method_id: str | None,
                       counts: dict | None) -> dict:
    execution = _execution_default(adapter_method_id)
    if counts:
        execution.update(counts)
    execution["status"] = _execution_status(adapter_method_id, counts)
    if execution["status"] == "ran":
        execution["reason"] = None
    elif execution["status"] == "no_artifact":
        execution["reason"] = ("succeeded task rows exist but none has an "
                               "artifact row; a success without a result record "
                               "is not a run")
    elif execution["status"] == "not_run":
        execution["reason"] = ("no succeeded task with an artifact is recorded "
                               "for this adapter in this project")
    return execution


def _execution_detail(conn: sqlite3.Connection, project_id: str,
                      adapter_method_id: str | None) -> dict:
    out = _execution_default(adapter_method_id)
    if not adapter_method_id:
        return out
    counts = _execution_counts(conn, project_id, [adapter_method_id]).get(
        adapter_method_id) or {}
    out.update(counts)
    rows = conn.execute(
        """SELECT t.task_id, t.protocol_id, t.finished_utc, t.started_utc,
                  (SELECT COUNT(*) FROM artifact a WHERE a.task_id=t.task_id)
                      AS n_artifacts
           FROM task t
           JOIN protocol p ON p.protocol_id=t.protocol_id
           WHERE p.project_id=? AND t.method_id=? AND t.status='succeeded'
           ORDER BY COALESCE(t.finished_utc, t.started_utc, '') DESC,
                    t.task_id""",
        (project_id, adapter_method_id)).fetchall()
    out["n_succeeded_tasks"] = len(rows)
    out["n_succeeded_with_artifacts"] = sum(
        1 for row in rows if int(row["n_artifacts"]) > 0)
    out["n_artifacts"] = sum(int(row["n_artifacts"]) for row in rows)
    last = next((row for row in rows if int(row["n_artifacts"]) > 0), None)
    if last is not None:
        out["last_success"] = {
            "task_id": last["task_id"],
            "protocol_id": last["protocol_id"],
            "finished_utc": last["finished_utc"],
            "started_utc": last["started_utc"],
            "n_artifacts": int(last["n_artifacts"]),
        }
        artifacts = conn.execute(
            """SELECT artifact_id, kind, path, checksum, checksum_algo,
                      n_rows, n_cols, created_utc
               FROM artifact WHERE task_id=?
               ORDER BY created_utc, artifact_id LIMIT 10""",
            (last["task_id"],)).fetchall()
        out["artifacts"] = [dict(row) for row in artifacts]
        out["artifacts_truncated"] = int(last["n_artifacts"]) > len(artifacts)
    out["status"] = _execution_status(adapter_method_id, out)
    if out["status"] == "ran":
        out["reason"] = None
    elif out["status"] == "no_artifact":
        out["reason"] = ("succeeded task rows exist but none has an artifact "
                         "row; a success without a result record is not a run")
    elif out["status"] == "not_run":
        out["reason"] = ("no succeeded task with an artifact is recorded for "
                         "this adapter in this project")
    return out


def _probe_dict(row: sqlite3.Row) -> dict:
    return {
        "probe_id": row["probe_id"],
        "record_version": int(row["record_version"]),
        "probe_kind": row["probe_kind"],
        "command": _json_list(row["command_json"], "command_json"),
        "cwd": row["cwd"],
        "returncode": row["returncode"],
        "launch_error": row["launch_error"],
        "timed_out": bool(row["timed_out"]),
        "duration_seconds": row["duration_seconds"],
        "stdout_sha256": row["stdout_sha256"],
        "stderr_sha256": row["stderr_sha256"],
        "stdout_excerpt": row["stdout_excerpt"],
        "stderr_excerpt": row["stderr_excerpt"],
        "checked_utc": row["checked_utc"],
    }


def _installation_from_probe(row: sqlite3.Row | None, *,
                             compact: bool = False) -> dict:
    if row is None:
        return {
            "status": "unprobed",
            "probe": None,
            "note": ("installation is unprobed by default; a source statement "
                     "or model output does not change this state"),
        }
    probe = _probe_dict(row)
    if compact:
        probe = {"probe_id": probe["probe_id"],
                 "record_version": probe["record_version"],
                 "probe_kind": probe["probe_kind"],
                 "returncode": probe["returncode"],
                 "launch_error": probe["launch_error"],
                 "timed_out": probe["timed_out"],
                 "checked_utc": probe["checked_utc"],
                 "stdout_sha256": probe["stdout_sha256"],
                 "stderr_sha256": probe["stderr_sha256"]}
    if probe["timed_out"]:
        status = "unavailable"
        note = "the probe timed out; no install conclusion is drawn"
    elif probe["launch_error"]:
        status = "unavailable"
        note = f"the probe could not be started: {probe['launch_error']}"
    elif probe["returncode"] == 0:
        status = "present"
        note = ("this exact probe command exited 0; it says nothing about "
                "whether a method task has run")
    else:
        status = "absent"
        note = (f"this exact probe command exited {probe['returncode']}; it "
                "does not establish that the method is unusable")
    return {
        "status": status,
        "probe": probe,
        "note": note,
        "interpretation": "return code of exactly the recorded argv",
    }


def _probe_rows(conn: sqlite3.Connection, project_id: str,
                ledger_ids: Sequence[str]) -> dict[str, sqlite3.Row]:
    ids = sorted({i for i in ledger_ids if i})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""SELECT p.* FROM method_ledger_probe p
            WHERE p.project_id=? AND p.ledger_id IN ({placeholders})
              AND p.record_version = (
                  SELECT MAX(p2.record_version) FROM method_ledger_probe p2
                  WHERE p2.project_id=p.project_id
                    AND p2.ledger_id=p.ledger_id)""",
        (project_id, *ids)).fetchall()
    return {row["ledger_id"]: row for row in rows}


def _external_source_ids(conn: sqlite3.Connection, project_id: str,
                         ledger_id: str) -> list[str]:
    rows = conn.execute(
        """SELECT DISTINCT external_id FROM method_ledger_source
           WHERE project_id=? AND ledger_id=? AND external_id IS NOT NULL
           ORDER BY external_id""",
        (project_id, ledger_id)).fetchall()
    return [row["external_id"] for row in rows]


def _source_summary(conn: sqlite3.Connection, project_id: str,
                    ledger_ids: Sequence[str]) -> dict[str, dict]:
    ids = sorted({i for i in ledger_ids if i})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""SELECT ledger_id, match_kind, COUNT(*) AS n,
                   SUM(CASE WHEN snapshot_path IS NOT NULL THEN 1 ELSE 0 END)
                       AS n_snapshots,
                   MAX(retrieved_utc) AS latest
            FROM method_ledger_source
            WHERE project_id=? AND ledger_id IN ({placeholders})
            GROUP BY ledger_id, match_kind""",
        (project_id, *ids)).fetchall()
    out: dict[str, dict] = {}
    for row in rows:
        rec = out.setdefault(row["ledger_id"], {
            "n_sources": 0, "n_snapshots": 0, "latest_source_utc": None,
            "match_kinds": {}})
        rec["n_sources"] += int(row["n"])
        rec["n_snapshots"] += int(row["n_snapshots"] or 0)
        rec["match_kinds"][row["match_kind"]] = int(row["n"])
        if row["latest"] and (rec["latest_source_utc"] is None
                              or row["latest"] > rec["latest_source_utc"]):
            rec["latest_source_utc"] = row["latest"]
    return out


def _provenance_level(match_kinds: dict[str, int], n_sources: int,
                      n_snapshots: int = 0) -> str:
    if n_sources <= 0:
        return "none"
    if n_snapshots > 0 or match_kinds.get("snapshot_bytes"):
        return "snapshot_recorded"
    if match_kinds.get("text_match"):
        return "text_match_only"
    return "external_identity"


def _provenance(n_sources: int, match_kinds: dict[str, int],
                n_snapshots: int = 0) -> dict:
    return {
        "level": _provenance_level(match_kinds, n_sources, n_snapshots),
        "n_sources": n_sources,
        "n_snapshots": n_snapshots,
        "match_kind_counts": dict(sorted(match_kinds.items())),
        "semantic_verification": "not_performed",
        "note": ("source records make a claim checkable; a text match or a "
                 "snapshot hash is not semantic verification that the method "
                 "does what its description says"),
    }


def _missing(purpose: str | None, inputs: list[str], prerequisites: list[str],
             limitations: list[str], n_sources: int,
             adapter_method_id: str | None) -> list[str]:
    missing: list[str] = []
    if not purpose:
        missing.append("purpose")
    if not inputs:
        missing.append("inputs")
    if not prerequisites:
        missing.append("prerequisites")
    if not limitations:
        missing.append("limitations")
    if n_sources <= 0:
        missing.append("external_source")
    if not adapter_method_id:
        missing.append("adapter_binding")
    return missing


def _base_fields(row: sqlite3.Row) -> dict:
    return {
        "ledger_id": row["ledger_id"],
        "name": row["name"],
        "purpose": row["purpose"],
        "inputs": _json_list(row["inputs_json"], "inputs_json"),
        "prerequisites": _json_list(row["prerequisites_json"],
                                    "prerequisites_json"),
        "limitations": _json_list(row["limitations_json"], "limitations_json"),
        "adapter_method_id": row["adapter_method_id"],
        "dedup_key": row["dedup_key"],
        "created_utc": row["created_utc"],
        "updated_utc": row["updated_utc"],
    }


def _compact(conn: sqlite3.Connection, project_id: str, row: sqlite3.Row,
             *, execution: dict | None = None,
             probe: sqlite3.Row | None = None,
             summary: dict | None = None,
             aliases: list[str] | None = None) -> dict:
    out = _base_fields(row)
    out["aliases"] = (aliases if aliases is not None
                      else _aliases(conn, project_id, row["ledger_id"]))
    out["wiring"] = _wiring(row["adapter_method_id"])
    out["installation"] = _installation_from_probe(probe, compact=True)
    if execution is None:
        counts = _execution_counts(
            conn, project_id, [row["adapter_method_id"]]
        ).get(row["adapter_method_id"]) if row["adapter_method_id"] else None
        execution = _execution_compact(row["adapter_method_id"], counts)
    out["execution"] = execution
    summary = summary or {"n_sources": 0, "n_snapshots": 0,
                          "latest_source_utc": None, "match_kinds": {}}
    out["n_sources"] = summary.get("n_sources", 0)
    out["latest_source_utc"] = summary.get("latest_source_utc")
    out["provenance"] = _provenance(
        out["n_sources"], summary.get("match_kinds") or {},
        summary.get("n_snapshots", 0))
    out["missing"] = _missing(out["purpose"], out["inputs"], out["prerequisites"],
                              out["limitations"], out["n_sources"],
                              out["adapter_method_id"])
    return out


# ------------------------------------------------------------------- snapshots
def _resolve_snapshot(proj: Project, snapshot_path: str) -> Path:
    path = Path(snapshot_path)
    if not path.is_absolute():
        path = proj.root / path
    return path.resolve()


def _record_snapshot(proj: Project, snapshot_path: Any,
                     expected_sha256: Any) -> tuple[str | None, str | None]:
    path_text = _optional_text(snapshot_path, "snapshot_path")
    expected = _optional_text(expected_sha256, "expected_sha256")
    if path_text is None:
        if expected is not None:
            raise ValidationError(
                "expected_sha256 was supplied without a snapshot_path")
        return None, None
    path = _resolve_snapshot(proj, path_text)
    if not path.is_file():
        raise ValidationError(
            f"snapshot_path does not exist or is not a file: {str(path)!r}; "
            "refusing to record material that cannot be checked")
    try:
        actual = sha256_file(path)
    except OSError as exc:
        raise ValidationError(
            f"snapshot_path could not be read: {str(path)!r}: {exc}") from exc
    if expected is not None:
        wanted = expected.lower()
        if len(wanted) != 64 or any(c not in "0123456789abcdef" for c in wanted):
            raise ValidationError(
                "expected_sha256 must be 64 hexadecimal characters")
        if wanted != actual:
            raise ValidationError(
                f"snapshot sha256 mismatch: expected {wanted}, got {actual}; "
                "the record was not written")
    return str(path), actual


def _snapshot_state(row: sqlite3.Row, verify: bool) -> dict:
    path = row["snapshot_path"]
    recorded = row["snapshot_sha256"]
    if not path:
        return {"status": "absent", "path": None, "recorded_sha256": None,
                "current_sha256": None, "checked": False}
    file_path = Path(path)
    if not file_path.is_file():
        return {"status": "missing", "path": path, "recorded_sha256": recorded,
                "current_sha256": None, "checked": True}
    if not verify:
        return {"status": "not_checked", "path": path,
                "recorded_sha256": recorded, "current_sha256": None,
                "checked": False}
    try:
        current = sha256_file(file_path)
    except OSError:
        return {"status": "unreadable", "path": path,
                "recorded_sha256": recorded, "current_sha256": None,
                "checked": True}
    status = "verified" if recorded and current == recorded else "drift"
    return {"status": status, "path": path, "recorded_sha256": recorded,
            "current_sha256": current, "checked": True}


def _source_dict(row: sqlite3.Row, verify_snapshots: bool) -> dict:
    quote = row["quote"]
    return {
        "source_id": row["source_id"],
        "record_version": int(row["record_version"]),
        "source_kind": row["source_kind"],
        "external_id": row["external_id"],
        "title": row["title"],
        "version": row["version"],
        "query": row["query"],
        "endpoint": row["endpoint"],
        "locator": row["locator"],
        "match_kind": row["match_kind"],
        "quote": quote,
        "quote_sha256": row["quote_sha256"],
        "retrieved_utc": row["retrieved_utc"],
        "notes": row["notes"],
        "dedup_key": row["dedup_key"],
        "created_utc": row["created_utc"],
        "snapshot": _snapshot_state(row, verify_snapshots),
        "verification_scope": {
            "metadata": "identity_only",
            "text_match": "text_present",
            "snapshot_bytes": "content_hash",
        }.get(row["match_kind"], "identity_only"),
        "semantic_verification": "not_performed",
        "note": ("a text match or snapshot hash is checkable material, not "
                 "semantic validation of the method"),
    }


def _sources_total(conn: sqlite3.Connection, project_id: str,
                   ledger_id: str) -> int:
    return conn.execute(
        """SELECT COUNT(*) c FROM method_ledger_source
           WHERE project_id=? AND ledger_id=?""",
        (project_id, ledger_id)).fetchone()["c"]


def _sources_page(conn: sqlite3.Connection, project_id: str, ledger_id: str,
                  *, limit: int, offset: int,
                  verify_snapshots: bool) -> dict:
    total = _sources_total(conn, project_id, ledger_id)
    rows = conn.execute(
        """SELECT * FROM method_ledger_source
           WHERE project_id=? AND ledger_id=?
           ORDER BY record_version DESC LIMIT ? OFFSET ?""",
        (project_id, ledger_id, limit, offset)).fetchall()
    sources = [_source_dict(row, verify_snapshots) for row in rows]
    end = offset + len(sources)
    return {
        "sources": sources,
        "total": total,
        "limit": limit,
        "offset": offset,
        "returned": len(sources),
        "has_more": end < total,
        "next_offset": end if end < total else None,
    }


# ------------------------------------------------------------------- detail/get
def _detail(conn: sqlite3.Connection, project_id: str,
            row: sqlite3.Row, *, verify_snapshots: bool,
            source_limit: int, source_offset: int) -> dict:
    out = _base_fields(row)
    ledger_id = row["ledger_id"]
    out["aliases"] = _aliases(conn, project_id, ledger_id)
    out["wiring"] = _wiring(row["adapter_method_id"])
    probe = _probe_rows(conn, project_id, [ledger_id]).get(ledger_id)
    out["installation"] = _installation_from_probe(probe)
    out["execution"] = _execution_detail(conn, project_id,
                                         row["adapter_method_id"])
    summary = _source_summary(conn, project_id, [ledger_id]).get(
        ledger_id) or {"n_sources": 0, "latest_source_utc": None,
                       "match_kinds": {}}
    pages = _sources_page(conn, project_id, ledger_id, limit=source_limit,
                          offset=source_offset,
                          verify_snapshots=verify_snapshots)
    out["sources"] = pages["sources"]
    out["sources_total"] = pages["total"]
    out["external_source_ids"] = _external_source_ids(
        conn, project_id, ledger_id)
    out["n_sources"] = summary["n_sources"]
    out["latest_source_utc"] = summary.get("latest_source_utc")
    out["sources_page"] = {
        "limit": pages["limit"], "offset": pages["offset"],
        "returned": pages["returned"], "total": pages["total"],
        "has_more": pages["has_more"],
        "next_offset": pages["next_offset"],
    }
    out["provenance"] = _provenance(summary["n_sources"],
                                    summary["match_kinds"],
                                    summary.get("n_snapshots", 0))
    status_counts: dict[str, int] = {}
    for source in pages["sources"]:
        status = source["snapshot"]["status"]
        status_counts[status] = status_counts.get(status, 0) + 1
    out["provenance"]["snapshot_checks"] = {
        "returned_sources": len(pages["sources"]),
        "status_counts": dict(sorted(status_counts.items())),
        "page_truncated": pages["has_more"],
    }
    out["missing"] = _missing(out["purpose"], out["inputs"],
                              out["prerequisites"], out["limitations"],
                              summary["n_sources"], out["adapter_method_id"])
    out["note"] = ("derived statuses are computed from catalog.EXECUTABLE, "
                   "recorded probes and task/artifact rows; they are not "
                   "writable fields of this entry")
    return out


def get_method(project_dir: str | Path, identifier: str, *,
               verify_snapshots: bool = True,
               source_limit: int = DEFAULT_SOURCE_PAGE,
               source_offset: int = 0) -> dict:
    """Return one ledger entry by id, canonical name or alias.

    ``verify_snapshots=True`` re-hashes each returned snapshot; a missing file
    reads back as ``missing`` and a changed file as ``drift``. This never turns
    a text match into semantic verification.
    """
    if not isinstance(verify_snapshots, bool):
        raise ValidationError("verify_snapshots must be a boolean")
    limit, offset = _page(source_limit, source_offset, "source_limit")
    with _open(project_dir) as (_proj, conn, project_id):
        with _read(conn):
            row = _ledger_row(conn, project_id, identifier)
            return _detail(conn, project_id, row,
                           verify_snapshots=verify_snapshots,
                           source_limit=limit, source_offset=offset)


# ---------------------------------------------------------------------- search
def _search_where(project_id: str, query: str | None,
                  adapter_method_id: str | None) -> tuple[str, list[Any],
                                                          list[str]]:
    clauses = ["m.project_id=?"]
    params: list[Any] = [project_id]
    if adapter_method_id is not None:
        clauses.append("m.adapter_method_id=?")
        params.append(adapter_method_id)
    normalized = _normalize(query) if query else ""
    tokens = normalized.split() if normalized else []
    if query and not tokens:
        clauses.append("0")  # punctuation-only query is not a request for everything
    for token in tokens:
        clauses.append("m.search_text LIKE ?")
        params.append(f"%{token}%")
    return " AND ".join(clauses), params, tokens


def search_methods(project_dir: str | Path, query: str | None = None, *,
                   adapter_method_id: str | None = None,
                   limit: int = DEFAULT_PAGE,
                   offset: int = 0) -> dict:
    """Page the ledger. Search is over canonical name, aliases and purpose."""
    limit, offset = _page(limit, offset)
    adapter = _optional_text(adapter_method_id, "adapter_method_id")
    query_text = _optional_text(query, "query")
    with _open(project_dir) as (_proj, conn, project_id):
        with _read(conn):
            where, params, tokens = _search_where(project_id, query_text, adapter)
            total = conn.execute(
                f"SELECT COUNT(*) c FROM method_ledger m WHERE {where}",
                params).fetchone()["c"]
            order_params: list[Any] = []
            if tokens:
                normalized = _normalize(query_text)
                order = ("CASE WHEN m.normalized_name=? THEN 0 "
                         "WHEN m.normalized_name LIKE ? THEN 1 ELSE 2 END, "
                         "m.name COLLATE NOCASE, m.ledger_id")
                order_params = [normalized, f"{normalized}%"]
            else:
                order = "m.name COLLATE NOCASE, m.ledger_id"
            rows = conn.execute(
                f"""SELECT m.* FROM method_ledger m WHERE {where}
                    ORDER BY {order} LIMIT ? OFFSET ?""",
                (*params, *order_params, limit, offset)).fetchall()
            ledger_ids = [row["ledger_id"] for row in rows]
            probes = _probe_rows(conn, project_id, ledger_ids)
            summaries = _source_summary(conn, project_id, ledger_ids)
            aliases_by_ledger = _aliases_many(conn, project_id, ledger_ids)
            adapter_ids = [row["adapter_method_id"] for row in rows
                           if row["adapter_method_id"]]
            executions = _execution_counts(conn, project_id, adapter_ids)
            methods: list[dict] = []
            for row in rows:
                mid = row["adapter_method_id"]
                counts = executions.get(mid) if mid else None
                execution = _execution_compact(mid, counts)
                methods.append(_compact(
                    conn, project_id, row, execution=execution,
                    probe=probes.get(row["ledger_id"]),
                    summary=summaries.get(row["ledger_id"]),
                    aliases=aliases_by_ledger.get(row["ledger_id"], [])))
            end = offset + len(methods)
            page = {
                "limit": limit,
                "offset": offset,
                "returned": len(methods),
                "total": total,
                "has_more": end < total,
                "next_offset": end if end < total else None,
            }
            return {
                "methods": methods,
                "total": total,
                "limit": limit,
                "offset": offset,
                "returned": len(methods),
                "has_more": page["has_more"],
                "next_offset": page["next_offset"],
                "query": query_text,
                "adapter_method_id": adapter,
                "page": page,
            }


# ------------------------------------------------------------------ registration
def _core_equal(row: sqlite3.Row, name: str, purpose: str | None,
                inputs: list[str], prerequisites: list[str],
                limitations: list[str], adapter: str | None) -> bool:
    return (
        row["normalized_name"] == _normalize(name)
        and (row["purpose"] or None) == purpose
        and _json_list(row["inputs_json"], "inputs_json") == inputs
        and _json_list(row["prerequisites_json"], "prerequisites_json")
            == prerequisites
        and _json_list(row["limitations_json"], "limitations_json")
            == limitations
        and (row["adapter_method_id"] or None) == adapter
    )


def _check_alias_free(conn: sqlite3.Connection, project_id: str,
                      ledger_id: str, alias: str, own_normalized: str) -> str:
    """Validate one alias against the project; returns its normalized form."""
    normalized = _normalize(_require_text(alias, "alias"))
    if not normalized:
        raise ValidationError(f"alias {alias!r} normalizes to an empty string")
    if normalized == own_normalized:
        return normalized
    clash = conn.execute(
        "SELECT ledger_id FROM method_ledger WHERE project_id=? AND normalized_name=?",
        (project_id, normalized)).fetchone()
    if clash is not None and clash["ledger_id"] != ledger_id:
        raise ConflictError(
            f"alias {alias!r} is the canonical name of ledger entry "
            f"{clash['ledger_id']!r}")
    clash = conn.execute(
        """SELECT ledger_id FROM method_ledger_alias
           WHERE project_id=? AND normalized_alias=?""",
        (project_id, normalized)).fetchone()
    if clash is not None and clash["ledger_id"] != ledger_id:
        raise ConflictError(
            f"alias {alias!r} already belongs to ledger entry "
            f"{clash['ledger_id']!r}")
    return normalized


def _add_aliases_locked(conn: sqlite3.Connection, project_id: str,
                        ledger_id: str, own_normalized: str,
                        aliases: Iterable[str], stamp: str) -> bool:
    added = False
    seen: set[str] = set()
    for alias in aliases:
        normalized = _check_alias_free(conn, project_id, ledger_id, alias,
                                       own_normalized)
        if normalized == own_normalized or normalized in seen:
            continue
        seen.add(normalized)
        existing = conn.execute(
            """SELECT 1 FROM method_ledger_alias
               WHERE project_id=? AND ledger_id=? AND normalized_alias=?""",
            (project_id, ledger_id, normalized)).fetchone()
        if existing is not None:
            continue
        conn.execute(
            """INSERT INTO method_ledger_alias(project_id, ledger_id, alias,
                       normalized_alias, created_utc) VALUES (?,?,?,?,?)""",
            (project_id, ledger_id, alias.strip(), normalized, stamp))
        added = True
    return added


def _rebuild_search_text(conn: sqlite3.Connection, project_id: str,
                         ledger_id: str, stamp: str) -> None:
    row = conn.execute(
        "SELECT * FROM method_ledger WHERE project_id=? AND ledger_id=?",
        (project_id, ledger_id)).fetchone()
    aliases = _aliases(conn, project_id, ledger_id)
    parts = [row["normalized_name"]]
    parts.extend(_normalize(alias) for alias in aliases)
    if row["purpose"]:
        parts.append(_normalize(row["purpose"]))
    search = " ".join(part for part in parts if part)
    conn.execute(
        """UPDATE method_ledger SET search_text=?, updated_utc=?
           WHERE project_id=? AND ledger_id=?""",
        (search, stamp, project_id, ledger_id))


def register_method(project_dir: str | Path, name: str, *,
                    aliases: Sequence[str] | str | None = None,
                    purpose: str | None = None,
                    inputs: Sequence[str] | str | None = None,
                    prerequisites: Sequence[str] | str | None = None,
                    limitations: Sequence[str] | str | None = None,
                    adapter_method_id: str | None = None,
                    dedup_key: str | None = None) -> dict:
    """Create or idempotently return one ledger entry.

    A binding to ``adapter_method_id`` is accepted only when that id exists in
    ``catalog.EXECUTABLE`` in this build; the ledger never stores a ``wired``
    flag. Repeating an identical registration returns ``created=False``.
    Aliases are additive: an existing entry may gain new aliases while its
    core fields stay unchanged. A different core payload under the same name
    or dedup key raises ``ConflictError``.

    External source identifiers, queries, locators and snapshot references do
    not live on this row: append them with :func:`add_source`, which keeps each
    observation separate and versioned.
    """
    clean_name = _require_text(name, "name")
    normalized = _normalize(clean_name)
    if not normalized:
        raise ValidationError(f"name {name!r} normalizes to an empty string")
    purpose = _optional_text(purpose, "purpose")
    inputs_l = _string_list(inputs, "inputs")
    prereq_l = _string_list(prerequisites, "prerequisites")
    limits_l = _string_list(limitations, "limitations")
    aliases_l = _string_list(aliases, "aliases")
    adapter = _optional_text(adapter_method_id, "adapter_method_id")
    if adapter is not None:
        from .catalog import EXECUTABLE
        if adapter not in EXECUTABLE:
            raise ValidationError(
                f"adapter_method_id {adapter!r} is not in catalog.EXECUTABLE; "
                "wiring is derived from the build and cannot be asserted")
    dedup = _optional_text(dedup_key, "dedup_key")
    stamp = now()
    created = False
    ledger_id: str
    with _open(project_dir) as (_proj, conn, project_id):
        with _write(conn):
            by_name = conn.execute(
                "SELECT * FROM method_ledger WHERE project_id=? AND normalized_name=?",
                (project_id, normalized)).fetchone()
            by_dedup = None
            if dedup is not None:
                by_dedup = conn.execute(
                    "SELECT * FROM method_ledger WHERE project_id=? AND dedup_key=?",
                    (project_id, dedup)).fetchone()
            if by_name is not None and by_dedup is not None \
                    and by_name["ledger_id"] != by_dedup["ledger_id"]:
                raise ConflictError(
                    f"name {clean_name!r} and dedup_key {dedup!r} name different "
                    "ledger entries")
            existing = by_name or by_dedup
            if existing is not None:
                if not _core_equal(existing, clean_name, purpose, inputs_l,
                                   prereq_l, limits_l, adapter):
                    raise ConflictError(
                        f"ledger entry {existing['ledger_id']!r} already exists "
                        "with a different name/normalized name, purpose, inputs, "
                        "prerequisites, limitations or adapter binding")
                if existing["dedup_key"] and dedup and existing["dedup_key"] != dedup:
                    raise ConflictError(
                        f"ledger entry {existing['ledger_id']!r} is already "
                        f"registered under dedup_key {existing['dedup_key']!r}")
                changed = False
                if dedup and not existing["dedup_key"]:
                    conn.execute(
                        "UPDATE method_ledger SET dedup_key=?, updated_utc=? "
                        "WHERE project_id=? AND ledger_id=?",
                        (dedup, stamp, project_id, existing["ledger_id"]))
                    changed = True
                if _add_aliases_locked(conn, project_id, existing["ledger_id"],
                                       existing["normalized_name"], aliases_l,
                                       stamp):
                    _rebuild_search_text(conn, project_id,
                                         existing["ledger_id"], stamp)
                    changed = True
                if changed:
                    conn.execute(
                        "UPDATE method_ledger SET updated_utc=? "
                        "WHERE project_id=? AND ledger_id=?",
                        (stamp, project_id, existing["ledger_id"]))
                ledger_id = existing["ledger_id"]
            else:
                # A new canonical name may not collide with an existing alias.
                alias_clash = conn.execute(
                    """SELECT ledger_id FROM method_ledger_alias
                       WHERE project_id=? AND normalized_alias=?""",
                    (project_id, normalized)).fetchone()
                if alias_clash is not None:
                    raise ConflictError(
                        f"name {clean_name!r} is already an alias of ledger "
                        f"entry {alias_clash['ledger_id']!r}")
                ledger_id = new_id("mled")
                search = " ".join(
                    [normalized] + [_normalize(a) for a in aliases_l]
                    + ([_normalize(purpose)] if purpose else []))
                conn.execute(
                    """INSERT INTO method_ledger(ledger_id, project_id, name,
                           normalized_name, search_text, purpose, inputs_json,
                           prerequisites_json, limitations_json,
                           adapter_method_id, dedup_key, created_utc, updated_utc)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (ledger_id, project_id, clean_name, normalized, search,
                     purpose, _json_text(inputs_l), _json_text(prereq_l),
                     _json_text(limits_l), adapter, dedup, stamp, stamp))
                _add_aliases_locked(conn, project_id, ledger_id, normalized,
                                    aliases_l, stamp)
                created = True
        row = _ledger_row(conn, project_id, ledger_id)
        with _read(conn):
            detail = _detail(conn, project_id, row,
                             verify_snapshots=False,
                             source_limit=DEFAULT_SOURCE_PAGE,
                             source_offset=0)
    return {"method": detail, "created": created}


def add_alias(project_dir: str | Path, ledger_id: str, alias: str) -> dict:
    """Append one alias to an existing entry; an exact repeat is a no-op."""
    lid = _require_text(ledger_id, "ledger_id")
    stamp = now()
    with _open(project_dir) as (_proj, conn, project_id):
        with _write(conn):
            row = _ledger_row(conn, project_id, lid)
            if row["ledger_id"] != lid:
                raise NotFoundError(
                    f"ledger id {lid!r} not found; add_alias requires an id")
            added = _add_aliases_locked(conn, project_id, lid,
                                        row["normalized_name"], [alias], stamp)
            if added:
                _rebuild_search_text(conn, project_id, lid, stamp)
        row = _ledger_row(conn, project_id, lid)
        with _read(conn):
            detail = _detail(conn, project_id, row,
                             verify_snapshots=False,
                             source_limit=DEFAULT_SOURCE_PAGE,
                             source_offset=0)
    return {"method": detail, "created": added}


# --------------------------------------------------------------------- sources
def _source_payload(source_kind: str, external_id: str | None,
                    title: str | None, version: str | None,
                    query: str | None, endpoint: str | None,
                    locator: str | None, match_kind: str,
                    quote: str | None, quote_sha256: str | None,
                    snapshot_sha256: str | None, notes: str | None) -> dict:
    return {
        "source_kind": source_kind,
        "external_id": external_id,
        "title": title,
        "version": version,
        "query": query,
        "endpoint": endpoint,
        "locator": locator,
        "match_kind": match_kind,
        "quote": quote,
        "quote_sha256": quote_sha256,
        "snapshot_sha256": snapshot_sha256,
        "notes": notes,
    }


def _row_source_payload(row: sqlite3.Row) -> dict:
    return _source_payload(
        row["source_kind"], row["external_id"], row["title"], row["version"],
        row["query"], row["endpoint"], row["locator"], row["match_kind"],
        row["quote"], row["quote_sha256"], row["snapshot_sha256"],
        row["notes"])


def add_source(project_dir: str | Path, ledger_id: str, *,
               source_kind: str, external_id: str | None = None,
               title: str | None = None, version: str | None = None,
               query: str | None = None, endpoint: str | None = None,
               locator: str | None = None, snapshot_path: str | None = None,
               expected_sha256: str | None = None, quote: str | None = None,
               match_kind: str = "metadata", notes: str | None = None,
               dedup_key: str | None = None,
               retrieved_utc: str | datetime | None = None) -> dict:
    """Append one source/verification record; never updates an older one.

    A supplied snapshot must exist and its sha256 is computed here. ``quote``
    plus ``locator`` records a text match, not semantic verification: the read
    path always reports ``semantic_verification='not_performed'``. Installation
    and execution cannot be recorded through this function.

    ``dedup_key`` selects a logical observation. Repeating the same payload
    under the same key returns the earlier record with ``created=False``; pass
    a new key to append a later retrieval of the same source.
    """
    lid = _require_text(ledger_id, "ledger_id")
    kind = _require_text(source_kind, "source_kind")
    if kind not in SOURCE_KINDS:
        raise ValidationError(
            f"source_kind must be one of {list(SOURCE_KINDS)}; got {kind!r}")
    match = _require_text(match_kind, "match_kind")
    if match not in MATCH_KINDS:
        raise ValidationError(
            f"match_kind must be one of {list(MATCH_KINDS)}; got {match!r}. "
            "Installation and execution are derived states, not source kinds.")
    external_id = _optional_text(external_id, "external_id")
    title = _optional_text(title, "title")
    source_version = _optional_text(version, "version")
    query = _optional_text(query, "query")
    endpoint = _optional_text(endpoint, "endpoint")
    locator = _optional_text(locator, "locator")
    quote = _optional_text(quote, "quote")
    notes = _optional_text(notes, "notes")
    snapshot_text = _optional_text(snapshot_path, "snapshot_path")
    if match == "text_match" and quote is None:
        raise ValidationError("match_kind='text_match' requires a quote")
    if match == "snapshot_bytes" and snapshot_text is None:
        raise ValidationError(
            "match_kind='snapshot_bytes' requires a snapshot_path")
    if not any((external_id, endpoint, locator, snapshot_text)):
        raise ValidationError(
            "a source record needs at least one source reference: "
            "external_id, endpoint, locator or snapshot_path; a query or "
            "title alone is not a checkable reference")
    stamp = now()
    retrieved = _iso(_parse_utc(retrieved_utc, "retrieved_utc")) \
        if retrieved_utc is not None else stamp
    explicit_dedup = _optional_text(dedup_key, "dedup_key")
    created = False
    source_id: str
    with _open(project_dir) as (proj, conn, project_id):
        with _write(conn):
            row = _ledger_row(conn, project_id, lid)
            if row["ledger_id"] != lid:
                raise NotFoundError(
                    f"ledger id {lid!r} not found; add_source requires an id")
            snapshot_abs, snapshot_sha = _record_snapshot(
                proj, snapshot_text, expected_sha256)
            quote_sha = sha256_text(quote) if quote is not None else None
            payload = _source_payload(
                kind, external_id, title, source_version, query, endpoint,
                locator, match, quote, quote_sha, snapshot_sha, notes)
            auto_key = "auto:" + sha256_text(_json_text(payload))
            dedup = explicit_dedup or auto_key
            existing = conn.execute(
                """SELECT * FROM method_ledger_source
                   WHERE project_id=? AND ledger_id=? AND dedup_key=?""",
                (project_id, lid, dedup)).fetchone()
            if existing is not None:
                if _row_source_payload(existing) != payload:
                    raise ConflictError(
                        f"dedup_key {dedup!r} already names a different source "
                        f"record ({existing['source_id']!r}); append a new "
                        "record with a new dedup_key instead")
                source_id = existing["source_id"]
            else:
                source_id = new_id("mlsrc")
                next_version = conn.execute(
                    """SELECT COALESCE(MAX(record_version), 0)+1 v
                       FROM method_ledger_source
                       WHERE project_id=? AND ledger_id=?""",
                    (project_id, lid)).fetchone()["v"]
                conn.execute(
                    """INSERT INTO method_ledger_source(source_id, project_id,
                           ledger_id, record_version, source_kind, external_id,
                           title, version, query, endpoint, locator, match_kind,
                           quote, quote_sha256, snapshot_path, snapshot_sha256,
                           retrieved_utc, notes, dedup_key, created_utc)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (source_id, project_id, lid, next_version, kind,
                     external_id, title, source_version, query, endpoint,
                     locator, match, quote, quote_sha, snapshot_abs,
                     snapshot_sha, retrieved, notes, dedup, stamp))
                conn.execute(
                    "UPDATE method_ledger SET updated_utc=? "
                    "WHERE project_id=? AND ledger_id=?",
                    (stamp, project_id, lid))
                created = True
        recorded = conn.execute(
            "SELECT * FROM method_ledger_source WHERE source_id=?",
            (source_id,)).fetchone()
        with _read(conn):
            source = _source_dict(recorded, True)
    return {"source": source, "created": created}


def list_sources(project_dir: str | Path, ledger_id: str, *,
                 limit: int = DEFAULT_SOURCE_PAGE,
                 offset: int = 0,
                 verify_snapshots: bool = True) -> dict:
    """Page the append-only source records, newest record version first."""
    lid = _require_text(ledger_id, "ledger_id")
    limit, offset = _page(limit, offset)
    if not isinstance(verify_snapshots, bool):
        raise ValidationError("verify_snapshots must be a boolean")
    with _open(project_dir) as (_proj, conn, project_id):
        with _read(conn):
            row = _ledger_row(conn, project_id, lid)
            if row["ledger_id"] != lid:
                raise NotFoundError(
                    f"ledger id {lid!r} not found; list_sources requires an id")
            page = _sources_page(conn, project_id, lid, limit=limit,
                                 offset=offset,
                                 verify_snapshots=verify_snapshots)
            page["ledger_id"] = lid
            return page


# ---------------------------------------------------------------------- probes
def _probe_argv(argv: Any) -> list[str]:
    if isinstance(argv, (str, bytes)) or not isinstance(argv, (list, tuple)):
        raise ValidationError(
            "argv must be a non-empty list of strings; a shell string is not "
            "accepted")
    if not argv:
        raise ValidationError("argv must not be empty")
    out: list[str] = []
    for item in argv:
        if not isinstance(item, str):
            raise ValidationError("every argv item must be a string")
        if "\x00" in item:
            raise ValidationError("argv items must not contain NUL")
        out.append(item)
    if not out[0].strip():
        raise ValidationError("argv[0] must be a non-empty executable path")
    return out


def _probe_timeout(timeout: Any) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
            or timeout <= 0:
        raise ValidationError("timeout must be a positive number of seconds")
    if timeout > MAX_PROBE_SECONDS:
        raise ValidationError(
            f"timeout must be <= {MAX_PROBE_SECONDS} seconds")
    return float(timeout)


def probe_installation(project_dir: str | Path, ledger_id: str, argv: Any, *,
                       timeout: float = 60, cwd: str | None = None,
                       env: dict[str, str] | None = None) -> dict:
    """Actually run ``argv`` (no shell) and record the observed result.

    The row captures the exact argv, working directory, return code, timeout
    flag, duration and sha256 of stdout/stderr. Only this recorded process
    result can change installation from ``unprobed``. A probe is not a method
    task: it never affects the derived execution state.

    ``cwd`` is resolved relative to the project root when it is not absolute.
    ``env``, when supplied, is the complete child environment rather than an
    overlay on the parent's environment; pass the inherited mapping explicitly
    if that is what you want.
    """
    lid = _require_text(ledger_id, "ledger_id")
    command = _probe_argv(argv)
    seconds = _probe_timeout(timeout)
    cwd_text = _optional_text(cwd, "cwd")
    if env is not None and not isinstance(env, dict):
        raise ValidationError("env must be a mapping of strings or null")
    env_map: dict[str, str] | None = None
    if env is not None:
        env_map = {}
        for key, value in env.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValidationError("env keys and values must be strings")
            env_map[key] = value
    with _open(project_dir) as (proj, conn, project_id):
        with _read(conn):
            row = _ledger_row(conn, project_id, lid)
            if row["ledger_id"] != lid:
                raise NotFoundError(
                    f"ledger id {lid!r} not found; probe_installation requires "
                    "an id")
        workdir = str(_resolve_snapshot(proj, cwd_text)) if cwd_text else None
    started = time.monotonic()
    timed_out = False
    launch_error: str | None = None
    returncode: int | None = None
    stdout = b""
    stderr = b""
    try:
        proc = subprocess.run(command, cwd=workdir, env=env_map,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=seconds, check=False)
        returncode = proc.returncode
        stdout = proc.stdout or b""
        stderr = proc.stderr or b""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
        if isinstance(stdout, str):
            stdout = stdout.encode("utf-8", "replace")
        if isinstance(stderr, str):
            stderr = stderr.encode("utf-8", "replace")
    except (OSError, ValueError) as exc:
        launch_error = f"{type(exc).__name__}: {exc}"
    duration = time.monotonic() - started
    stamp = now()
    with _open(project_dir) as (_proj, conn, project_id):
        with _write(conn):
            row = _ledger_row(conn, project_id, lid)
            if row["ledger_id"] != lid:
                raise NotFoundError(
                    f"ledger id {lid!r} not found; probe_installation requires "
                    "an id")
            probe_id = new_id("mlprobe")
            next_version = conn.execute(
                """SELECT COALESCE(MAX(record_version), 0)+1 v
                   FROM method_ledger_probe
                   WHERE project_id=? AND ledger_id=?""",
                (project_id, lid)).fetchone()["v"]
            conn.execute(
                """INSERT INTO method_ledger_probe(probe_id, project_id,
                       ledger_id, record_version, probe_kind, command_json, cwd,
                       returncode, launch_error, timed_out, duration_seconds,
                       stdout_sha256, stderr_sha256, stdout_excerpt,
                       stderr_excerpt, checked_utc)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (probe_id, project_id, lid, next_version, "install",
                 _json_text(command), workdir, returncode, launch_error,
                 1 if timed_out else 0, duration, _sha256_bytes(stdout),
                 _sha256_bytes(stderr), _excerpt(stdout), _excerpt(stderr),
                 stamp))
            conn.execute(
                "UPDATE method_ledger SET updated_utc=? "
                "WHERE project_id=? AND ledger_id=?",
                (stamp, project_id, lid))
        recorded = conn.execute(
            "SELECT * FROM method_ledger_probe WHERE probe_id=?",
            (probe_id,)).fetchone()
        probe = _probe_dict(recorded)
        installation = _installation_from_probe(recorded)
    return {"probe": probe, "installation": installation}
