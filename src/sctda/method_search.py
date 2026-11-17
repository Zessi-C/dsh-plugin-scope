"""Method discovery over the frozen corpus, plus public-literature lookup.

Two questions are deliberately answered by different tools and never merged:

``sctda_method_search`` / ``sctda_method_get`` / ``sctda_method_stats``
    Read the local method registry. A record carries its corpus provenance
    (PMIDs, locators, extraction source) and exactly one *support status*:

    - ``executable``    - one of the four wired adapters in
                          :data:`sctda.catalog.EXECUTABLE` is an exact match.
                          Wiring is a code fact, not a claim that the R package
                          is installed or that a task ever succeeded.
    - ``corpus_only``   - the method occurs in the extracted literature but no
                          adapter is wired for it.
    - ``external_only`` - returned only by ``sctda_method_external_search``.
                          A literature hit is a pointer, never support.
    - ``not_found``     - the selected source has no record. This is not a
                          claim that the method does not exist.

``sctda_method_external_search``
    Queries PubMed / Europe PMC through :mod:`sctda.literature`, so every
    request keeps its endpoint, request time, HTTP status, body hash and cache
    path in the project's ``literature_cache``. It never upgrades a hit to
    evidence: every hit says ``usable_as_evidence=False`` and the caller still
    has to read the paper and verify the anchor.

The registry loader is intentionally one function, ``_load_registry_payload``,
so the surface stays stable while ``method_registry`` evolves. When that module
is absent or exposes an unusable shape, the loader falls back to
``catalog.discovered_methods`` and labels the source instead of inventing
records.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import sqlite3
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Any

from . import catalog, literature
from .project import Project, now

# --------------------------------------------------------------- vocabulary
SUPPORT_STATUSES = ("executable", "corpus_only", "external_only", "not_found")
SUPPORT_VOCABULARY: dict[str, str] = {
    "executable": (
        "an exact method id is wired in sctda.catalog.EXECUTABLE; this is a "
        "code-level adapter fact, not an installation check and not a successful run"
    ),
    "corpus_only": (
        "the method occurs in the local extracted literature but no adapter is "
        "wired for it; it cannot be run by this kernel"
    ),
    "external_only": (
        "the item came from a public PubMed/Europe PMC search; it is a pointer, "
        "not support, not evidence, and not a verified method use"
    ),
    "not_found": (
        "the selected local source has no record; this does not prove the method "
        "does not exist"
    ),
}

MAX_PAGE_SIZE = 100
MAX_EXTERNAL_PAGE_SIZE = 25
MAX_EPMC_PAGE = 400
MAX_ANCHOR_PAGE_SIZE = 100
MAX_QUERY_CHARS = 500
DEFAULT_PAGE_SIZE = 20
DEFAULT_EXTERNAL_PAGE_SIZE = 10
DEFAULT_ANCHOR_PAGE_SIZE = 20

# Public-source scoping. The filter is sent to the source and echoed back in
# `effective_query`; it is never applied silently after retrieval.
PUBMED_SC_FILTER = (
    '("single-cell"[Title/Abstract] OR "single cell"[Title/Abstract] '
    'OR "scRNA-seq"[Title/Abstract] OR "single-cell RNA"[Title/Abstract])'
)
EPMC_SC_FILTER = '("single-cell" OR "single cell" OR "scRNA-seq" OR "single-cell RNA")'

_SPLIT = re.compile(r"[\W_]+", re.UNICODE)
_HTML_TAG = re.compile(r"<[^>]+>")


# ------------------------------------------------------------------ helpers
def _norm(value: Any) -> str:
    """NFKC + casefold, keeping Unicode letters/digits.

    An ASCII-only filter turned Chinese or punctuation-only queries into an
    empty term list, which previously matched every record. Keep CJK searchable
    and let the caller reject queries with no terms.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(ch for ch in text if ch.isalnum())


def _slug(value: str) -> str:
    out = "_".join(part for part in _SPLIT.split(str(value or "").casefold()) if part)
    if out:
        return out
    return "method_" + hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:8]


def _as_list(value: Any) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        return [item for item in value if item not in (None, "")]
    return [value]


def _as_text_list(value: Any) -> list[str]:
    out: list[str] = []
    for item in _as_list(value):
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _page_bounds(page: Any, page_size: Any, maximum: int) -> tuple[int, int]:
    try:
        page_num = int(page)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"page must be an integer >= 1 (got {page!r})") from exc
    try:
        size = int(page_size)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"page_size must be an integer (got {page_size!r})") from exc
    if page_num < 1:
        raise ValueError(f"page must be >= 1 (got {page_num})")
    if size < 1 or size > maximum:
        raise ValueError(f"page_size must be 1..{maximum} (got {size})")
    return page_num, size


