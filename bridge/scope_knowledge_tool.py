#!/usr/bin/env python3
"""JSON bridge: the SCOPE dsh bundle -> src/scope_knowledge.py.

One request per process, JSON on stdin, one JSON object on stdout. The bridge
owns no retrieval logic; it validates the operation and forwards to the real
`ScopeKnowledge` API so that corpus scope, retraction isolation, tier labels and
pagination stay in the module that implements them.

Exit codes: 0 when a JSON answer was produced (including domain statuses such as
`not_found` or `query` errors), 2 when the request itself was unusable. The
caller treats unusable transports as failed calls, never as empty corpora.
"""
from __future__ import annotations

import json
import sys
import traceback

SEARCH_KEYS = (
    "pmids", "source_kinds", "sections", "granularities", "tiers", "fields",
    "pipeline_stages", "purposes", "method_names", "method_classes", "in_scope",
    "evidence_types", "directions", "rationale_sources", "include_unverified",
    "match",
)
# `include_author_focus` is intentionally absent: the bridge must not let an
# agent widen `record()` into author-focus data. A human auditing that field
# calls `ScopeKnowledge.record(..., include_author_focus=True)` in Python.
RECORD_KEYS = ("record_id", "source_kind", "limit", "offset")
GET_KEYS = ("include_unverified", "matched_fields")
CONTEXT_KEYS = ("before", "after", "include_unverified", "matched_fields")


def emit(payload: dict, code: int = 0) -> int:
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return code


def fail(message: str, kind: str = "unexpected", detail: str | None = None) -> int:
    payload = {"status": "error", "error_kind": kind, "error": message}
    if detail:
        payload["detail"] = detail
    return emit(payload, 1)


def pick(args: dict, keys) -> dict:
    """Forward supplied keys; an empty list means "not supplied", not "match nothing"."""
    picked = {}
    for key in keys:
        value = args.get(key)
        if value is None:
            continue
        if isinstance(value, list) and not value:
            continue
        picked[key] = value
    return picked


def dispatch(index, operation: str, args: dict) -> dict:
    if operation == "search":
        kwargs = pick(args, SEARCH_KEYS)
        if args.get("limit") is not None:
            kwargs["limit"] = args["limit"]
        if args.get("offset") is not None:
            kwargs["offset"] = args["offset"]
        return index.search(args.get("query"), **kwargs)
    if operation == "get":
        return index.get(
            str(args.get("evidence_id", "")),
            **pick(args, GET_KEYS),
        )
    if operation == "context":
        return index.context(
            str(args.get("evidence_id", "")),
            **pick(args, CONTEXT_KEYS),
        )
    if operation == "record":
        kwargs = pick(args, RECORD_KEYS)
        return index.record(str(args.get("pmid", "")), **kwargs)
    if operation == "stats":
        return index.stats()
    raise ValueError(f"unknown operation {operation!r}")


def main() -> int:
    raw = sys.stdin.read()
    try:
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
    except Exception as exc:  # noqa: BLE001 - the caller needs the parse failure
        return emit({"status": "error", "error_kind": "bridge_internal",
                     "error": f"unreadable request: {type(exc).__name__}: {exc}"}, 2)

    operation = str(request.get("operation", ""))
    args = request.get("args") or {}
    config = request.get("config") or {}
    src_root = request.get("src_root")
    if not operation:
        return emit({"status": "error", "error_kind": "bridge_internal",
                     "error": "request has no operation"}, 2)
    if not isinstance(args, dict):
        return emit({"status": "error", "error_kind": "query",
                     "error": "args must be a JSON object"}, 0)
    if not src_root:
        return emit({"status": "error", "error_kind": "bridge_internal",
                     "error": "request has no src_root"}, 2)

    sys.path.insert(0, str(src_root))
    try:
        from scope_knowledge import (  # noqa: PLC0415 - path is configured at call time
            CorpusConfigError,
            QueryError,
            ScopeKnowledge,
            ScopeKnowledgeError,
        )
    except Exception as exc:  # noqa: BLE001
        return fail(
            f"cannot import scope_knowledge from {src_root}: {type(exc).__name__}: {exc}",
            kind="bridge_internal",
            detail=traceback.format_exc(limit=3),
        )

    research_dir = config.get("research_dir") or None
    pmid_list_path = config.get("pmid_list_path") or None
    try:
        index = ScopeKnowledge(
            research_dir=research_dir,
            pmid_list_path=pmid_list_path,
        )
    except CorpusConfigError as exc:
        return emit({"status": "error", "error_kind": "corpus_config",
                     "error": str(exc)}, 0)
    except Exception as exc:  # noqa: BLE001
        return fail(
            f"ScopeKnowledge could not be constructed: {type(exc).__name__}: {exc}",
            kind="bridge_internal",
            detail=traceback.format_exc(limit=3),
        )

    try:
        result = dispatch(index, operation, args)
    except QueryError as exc:
        return emit({"status": "error", "error_kind": "query", "error": str(exc)}, 0)
    except CorpusConfigError as exc:
        return emit({"status": "error", "error_kind": "corpus_config",
                     "error": str(exc)}, 0)
    except ScopeKnowledgeError as exc:
        return emit({"status": "error", "error_kind": "knowledge",
                     "error": f"{type(exc).__name__}: {exc}"}, 0)
    except Exception as exc:  # noqa: BLE001
        return fail(
            f"{operation} failed: {type(exc).__name__}: {exc}",
            detail=traceback.format_exc(limit=4),
        )
    return emit(result, 0)


if __name__ == "__main__":
    sys.exit(main())
