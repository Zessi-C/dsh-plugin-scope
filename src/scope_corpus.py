"""Read-only access to the projected SCOPE run-time corpus (``records.sqlite``).

This module is the *storage and query* layer only. It never derives evidence
units, never applies the authoritative PMID list and never decides what is
evidence: those live in ``scope_knowledge``. Here we define

* the on-disk schema (``SCHEMA_SQL``) written by ``tools/build_runtime_corpus.py``;
* the projection whitelist that turns a development extraction record into the
  row payload a reader is allowed to see (``project_record``);
* read-only helpers that open the database (``file:...?mode=ro`` and
  ``PRAGMA query_only=ON``), validate its identity and serve rows.

Hard rules
----------
1. Read-only. ``open_db`` refuses to create a file, refuses to migrate a schema
   and refuses to rebuild anything. A missing file, a different ``user_version``
   / ``schema_id``, or a missing FTS5 index raises instead of degrading to the
   development JSONL files. There is no run-time JSONL path.
2. No raw records. ``payload_json`` holds the whitelisted projection produced by
   ``project_record``; dropped keys are listed in ``DROPPED_FIELDS`` and the
   builder never writes the original line. A reader cannot reconstruct the
   extraction record object, the brief or the paper text.
3. Scope and retraction stay read-time decisions. The database carries the
   build-time PMID list and the frozen retraction registry snapshots
   (``pmids``, ``retractions``, ``editorial_notices``) so a run can be explained
   offline, but a caller must apply its own live list and registry on top.
   ``pmid_refs`` records every PMID-shaped token a stored row's raw JSON
   mentions, so a notice added *after* the build still excludes a row that
   merely cites the retracted paper; ``records.retracted_ref`` /
   ``code_sources.retracted_ref`` are the frozen build-time verdict for
   provenance and for the build-time exclusion count only.
4. ``seq`` is contiguous over the *stored* rows in corpus order (workflows,
   methods, patterns). Dropped or filtered rows leave no gap-free guarantee, but
   the relative order of the remaining rows is identical to the in-memory JSONL
   index, so ordering by ``(pmid, seq)`` is unchanged.
5. Text search is a *candidate prefilter*, not the matcher. ``search_hit_rowids``
   returns a superset of the units whose normalised text contains every (or any)
   query term as a substring; the caller re-applies ``scope_knowledge``'s ASCII
   token-boundary regex, which preserves ``UC`` vs ``AUCell``, ``CD4+``,
   ``PD-1`` and CJK substring semantics. The FTS5 index uses the ``trigram``
   tokenizer for exactly that reason: ``unicode61`` drops punctuation and does
   not match ``uc`` inside ``uc患者``.

No full paper text
------------------
The database holds citation-sized evidence excerpts that the extraction already
carries (``verbatim``, statement fields) — never a brief, a full paper, a PDF
page dump or a code body. There is deliberately no full-text index and no
ingest path for paper files: a search surface over complete papers is out of
scope for this artifact.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.request import pathname2url

__all__ = [
    "CorpusDbError",
    "CorpusDbMissing",
    "CorpusDbSchemaError",
    "DB_ENV",
    "DB_NAME",
    "DROPPED_FIELDS",
    "FTS_TOKENIZER",
    "MIN_FTS_TERM",
    "PROJECTION",
    "SCHEMA_ID",
    "SCHEMA_SQL",
    "SCHEMA_VERSION",
    "code_source_rows",
    "counts",
    "create_schema",
    "editorial_notice_rows",
    "field_names",
    "fts5_available",
    "hit_rows",
    "iter_record_rows",
    "open_db",
    "pmid_refs",
    "pmid_rows",
    "pmids_mentioning",
    "project_record",
    "read_meta",
    "record_row",
    "resolve_db_path",
    "resolve_research_dir",
    "retracted_pmids",
    "search_hit_rowids",
]

SCHEMA_ID = "sctda.scope_corpus/2"
SCHEMA_VERSION = 2
DB_NAME = "records.sqlite"
DB_ENV = "SCOPE_CORPUS_DB"
RESEARCH_DIR_ENV = "SCOPE_RESEARCH_DIR"
CORPUS_SUBDIR = "corpus"
DEFAULT_RESEARCH_DIR = Path(__file__).resolve().parent.parent / "research"
FTS_TOKENIZER = "trigram"
#: FTS5 trigram needs at least three characters; shorter terms narrow with LIKE.
MIN_FTS_TERM = 3

TABLES = (
    "meta",
    "sources",
    "records",
    "hits",
    "hits_fts",
    "code_sources",
    "pmids",
    "retractions",
    "editorial_notices",
    "pmid_refs",
)

# --------------------------------------------------------------- projection

#: Field whitelist per source kind. A key absent from the raw record stays
#: absent in the projection, so ``record.get(key)`` keeps its old meaning.
PROJECTION: dict[str, dict[str, Any]] = {
    "workflow": {
        "fields": (
            "workflow_id",
            "pmid",
            "comparison",
            "cell_level",
            "author_focus_groups",
            "no_prior_warning",
            "citation",
        ),
        "question": ("text", "evidence"),
        "question_evidence": ("locator", "verbatim"),
        "steps": (
            "observation",
            "locator",
            "verbatim",
            "next_analysis",
            "rationale",
        ),
        "claims": ("claim", "locator", "verbatim", "evidence_type", "direction"),
        "gaps": ("text", "locator", "verbatim", "gap", "note", "claim", "direction"),
        "author_focus_groups": ("group", "name", "locator", "verbatim"),
    },
    "method": {
        "fields": (
            "instance_id",
            "pmid",
            "mentioned_name",
            "canonical_name",
            "package",
            "operation",
            "pipeline_stage",
            "analysis_object",
            "inputs",
            "outputs",
            "purpose",
            "constraints",
            "version_in_text",
            "method_class",
            "in_scope",
            "scope_reason",
            "rerunnable_role",
            "params",
            "configuration",
            "locator",
            "verbatim",
        ),
    },
    "pattern": {
        "fields": (
            "pmid",
            "status",
            "source_sha256",
            "question",
            "citation",
            "reading_scope",
            "read_scope",
            "unread_parts",
            "unresolved_reason",
        ),
        "patterns": (
            "source_locator",
            "verbatim",
            "pdf_quote",
            "pdf_page",
            "pdf_journal_page",
            "extra_locators",
            "pdf_quote_note",
            "anchor_note",
            "observation",
            "ask_next",
            "limitations",
            "prompt_lesson",
            "rationale_source",
            "provenance",
            "anchor_type",
            "verbatim_unique_in_brief",
            "verbatim_occurrences",
        ),
    },
    "code_source": {
        "fields": (
            "source_id",
            "pmid",
            "url",
            "url_kind",
            "ownership",
            "locator",
            "verbatim",
            "resolves_to",
            "inputs",
            "outputs",
            "params_found",
            "status",
            "checked_utc",
            "notes",
        ),
    },
}

#: Record-level keys that intentionally do not survive the projection. They are
#: repair/audit bookkeeping and discussion transcript, read by no unit builder
#: and by no ``record_context``; dropping them narrows ``record()`` output only.
DROPPED_FIELDS: dict[str, tuple[str, ...]] = {
    "workflow": (),
    "method": (),
    "pattern": (
        "sources",
        "source",
        "discussion_notes",
        "repair",
        "brief_defects",
        "corrections",
        "batch",
        "prior_status",
        "remaining",
        "retraction",
        "remaining_obstacles",
    ),
    "code_source": (),
}


def project_record(kind: str, record: Mapping[str, Any]) -> dict:
    """Projected mapping for one extraction record (whitelist, no raw text).

    Nothing is invented: absent keys stay absent and non-whitelisted keys are
    dropped. Nested containers that are neither mappings nor lists are passed
    through unchanged (they are scalar metadata such as PMID lists).
    """
    kind = str(kind).strip().lower()
    rules = PROJECTION.get(kind)
    if rules is None:
        raise CorpusDbError(f"unknown record kind: {kind!r}")
    out: dict[str, Any] = {}
    for field in rules["fields"]:
        if field in record:
            out[field] = record[field]
    if kind == "workflow":
        question = record.get("question")
        if isinstance(question, Mapping):
            projected_question = {
                key: question[key] for key in rules["question"] if key in question
            }
        elif question is not None:
            projected_question = question
        else:
            projected_question = None
        evidence = question.get("evidence") if isinstance(question, Mapping) else None
        if isinstance(evidence, list):
            projected_question["evidence"] = [
                {
                    key: item[key]
                    for key in rules["question_evidence"]
                    if key in item
                }
                for item in evidence
                if isinstance(item, Mapping)
            ]
        if projected_question is not None:
            out["question"] = projected_question
        for key in ("steps", "claims"):
            items = record.get(key)
            if isinstance(items, list):
                out[key] = [
                    {name: item[name] for name in rules[key] if name in item}
                    for item in items
                    if isinstance(item, Mapping)
                ]
            elif items is not None:
                out[key] = items
        gaps = record.get("gaps")
        if isinstance(gaps, list):
            out["gaps"] = [
                {name: item[name] for name in rules["gaps"] if name in item}
                if isinstance(item, Mapping)
                else item
                for item in gaps
            ]
        elif gaps is not None:
            out["gaps"] = gaps
        focus = record.get("author_focus_groups")
        if isinstance(focus, list):
            out["author_focus_groups"] = [
                {name: item[name] for name in rules["author_focus_groups"] if name in item}
                if isinstance(item, Mapping)
                else item
                for item in focus
            ]
    elif kind == "pattern":
        patterns = record.get("patterns")
        if isinstance(patterns, list):
            out["patterns"] = [
                {name: item[name] for name in rules["patterns"] if name in item}
                for item in patterns
                if isinstance(item, Mapping)
            ]
        elif patterns is not None:
            out["patterns"] = patterns
    return out


# --------------------------------------------------------------------- errors


class CorpusDbError(RuntimeError):
    """Base error for the on-disk corpus database."""


class CorpusDbMissing(CorpusDbError):
    """No database file at the resolved path; run the builder first."""


class CorpusDbSchemaError(CorpusDbError):
    """The file is not a database this module can read."""


# ----------------------------------------------------------------------- paths


def resolve_research_dir(research_dir: str | Path | None = None) -> Path:
    """``research_dir`` kwarg > ``SCOPE_RESEARCH_DIR`` > the repository default."""
    if research_dir is not None:
        base = Path(research_dir).expanduser()
    else:
        base = Path(os.environ.get(RESEARCH_DIR_ENV) or DEFAULT_RESEARCH_DIR).expanduser()
    return base


def resolve_db_path(
    research_dir: str | Path | None = None,
    db_path: str | Path | None = None,
) -> Path:
    """Resolve the runtime database path.

    Order: ``db_path`` argument, then ``SCOPE_CORPUS_DB``, then
    ``<research_dir>/corpus/records.sqlite``. A relative path is resolved
    against ``research_dir`` for all three sources.
    """
    base = resolve_research_dir(research_dir)
    raw = db_path if db_path is not None else os.environ.get(DB_ENV)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return base / CORPUS_SUBDIR / DB_NAME
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base / path
    return path


# ---------------------------------------------------------------------- open


def fts5_available() -> bool:
    """True when the linked SQLite has FTS5 (the runtime needs it)."""
    try:
        conn = sqlite3.connect(":memory:")
    except sqlite3.Error:  # pragma: no cover - a broken sqlite3 module
        return False
    try:
        conn.execute("CREATE VIRTUAL TABLE _probe USING fts5(x, tokenize='trigram')")
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return True


def _connect_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{pathname2url(str(path))}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.OperationalError as exc:
        raise CorpusDbMissing(
            f"corpus database is not readable: {path} ({exc}); "
            "build it with tools/build_runtime_corpus.py"
        ) from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def open_db(
    path: str | Path,
    *,
    require_fts: bool = True,
    verify_counts: bool = True,
) -> sqlite3.Connection:
    """Open the runtime database read-only and validate its identity.

    Raises :class:`CorpusDbMissing` when the file is absent and
    :class:`CorpusDbSchemaError` when the file exists but is not a
    ``SCHEMA_ID`` database at ``SCHEMA_VERSION``. Never creates or migrates.
    """
    database = Path(path).expanduser()
    if not database.is_file():
        raise CorpusDbMissing(
            f"corpus database missing: {database}; "
            "build it with tools/build_runtime_corpus.py"
        )
    conn = _connect_ro(database)
    try:
        _validate(conn, require_fts=require_fts, verify_counts=verify_counts)
    except Exception:
        conn.close()
        raise
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }


def _validate(
    conn: sqlite3.Connection, *, require_fts: bool, verify_counts: bool
) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise CorpusDbSchemaError(
            f"corpus database user_version={version}, expected {SCHEMA_VERSION}; "
            "rebuild it with tools/build_runtime_corpus.py"
        )
    names = _table_names(conn)
    missing = [name for name in TABLES if name not in names]
    if missing:
        raise CorpusDbSchemaError(
            f"corpus database is missing tables {missing}; expected {SCHEMA_ID}"
        )
    meta = read_meta(conn)
    if meta.get("schema_id") != SCHEMA_ID:
        raise CorpusDbSchemaError(
            f"corpus database schema_id={meta.get('schema_id')!r}, expected {SCHEMA_ID!r}"
        )
    if int(meta.get("schema_version") or 0) != SCHEMA_VERSION:
        raise CorpusDbSchemaError(
            "corpus database meta schema_version="
            f"{meta.get('schema_version')!r}, expected {SCHEMA_VERSION}"
        )
    if require_fts:
        enabled = bool(meta.get("fts_enabled"))
        if not enabled:
            raise CorpusDbSchemaError(
                "corpus database was built without the FTS5 index; "
                "rebuild with tools/build_runtime_corpus.py"
            )
        if not fts5_available():
            raise CorpusDbSchemaError(
                "this Python's SQLite has no FTS5 support; the corpus database "
                "cannot be queried without it"
            )
        try:
            conn.execute("SELECT rowid FROM hits_fts LIMIT 0").fetchall()
        except sqlite3.Error as exc:
            raise CorpusDbSchemaError(
                f"corpus database hits_fts is unreadable: {exc}"
            ) from exc
    if verify_counts:
        expected_hits = int(meta.get("n_hits") or 0)
        actual_hits = conn.execute("SELECT COUNT(*) FROM hits").fetchone()[0]
        if actual_hits != expected_hits:
            raise CorpusDbSchemaError(
                f"corpus database hits rows={actual_hits}, meta n_hits={expected_hits}; "
                "the file was modified after the build"
            )
        expected_records = int(meta.get("n_records") or 0)
        actual_records = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        if actual_records != expected_records:
            raise CorpusDbSchemaError(
                "corpus database records rows="
                f"{actual_records}, meta n_records={expected_records}"
            )


# ---------------------------------------------------------------------- meta


def _decode_meta(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def read_meta(conn: sqlite3.Connection) -> dict:
    """Every ``meta`` row, JSON-decoded (raw text when it is not JSON)."""
    out: dict[str, Any] = {}
    for row in conn.execute("SELECT key, value FROM meta ORDER BY key"):
        out[str(row[0])] = _decode_meta(row[1])
    return out


def counts(conn: sqlite3.Connection) -> dict:
    """Live row counts, for a caller that wants them without trusting meta."""
    return {
        "records": conn.execute("SELECT COUNT(*) FROM records").fetchone()[0],
        "hits": conn.execute("SELECT COUNT(*) FROM hits").fetchone()[0],
        "code_sources": conn.execute("SELECT COUNT(*) FROM code_sources").fetchone()[0],
        "pmids": conn.execute("SELECT COUNT(*) FROM pmids").fetchone()[0],
        "pmid_refs": conn.execute("SELECT COUNT(*) FROM pmid_refs").fetchone()[0],
    }


def field_names(conn: sqlite3.Connection) -> list[str]:
    """Every field name a hit carries a tier for (the ``fields=`` vocabulary)."""
    meta = read_meta(conn)
    names = meta.get("field_names")
    if isinstance(names, list) and names:
        return [str(name) for name in names]
    found = {
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT key FROM hits, json_each(hits.field_tiers_json)"
        )
    }
    return sorted(found)


# -------------------------------------------------------------------- records


def _record_payload(row: sqlite3.Row) -> dict:
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, ValueError) as exc:  # pragma: no cover - builder guarantees JSON
        raise CorpusDbSchemaError(
            f"record {row['record_uid']} payload is not JSON: {exc}"
        ) from exc
    return payload if isinstance(payload, dict) else {}


def _record_row_to_dict(row: sqlite3.Row) -> dict:
    in_scope = row["in_scope"]
    return {
        "record_uid": row["record_uid"],
        "source_kind": row["source_kind"],
        "source_label": row["source_label"],
        "source_file": row["source_file"],
        "source_sha256": row["source_sha256"],
        "pmid": row["pmid"],
        "record_id": row["record_id"],
        "row": row["row"],
        "record_payload": _record_payload(row),
        "retracted_ref": bool(row["retracted_ref"]),
        "in_scope": None if in_scope is None else bool(in_scope),
    }


def iter_record_rows(
    conn: sqlite3.Connection,
    *,
    pmid: str | None = None,
    source_kind: str | None = None,
    record_id: str | None = None,
    record_uids: Iterable[str] | None = None,
    include_retracted_ref: bool = True,
):
    """Yield projected record rows in corpus order (source file, then row)."""
    clauses: list[str] = []
    params: list[Any] = []
    if pmid is not None:
        clauses.append("pmid = ?")
        params.append(str(pmid))
    if source_kind is not None:
        clauses.append("source_kind = ?")
        params.append(str(source_kind).casefold())
    if record_id is not None:
        clauses.append("record_id = ?")
        params.append(str(record_id))
    if record_uids is not None:
        uids = [str(uid) for uid in record_uids]
        if not uids:
            return
        clauses.append(f"record_uid IN ({','.join('?' * len(uids))})")
        params.extend(uids)
    if not include_retracted_ref:
        clauses.append("retracted_ref = 0")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        "SELECT record_uid, source_kind, source_label, source_file, source_sha256, "
        "pmid, record_id, row, payload_json, retracted_ref, in_scope "
        f"FROM records{where} ORDER BY source_order, row"
    )
    for row in conn.execute(sql, params):
        yield _record_row_to_dict(row)


def record_row(conn: sqlite3.Connection, record_uid: str) -> dict | None:
    """One projected record row by ``record_uid``, or ``None``."""
    row = conn.execute(
        "SELECT record_uid, source_kind, source_label, source_file, source_sha256, "
        "pmid, record_id, row, payload_json, retracted_ref, in_scope "
        "FROM records WHERE record_uid = ?",
        (str(record_uid),),
    ).fetchone()
    return _record_row_to_dict(row) if row is not None else None


# ----------------------------------------------------------------------- hits

_HIT_COLUMNS = (
    "hits.rowid AS rowid, hits.evidence_id AS evidence_id, hits.record_uid AS record_uid, "
    "hits.seq AS seq, hits.unit_index AS unit_index, hits.granularity AS granularity, "
    "hits.locator AS locator, hits.locator_type AS locator_type, "
    "hits.verbatim AS verbatim, hits.is_anchored AS is_anchored, "
    "hits.in_scope AS in_scope, hits.content_json AS content_json, "
    "hits.record_context_json AS record_context_json, "
    "hits.field_tiers_json AS field_tiers_json, "
    "hits.context_fields_json AS context_fields_json, "
    "hits.search_fields_json AS search_fields_json, hits.search_text AS search_text, "
    "records.source_kind AS source_kind, records.source_label AS source_label, "
    "records.source_file AS source_file, records.source_sha256 AS source_sha256, "
    "records.pmid AS pmid, records.record_id AS record_id, "
    "records.retracted_ref AS retracted_ref"
)


def _decode_list(value: str) -> list:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):  # pragma: no cover - builder guarantees JSON
        return []
    return decoded if isinstance(decoded, list) else []


def _decode_dict(value: str) -> dict:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):  # pragma: no cover - builder guarantees JSON
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _hit_row_to_dict(row: sqlite3.Row) -> dict:
    in_scope = row["in_scope"]
    return {
        "rowid": row["rowid"],
        "evidence_id": row["evidence_id"],
        "record_uid": row["record_uid"],
        "source_kind": row["source_kind"],
        "source_label": row["source_label"],
        "source_file": row["source_file"],
        "source_sha256": row["source_sha256"],
        "pmid": row["pmid"],
        "record_id": row["record_id"],
        "seq": int(row["seq"]),
        "unit_index": int(row["unit_index"]),
        "granularity": row["granularity"],
        "locator": row["locator"],
        "locator_type": row["locator_type"],
        "verbatim": row["verbatim"],
        "content": _decode_list(row["content_json"]),
        "record_context": _decode_list(row["record_context_json"]),
        "field_tiers": _decode_dict(row["field_tiers_json"]),
        "context_fields": _decode_list(row["context_fields_json"]),
        "search_fields": _decode_list(row["search_fields_json"]),
        "search_text": row["search_text"],
        "is_anchored": bool(row["is_anchored"]),
        "in_scope": None if in_scope is None else bool(in_scope),
        "retracted_ref": bool(row["retracted_ref"]),
    }


def hit_rows(
    conn: sqlite3.Connection,
    *,
    pmids: Iterable[str] | None = None,
    record_uid: str | None = None,
    evidence_id: str | None = None,
    rowids: Iterable[int] | None = None,
    source_kinds: Iterable[str] | None = None,
    granularities: Iterable[str] | None = None,
    anchored_only: bool = False,
    include_pmids: Iterable[str] | None = None,
    exclude_pmids: Iterable[str] | None = None,
    exclude_retracted_ref: bool = True,
) -> list[dict]:
    """Hit rows ordered by ``(pmid, seq)``.

    All filters are ANDed and applied in SQL so a caller can page through the
    corpus without materialising every row. ``exclude_retracted_ref=True`` (the
    default) drops units whose *record* mentions a retracted PMID anywhere; that
    mirrors the JSONL reader, which never indexes those records at all.
    Retraction of a hit's own PMID is the caller's gate — such records are not
    stored, but the live registry can grow after the build, so the caller must
    still consult it.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if source_kinds is not None:
        values = [str(item).casefold() for item in source_kinds]
        if not values:
            return []
        clauses.append(f"records.source_kind IN ({','.join('?' * len(values))})")
        params.extend(values)
    if granularities is not None:
        values = [str(item) for item in granularities]
        if not values:
            return []
        clauses.append(f"hits.granularity IN ({','.join('?' * len(values))})")
        params.extend(values)
    if anchored_only:
        clauses.append("hits.is_anchored = 1")
    if include_pmids is not None:
        values = [str(item) for item in include_pmids]
        if not values:
            return []
        clauses.append(f"records.pmid IN ({','.join('?' * len(values))})")
        params.extend(values)
    if exclude_pmids is not None:
        values = [str(item) for item in exclude_pmids]
        if values:
            clauses.append(f"records.pmid NOT IN ({','.join('?' * len(values))})")
            params.extend(values)
    if pmids is not None:
        values = [str(item) for item in pmids]
        if not values:
            return []
        clauses.append(f"records.pmid IN ({','.join('?' * len(values))})")
        params.extend(values)
    if record_uid is not None:
        clauses.append("hits.record_uid = ?")
        params.append(str(record_uid))
    if evidence_id is not None:
        clauses.append("hits.evidence_id = ?")
        params.append(str(evidence_id))
    if rowids is not None:
        values = [int(item) for item in rowids]
        if not values:
            return []
        clauses.append(f"hits.rowid IN ({','.join('?' * len(values))})")
        params.extend(values)
    if exclude_retracted_ref:
        clauses.append("records.retracted_ref = 0")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        f"SELECT {_HIT_COLUMNS} FROM hits JOIN records ON records.record_uid = hits.record_uid"
        f"{where} ORDER BY records.pmid, hits.seq"
    )
    return [_hit_row_to_dict(row) for row in conn.execute(sql, params)]