# ------------------------------------------------------------- registry load
def _normalize_example(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    out = {
        "pmid": str(raw.get("pmid")) if raw.get("pmid") not in (None, "") else None,
        "locator": raw.get("locator") or raw.get("anchor") or raw.get("location"),
        "verbatim": raw.get("verbatim") or raw.get("quote") or raw.get("sentence"),
        "operation": raw.get("operation") or raw.get("verbatim_operation")
        or raw.get("method") or raw.get("canonical_name"),
        "canonical_name": raw.get("canonical_name"),
    }
    if not any(out.values()):
        return None
    return out


def _example_key(example: dict) -> str:
    return "|".join(str(example.get(field) or "") for field in
                    ("pmid", "locator", "verbatim", "operation"))


def _normalize_record(raw: Any, max_examples: int | None = 200) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = (raw.get("name") or raw.get("canonical_name") or raw.get("method_name")
            or raw.get("preferred_name"))
    method_id = (raw.get("method_id") or raw.get("id") or raw.get("slug")
                 or raw.get("canonical_id") or raw.get("key"))
    if not name and method_id:
        name = str(method_id)
    if not method_id and name:
        method_id = _slug(str(name))
    if not method_id:
        return None

    aliases = _as_text_list(
        raw.get("aliases") or raw.get("alias") or raw.get("synonyms")
        or raw.get("name_variants") or raw.get("mentioned_names")
    )
    name_s = str(name)
    aliases = [alias for alias in aliases if _norm(alias) != _norm(name_s)]

    stages = _as_text_list(
        raw.get("stages") or raw.get("stage") or raw.get("pipeline_stages")
        or raw.get("pipeline_stage")
    )
    packages = _as_text_list(
        raw.get("packages") or raw.get("package") or raw.get("software")
        or raw.get("tool")
    )

    prov_raw = raw.get("provenance") or raw.get("corpus") or {}
    if not isinstance(prov_raw, dict):
        prov_raw = {}
    examples_raw = list(_as_list(prov_raw.get("examples") or prov_raw.get("instances")))
    examples_raw += list(_as_list(raw.get("examples")))
    # `registry_get` adds every evidence anchor under `evidence`; keep those
    # even when `corpus.examples` is a shorter summary.
    examples_raw += list(_as_list(raw.get("evidence")))
    examples: list[dict] = []
    seen_examples: set[str] = set()
    for item in examples_raw:
        example = _normalize_example(item)
        if example is None:
            continue
        key = _example_key(example)
        if key not in seen_examples:
            seen_examples.add(key)
            examples.append(example)
    pmids = _as_text_list(prov_raw.get("pmids") or raw.get("pmids"))
    if not pmids:
        pmids = [e["pmid"] for e in examples if e.get("pmid")]
    pmids = list(dict.fromkeys(pmids))
    provenance = {
        "n_instances": _int_or_none(prov_raw.get("n_instances", raw.get("n_instances"))),
        "n_papers": _int_or_none(prov_raw.get("n_papers", raw.get("n_papers"))),
        "pmids": pmids,
        "examples": (examples if max_examples is None
                     else examples[:max_examples]),
        "examples_truncated": (max_examples is not None
                               and len(examples) > max_examples),
    }
    if provenance["n_papers"] is None and pmids:
        provenance["n_papers"] = len(pmids)

    declared = raw.get("support")
    declared_status = declared.get("status") if isinstance(declared, dict) else declared
    declared_adapters: list[dict] = []
    declared_adapter_id = None
    if isinstance(declared, dict):
        declared_adapter_id = declared.get("adapter_method_id")
        for candidate in _as_list(declared.get("adapters")):
            if isinstance(candidate, dict) and candidate.get("method_id"):
                declared_adapters.append({
                    "method_id": str(candidate.get("method_id")),
                    "quality": candidate.get("quality"),
                    "wired_operation": candidate.get("wired_operation"),
                })
    # The registry only marks `kind="executable_adapter"` records executable;
    # a literature record's `support.adapters` is a non-support relation. Other
    # registry backends may expose an explicit top-level exact mapping, which is
    # still accepted because it names the adapter id itself.
    if raw.get("kind") == "executable_adapter":
        adapter_candidates = [
            declared_adapter_id, raw.get("adapter_method_id"),
            raw.get("executable_method_id"), raw.get("wired_method_id"),
        ]
    else:
        adapter_candidates = [
            raw.get("adapter_method_id"), raw.get("executable_method_id"),
            raw.get("wired_method_id"),
        ]
    adapter_method_id = next(
        (str(c) for c in adapter_candidates
         if isinstance(c, str) and c in catalog.EXECUTABLE),
        None,
    )
    raw_adapter = raw.get("adapter")
    if (adapter_method_id is None and isinstance(raw_adapter, str)
            and raw_adapter in catalog.EXECUTABLE):
        adapter_method_id = raw_adapter

    in_scope = raw.get("in_scope")
    if in_scope is None and "in_scope" not in raw:
        scope_value = raw.get("scope")
        if isinstance(scope_value, str):
            in_scope = scope_value != "out_of_scope"
    rerunnable_roles = raw.get("rerunnable_roles")
    if isinstance(rerunnable_roles, dict):
        rerunnable_roles = {str(k): _int_or_none(v)
                            for k, v in sorted(rerunnable_roles.items())}
    else:
        rerunnable_roles = {}
    return {
        "method_id": str(method_id),
        "kind": raw.get("kind") or ("executable_adapter"
                                    if adapter_method_id else "literature_method"),
        "name": name_s,
        "canonical_name": raw.get("canonical_name") or name_s,
        "aliases": aliases,
        "stages": stages,
        "packages": packages,
        "operations": _as_text_list(raw.get("operations") or raw.get("operation")),
        "purposes": _as_text_list(raw.get("purposes") or raw.get("purpose")),
        "method_class": raw.get("method_class"),
        "in_scope": (None if in_scope is None else bool(in_scope)),
        "upstream_background": bool(raw.get("upstream_background", False)),
        "rerunnable_roles": rerunnable_roles,
        "provenance": provenance,
        "adapter_method_id": adapter_method_id,
        "registry_mapped_adapters": declared_adapters,
        "registry_mapping_quality": (declared.get("mapping_quality")
                                     if isinstance(declared, dict) else None),
        "registry_support_reason": (declared.get("reason")
                                    if isinstance(declared, dict) else None),
        # Kept only so `get` can show what the registry claimed; it never
        # promotes a record to executable.
        "registry_declared_support": declared_status,
    }


def _normalize_payload(raw: Any, source_name: str, registry_used: bool) -> dict:
    if isinstance(raw, dict):
        methods_raw = (raw.get("methods") or raw.get("records") or raw.get("items")
                       or raw.get("entries") or [])
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        metadata = dict(metadata)
        for key in ("schema", "stats", "scope", "corpus", "source", "pmid_list",
                    "retraction_excluded", "corpus_sha256", "incomplete",
                    "n_pmids", "source_path"):
            if key in raw and key not in metadata:
                metadata[key] = raw[key]
    elif isinstance(raw, (list, tuple)):
        methods_raw, metadata = raw, {}
    else:
        methods_raw, metadata = [], {}

    merged: dict[str, dict] = {}
    duplicates: list[str] = []
    for item in methods_raw:
        rec = _normalize_record(item)
        if rec is None:
            continue
        existing = merged.get(rec["method_id"])
        if existing is not None:
            duplicates.append(rec["method_id"])
            # Merge provenance rather than dropping evidence; names stay with
            # the first record because ids must be stable.
            seen_pmids = set(existing["provenance"]["pmids"])
            for pmid in rec["provenance"]["pmids"]:
                if pmid not in seen_pmids:
                    existing["provenance"]["pmids"].append(pmid)
                    seen_pmids.add(pmid)
            existing["provenance"]["n_papers"] = max(
                existing["provenance"].get("n_papers") or 0,
                rec["provenance"].get("n_papers") or 0,
                len(existing["provenance"]["pmids"]),
            ) or None
            existing["provenance"]["n_instances"] = (
                (existing["provenance"]["n_instances"] or 0)
                + (rec["provenance"]["n_instances"] or 0)
            ) or None
            known = {_example_key(e) for e in existing["provenance"]["examples"]}
            for example in rec["provenance"]["examples"]:
                key = _example_key(example)
                if key not in known and len(existing["provenance"]["examples"]) < 200:
                    existing["provenance"]["examples"].append(example)
                    known.add(key)
            for alias in rec["aliases"]:
                if alias not in existing["aliases"]:
                    existing["aliases"].append(alias)
            existing["in_scope"] = existing["in_scope"] or rec["in_scope"]
            continue
        merged[rec["method_id"]] = rec

    methods = sorted(merged.values(), key=lambda r: (r["name"].lower(), r["method_id"]))
    if duplicates:
        metadata.setdefault("duplicate_method_ids_merged", sorted(set(duplicates))[:20])
    return {
        "source": {"name": source_name, "registry_used": registry_used,
                   "n_records": len(methods)},
        "metadata": metadata,
        "methods": methods,
    }


def _fallback_payload(reason: str | None) -> dict:
    try:
        records = catalog.discovered_methods(limit=None)
    except TypeError:                                   # older catalog signature
        records = catalog.discovered_methods()
    except Exception as exc:                            # noqa: BLE001
        return {"source": {"name": "unavailable", "registry_used": False,
                           "n_records": 0,
                           "reason": f"{reason or ''}; catalog fallback failed: "
                                     f"{type(exc).__name__}: {exc}"},
                "metadata": {"incomplete": True},
                "methods": []}

    used: set[str] = set()
    methods = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        name = rec.get("canonical_name") or rec.get("name") or rec.get("method_id")
        if not name:
            continue
        method_id = _slug(str(name))
        if method_id in used:
            method_id = f"{method_id}__{hashlib.sha256(str(name).encode()).hexdigest()[:6]}"
        used.add(method_id)
        methods.append({
            "method_id": method_id,
            "name": str(name),
            "canonical_name": str(name),
            "aliases": [],
            "stages": _as_text_list(rec.get("stages") or rec.get("stage")),
            "packages": _as_text_list(rec.get("packages") or rec.get("package")),
            "in_scope": bool(rec.get("in_scope", True)),
            "provenance": {
                "n_instances": _int_or_none(rec.get("n_instances")),
                "n_papers": _int_or_none(rec.get("n_papers")),
                "pmids": _as_text_list(rec.get("pmids")),
                "examples": [e for e in (_normalize_example(x)
                                         for x in _as_list(rec.get("examples"))) if e],
                "examples_truncated": False,
            },
            "adapter_method_id": None,
            "registry_declared_support": None,
        })
    methods.sort(key=lambda r: (r["name"].lower(), r["method_id"]))
    return {
        "source": {"name": "catalog.discovered_methods", "registry_used": False,
                   "n_records": len(methods), "reason": reason},
        "metadata": {"incomplete": True, "source_path": "research/methods.jsonl"},
        "methods": methods,
    }


def _load_registry_payload() -> dict:
    """Single registry seam. Tests monkeypatch this function, not internals."""
    try:
        from . import method_registry as mr
    except Exception as exc:                            # noqa: BLE001
        return _fallback_payload(
            f"method_registry unavailable: {type(exc).__name__}: {exc}")

    loaders = []
    for attr in ("load_registry", "registry", "load_methods", "load"):
        fn = getattr(mr, attr, None)
        if callable(fn):
            loaders.append((attr, fn))
    for attr, fn in loaders:
        try:
            raw = fn()
        except TypeError:
            continue
        except Exception as exc:                        # noqa: BLE001
            return _fallback_payload(
                f"method_registry.{attr} failed: {type(exc).__name__}: {exc}")
        payload = _normalize_payload(raw, f"method_registry.{attr}",
                                     registry_used=True)
        if payload["methods"]:
            return payload

    for attr in ("REGISTRY", "METHODS", "registry"):
        raw = getattr(mr, attr, None)
        if isinstance(raw, (dict, list, tuple)):
            payload = _normalize_payload(raw, f"method_registry.{attr}",
                                         registry_used=True)
            if payload["methods"]:
                return payload

    return _fallback_payload(
        "method_registry is importable but exposes no usable registry payload")


# --------------------------------------------------------------- support
def _adapter_for(record: dict, detail: bool = False) -> dict | None:
    """Exact adapter wiring only.

    A name that merely resembles an adapter ("UCell", "CopyKAT", "GSEA",
    "DESeq2") never becomes executable here; that fuzzy relationship is
    reported separately as non-support by :func:`_related_wiring`.
    """
    adapter_id = record.get("adapter_method_id")
    if not isinstance(adapter_id, str) or adapter_id not in catalog.EXECUTABLE:
        adapter_id = record["method_id"] if record["method_id"] in catalog.EXECUTABLE else None
    if adapter_id is None:
        return None
    wired = catalog.EXECUTABLE[adapter_id]
    out = {
        "adapter_method_id": adapter_id,
        "package": wired.get("package"),
        "adapter": wired.get("adapter"),
        "stage": wired.get("stage"),
        "requires": wired.get("requires"),
        "provides": wired.get("provides"),
        "runtime_check": "not_performed_by_this_tool",
    }
    if detail:
        for key in ("name", "status", "statistical_notes", "defaults",
                    "params_schema", "literature_refs"):
            out[key] = wired.get(key)
    return out


def _support_for(record: dict) -> dict:
    adapter = _adapter_for(record)
    if adapter is not None:
        return {
            "status": "executable",
            "reason": "exact method id is wired in sctda.catalog.EXECUTABLE",
            "counts_as_support": True,
            "adapter_method_id": adapter["adapter_method_id"],
            "mapping_quality": record.get("registry_mapping_quality"),
            "runtime_check": "not_performed_by_this_tool",
        }
    declared = record.get("registry_declared_support")
    reason = "no wired adapter matches this method id exactly"
    if declared and declared != "corpus_only":
        reason += f"; registry declared {declared!r}, which is not support"
    registry_reason = record.get("registry_support_reason")
    if registry_reason:
        reason += f"; registry note: {registry_reason}"
    return {
        "status": "corpus_only",
        "reason": reason,
        "counts_as_support": False,
        "adapter_method_id": None,
    }


def _related_wiring(record: dict) -> list[dict]:
    """Adapter relations, explicitly not support.

    The registry's own ``support.adapters`` mapping is authoritative for the
    relation; the catalog lookup is only a fallback when the registry did not
    provide one. Neither can promote a descriptor-matching name (UCell,
    CopyKAT, GSEA, DESeq2) to ``executable``.
    """
    mapped = record.get("registry_mapped_adapters") or []
    if mapped:
        out = []
        for hit in mapped:
            counts = hit.get("method_id") == record.get("adapter_method_id")
            out.append({
                "method_id": hit.get("method_id"),
                "adapter_record_id": (f"adapter:{hit['method_id']}"
                                      if hit.get("method_id") in catalog.EXECUTABLE
                                      else None),
                "quality": hit.get("quality"),
                "wired_operation": hit.get("wired_operation"),
                "counts_as_executable": counts,
                "note": ("exact adapter mapping named by the registry" if counts
                         else "registry relation only; fetch the separate "
                              "adapter:<method_id> record for executable status"),
            })
        return out
    try:
        hits = catalog.supported_by_detail(record["name"])
    except Exception:                                   # noqa: BLE001
        return []
    return [{
        "method_id": hit.get("method_id"),
        "quality": hit.get("quality"),
        "wired_operation": hit.get("wired_operation"),
        "counts_as_executable": False,
        "note": ("related name/package/operation only; it does not make this "
                 "method executable"),
    } for hit in hits]


# --------------------------------------------------------------- matching
def _query_terms(query: str | None) -> list[str]:
    if not query:
        return []
    return [term for term in _SPLIT.split(query.casefold()) if term]


def _record_fields(record: dict) -> list[tuple[str, str, list[str]]]:
    fields: list[tuple[str, str, list[str]]] = []

    def add(kind: str, value: Any) -> None:
        text = str(value or "").strip()
        if not text:
            return
        tokens = [token for token in _SPLIT.split(text.casefold()) if token]
        normalized = _norm(text)
        if normalized:
            fields.append((kind, normalized, tokens))

    add("method_id", record.get("method_id"))
    add("name", record.get("name"))
    add("canonical_name", record.get("canonical_name"))
    for alias in record.get("aliases") or []:
        add("alias", alias)
    for stage in record.get("stages") or []:
        add("stage", stage)
    for package in record.get("packages") or []:
        add("package", package)
    for operation in record.get("operations") or []:
        add("operation", operation)
    for example in record.get("provenance", {}).get("examples") or []:
        add("operation", example.get("operation"))
        add("locator", example.get("locator"))
    return fields


def _match_record(record: dict, terms: list[str], raw_query: str | None) -> dict | None:
    if not terms:
        return {"quality": "all", "matched_fields": [], "matched_terms": []}
    fields = _record_fields(record)
    matched_fields: list[str] = []
    worst = 0
    for term in terms:
        best = None                                   # (tier, kind)
        for kind, normalized, tokens in fields:
            if normalized == term:
                best = (0, kind)
                break
            if len(term) >= 2 and normalized.startswith(term):
                if best is None or best[0] > 1:
                    best = (1, kind)
            elif term in tokens:
                if best is None or best[0] > 2:
                    best = (2, kind)
            elif len(term) >= 2 and any(token.startswith(term) for token in tokens):
                if best is None or best[0] > 1:
                    best = (1, kind)
        if best is None:
            return None
        worst = max(worst, best[0])
        if best[1] not in matched_fields:
            matched_fields.append(best[1])
    quality = {0: "exact", 1: "prefix", 2: "token"}[worst]
    return {"quality": quality, "matched_fields": matched_fields,
            "matched_terms": list(terms), "query": raw_query}


def _method_payload(record: dict, detail: bool = False,
                    match: dict | None = None,
                    include_examples: bool = True) -> dict:
    provenance = record.get("provenance") or {}
    pmids = provenance.get("pmids") or []
    examples = provenance.get("examples") or []
    pmid_cap = 100 if detail else 10
    example_cap = 100 if detail else 3
    out = {
        "method_id": record["method_id"],
        "kind": record.get("kind"),
        "name": record["name"],
        "canonical_name": record.get("canonical_name"),
        "aliases": record.get("aliases") or [],
        "stages": record.get("stages") or [],
        "packages": record.get("packages") or [],
        "operations": (record.get("operations") or [])[:10],
        "method_class": record.get("method_class"),
        "in_scope": record.get("in_scope", True),
        "support": _support_for(record),
        "adapter": _adapter_for(record, detail=detail),
        "provenance": {
            "n_instances": provenance.get("n_instances"),
            "n_papers": provenance.get("n_papers"),
            "n_pmids": len(pmids),
            "pmids": pmids if pmid_cap is None else pmids[:pmid_cap],
            "pmids_truncated": pmid_cap is not None and len(pmids) > pmid_cap,
            "n_examples": len(examples),
        },
    }
    if include_examples:
        out["provenance"]["examples"] = (examples if example_cap is None
                                         else examples[:example_cap])
        out["provenance"]["examples_truncated"] = (
            example_cap is not None and len(examples) > example_cap)
    if match is not None:
        out["match"] = match
    if detail:
        out["related_wiring"] = _related_wiring(record)
        out["registry_declared_support"] = record.get("registry_declared_support")
        out["registry_support_reason"] = record.get("registry_support_reason")
        out["purposes"] = record.get("purposes") or []
        out["rerunnable_roles"] = record.get("rerunnable_roles") or []
        out["upstream_background"] = bool(record.get("upstream_background", False))
    return out


def _registry_get_record(method_id: str) -> dict | None:
    """Full native record (including all anchors) when the registry offers it.

    ``load_registry`` is enough for search, but ``registry_get`` carries every
    evidence anchor. That distinction is preserved: search stays cheap, get is
    allowed to be complete. A missing or failing registry is not an error here;
    the already-loaded record remains authoritative.
    """
    try:
        from . import method_registry as mr
    except Exception:                                   # noqa: BLE001
        return None
    fn = getattr(mr, "registry_get", None)
    if not callable(fn):
        return None
    try:
        raw = fn(method_id)
    except Exception:                                   # noqa: BLE001
        return None
    if not isinstance(raw, dict):
        return None
    return _normalize_record(raw, max_examples=None)


def _merge_detail_record(base: dict, extra: dict,
                         max_examples: int | None = None) -> dict:
    """Merge native evidence into a loaded record without changing support."""
    merged = dict(base)
    for field in ("aliases", "stages", "packages", "operations", "purposes"):
        values = list(merged.get(field) or [])
        for value in extra.get(field) or []:
            if value not in values:
                values.append(value)
        merged[field] = values
    roles = dict(merged.get("rerunnable_roles") or {})
    for role, count in (extra.get("rerunnable_roles") or {}).items():
        roles[role] = max(roles.get(role) or 0, count or 0)
    merged["rerunnable_roles"] = roles
    base_prov = dict(merged.get("provenance") or {})
    extra_prov = extra.get("provenance") or {}
    pmids = list(base_prov.get("pmids") or [])
    for pmid in extra_prov.get("pmids") or []:
        if pmid not in pmids:
            pmids.append(pmid)
    examples = list(base_prov.get("examples") or [])
    known = {_example_key(example) for example in examples}
    dropped = False
    for example in extra_prov.get("examples") or []:
        key = _example_key(example)
        if key in known:
            continue
        if max_examples is not None and len(examples) >= max_examples:
            dropped = True
            continue
        examples.append(example)
        known.add(key)
    base_prov["pmids"] = pmids
    base_prov["examples"] = examples
    base_prov["examples_truncated"] = (
        dropped or (extra_prov.get("examples_truncated") is True
                    and max_examples is not None))
    base_prov["n_papers"] = max(base_prov.get("n_papers") or 0,
                                extra_prov.get("n_papers") or 0,
                                len(pmids)) or None
    base_prov["n_instances"] = max(base_prov.get("n_instances") or 0,
                                   extra_prov.get("n_instances") or 0) or None
    merged["provenance"] = base_prov
    if not merged.get("adapter_method_id") and extra.get("adapter_method_id"):
        merged["adapter_method_id"] = extra["adapter_method_id"]
    if not merged.get("registry_mapped_adapters") and extra.get("registry_mapped_adapters"):
        merged["registry_mapped_adapters"] = extra["registry_mapped_adapters"]
    if not merged.get("registry_support_reason") and extra.get("registry_support_reason"):
        merged["registry_support_reason"] = extra["registry_support_reason"]
    return merged


def _source_failure(payload: dict) -> str | None:
    """An empty registry with a reason is a failure, not an empty answer."""
    if payload.get("methods"):
        return None
    reason = (payload.get("source") or {}).get("reason")
    return str(reason) if reason else None


def _compact_metadata(metadata: dict | None) -> dict:
    """Small scope/hash/count block for search and get responses.

    The registry's full audit payload contains exclusion PMID lists and mapping
    diagnostics that can be hundreds of IDs long. Those belong to
    ``sctda_method_stats``; each method response must stay bounded.
    """
    if not isinstance(metadata, dict):
        return {}
    out: dict = {"incomplete": metadata.get("incomplete")}
    corpus = metadata.get("corpus") if isinstance(metadata.get("corpus"), dict) else {}
    if corpus:
        out["corpus"] = {key: corpus.get(key) for key in (
            "path", "sha256", "sha256_16", "bytes", "n_lines",
            "n_records_parsed", "n_records_in_scope", "n_records_qualifying",
        ) if key in corpus}
    scope = metadata.get("scope") if isinstance(metadata.get("scope"), dict) else {}
    if scope:
        compact_scope = {key: scope.get(key) for key in (
            "pmid_list", "pmid_list_path", "pmid_list_sha256",
            "pmid_list_sha256_16", "pmid_list_matches_known", "n_papers",
            "n_included_pmids", "n_effective_pmids",
            "n_pmids_with_qualifying_records", "n_retracted_pmids",
        ) if key in scope}
        if "retraction_excluded" in scope:
            compact_scope["retraction_excluded"] = scope["retraction_excluded"][:20]
        out["scope"] = compact_scope
    stats = metadata.get("stats") if isinstance(metadata.get("stats"), dict) else {}
    counts = stats.get("counts") if isinstance(stats.get("counts"), dict) else {}
    if counts:
        out["counts"] = {key: counts.get(key) for key in (
            "n_records_total", "n_records_qualifying", "n_records_excluded",
            "n_methods_literature", "n_methods_total", "n_adapters",
            "n_single_paper_methods",
        ) if key in counts}
    pmid_list = stats.get("pmid_list") if isinstance(stats.get("pmid_list"), dict) else {}
    if pmid_list:
        out["pmid_list"] = {key: pmid_list.get(key) for key in (
            "sha256", "sha256_16", "n_pmids", "matches_known",
        ) if key in pmid_list}
    retractions = stats.get("retractions")
    if isinstance(retractions, list):
        out["retraction"] = {
            "n": len(retractions),
            "pmids": [str(row.get("pmid")) for row in retractions
                      if isinstance(row, dict) and row.get("pmid")][:20],
        }
    if "corpus_sha256" in metadata:
        out["corpus_sha256"] = metadata["corpus_sha256"]
    if "n_pmids" in metadata:
        out["n_pmids"] = metadata["n_pmids"]
    out["detail_tool"] = "sctda_method_stats"
    return out


# ------------------------------------------------------------------ search
def search_methods(query: str | None = None, page: Any = 1,
                   page_size: Any = DEFAULT_PAGE_SIZE, stage: str | None = None,
                   package: str | None = None, support: str | None = None,
                   in_scope: bool | None = None) -> dict:
    page_num, size = _page_bounds(page, page_size, MAX_PAGE_SIZE)
    if support is not None and support not in SUPPORT_STATUSES:
        raise ValueError(f"support must be one of {list(SUPPORT_STATUSES)} (got {support!r})")
    if query is not None and not isinstance(query, str):
        query = str(query)
    terms = _query_terms(query)
    if query is not None and str(query).strip() and not terms:
        raise ValueError("query contains no searchable terms")
    payload = _load_registry_payload()
    records = payload["methods"]
    failure = _source_failure(payload)
    if failure is not None:
        return {
            "status": "unavailable",
            "scope": "registry",
            "query": query,
            "filters": {"stage": stage, "package": package, "support": support,
                        "in_scope": in_scope},
            "source": payload["source"],
            "page": page_num,
            "page_size": size,
            "total": 0,
            "returned": 0,
            "match_state": "unavailable",
            "error": failure,
            "next": {"tool": "sctda_method_external_search",
                     "arguments": {"query": query or "", "scope": "both"}},
            "support_vocabulary": SUPPORT_VOCABULARY,
            "methods": [],
            "notes": [
                "the local registry could not be loaded; this is not an empty "
                "result and not a claim that no method exists",
                "the external lookup is still available, but its hits are "
                "pointers, not support",
            ],
        }

    # Filters are applied before matching; support is computed from catalog
    # wiring, never from a registry-declared label.
    stage_key = _norm(stage) if stage else None
    package_key = _norm(package) if package else None
    selected: list[tuple[dict, dict]] = []
    for record in records:
        if in_scope is not None:
            record_scope = record.get("in_scope")
            # A record with unknown scope (adapter records) is never excluded by
            # this corpus-scope filter.
            if record_scope is not None and bool(record_scope) is not bool(in_scope):
                continue
        if stage_key and stage_key not in {_norm(s) for s in record.get("stages") or []}:
            continue
        if package_key and package_key not in {_norm(p) for p in record.get("packages") or []}:
            continue
        record_support = _support_for(record)["status"]
        if support and record_support != support:
            continue
        match = _match_record(record, terms, query)
        if match is None:
            continue
        selected.append((record, match))

    selected.sort(key=lambda item: (
        {"exact": 0, "prefix": 1, "token": 2, "all": 0}[item[1]["quality"]],
        item[0]["name"].lower(),
        item[0]["method_id"],
    ))
    total = len(selected)
    total_pages = (total + size - 1) // size
    offset = (page_num - 1) * size
    window = selected[offset:offset + size]
    methods = [_method_payload(record, match=match) for record, match in window]

    return {
        "status": "ok",
        "scope": "registry",
        "query": query,
        "filters": {"stage": stage, "package": package, "support": support,
                    "in_scope": in_scope},
        "source": payload["source"],
        "registry_metadata": _compact_metadata(payload.get("metadata")),
        "page": page_num,
        "page_size": size,
        "offset": offset,
        "total": total,
        "total_pages": total_pages,
        "has_more": offset + len(methods) < total,
        "page_out_of_range": page_num > total_pages and total > 0,
        "returned": len(methods),
        "match_state": "matched" if total else "not_found",
        "next": ({"tool": "sctda_method_external_search",
                  "arguments": {"query": query or "",
                                "scope": "both"}}
                 if total == 0 else None),
        "support_vocabulary": SUPPORT_VOCABULARY,
        "methods": methods,
        "notes": [
            "ordering is exact/prefix/token match then name; corpus frequency is "
            "provenance, never a ranking or admission filter",
            "the local extraction is known to be incomplete; an empty result is "
            "a gap in the local source, not a claim that no method exists",
        ],
    }


def _anchor_page(record: dict, page: Any, page_size: Any) -> dict:
    page_num, size = _page_bounds(page, page_size, MAX_ANCHOR_PAGE_SIZE)
    examples = (record.get("provenance") or {}).get("examples") or []
    total = len(examples)
    total_pages = (total + size - 1) // size
    offset = (page_num - 1) * size
    items = examples[offset:offset + size]
    return {
        "page": page_num,
        "page_size": size,
        "offset": offset,
        "total": total,
        "total_pages": total_pages,
        "has_more": offset + len(items) < total,
        "page_out_of_range": page_num > total_pages and total > 0,
        "returned": len(items),
        "items": items,
    }


# --------------------------------------------------------------------- get
def get_method(method_id: str, anchor_page: Any = 1,
               anchor_page_size: Any = DEFAULT_ANCHOR_PAGE_SIZE) -> dict:
    if not method_id or not str(method_id).strip():
        raise ValueError("method_id is required")
    anchor_page_num, anchor_size = _page_bounds(
        anchor_page, anchor_page_size, MAX_ANCHOR_PAGE_SIZE)
    query = str(method_id).strip()
    payload = _load_registry_payload()
    records = payload["methods"]
    failure = _source_failure(payload)
    if failure is not None:
        return {
            "status": "unavailable",
            "found": None,
            "query": query,
            "error": failure,
            "source": payload["source"],
            "support_vocabulary": SUPPORT_VOCABULARY,
            "notes": [
                "the local registry could not be loaded; no conclusion about "
                "this method follows",
                "use sctda_method_external_search for a public lookup; its hits "
                "are pointers, not support",
            ],
        }

    # `registry_get` accepts an exact registry id, canonical name, or the short
    # executable id (for example `de_vs_rest_wilcox`). Use it first so a user
    # does not have to know the `adapter:` prefix. Tests isolate this seam.
    native = _registry_get_record(query)
    if native is not None:
        base = next((r for r in records if r["method_id"] == native["method_id"]), None)
        record = (_merge_detail_record(base, native, max_examples=None)
                  if base is not None else native)
        norm_query = _norm(query)
        if record["method_id"] == query:
            matched_by = "method_id"
        elif norm_query in {_norm(record["name"]), _norm(record.get("canonical_name"))}:
            matched_by = "canonical_name"
        else:
            matched_by = "registry_lookup"
        return {
            "status": "ok",
            "found": True,
            "query": query,
            "method_id": record["method_id"],
            "matched_by": matched_by,
            "method": _method_payload(record, detail=True, include_examples=False),
            "anchors": _anchor_page(record, anchor_page_num, anchor_size),
            "source": payload["source"],
            "registry_metadata": _compact_metadata(payload.get("metadata")),
            "support_vocabulary": SUPPORT_VOCABULARY,
            "notes": [
                "support=executable means an adapter is wired; installation and "
                "successful execution are separate facts not checked here",
                "registry_declared_support is shown for audit only and never "
                "upgrades support",
                "every evidence anchor is returned through anchors; use "
                "anchor_page/anchor_page_size to read the full list",
            ],
        }

    exact = [r for r in records if r["method_id"] == query]
    if not exact:
        key = _norm(query)
        exact = [r for r in records
                 if key and key in {_norm(r["name"]),
                                     _norm(r.get("canonical_name"))}]
    if len(exact) == 1:
        record = exact[0]
        return {
            "status": "ok",
            "found": True,
            "query": query,
            "method_id": record["method_id"],
            "matched_by": ("method_id" if record["method_id"] == query
                           else "canonical_name"),
            "method": _method_payload(record, detail=True, include_examples=False),
            "anchors": _anchor_page(record, anchor_page_num, anchor_size),
            "source": payload["source"],
            "registry_metadata": _compact_metadata(payload.get("metadata")),
            "support_vocabulary": SUPPORT_VOCABULARY,
            "notes": [
                "support=executable means an adapter is wired; installation and "
                "successful execution are separate facts not checked here",
                "registry_declared_support is shown for audit only and never "
                "upgrades support",
                "every evidence anchor is returned through anchors; use "
                "anchor_page/anchor_page_size to read the full list",
            ],
        }
    if len(exact) > 1:
        return {
            "status": "ok",
            "found": False,
            "ambiguous": True,
            "query": query,
            "support": {"status": "corpus_only" if exact else "not_found",
                        "reason": "multiple registry records match this identifier",
                        "counts_as_support": False},
            "candidates": [{"method_id": r["method_id"], "name": r["name"],
                            "support_status": _support_for(r)["status"]}
                           for r in exact],
            "source": payload["source"],
            "notes": ["use the exact method_id to disambiguate; no record was chosen"],
        }

    # Near matches are suggestions only. They are never returned as the record.
    names = list({r["name"] for r in records} | {r["method_id"] for r in records})
    close = difflib.get_close_matches(query, names, n=5, cutoff=0.5)
    if not close:
        key = _norm(query)
        close = [r["name"] for r in records if key and key in _norm(r["name"])][:5]
    near = []
    for candidate in close:
        for record in records:
            if candidate in (record["name"], record["method_id"]):
                near.append({"method_id": record["method_id"], "name": record["name"],
                             "support_status": _support_for(record)["status"]})
                break
    return {
        "status": "ok",
        "found": False,
        "query": query,
        "support": {"status": "not_found",
                    "reason": SUPPORT_VOCABULARY["not_found"],
                    "counts_as_support": False},
        "near_matches": near,
        "source": payload["source"],
        "support_vocabulary": SUPPORT_VOCABULARY,
        "notes": [
            "no registry record matched; this does not prove the method does not exist",
            "use sctda_method_external_search for a public-literature lookup; hits "
            "there are pointers, not support",
        ],
    }


# ------------------------------------------------------------------- stats
def method_stats() -> dict:
    payload = _load_registry_payload()
    records = payload["methods"]
    failure = _source_failure(payload)
    if failure is not None:
        return {
            "status": "unavailable",
            "scope": "registry",
            "error": failure,
            "source": payload["source"],
            "support_vocabulary": SUPPORT_VOCABULARY,
            "notes": [
                "the local registry could not be loaded; no counts are reported",
                "this is not a zero-method census",
            ],
        }
    by_support = {status: 0 for status in SUPPORT_STATUSES}
    by_kind: dict[str, int] = {}
    stages: dict[str, int] = {}
    packages: dict[str, int] = {}
    pmids: set[str] = set()
    instances = 0
    for record in records:
        by_support[_support_for(record)["status"]] += 1
        kind = record.get("kind") or "unknown"
        by_kind[kind] = by_kind.get(kind, 0) + 1
        for stage in record.get("stages") or []:
            stages[stage] = stages.get(stage, 0) + 1
        for package in record.get("packages") or []:
            packages[package] = packages.get(package, 0) + 1
        provenance = record.get("provenance") or {}
        pmids.update(provenance.get("pmids") or [])
        instances += provenance.get("n_instances") or 0

    stage_rows = [{"stage": key, "n_methods": value}
                  for key, value in sorted(stages.items(),
                                           key=lambda kv: (-kv[1], kv[0].lower()))]
    package_rows = [{"package": key, "n_methods": value}
                    for key, value in sorted(packages.items(),
                                             key=lambda kv: (-kv[1], kv[0].lower()))]
    adapters = []
    for method_id, wired in sorted(catalog.EXECUTABLE.items()):
        adapters.append({
            "adapter_method_id": method_id,
            "name": wired.get("name"),
            "package": wired.get("package"),
            "adapter": wired.get("adapter"),
            "stage": wired.get("stage"),
            "requires": wired.get("requires"),
            "provides": wired.get("provides"),
            "runtime_check": "not_performed_by_this_tool",
        })

    metadata = payload.get("metadata") or {}
    return {
        "status": "ok",
        "scope": "registry",
        "source": payload["source"],
        "registry_metadata": metadata,
        "counts": {
            "n_methods": len(records),
            "n_literature_methods": sum(1 for r in records
                                        if r.get("kind") == "literature_method"),
            "n_adapter_records": sum(1 for r in records
                                     if r.get("kind") == "executable_adapter"),
            "n_in_scope": sum(1 for r in records if r.get("in_scope") is True),
            "n_out_of_scope": sum(1 for r in records if r.get("in_scope") is False),
            "n_unknown_scope": sum(1 for r in records if r.get("in_scope") is None),
            "n_pmids_represented": len(pmids),
            "n_instances_summed": instances,
            "by_kind": by_kind,
            "by_support": by_support,
        },
        "stages": stage_rows,
        "n_stages": len(stage_rows),
        "packages": package_rows[:50],
        "n_packages": len(package_rows),
        "packages_truncated": len(package_rows) > 50,
        "adapters": adapters,
        "n_adapters": len(adapters),
        "external_search": {
            "tool": "sctda_method_external_search",
            "sources": ["pubmed", "europepmc"],
            "endpoints": {"pubmed": literature.EUTILS, "europepmc": literature.EPMC},
            "cache": "per-project literature_cache (endpoint, requested_utc, "
                     "http_status, response_sha256, body_path)",
            "live_reachability": "not_tested",
        },
        "support_vocabulary": SUPPORT_VOCABULARY,
        "notes": [
            "by_support.external_only and by_support.not_found are answer states, "
            "not registry records; they stay 0 here by construction",
            "adapter wiring is code-level; R package installation is not probed",
            "the local method extraction is known to be incomplete and is not an "
            "exhaustive census",
            "counts are descriptive, not a ranking or a candidate filter",
        ],
    }


# ------------------------------------------------------- external literature
def _validate_external_query(query: Any) -> str:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query is required and must be a non-empty string")
    cleaned = "".join(ch if ch.isprintable() else " " for ch in query).strip()
    if len(cleaned) > MAX_QUERY_CHARS:
        raise ValueError(f"query must be <= {MAX_QUERY_CHARS} characters")
    return cleaned


def _effective_query(query: str, single_cell_filter: bool, source: str) -> str:
    if not single_cell_filter:
        return query
    body = PUBMED_SC_FILTER if source == "pubmed" else EPMC_SC_FILTER
    return f"({query}) AND {body}"


def _strip_html(text: Any) -> str:
    return re.sub(r"\s+", " ", _HTML_TAG.sub(" ", str(text or ""))).strip()


def _cache_trace(conn: sqlite3.Connection, url: str) -> dict:
    cache_id = literature._cache_key(url, None)
    row = conn.execute(
        "SELECT requested_utc, http_status, response_sha256, body_path, notes "
        "FROM literature_cache WHERE cache_id=?", (cache_id,)).fetchone()
    if not row:
        return {"endpoint": url, "requested_utc": None, "http_status": None,
                "response_sha256": None, "body_path": None,
                "notes": "no literature_cache row found"}
    return {"endpoint": url, "requested_utc": row["requested_utc"],
            "http_status": row["http_status"],
            "response_sha256": row["response_sha256"],
            "body_path": row["body_path"], "notes": row["notes"]}


def _pubmed_page(proj: Project, conn: sqlite3.Connection, query: str,
                 page: int, page_size: int, include_abstracts: bool) -> dict:
    retstart = (page - 1) * page_size
    url = (f"{literature.EUTILS}/esearch.fcgi?db=pubmed&retmode=json"
           f"&retmax={page_size}&retstart={retstart}"
           f"&term={urllib.parse.quote(query)}")
    response = literature.http_get(proj, conn, url)
    trace = _cache_trace(conn, url)
    if not response.get("ok"):
        http_status = response.get("status")
        return {"source": "pubmed",
                "status": "failed" if http_status is not None else "unreachable",
                "count": None,
                "error": response.get("error") or f"HTTP {http_status}",
                "effective_query": query, "traces": [trace], "hits": [],
                "has_more": False}
    try:
        result = json.loads(response["body"])["esearchresult"]
    except (KeyError, json.JSONDecodeError) as exc:
        return {"source": "pubmed", "status": "failed", "count": None,
                "error": f"parse: {exc}", "effective_query": query,
                "traces": [trace], "hits": [], "has_more": False}

    pmids = [str(p) for p in result.get("idlist") or []]
    count = _int_or_none(result.get("count"))
    metadata: dict[str, dict] = {}
    metadata_status = "not_fetched"
    traces = [trace]
    if pmids:
        fetched = literature.fetch_abstracts(proj, conn, pmids)
        efetch_url = (f"{literature.EUTILS}/efetch.fcgi?db=pubmed&retmode=xml"
                      f"&id={','.join(pmids)}")
        traces.append(_cache_trace(conn, efetch_url))
        if fetched.get("status") == "ok":
            metadata = {str(r.get("pmid")): r for r in fetched.get("records") or []}
            metadata_status = "ok"
        else:
            metadata_status = fetched.get("status") or "failed"

    hits = []
    for rank, pmid in enumerate(pmids, start=retstart + 1):
        record = metadata.get(pmid, {})
        abstract = record.get("abstract") or ""
        doi = record.get("doi")
        pmcid = record.get("pmcid")
        identifiers = {
            "pmid": pmid,
            "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        }
        if doi:
            identifiers["doi"] = doi
        if pmcid:
            identifiers["pmcid"] = pmcid
            pmc_key = (pmcid if str(pmcid).upper().startswith("PMC")
                       else f"PMC{pmcid}")
            identifiers["epmc_url"] = f"https://europepmc.org/article/PMC/{pmc_key}"
        hits.append({
            "rank": rank,
            "pmid": pmid,
            "pmcid": pmcid,
            "doi": doi,
            "title": record.get("title"),
            "journal": record.get("journal"),
            "year": record.get("year"),
            "abstract": abstract if include_abstracts else None,
            "abstract_available": bool(abstract),
            "metadata_status": ("ok" if pmid in metadata else
                                ("missing" if metadata_status == "ok"
                                 else metadata_status)),
            "source": "pubmed",
            "sources": ["pubmed"],
            "identifiers": identifiers,
        })
    return {
        "source": "pubmed",
        "status": "ok" if metadata_status in ("ok", "not_fetched") else "partial",
        "count": count,
        "effective_query": query,
        "metadata_status": metadata_status,
        "traces": traces,
        "hits": hits,
        "has_more": bool(count is not None and retstart + len(pmids) < count),
        "next_offset": retstart + page_size,
    }


def _epmc_page(proj: Project, conn: sqlite3.Connection, query: str,
               page: int, page_size: int, include_abstracts: bool) -> dict:
    if page > MAX_EPMC_PAGE:
        raise ValueError(
            f"page must be <= {MAX_EPMC_PAGE} for Europe PMC cursor paging")
    cursor = "*"
    traces: list[dict] = []
    result: dict | None = None
    hit_count: int | None = None
    for index in range(1, page + 1):
        if not cursor:
            return {"source": "europepmc", "status": "ok", "count": hit_count,
                    "effective_query": query, "traces": traces, "hits": [],
                    "has_more": False, "page_out_of_range": True,
                    "pages_fetched": index - 1}
        url = (f"{literature.EPMC}/search?query={urllib.parse.quote(query)}"
               f"&format=json&pageSize={page_size}&resultType=core"
               f"&cursorMark={urllib.parse.quote(cursor, safe='*')}")
        response = literature.http_get(proj, conn, url)
        traces.append(_cache_trace(conn, url))
        if not response.get("ok"):
            http_status = response.get("status")
            return {"source": "europepmc",
                    "status": "failed" if http_status is not None else "unreachable",
                    "count": hit_count,
                    "error": response.get("error") or f"HTTP {http_status}",
                    "failed_at_page": index, "effective_query": query,
                    "traces": traces, "hits": [], "has_more": False}
        try:
            payload = json.loads(response["body"])
        except json.JSONDecodeError as exc:
            return {"source": "europepmc", "status": "failed", "count": hit_count,
                    "error": f"parse: {exc}", "failed_at_page": index,
                    "effective_query": query, "traces": traces, "hits": [],
                    "has_more": False}
        page_hit_count = _int_or_none(payload.get("hitCount"))
        if page_hit_count is not None:
            hit_count = page_hit_count
        next_cursor = payload.get("nextCursorMark") or ""
        if index == page:
            result = payload
        if index < page and next_cursor == cursor:
            return {"source": "europepmc", "status": "ok", "count": hit_count,
                    "effective_query": query, "traces": traces, "hits": [],
                    "has_more": False, "page_out_of_range": True,
                    "pages_fetched": index,
                    "note": "cursor did not advance; requested page is past the end"}
        cursor = next_cursor

    if result is None:
        return {"source": "europepmc", "status": "failed", "count": hit_count,
                "error": "no result page retrieved", "effective_query": query,
                "traces": traces, "hits": [], "has_more": False}

    hits = []
    result_rows = (result.get("resultList") or {}).get("result") or []
    for rank, item in enumerate(result_rows, start=(page - 1) * page_size + 1):
        pmid = str(item.get("pmid")) if item.get("pmid") else None
        pmcid = item.get("pmcid")
        doi = item.get("doi")
        identifiers = {"pmid": pmid, "pmcid": pmcid, "doi": doi}
        if pmid:
            identifiers["pubmed_url"] = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        if pmcid:
            identifiers["epmc_url"] = f"https://europepmc.org/article/PMC/{pmcid}"
        abstract = _strip_html(item.get("abstractText")) if include_abstracts else None
        hits.append({
            "rank": rank,
            "pmid": pmid,
            "pmcid": pmcid,
            "doi": doi,
            "title": item.get("title"),
            "journal": item.get("journalTitle"),
            "year": item.get("pubYear"),
            "abstract": abstract,
            "abstract_available": bool(item.get("abstractText")),
            "metadata_status": "core",
            "is_open_access": item.get("isOpenAccess") == "Y",
            "fulltext_status": ("in_epmc" if item.get("inEPMC") == "Y"
                                else "open_access" if item.get("isOpenAccess") == "Y"
                                else "not_available"),
            "source": "europepmc",
            "sources": ["europepmc"],
            "identifiers": identifiers,
        })
    hit_count = _int_or_none(result.get("hitCount"))
    return {
        "source": "europepmc",
        "status": "ok",
        "count": hit_count,
        "effective_query": query,
        "traces": traces,
        "hits": hits,
        "pages_fetched": page,
        "page_out_of_range": False,
        "has_more": bool(hit_count is not None and page * page_size < hit_count),
        "next_offset": page * page_size,
    }


def _hit_key(hit: dict) -> str:
    if hit.get("pmid"):
        return "pmid:" + str(hit["pmid"])
    if hit.get("doi"):
        return "doi:" + str(hit["doi"]).lower()
    return "title:" + _norm(hit.get("title")) + ":" + str(hit.get("year") or "")


def _merge_hits(source_results: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    doi_index: dict[str, str] = {}
    pmid_index: dict[str, str] = {}

    def provenance(hit: dict, trace: dict | None) -> dict:
        return {
            "source": hit["source"],
            "endpoint": trace.get("endpoint") if trace else None,
            "requested_utc": trace.get("requested_utc") if trace else None,
            "http_status": trace.get("http_status") if trace else None,
            "response_sha256": trace.get("response_sha256") if trace else None,
            "cache_path": trace.get("body_path") if trace else None,
        }

    def absorb(existing: dict, hit: dict, trace: dict | None, key: str) -> None:
        for field in ("pmcid", "doi", "title", "journal", "year",
                      "is_open_access", "fulltext_status"):
            missing = field not in existing or existing.get(field) is None
            if missing and hit.get(field) is not None:
                existing[field] = hit[field]
        if not existing.get("abstract") and hit.get("abstract"):
            existing["abstract"] = hit["abstract"]
            existing["abstract_available"] = True
        existing["sources"] = sorted(set(existing.get("sources") or []) |
                                     set(hit.get("sources") or []))
        existing["identifiers"] = {**hit.get("identifiers", {}),
                                   **existing.get("identifiers", {})}
        if trace:
            existing.setdefault("provenance", []).append(provenance(hit, trace))
        if hit.get("doi"):
            doi_index.setdefault(str(hit["doi"]).lower(), key)
        if hit.get("pmid"):
            pmid_index.setdefault(str(hit["pmid"]), key)

    for source in source_results:
        trace = (source.get("traces") or [None])[-1]
        for hit in source.get("hits") or []:
            key = _hit_key(hit)
            existing = merged.get(key)
            if existing is None and hit.get("doi"):
                existing = merged.get(doi_index.get(str(hit["doi"]).lower(), ""))
            if existing is None and hit.get("pmid"):
                existing = merged.get(pmid_index.get(str(hit["pmid"]), ""))
            if existing is None:
                hit = dict(hit)
                hit["provenance"] = [provenance(hit, trace)] if trace else []
                merged[key] = hit
                if hit.get("doi"):
                    doi_index.setdefault(str(hit["doi"]).lower(), key)
                if hit.get("pmid"):
                    pmid_index.setdefault(str(hit["pmid"]), key)
                continue
            existing_key = next((k for k, v in merged.items() if v is existing), key)
            absorb(existing, hit, trace, existing_key)

    out = list(merged.values())
    for index, hit in enumerate(out, start=1):
        source_names = hit.get("sources") or [hit.get("source")]
        hit["source"] = "both" if len(source_names) > 1 else source_names[0]
        hit["rank"] = index
        hit["support"] = {
            "status": "external_only",
            "reason": SUPPORT_VOCABULARY["external_only"],
            "counts_as_support": False,
            "usable_as_evidence": False,
        }
        hit["method_support"] = "not_asserted"
        hit["anchor_status"] = "not_checked"
    return out


def external_search(project: str, query: Any, scope: str = "both",
                    page: Any = 1, page_size: Any = DEFAULT_EXTERNAL_PAGE_SIZE,
                    include_abstracts: bool = False,
                    single_cell_filter: bool = True) -> dict:
    if scope not in ("pubmed", "europepmc", "both"):
        raise ValueError("scope must be 'pubmed', 'europepmc' or 'both'")
    page_num, size = _page_bounds(page, page_size, MAX_EXTERNAL_PAGE_SIZE)
    raw_query = _validate_external_query(query)
    wanted = ["pubmed", "europepmc"] if scope == "both" else [scope]
    if "europepmc" in wanted and page_num > MAX_EPMC_PAGE:
        raise ValueError(
            f"page must be <= {MAX_EPMC_PAGE} when scope includes Europe PMC")
    sc_filter = bool(single_cell_filter)

    proj = Project(Path(project)).ensure()
    conn = proj.connect()
    try:
        source_results = []
        for source in wanted:
            effective = _effective_query(raw_query, sc_filter, source)
            if source == "pubmed":
                source_results.append(_pubmed_page(proj, conn, effective,
                                                   page_num, size, include_abstracts))
            else:
                source_results.append(_epmc_page(proj, conn, effective,
                                                 page_num, size, include_abstracts))
        hits = _merge_hits(source_results)
    finally:
        conn.close()

    statuses = [result.get("status") for result in source_results]
    ok_sources = [s for s in statuses if s == "ok"]
    if statuses and len(ok_sources) == len(statuses):
        top_status = "ok"
    elif ok_sources:
        top_status = "partial"
    elif any(s == "unreachable" for s in statuses):
        top_status = "unreachable"
    else:
        top_status = "failed"

    total_by_source = {result["source"]: result.get("count")
                       for result in source_results}
    has_more = any(result.get("has_more") for result in source_results)
    page_out_of_range = any(result.get("page_out_of_range")
                            for result in source_results)
    return {
        "status": top_status,
        "scope": scope,
        "query": raw_query,
        "single_cell_filter": sc_filter,
        "effective_query": {result["source"]: result.get("effective_query")
                            for result in source_results},
        "page": page_num,
        "page_size": size,
        "returned": len(hits),
        "total_by_source": total_by_source,
        "has_more": has_more,
        "page_out_of_range": page_out_of_range,
        "next_page": page_num + 1 if has_more else None,
        "sources": [{
            "source": result["source"],
            "status": result.get("status"),
            "count": result.get("count"),
            "error": result.get("error"),
            "failed_at_page": result.get("failed_at_page"),
            "metadata_status": result.get("metadata_status"),
            "traces": result.get("traces") or [],
            "has_more": result.get("has_more"),
            "page_out_of_range": bool(result.get("page_out_of_range", False)),
            "pages_fetched": result.get("pages_fetched"),
            "note": result.get("note"),
        } for result in source_results],
        "hits": hits,
        "support_vocabulary": SUPPORT_VOCABULARY,
        "notes": [
            "public-literature hits are pointers (external_only), not evidence and "
            "not verified method use; every hit has usable_as_evidence=false",
            "abstracts can omit the analysis section; anchor verification remains "
            "the caller's job",
            "a failed source is reported as unreachable/partial, never as an empty "
            "negative result",
            "paging is applied per source; the merged page can be shorter after "
            "cross-source deduplication, and has_more is true if any source has more",
            "request provenance is stored in the project literature_cache",
        ],
        "requested_utc": now(),
    }
