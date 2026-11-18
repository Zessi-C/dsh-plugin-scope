#!/usr/bin/env python3
"""Build the read-only SQLite corpus that the run time actually reads.

Input (development only)
------------------------
``<research>/corpus/workflows.jsonl``, ``methods.jsonl``, ``patterns.jsonl``,
``code_sources.jsonl``
    The copies/merge produced by ``tools/build_corpus.py``.
``<research>/corpus/manifest.json``
    Per-file sha256, sizes and record counts; stored verbatim in ``meta`` so a
    reader can report the same provenance it reports today.
The configured PMID list (``--pmid-list`` / ``SCOPE_INCLUDED_PMIDS`` / the 405
candidates) when readable.

Output (never published)
------------------------
``<research>/corpus/records.sqlite`` by default (``--db`` overrides).
    ``meta``, ``sources``, ``records``, ``hits``, ``hits_fts``,
    ``code_sources``, ``pmids``, ``retractions``, ``editorial_notices``.
    Published atomically (``os.replace``) and then chmod 0444. No WAL files.

Rules
-----
* Evidence units are not derived here. ``tools/build_runtime_corpus.py`` calls
  ``scope_knowledge.derive_record_hits`` (the same code the in-memory index
  uses) so ``evidence_id``, ``seq``, ``field_tiers`` and the search text cannot
  drift from the JSONL reader.
* ``payload_json`` is the whitelisted projection in
  ``scope_corpus.project_record``: no raw record line, no brief, no paper text
  beyond the extraction's own quoted evidence fields. Dropped keys are listed
  in ``scope_corpus.DROPPED_FIELDS``.
* Scope is *not* final at build time. Rows outside the build-time list are
  stored with ``in_scope=0``; the reader recomputes scope from its own
  configured list. Only two classes never reach the database: a record with no
  attributable PMID, and a record whose own PMID is in the frozen retraction
  registry (hard exclusion; counted in ``meta.excluded``).
* A record that merely *mentions* a retracted PMID is stored with
  ``retracted_ref=1`` and excluded by the reader by default, which reproduces
  the JSONL reader's conservative blob scan.
* Citation excerpts only. The database carries the extraction's own quoted
  evidence (``verbatim`` and statement fields), never a brief, a full paper, a
  PDF dump or a code body. There is no full-text ingest path: a paper-complete
  search surface is out of scope for this artifact.

Usage
-----
    python3 tools/build_runtime_corpus.py            # dry run: report, no write
    python3 tools/build_runtime_corpus.py --write    # build + publish atomically
    python3 tools/build_runtime_corpus.py --check    # rebuild to temp, diff, exit 1 on drift

``SOURCE_DATE_EPOCH`` pins ``built_utc`` so a rebuild is reproducible.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import scope_corpus as SC  # noqa: E402  (path set above)

RESEARCH = ROOT / "research"
GENERATOR = "tools/build_runtime_corpus.py"
GENERATOR_VERSION = "1"

#: Corpus order matters: ``seq`` and ``record()`` ordering follow it, and it is
#: the order ``scope_knowledge.CORPUS_FILES`` uses for the JSONL reader.
SOURCES = (
    ("workflow", "workflows.jsonl", "workflows"),
    ("method", "methods.jsonl", "methods"),
    ("pattern", "patterns.jsonl", "patterns"),
    ("code_source", "code_sources.jsonl", "code_sources"),
)
#: Source kinds that produce evidence units (``code_sources`` does not).
HIT_KINDS = ("workflow", "method", "pattern")

#: Exclusion reasons recorded per source kind in ``meta.excluded_by_kind``.
EXCLUSION_REASONS = (
    "retracted_records",
    "retracted_reference_records",
    "out_of_scope_records",
    "unattributed_records",
    "malformed_records",
    "superseded_records",
)



class BuildError(RuntimeError):
    """The inputs cannot be turned into a database without inventing a decision."""


# --------------------------------------------------------------------- helpers


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def utc_now() -> str:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def load_scope_knowledge():
    """Import ``scope_knowledge`` lazily (it imports nothing from here)."""
    try:
        import scope_knowledge as sk  # noqa: PLC0415 - lazy on purpose
    except Exception as exc:  # noqa: BLE001
        raise BuildError(
            f"cannot import scope_knowledge from {SRC}: {type(exc).__name__}: {exc}"
        ) from exc
    missing = [
        name
        for name in (
            "derive_record_hits",
            "_extract_pmid",
            "normalize_pmid",
            "ScopeKnowledge",
            "DEFAULT_RETRACTIONS",
            "DEFAULT_EDITORIAL_NOTICES",
            "DEFAULT_PMID_LIST_CANDIDATES",
        )
        if not hasattr(sk, name)
    ]
    if missing:
        raise BuildError(
            "scope_knowledge is missing shared helpers the builder must reuse: "
            + ", ".join(missing)
            + " (derive_record_hits is required so evidence units are not "
            "re-derived here)"
        )
    return sk


def read_jsonl(path: Path) -> tuple[bytes, list[dict], list[dict]]:
    """Return original bytes, parsed object rows and malformed rows."""
    data = path.read_bytes()
    rows: list[dict] = []
    malformed: list[dict] = []
    for number, line in enumerate(
        data.decode("utf-8-sig", errors="replace").splitlines(), 1
    ):
        text = line.strip()
        if not text:
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            malformed.append({"line": number, "error": exc.msg})
            continue
        if not isinstance(value, dict):
            malformed.append({"line": number, "error": f"non-object {type(value).__name__}"})
            continue
        rows.append(value)
    return data, rows, malformed


def display_path(research: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(research.resolve()))
    except (ValueError, OSError):
        return str(path)


def parse_pmid_list(path: Path) -> dict:
    """Read the authoritative PMID list the same way the reader does."""
    data = path.read_bytes()
    sk = load_scope_knowledge()
    pmids: list[str] = []
    seen: set[str] = set()
    malformed = 0
    duplicates = 0
    for line in data.decode("utf-8-sig", errors="replace").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        pmid = sk.normalize_pmid(text)
        if pmid is None:
            malformed += 1
            continue
        if pmid in seen:
            duplicates += 1
            continue
        seen.add(pmid)
        pmids.append(pmid)
    if not pmids:
        raise BuildError(f"pmid_list_empty:{path}")
    return {
        "path": str(path),
        "sha256": sha256_bytes(data),
        "n_pmids": len(pmids),
        "pmids": pmids,
        "n_malformed": malformed,
        "n_duplicates": duplicates,
    }


def resolve_scope(research: Path, pmid_list_path: str | Path | None) -> dict:
    """Resolve and parse the authoritative PMID list.

    There is no audit bypass: a database built without a scope list cannot say
    which papers its rows belong to, so a missing list is a hard error. A caller
    that must replay a private corpus passes ``--pmid-list`` explicitly.
    """
    sk = load_scope_knowledge()
    if pmid_list_path is not None:
        candidate = Path(pmid_list_path).expanduser()
        if not candidate.is_file():
            raise BuildError(f"pmid_list_missing:{candidate}")
        return parse_pmid_list(candidate)
    env_path = os.environ.get(sk.PMID_LIST_ENV)
    if env_path:
        candidate = Path(env_path).expanduser()
        if not candidate.is_file():
            raise BuildError(f"pmid_list_missing:{candidate}")
        return parse_pmid_list(candidate)
    for candidate in sk.DEFAULT_PMID_LIST_CANDIDATES:
        if Path(candidate).is_file():
            return parse_pmid_list(Path(candidate))
    raise BuildError(
        "pmid_list_missing: no readable PMID list at "
        + ", ".join(str(path) for path in sk.DEFAULT_PMID_LIST_CANDIDATES)
        + "; pass --pmid-list or set "
        + sk.PMID_LIST_ENV
    )


def read_superseded_pmids(corpus: Path) -> list[str]:
    """PMIDs whose shard record was replaced by a repair, from the provenance file.

    The merge already happened in ``tools/build_corpus.py``; this only records
    how many records were superseded so ``stats()["excluded"]`` keeps its shape.
    """
    path = corpus / "provenance" / "patterns_records.jsonl"
    if not path.is_file():
        return []
    pmids: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("rule") != "shard_kept":
            pmid = str(row.get("pmid") or "")
            if pmid:
                pmids.append(pmid)
    return sorted(set(pmids))


def retraction_patterns(sk) -> list[tuple[str, re.Pattern[str]]]:
    return [
        (notice.pmid, re.compile(rf"(?<!\d){re.escape(notice.pmid)}(?!\d)"))
        for notice in sk.DEFAULT_RETRACTIONS
    ]


def mentions_retraction(blob: str, patterns) -> list[str]:
    return [pmid for pmid, pattern in patterns if pattern.search(blob)]


def pmid_ref_digest(rows) -> str:
    """Hash reference triples in primary-key order."""
    digest = hashlib.sha256()
    for ref, owner, key in rows:
        digest.update((canonical([ref, owner, key]) + "\n").encode("utf-8", "replace"))
    return digest.hexdigest()


# ------------------------------------------------------------------ derivation


def _accepts_evidence_ids(derive) -> bool:
    try:
        parameters = inspect.signature(derive).parameters
    except (TypeError, ValueError):  # pragma: no cover - C callables
        return False
    return "evidence_ids" in parameters or any(
        item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()
    )


def derive_hits(sk, **kwargs) -> list[dict]:
    """Call the shared unit builder and normalise its rows.

    ``evidence_ids`` is passed only when the shared function declares it, so a
    TypeError raised *inside* the derivation is never mistaken for a signature
    mismatch.
    """
    derive = sk.derive_record_hits
    params = dict(kwargs)
    if not _accepts_evidence_ids(derive):
        params.pop("evidence_ids", None)
    rows = derive(**params)
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            raise BuildError(
                f"derive_record_hits returned {type(row).__name__}, expected dict"
            )
        missing = [
            key
            for key in (
                "evidence_id",
                "seq",
                "granularity",
                "content",
                "record_context",
                "field_tiers",
                "context_fields",
                "search_fields",
            )
            if key not in row
        ]
        if missing:
            raise BuildError(
                "derive_record_hits row is missing " + ", ".join(missing)
            )
        search_fields = [list(item) for item in row.get("search_fields") or ()]
        search_text = row.get("search_text")
        if not isinstance(search_text, str) or not search_text:
            search_text = "\n".join(str(item[1]) for item in search_fields)
        is_anchored = row.get("is_anchored")
        if is_anchored is None:
            is_anchored = bool(
                row.get("locator") and row.get("locator_type") and row.get("verbatim")
            )
        out.append(
            {
                "evidence_id": str(row["evidence_id"]),
                "seq": int(row["seq"]),
                "unit_index": int(row.get("unit_index") or 0),
                "granularity": str(row["granularity"]),
                "locator": row.get("locator"),
                "locator_type": row.get("locator_type"),
                "verbatim": row.get("verbatim"),
                "content": [list(item) for item in row.get("content") or ()],
                "record_context": [list(item) for item in row.get("record_context") or ()],
                "field_tiers": dict(row.get("field_tiers") or {}),
                "context_fields": [str(item) for item in row.get("context_fields") or ()],
                "search_fields": search_fields,
                "search_text": search_text,
                "is_anchored": int(bool(is_anchored)),
            }
        )
    return out


# ----------------------------------------------------------------------- build


def _insert(conn: sqlite3.Connection, table: str, row: dict) -> None:
    columns = ", ".join(row)
    places = ", ".join("?" * len(row))
    conn.execute(f"INSERT INTO {table}({columns}) VALUES ({places})", list(row.values()))


def build(
    research_dir: str | Path = RESEARCH,
    db_path: str | Path | None = None,
    *,
    pmid_list_path: str | Path | None = None,
) -> dict:
    """Build one database and return its report (no printing, no CLI)."""
    research = Path(research_dir).expanduser().resolve()
    corpus = research / SC.CORPUS_SUBDIR
    target = (
        Path(db_path).expanduser()
        if db_path is not None
        else corpus / SC.DB_NAME
    )
    if not target.is_absolute():
        target = research / target
    sk = load_scope_knowledge()
    scope = resolve_scope(research, pmid_list_path)
    included = set(scope["pmids"])
    retractions = list(sk.DEFAULT_RETRACTIONS)
    retracted = {notice.pmid for notice in retractions}
    patterns = retraction_patterns(sk)

    manifest_path = corpus / "manifest.json"
    manifest_text: str | None = None
    manifest: dict = {}
    if manifest_path.is_file():
        manifest_text = manifest_path.read_text(encoding="utf-8-sig")
        try:
            manifest = json.loads(manifest_text)
        except ValueError as exc:
            raise BuildError(f"manifest_unreadable:{manifest_path}:{exc}") from exc
        if not isinstance(manifest, dict):
            raise BuildError(f"manifest_not_object:{manifest_path}")
    manifest_files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}

    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".tmp", dir=str(target.parent)
    )
    os.close(handle)
    tmp_path = Path(tmp_name)
    try:
        conn = sqlite3.connect(str(tmp_path))
        conn.row_factory = sqlite3.Row
        SC.create_schema(conn)
        conn.execute("PRAGMA synchronous=OFF")

        excluded = {
            "retracted_records": 0,
            "retracted_reference_records": 0,
            "superseded_records": 0,
            "out_of_scope_records": 0,
            "unattributed_records": 0,
            "malformed_records": 0,
        }
        excluded_pmids: dict[str, dict[str, list[str]]] = {
            kind: {reason: [] for reason in EXCLUSION_REASONS}
            for kind, _name, _label in SOURCES
        }
        # The shard -> repair merge happens upstream; only the provenance file
        # knows which shard record a repair replaced, and no row is dropped here.
        superseded_pmids = read_superseded_pmids(corpus)
        if superseded_pmids:
            excluded["superseded_records"] = len(superseded_pmids)
            excluded_pmids["pattern"]["superseded_records"] = superseded_pmids
        source_stats: list[dict] = []
        source_n_lines: dict[str, int] = {}
        record_counts: dict[str, int] = {}
        n_records_source = 0
        n_hits = 0
        anchored_hits = 0
        seq = 0
        hit_rowid = 0
        field_names: set[str] = set()
        used_evidence_ids: dict[str, int] = {}
        record_digest_lines: list[str] = []
        hit_digest_lines: list[str] = []
        code_digest_lines: list[str] = []
        pmids_with_records: dict[str, int] = {}
        pmid_ref_rows: set[tuple[str, str, str]] = set()
        code_rows = 0


        for source_order, (kind, name, label) in enumerate(SOURCES):
            path = corpus / name
            sha = None
            data = b""
            rows: list[dict] = []
            malformed: list[dict] = []
            if path.is_file():
                data, rows, malformed = read_jsonl(path)
                sha = sha256_bytes(data)
                excluded["malformed_records"] += len(malformed)
                excluded_pmids[kind]["malformed_records"].extend(
                    f"{path.name}:L{item['line']}" for item in malformed
                )
                n_records_source += len(rows)
                record_counts[name] = len(rows)
                source_n_lines[name] = len(data.splitlines())
            manifest_entry = manifest_files.get(name)
            manifest_sha = (
                manifest_entry.get("sha256") if isinstance(manifest_entry, dict) else None
            )
            manifest_status = (
                "match" if manifest_sha and manifest_sha == sha
                else ("sha256_mismatch" if manifest_sha else "not_listed")
            )
            _insert(
                conn,
                "sources",
                {
                    "name": name,
                    "kind": kind,
                    "label": label,
                    "path": display_path(research, path),
                    "sha256": sha,
                    "n_records": len(rows),
                    "manifest_sha256": manifest_sha,
                    "manifest_status": manifest_status,
                },
            )
            source_stats.append(
                {
                    "kind": kind,
                    "label": label,
                    "file": name,
                    "path": display_path(research, path),
                    "status": "ok" if path.is_file() else "missing",
                    "sha256": sha,
                    "n_records": len(rows),
                    "n_records_source": len(rows),
                    "n_lines": len(data.splitlines()) if data else 0,
                    "manifest_sha256": manifest_sha,
                    "manifest_status": manifest_status,
                    "n_malformed": len(malformed),
                }
            )

            pmid_token_re = re.compile(rf"(?<!\d)({sk.PMID_DIGITS})(?!\d)")
            for row_number, record in enumerate(rows, 1):
                blob = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
                mentions = mentions_retraction(blob, patterns)

                if kind == "code_source":
                    pmid = sk._extract_pmid(record)
                    if pmid is None:
                        excluded["unattributed_records"] += 1
                        excluded_pmids[kind]["unattributed_records"].append(
                            f"{name}:row{row_number}"
                        )
                        continue
                    if pmid in retracted:
                        excluded["retracted_records"] += 1
                        excluded_pmids[kind]["retracted_records"].append(pmid)
                        continue
                    in_scope = 1 if pmid in included else 0
                    if not in_scope:
                        excluded["out_of_scope_records"] += 1
                        excluded_pmids[kind]["out_of_scope_records"].append(pmid)
                    if mentions:
                        excluded["retracted_reference_records"] += 1
                        excluded_pmids[kind]["retracted_reference_records"].append(pmid)
                    payload = SC.project_record("code_source", record)
                    _insert(
                        conn,
                        "code_sources",
                        {
                            "source_id": str(record.get("source_id") or f"{pmid}:cs{row_number}"),
                            "pmid": pmid,
                            "locator": record.get("locator"),
                            "url": record.get("url"),
                            "source_order": source_order,
                            "row": row_number,
                            "payload_json": canonical(payload),
                            "retracted_ref": int(bool(mentions)),
                        },
                    )
                    code_rowid = conn.execute(
                        "SELECT last_insert_rowid()"
                    ).fetchone()[0]
                    pmid_ref_rows.update(
                        (token, "code_source", str(code_rowid))
                        for token in pmid_token_re.findall(blob)
                    )
                    code_rows += 1
                    code_digest_lines.append(
                        f"{row_number}\x1f{pmid}\x1f{sha256_text(canonical(payload))}"
                    )
                    continue

                pmid = sk._extract_pmid(record)
                if pmid is None:
                    excluded["unattributed_records"] += 1
                    excluded_pmids[kind]["unattributed_records"].append(
                        f"{name}:row{row_number}"
                    )
                    continue
                if pmid in retracted:
                    excluded["retracted_records"] += 1
                    excluded_pmids[kind]["retracted_records"].append(pmid)
                    continue
                in_scope = 1 if pmid in included else 0
                if not in_scope:
                    excluded["out_of_scope_records"] += 1
                    excluded_pmids[kind]["out_of_scope_records"].append(pmid)
                if mentions:
                    excluded["retracted_reference_records"] += 1
                    excluded_pmids[kind]["retracted_reference_records"].append(pmid)

                spec = sk.SourceSpec(kind, path, label)
                record_id = sk.ScopeKnowledge._record_id(spec, record, pmid, row_number)
                record_uid = f"{kind}:{label}:{record_id}"
                payload = SC.project_record(kind, record)
                payload_json = canonical(payload)
                _insert(
                    conn,
                    "records",
                    {
                        "record_uid": record_uid,
                        "source_kind": kind,
                        "source_label": label,
                        "source_file": display_path(research, path),
                        "source_sha256": sha or "",
                        "pmid": pmid,
                        "record_id": record_id,
                        "source_order": source_order,
                        "row": row_number,
                        "payload_json": payload_json,
                        "retracted_ref": int(bool(mentions)),
                        "in_scope": in_scope,
                    },
                )
                record_digest_lines.append(
                    f"{record_uid}\x1f{pmid}\x1f{record_id}\x1f{sha or ''}"
                    f"\x1f{int(bool(mentions))}\x1f{in_scope}\x1f{sha256_text(payload_json)}"
                )
                pmid_ref_rows.update(
                    (token, "record", record_uid)
                    for token in pmid_token_re.findall(blob)
                )
                pmids_with_records[pmid] = pmids_with_records.get(pmid, 0) + 1


                if kind not in HIT_KINDS:
                    continue
                hits = derive_hits(
                    sk,
                    kind=kind,
                    record=payload,
                    pmid=pmid,
                    record_id=record_id,
                    record_uid=record_uid,
                    source_file=display_path(research, path),
                    source_label=label,
                    source_sha256=sha or "",
                    in_scope=in_scope,
                    start_seq=seq,
                    evidence_ids=set(used_evidence_ids),
                )
                for hit in hits:
                    evidence_id = hit["evidence_id"]
                    if evidence_id in used_evidence_ids:
                        used_evidence_ids[evidence_id] += 1
                        evidence_id = f"{evidence_id}#dup{used_evidence_ids[evidence_id]}"
                    else:
                        used_evidence_ids[evidence_id] = 0
                    hit_rowid += 1
                    _insert(
                        conn,
                        "hits",
                        {
                            "rowid": hit_rowid,
                            "evidence_id": evidence_id,
                            "record_uid": record_uid,
                            "seq": hit["seq"],
                            "unit_index": hit["unit_index"],
                            "granularity": hit["granularity"],
                            "locator": hit["locator"],
                            "locator_type": hit["locator_type"],
                            "verbatim": hit["verbatim"],
                            "is_anchored": hit["is_anchored"],
                            "in_scope": in_scope,
                            "content_json": canonical(hit["content"]),
                            "record_context_json": canonical(hit["record_context"]),
                            "field_tiers_json": canonical(hit["field_tiers"]),
                            "context_fields_json": canonical(hit["context_fields"]),
                            "search_fields_json": canonical(hit["search_fields"]),
                            "search_text": hit["search_text"],
                        },
                    )
                    conn.execute(
                        "INSERT INTO hits_fts(rowid, search_text) VALUES (?, ?)",
                        (hit_rowid, hit["search_text"]),
                    )
                    hit_digest_lines.append(
                        f"{hit_rowid}\x1f{evidence_id}\x1f{record_uid}\x1f{hit['seq']}"
                        f"\x1f{hit['is_anchored']}\x1f{sha256_text(hit['search_text'])}"
                    )
                    n_hits += 1
                    anchored_hits += hit["is_anchored"]
                    seq = max(seq, hit["seq"]) + 1
                    field_names.update(hit["field_tiers"])

        # ---- registries and per-PMID summary
        for notice in retractions:
            _insert(
                conn,
                "retractions",
                {"pmid": notice.pmid, "payload_json": canonical(notice.as_dict())},
            )
        for notice in sk.DEFAULT_EDITORIAL_NOTICES:
            _insert(
                conn,
                "editorial_notices",
                {"pmid": notice.pmid, "payload_json": canonical(notice.as_dict())},
            )
        all_pmids = sorted(included | set(pmids_with_records))
        for pmid in all_pmids:
            _insert(
                conn,
                "pmids",
                {
                    "pmid": pmid,
                    "in_scope": 1 if pmid in included else 0,
                    "n_records": pmids_with_records.get(pmid, 0),
                    "retracted": int(pmid in retracted),
                },
            )

        # ---- cross-reference table (live retraction parity)
        # Written for every stored row, including fields the projection drops.
        # ``retracted_ref`` above stays the frozen build-time verdict; this table
        # lets a reader apply a retraction registry that grew after the build.
        conn.executemany(
            "INSERT OR IGNORE INTO pmid_refs(ref_pmid, owner, owner_key) VALUES (?, ?, ?)",
            sorted(pmid_ref_rows),
        )

        # ---- corpus fingerprint
        # Same formula as ``scope_knowledge``: it covers exactly the three
        # retrieval sources (workflows, methods, patterns) in that order, with
        # the scope list hash first. ``code_sources`` is a fourth file this
        # database also projects, so it is recorded in ``source_files`` and in
        # ``digests.code_sources_sha256`` instead — including it here would make
        # the fingerprint impossible to compare with the JSONL reader.
        fingerprint_parts = [f"scope:{scope['sha256']}:included"]
        for stat in source_stats:
            if stat["kind"] == "code_source":
                continue
            fingerprint_parts.append(
                f"{stat['label']}:{stat['sha256'] or 'missing'}:{stat['n_records']}"
            )
        corpus_fingerprint = sha256_text("|".join(fingerprint_parts))[:32]

        digests = {
            "records_sha256": sha256_text("\n".join(record_digest_lines) + "\n"),
            "hits_sha256": sha256_text("\n".join(hit_digest_lines) + "\n"),
            "code_sources_sha256": sha256_text("\n".join(code_digest_lines) + "\n"),
            "pmids_sha256": sha256_text("\n".join(all_pmids) + "\n"),
            "pmid_refs_sha256": pmid_ref_digest(sorted(pmid_ref_rows)),
        }
        excluded_by_kind = {
            kind: {
                **{
                    reason: {"records": len(values), "pmids": values}
                    for reason, values in excluded_pmids[kind].items()
                },
                "n_records_source": record_counts.get(name, 0),
                "n_lines": source_n_lines.get(name, 0),
            }
            for kind, name, _label in SOURCES
        }

        meta = {
            "schema_id": SC.SCHEMA_ID,
            "schema_version": SC.SCHEMA_VERSION,
            "generator": GENERATOR,
            "generator_version": GENERATOR_VERSION,
            "built_utc": utc_now(),
            "fts_enabled": True,
            "fts_tokenizer": SC.FTS_TOKENIZER,
            "hit_derivation": "scope_knowledge.derive_record_hits",
            "n_records": len(record_digest_lines),
            "n_hits": n_hits,
            "n_code_sources": code_rows,
            "n_pmids": len(all_pmids),
            "n_records_source": n_records_source,
            "n_pmid_refs": len(pmid_ref_rows),
            "search_text_chars": sum(
                len(row[0] or "") for row in conn.execute("SELECT search_text FROM hits")
            ),
            "field_names": sorted(field_names),
            "source_files": {stat["file"]: stat["sha256"] for stat in source_stats},
            "sources": source_stats,
            "pmid_list": {
                "path": scope["path"],
                "sha256": scope["sha256"],
                "n_pmids": scope["n_pmids"],
                "n_malformed": scope["n_malformed"],
                "n_duplicates": scope["n_duplicates"],
            },
            "corpus_manifest": manifest if manifest_text else None,
            "corpus_manifest_sha256": (
                sha256_text(manifest_text) if manifest_text is not None else None
            ),
            "corpus_fingerprint": corpus_fingerprint,
            "excluded": excluded,
            "excluded_by_kind": excluded_by_kind,
            "digests": digests,
        }
        for key, value in meta.items():
            _insert(
                conn,
                "meta",
                {"key": key, "value": canonical(value)},
            )
        conn.commit()
        conn.execute("PRAGMA optimize")
        conn.commit()
        conn.close()

        # The published file cannot carry its own size or sha256: writing either
        # would change the bytes it describes. Both are reported by this builder
        # (and by --check) and are available at read time from the file itself;
        # `digests` is the content identity a reader can verify.
        db_bytes = tmp_path.stat().st_size
        db_sha256 = sha256_bytes(tmp_path.read_bytes())
        os.replace(tmp_path, target)
        os.chmod(target, 0o444)
        return {
            "status": "built",
            "db": str(target),
            "schema_id": SC.SCHEMA_ID,
            "schema_version": SC.SCHEMA_VERSION,
            "built_utc": meta["built_utc"],
            "n_records": meta["n_records"],
            "n_records_source": n_records_source,
            "n_hits": n_hits,
            "n_hits_anchored": anchored_hits,
            "n_code_sources": code_rows,
            "n_pmids": len(all_pmids),
            "n_pmid_refs": len(pmid_ref_rows),
            "n_field_names": len(field_names),
            "search_text_chars": meta["search_text_chars"],
            "pmid_list": meta["pmid_list"],
            "sources": source_stats,
            "excluded": excluded,
            "excluded_by_kind": excluded_by_kind,
            "digests": digests,
            "corpus_fingerprint": corpus_fingerprint,
            "db_bytes": db_bytes,
            "db_sha256": db_sha256,
        }
    except BaseException:
        try:
            conn.close()  # type: ignore[possibly-undefined]
        except Exception:  # noqa: BLE001
            pass
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


# ---------------------------------------------------------------------- report


def compare_with_existing(target: Path, report: dict) -> dict:
    """Diff a fresh build against the published database (``--check``)."""
    mismatches: list[str] = []
    if not target.is_file():
        return {"status": "missing", "mismatches": [f"missing:{target}"]}
    try:
        conn = SC.open_db(target)
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "mismatches": [f"unreadable:{exc}"]}
    try:
        meta = SC.read_meta(conn)
        live_counts = SC.counts(conn)
        live_refs_digest = pmid_ref_digest(conn.execute(
            "SELECT ref_pmid, owner, owner_key FROM pmid_refs ORDER BY ref_pmid, owner, owner_key"
        ))
    finally:
        conn.close()
    for key in ("schema_id", "schema_version", "corpus_fingerprint", "digests"):
        if meta.get(key) != report.get(key):
            mismatches.append(f"meta:{key}")
    for key in ("n_records", "n_hits", "n_code_sources", "n_pmids"):
        if int(meta.get(key) or 0) != int(report.get(key) or 0):
            mismatches.append(f"meta:{key}")
    if live_counts["hits"] != report["n_hits"]:
        mismatches.append("rows:hits")
    if live_counts["records"] != report["n_records"]:
        mismatches.append("rows:records")
    if live_counts["code_sources"] != report["n_code_sources"]:
        mismatches.append("rows:code_sources")
    if live_refs_digest != report["digests"]["pmid_refs_sha256"]:
        mismatches.append("rows:pmid_refs")
    if (meta.get("pmid_list") or {}).get("sha256") != report["pmid_list"]["sha256"]:
        mismatches.append("meta:pmid_list.sha256")
    return {"status": "ok" if not mismatches else "drift", "mismatches": mismatches}


# ---------------------------------------------------------------------- driver


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--research-dir", default=str(RESEARCH))
    parser.add_argument(
        "--db", default=None, help="output database (default <research>/corpus/records.sqlite)"
    )
    parser.add_argument("--pmid-list", default=None)
    parser.add_argument("--write", action="store_true", help="build and publish the database")
    parser.add_argument(
        "--check", action="store_true", help="rebuild to a temp file and diff against --db"
    )
    parser.add_argument("--json", action="store_true", help="emit the full report")
    args = parser.parse_args(argv)

    research = Path(args.research_dir).expanduser().resolve()
    target = (
        Path(args.db).expanduser()
        if args.db
        else research / SC.CORPUS_SUBDIR / SC.DB_NAME
    )
    if not target.is_absolute():
        target = research / target

    try:
        if args.check:
            with tempfile.TemporaryDirectory(prefix="scope-corpus-check-") as tmp:
                temp_target = Path(tmp) / SC.DB_NAME
                report = build(
                    research,
                    temp_target,
                    pmid_list_path=args.pmid_list,
                )
            report["target"] = str(target)
            report["check"] = compare_with_existing(target, report)
            print(json.dumps(report, ensure_ascii=False, indent=1))
            return 1 if report["check"]["status"] != "ok" else 0

        if not args.write:
            # Dry run: derive nothing into a database, just report the inputs.
            sk = load_scope_knowledge()
            scope = resolve_scope(research, args.pmid_list)
            corpus = research / SC.CORPUS_SUBDIR
            files = {}
            for kind, name, _label in SOURCES:
                path = corpus / name
                if path.is_file():
                    files[name] = {
                        "sha256": sha256_bytes(path.read_bytes()),
                        "bytes": path.stat().st_size,
                    }
                else:
                    files[name] = {"status": "missing"}
            print(
                json.dumps(
                    {
                        "status": "plan",
                        "db": str(target),
                        "db_exists": target.is_file(),
                        "research_dir": str(research),
                        "files": files,
                        "manifest": (corpus / "manifest.json").is_file(),
                        "pmid_list": {
                            key: scope[key] for key in ("path", "sha256", "n_pmids")
                        },
                        "retractions": [notice.pmid for notice in sk.DEFAULT_RETRACTIONS],
                        "note": "pass --write to publish; --check to diff",
                    },
                    ensure_ascii=False,
                    indent=1,
                )
            )
            return 0

        report = build(
            research,
            target,
            pmid_list_path=args.pmid_list,
        )
    except BuildError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "db": report["db"],
                    "db_bytes": report["db_bytes"],
                    "db_sha256": report["db_sha256"],
                    "schema": f"{report['schema_id']} (v{report['schema_version']})",
                    "built_utc": report["built_utc"],
                    "n_records": report["n_records"],
                    "n_hits": report["n_hits"],
                    "n_code_sources": report["n_code_sources"],
                    "n_pmids": report["n_pmids"],
                    "n_field_names": report["n_field_names"],
                    "search_text_chars": report["search_text_chars"],
                    "pmid_list": {
                        "path": report["pmid_list"]["path"],
                        "sha256": report["pmid_list"]["sha256"],
                        "n_pmids": report["pmid_list"]["n_pmids"],
                    },
                    "excluded": report["excluded"],
                    "corpus_fingerprint": report["corpus_fingerprint"],
                },
                ensure_ascii=False,
                indent=1,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