# ------------------------------------------------------------------- text FTS

_WS_RE = re.compile(r"\s+")


def _normalize_term(value: Any) -> str:
    return _WS_RE.sub(" ", str(value)).strip().casefold()


def _like_pattern(term: str) -> str:
    escaped = (
        term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


def _like_rowids(conn: sqlite3.Connection, term: str) -> set[int]:
    rows = conn.execute(
        "SELECT rowid FROM hits WHERE search_text LIKE ? ESCAPE '\\'", (_like_pattern(term),)
    )
    return {int(row[0]) for row in rows}


def search_hit_rowids(
    conn: sqlite3.Connection,
    terms: Sequence[str],
    *,
    match: str = "all",
) -> set[int] | None:
    """Candidate ``hits.rowid`` set for normalised *terms*.

    Returns ``None`` when there are no usable terms (meaning "no text filter").
    Each term is narrowed with the trigram FTS index when it is at least
    :data:`MIN_FTS_TERM` characters and with ``LIKE`` otherwise, so the result
    never misses a unit whose normalised text contains the term as a substring.
    The caller must still apply its own token-boundary matcher: this is a
    prefilter, deliberately broader than the retrieval semantics.
    """
    if match not in ("all", "any"):
        raise CorpusDbError("match must be 'all' or 'any'")
    normalized: list[str] = []
    for term in terms:
        text = _normalize_term(term)
        if text and text not in normalized:
            normalized.append(text)
    if not normalized:
        return None
    sets: list[set[int]] = []
    for term in normalized:
        found: set[int] | None = None
        if len(term) >= MIN_FTS_TERM:
            expression = '"' + term.replace('"', '""') + '"'
            try:
                found = {
                    int(row[0])
                    for row in conn.execute(
                        "SELECT rowid FROM hits_fts WHERE hits_fts MATCH ?",
                        (expression,),
                    )
                }
            except sqlite3.OperationalError:
                found = None
        sets.append(found if found is not None else _like_rowids(conn, term))
    if match == "all":
        result = set(sets[0])
        for item in sets[1:]:
            result &= item
        return result
    result = set()
    for item in sets:
        result |= item
    return result


# ------------------------------------------------------------------ code data


def code_source_rows(
    conn: sqlite3.Connection,
    pmid: str | None = None,
    *,
    exclude_retracted_ref: bool = True,
) -> list[dict]:
    """Projected ``code_sources`` payloads in file order (the extraction order)."""
    clauses: list[str] = []
    params: list[Any] = []
    if pmid is not None:
        clauses.append("pmid = ?")
        params.append(str(pmid))
    if exclude_retracted_ref:
        clauses.append("retracted_ref = 0")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    out: list[dict] = []
    for row in conn.execute(
        "SELECT rowid, source_id, pmid, locator, url, payload_json, retracted_ref "
        f"FROM code_sources{where} ORDER BY row",
        params,
    ):
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):  # pragma: no cover
            payload = {}
        out.append(
            {
                "rowid": row["rowid"],
                "source_id": row["source_id"],
                "pmid": row["pmid"],
                "locator": row["locator"],
                "url": row["url"],
                "retracted_ref": bool(row["retracted_ref"]),
                "payload": payload if isinstance(payload, dict) else {},
            }
        )
    return out


# --------------------------------------------------------------- registries


def retracted_pmids(conn: sqlite3.Connection) -> set[str]:
    """The frozen retraction registry as recorded at build time."""
    return {str(row[0]) for row in conn.execute("SELECT pmid FROM retractions")}


def editorial_notice_rows(conn: sqlite3.Connection) -> list[dict]:
    out: list[dict] = []
    for row in conn.execute(
        "SELECT pmid, payload_json FROM editorial_notices ORDER BY pmid"
    ):
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):  # pragma: no cover
            payload = {}
        out.append(
            {"pmid": row["pmid"], "payload": payload if isinstance(payload, dict) else {}}
        )
    return out


def pmid_refs(
    conn: sqlite3.Connection,
    *,
    ref_pmids: Iterable[str] | None = None,
    owner: str | None = None,
    owner_keys: Iterable[str] | None = None,
) -> list[dict]:
    """``(ref_pmid, owner, owner_key)`` rows, ordered for stable output."""
    clauses: list[str] = []
    params: list[Any] = []
    if ref_pmids is not None:
        values = [str(item) for item in ref_pmids]
        if not values:
            return []
        clauses.append(f"ref_pmid IN ({','.join('?' * len(values))})")
        params.extend(values)
    if owner is not None:
        clauses.append("owner = ?")
        params.append(str(owner))
    if owner_keys is not None:
        values = [str(item) for item in owner_keys]
        if not values:
            return []
        clauses.append(f"owner_key IN ({','.join('?' * len(values))})")
        params.extend(values)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return [
        {"ref_pmid": row[0], "owner": row[1], "owner_key": row[2]}
        for row in conn.execute(
            f"SELECT ref_pmid, owner, owner_key FROM pmid_refs{where}"
            " ORDER BY owner, owner_key, ref_pmid",
            params,
        )
    ]


def pmids_mentioning(
    conn: sqlite3.Connection, ref_pmids: Iterable[str], *, owner: str = "record"
) -> set[str]:
    """Owner keys whose raw JSON mentions any of *ref_pmids*.

    The reader passes its **live** retraction PMIDs here; the answer covers rows
    built before the notice existed. Returns ``owner_key`` values, which are
    ``records.record_uid`` for ``owner='record'`` and the stringified
    ``code_sources.rowid`` for ``owner='code_source'``.
    """
    values = [str(item) for item in ref_pmids]
    if not values:
        return set()
    return {
        str(row[0])
        for row in conn.execute(
            f"SELECT DISTINCT owner_key FROM pmid_refs WHERE owner = ? AND "
            f"ref_pmid IN ({','.join('?' * len(values))})",
            [str(owner), *values],
        )
    }


def pmid_rows(conn: sqlite3.Connection) -> list[dict]:
    """Per-PMID summary rows: scope verdict, stored record count, retraction."""
    return [
        {
            "pmid": row["pmid"],
            "in_scope": None if row["in_scope"] is None else bool(row["in_scope"]),
            "n_records": int(row["n_records"]),
            "retracted": bool(row["retracted"]),
        }
        for row in conn.execute(
            "SELECT pmid, in_scope, n_records, retracted FROM pmids ORDER BY pmid"
        )
    ]


# -------------------------------------------------------------------- schema

SCHEMA_SQL = f"""
PRAGMA journal_mode=DELETE;
PRAGMA foreign_keys=ON;

CREATE TABLE meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE sources (
    name            TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    label           TEXT NOT NULL,
    path            TEXT NOT NULL,
    sha256          TEXT,
    n_records       INTEGER NOT NULL DEFAULT 0,
    manifest_sha256 TEXT,
    manifest_status TEXT NOT NULL DEFAULT 'unknown'
);

-- One row per extraction record. `payload_json` is the whitelisted projection
-- (scope_corpus.project_record), never the original line.
CREATE TABLE records (
    record_uid    TEXT PRIMARY KEY,
    source_kind   TEXT NOT NULL,
    source_label  TEXT NOT NULL,
    source_file   TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    pmid          TEXT NOT NULL,
    record_id     TEXT NOT NULL,
    -- Position of the source file in corpus order (workflows, methods,
    -- patterns, code_sources). Alphabetical order would put methods before
    -- workflows and change record() output.
    source_order  INTEGER NOT NULL DEFAULT 0,
    row           INTEGER NOT NULL,
    payload_json  TEXT NOT NULL,
    retracted_ref INTEGER NOT NULL DEFAULT 0,
    in_scope      INTEGER
);

CREATE TABLE hits (
    rowid              INTEGER PRIMARY KEY,
    evidence_id        TEXT NOT NULL UNIQUE,
    record_uid         TEXT NOT NULL REFERENCES records(record_uid),
    seq                INTEGER NOT NULL,
    unit_index         INTEGER NOT NULL DEFAULT 0,
    granularity        TEXT NOT NULL,
    locator            TEXT,
    locator_type       TEXT,
    verbatim           TEXT,
    is_anchored        INTEGER NOT NULL DEFAULT 0,
    in_scope           INTEGER,
    content_json       TEXT NOT NULL,
    record_context_json TEXT NOT NULL,
    field_tiers_json   TEXT NOT NULL,
    context_fields_json TEXT NOT NULL,
    search_fields_json TEXT NOT NULL,
    search_text        TEXT NOT NULL
);

CREATE VIRTUAL TABLE hits_fts USING fts5(
    search_text,
    content='',
    tokenize='{FTS_TOKENIZER}'
);

CREATE TABLE code_sources (
    rowid         INTEGER PRIMARY KEY,
    source_id     TEXT NOT NULL,
    pmid          TEXT NOT NULL,
    locator       TEXT,
    url           TEXT,
    source_order  INTEGER NOT NULL DEFAULT 0,
    row           INTEGER NOT NULL DEFAULT 0,
    payload_json  TEXT NOT NULL,
    retracted_ref INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE pmids (
    pmid      TEXT PRIMARY KEY,
    in_scope  INTEGER,
    n_records INTEGER NOT NULL DEFAULT 0,
    retracted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE retractions (
    pmid         TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL
);

CREATE TABLE editorial_notices (
    rowid        INTEGER PRIMARY KEY,
    pmid         TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

-- Every PMID-shaped token that appears in a stored row's *raw* serialized JSON,
-- one row per (token, owner, owner_key). The reader joins this against its LIVE
-- retraction registry, so a notice added after the build still excludes a record
-- that merely mentions the retracted paper. Deliberately a superset: tokens in
-- ids and locators are kept, because the frozen build flag used the same
-- digit-boundary semantics.
CREATE TABLE pmid_refs (
    ref_pmid  TEXT NOT NULL,
    owner     TEXT NOT NULL,
    owner_key TEXT NOT NULL,
    PRIMARY KEY (ref_pmid, owner, owner_key)
) WITHOUT ROWID;

CREATE INDEX idx_records_pmid ON records(pmid);
CREATE INDEX idx_records_kind_row ON records(source_kind, row);
CREATE INDEX idx_records_order ON records(source_order, row);
CREATE INDEX idx_records_retracted_ref ON records(retracted_ref);
CREATE INDEX idx_hits_record ON hits(record_uid);
CREATE INDEX idx_hits_seq ON hits(seq);
CREATE INDEX idx_code_sources_pmid ON code_sources(pmid);
CREATE INDEX idx_editorial_notices_pmid ON editorial_notices(pmid);
CREATE INDEX idx_pmid_refs_owner ON pmid_refs(owner, owner_key);
"""


def create_schema(conn: sqlite3.Connection) -> None:
    """Create the runtime schema on a *new* database (builder-side only)."""
    conn.executescript(SCHEMA_SQL)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()
