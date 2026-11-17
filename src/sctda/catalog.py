"""Method catalog.

Three layers are kept deliberately separate, because the design requires it:

  - EXECUTABLE  : the four adapters wired in ``r/adapters.R`` that have actually
                  run. This module is their single authoritative definition.
  - DISCOVERED  : methods extracted from the frozen literature corpus, filtered
                  to the authoritative 422-PMID scope list with retractions
                  hard-excluded. A discovered method never implies support.
  - REGISTRY    : ``method_registry`` owns the corpus index and the explicit
                  adapter-to-instance mapping. Catalog delegates to it instead
                  of re-scanning ``research/methods.jsonl`` or guessing matches.

``catalog_summary`` reports both layers separately and pages the discovered
layer by default, so one tool call cannot return the whole multi-megabyte
corpus index. A discovered method is only reported as backed by an adapter when
the registry's explicit mapping says so; fuzzy name matching, package
substrings and pattern lists are not used anywhere in this module
(AUCell != UCell, inferCNV != CopyKAT, FindMarkers(wilcox) != DESeq2).
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

# Discovered methods are paged: the full 422-scoped index is several megabytes.
# Callers that need the complete list (report/CSV generation, not tool output)
# call ``discovered_methods()`` without a limit.
DISCOVERED_PAGE_DEFAULT = 50
DISCOVERED_PAGE_MAX = 500
_REGISTRY_PAGE = 500

SCOPE_NOTE = (
    "The 422-PMID list is authoritative, retracted PMID 36450980 is hard-excluded, "
    "and only records with in_scope=true and method_class=computational are listed. "
    "Extraction is known to be incomplete: absence here is not evidence that a "
    "method does not exist in the literature."
)

# Executable adapters. `requires` drives the applicability check before any
# task is queued, so a missing input becomes `insufficient_input` rather than a
# fabricated result.
EXECUTABLE: dict[str, dict] = {
    "composition": {
        "method_id": "composition",
        "name": "per-sample cell composition of each annotated group",
        "package": None,
        "stage": "composition",
        "adapter": "r/adapters.R::task_composition",
        "status": "executable",
        "requires": {"annotation_column": True, "sample_column": False,
                     "condition_column": False},
        "provides": ["composition_by_sample.csv", "composition_overall.csv"],
        "statistical_notes": [
            "counts and proportions are descriptive at group and sample levels; overall proportions are literature-standard",
            "without a sample column only overall counts are reported; per-sample proportions provide supplementary cross-sample corroboration",
        ],
        "defaults": {},
        "params_schema": {},
        "literature_refs": [
            {"pmid": "37922910", "locator": "methods:L137:P20:S1",
             "verbatim": "Cluster frequencies were computed as a percentage of patient cells within each cluster divided by the total number of patient cells.",
             "note": "per-patient cluster frequency as a percentage of that patient's cells"},
            {"pmid": "33907320", "locator": "methods:L84:P10:S9",
             "verbatim": "To calculate the sample composition based on cell type, the number of cells for each cell type from each sample were counted.",
             "note": "per-sample cell-type counts, the count column this adapter reports"},
        ],
    },
    "de_vs_rest_wilcox": {
        "method_id": "de_vs_rest_wilcox",
        "name": "differential expression of one group vs the rest",
        "package": "Seurat::FindMarkers (wilcox)",
        "stage": "de",
        "adapter": "r/adapters.R::task_de_vs_rest_wilcox",
        "status": "executable",
        "requires": {"annotation_column": True, "sample_column": False,
                     "condition_column": False},
        "provides": ["de_cell_level.csv", "de_per_sample.csv", "de_sample_level.csv"],
        "statistical_notes": [
            "cell-level Wilcoxon is the primary literature-comparable result, with caveat: cells are not independent replicates",
            "sample-level mean log2FC with a t-based 95% interval is supplementary corroboration when >=3 samples support the group",
            "samples with <10 cells of the candidate are excluded from pseudobulk corroboration",
        ],
        "defaults": {"min_cells": 30, "logfc_threshold": 0.1, "min_pct": 0.1, "top_n": 200},
        "params_schema": {
            "min_cells": "integer >= 3",
            "logfc_threshold": "float",
            "min_pct": "float 0..1",
            "top_n": "integer",
        },
        "literature_refs": [
            {"pmid": "36517593", "locator": "methods:L314:P38:S2",
             "verbatim": "Differential expression between identified clusters was computed using a two-sided Wilcoxon rank-sum test as implemented in Seurat FindMarkers.",
             "note": "two-sided Wilcoxon rank-sum implemented in Seurat FindMarkers"},
            {"pmid": "40780212", "locator": "methods:L235:P20:S1",
             "verbatim": "Within-cluster differential expression analysis was performed using the FindMarkers() function in Seurat employing a Wilcoxon rank-sum test (Mann-Whitney U test) to identify differentially expressed genes between groups.",
             "note": "FindMarkers with Wilcoxon rank-sum to identify DE genes between groups"},
        ],
    },
    "signature_score_aucell": {
        "method_id": "signature_score_aucell",
        "name": "per-cell gene set activity (AUCell)",
        "package": "AUCell",
        "stage": "enrichment",
        "adapter": "r/adapters.R::task_signature_score_aucell",
        "status": "executable",
        "requires": {"annotation_column": True, "gene_sets": True},
        "provides": ["signature_scores_per_cell.csv", "signature_summary.csv"],
        "statistical_notes": [
            "AUCell scores are relative within a cell; they are not absolute activities",
            "gene sets with <5 measured genes are dropped and listed in the run metrics",
        ],
        "defaults": {"max_cells_written": 20000},
        "params_schema": {"max_cells_written": "integer"},
        "literature_refs": [
            {"pmid": "32997960", "locator": "methods:L141:P22:S1",
             "verbatim": "Single cell enrichment score was run using AUCell (version 1.6.0) in R (Aibar et al., 2017) on normalized expression values and Molecular Signatures Database C2 (version 5) signatures and custom signatures.",
             "note": "AUCell v1.6.0 on normalized values with MSigDB C2 + custom sets"},
        ],
    },
    "cnv_infercnv": {
        "method_id": "cnv_infercnv",
        "name": "large-scale copy number variation inference (inferCNV)",
        "package": "infercnv",
        "stage": "cnv",
        "adapter": "r/adapters.R::task_cnv_infercnv",
        "status": "executable",
        "requires": {"annotation_column": True, "reference_group": False},
        "provides": ["cnv_scores_per_cell.csv", "cnv_summary_by_group.csv"],
        "statistical_notes": [
            "inferCNV estimates genomic copy number variations relative to reference cells",
            "CopyKAT is a different algorithm and not an executable alternative: the R adapter requires infercnv and never substitutes CopyKAT",
            "requires a non-malignant reference group from the annotation (e.g. immune/stromal/normal)",
            "if no package is installed or no valid reference group can be found, returns insufficient_input",
        ],
        "defaults": {"window_size": 101, "min_cells": 10, "reference_group": None},
        "params_schema": {
            "reference_group": "string (name of annotated non-malignant/normal group, or comma-separated list)",
            "window_size": "integer (sliding window size along chromosome, default 101)",
            "min_cells": "integer >= 3 (minimum cells per group to analyze)",
        },
        "literature_refs": [
            {"pmid": "33495604", "locator": "methods:L146:P34:S1",
             "verbatim": "To infer CNAs from the scRNA-seq data we used the approach described in25, as implemented in the R code provided in https://github.com/broadinstitute/inferCNV with the default parameters.",
             "note": "inferCNV used for CNA inference with default parameters"},
            {"pmid": "33664492", "locator": "methods:L1243:P160:S1",
             "verbatim": "copy number alteration profiles generated from inferCNV v.0.99.7 (https://github.",
             "note": "copy number alteration profiles generated from inferCNV v0.99.7"},
        ],
    },
}


class CatalogError(RuntimeError):
    """Base error for the method catalog."""


class CatalogUnavailable(CatalogError):
    """The unified method registry is not importable or cannot answer."""


class CatalogContractError(CatalogError):
    """The registry returned a record that violates the agreed interface."""


def _registry() -> Any:
    """Lazy import: method_registry imports catalog.EXECUTABLE inside its calls."""
    try:
        from . import method_registry
    except Exception as exc:                                  # noqa: BLE001
        raise CatalogUnavailable(
            "unified method registry is unavailable: "
            f"{type(exc).__name__}: {exc}") from exc
    return method_registry


def _page_bounds(limit: int | None, offset: int | None) -> tuple[int, int, bool]:
    """Validate a tool-facing page request and cap it.

    ``None`` means the default page, not "everything": an accidental unbounded
    tool response is a defect, not a convenience.
    """
    offset = 0 if offset is None else offset
    try:
        offset = int(offset)
    except (TypeError, ValueError) as exc:
        raise CatalogError("offset must be an integer") from exc
    if offset < 0:
        raise CatalogError("offset must be >= 0")
    limit = DISCOVERED_PAGE_DEFAULT if limit is None else limit
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise CatalogError("limit must be an integer") from exc
    if limit < 1:
        raise CatalogError("limit must be >= 1")
    clamped = limit > DISCOVERED_PAGE_MAX
    return (DISCOVERED_PAGE_MAX if clamped else limit), offset, clamped


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    return sorted({str(v) for v in value if v not in (None, "")})


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _method_entry(record: dict) -> dict:
    """Normalise one registry method record into the catalog's public shape.

    The registry is the owner of aggregation; this function only names fields
    and refuses a record that cannot be traced to a canonical method name.
    """
    if not isinstance(record, dict):
        raise CatalogContractError(f"registry method record is not an object: {record!r}")
    name = record.get("canonical_name") or record.get("name")
    if not name:
        raise CatalogContractError(
            f"registry method record has no canonical_name: {record!r}"[:400])
    pmids = _str_list(record.get("pmids"))
    examples: list[dict] = []
    raw_examples = record.get("examples") or record.get("instances") or []
    if not isinstance(raw_examples, list):
        raw_examples = []
    for ex in raw_examples[:3]:
        if not isinstance(ex, dict):
            continue
        examples.append({
            "pmid": ex.get("pmid"),
            "locator": ex.get("locator"),
            "operation": ex.get("operation"),
            "canonical_name": ex.get("canonical_name") or name,
            "verbatim": ex.get("verbatim"),
            "has_verbatim": ex.get("has_verbatim"),
        })
    roles = record.get("rerunnable_roles")
    if roles is None and record.get("rerunnable_role") is not None:
        roles = [record.get("rerunnable_role")]
    if isinstance(roles, dict):
        role_counts = {str(k): _int(v) for k, v in roles.items()}
    else:
        role_counts = {r: 1 for r in _str_list(roles)}
    registry_id = record.get("id") or record.get("registry_id") or record.get("method_id")
    in_scope = record.get("in_scope")
    if in_scope is not None:
        in_scope = bool(in_scope)
    match = record.get("match")
    match = deepcopy(match) if isinstance(match, dict) else {}
    matched_fields = match.get("fields")
    if not isinstance(matched_fields, list):
        matched_fields = []
    return {
        "id": registry_id,
        "registry_id": registry_id,
        "kind": record.get("kind") or "literature_method",
        "match": match,
        "matched_fields": [str(f) for f in matched_fields],
        "canonical_name": str(name),
        "aliases": _str_list(record.get("aliases")),
        "n_instances": _int(record.get("n_instances")),
        "n_papers": _int(record.get("n_papers"), len(pmids)),
        "evidence_count": _int(record.get("evidence_count"), _int(record.get("n_instances"))),
        "pmids": pmids,
        "stages": _str_list(record.get("stages") or record.get("pipeline_stages")),
        "packages": _str_list(record.get("packages")),
        "operations": _str_list(record.get("operations")),
        "purposes": _str_list(record.get("purposes")),
        "in_scope": in_scope,
        "method_class": record.get("method_class"),
        "rerunnable_roles": role_counts,
        "upstream_background": bool(record.get("upstream_background", False)),
        "completeness": deepcopy(record.get("completeness") or {}),
        "registry_support": deepcopy(record.get("support") or {}),
        "examples": examples,
    }


def _collect_all(query: str | None = None,
                 stages: list[str] | None = None) -> list[dict]:
    """Read every matching literature-method page. For file artifacts, not tools."""
    registry = _registry()
    out: list[dict] = []
    offset = 0
    for _ in range(10_000):                                   # page-loop guard
        page = registry.registry_search(query=query, stages=stages,
                                        limit=_REGISTRY_PAGE, offset=offset,
                                        kinds="literature_method")
        if not isinstance(page, dict):
            raise CatalogContractError("registry_search did not return an object")
        batch = page.get("methods") or []
        out.extend(batch)
        total = page.get("total")
        has_more = page.get("has_more")
        if has_more is False:
            if total is not None and len(out) != _int(total):
                raise CatalogContractError(
                    f"registry pagination returned {len(out)} of {total} methods")
            break
        if total is not None and len(out) >= _int(total):
            break
        if not batch:
            raise CatalogContractError(
                "registry_search reported more results but returned an empty page")
        offset += len(batch)
    else:
        raise CatalogContractError("registry_search did not terminate")
    return out


def discovered_methods(limit: int | None = None, offset: int = 0) -> list[dict]:
    """Aggregate literature method instances by canonical name.

    Sourced from the unified registry, so the 422-PMID scope and the retraction
    exclusion are applied once, not re-implemented here. With ``limit=None``
    this pages through the entire registry and is intended for file artifacts;
    tool calls should use :func:`catalog_summary`, whose default is bounded.
    """
    offset = max(0, int(offset))
    if limit is None:
        records = _collect_all()[offset:]
    else:
        need = int(limit)
        if need < 0:
            raise CatalogError("limit must be >= 0")
        records = []
        cursor = offset
        while need > 0:
            page = _registry().registry_search(
                limit=min(_REGISTRY_PAGE, need), offset=cursor,
                kinds="literature_method")
            batch = page.get("methods") or []
            records.extend(batch)
            need -= len(batch)
            cursor += len(batch)
            if not batch or page.get("has_more") is False:
                break
    return [_method_entry(r) for r in records]


def supported_by_detail(canonical_name: str) -> list[dict]:
    """Explicit registry mapping from one canonical name to wired adapters.

    Only exact normalized name/alias equality counts. A toolkit record such as
    ``Seurat`` maps at quality ``toolkit`` ("one operation is wired"), never as
    full support; unrelated algorithms are not matched by substring.
    """
    name = (canonical_name or "").strip()
    if not name:
        return []
    registry = _registry()
    lookup = getattr(registry, "registry_adapters_for", None)
    if lookup is None:
        raise CatalogContractError("method_registry has no registry_adapters_for()")
    hits: list[dict] = []
    for hit in lookup(name) or []:
        if not isinstance(hit, dict):
            raise CatalogContractError(
                f"registry_adapters_for returned a non-object: {hit!r}")
        method_id = hit.get("method_id")
        # EXECUTABLE is authoritative: a mapping to an id this build does not
        # ship must not be presented as support.
        if method_id not in EXECUTABLE:
            continue
        quality = hit.get("quality")
        if quality not in ("exact", "operation", "toolkit"):
            raise CatalogContractError(
                f"registry_adapters_for gave unknown quality {quality!r} "
                f"for {canonical_name!r}")
        wired = (hit.get("wired_operation")
                 or EXECUTABLE[method_id].get("package")
                 or EXECUTABLE[method_id]["name"])
        hits.append({"method_id": method_id, "quality": quality,
                     "wired_operation": str(wired)})
    hits.sort(key=lambda h: (h["method_id"], h["quality"]))
    return hits


def supported_by(canonical_name: str) -> list[str]:
    return [h["method_id"] for h in supported_by_detail(canonical_name)]


def _group_instances(instances: list[dict], limit: int) -> tuple[list[dict], int]:
    groups: dict[str, dict] = {}
    for inst in instances:
        if not isinstance(inst, dict):
            continue
        pmid = inst.get("pmid")
        if pmid in (None, ""):
            continue
        pmid = str(pmid)
        rec = groups.setdefault(pmid, {"pmid": pmid, "n_instances": 0, "examples": []})
        rec["n_instances"] += 1
        if len(rec["examples"]) < 2:
            rec["examples"].append({
                "locator": inst.get("locator"),
                "operation": inst.get("operation"),
                "canonical_name": inst.get("canonical_name"),
                "verbatim": inst.get("verbatim"),
            })
    ordered = sorted(groups.values(), key=lambda r: (-r["n_instances"], r["pmid"]))
    return ordered[:limit], len(ordered)


DETAIL_INSTANCE_MAX = 100


def literature_backing(method_id: str, limit: int = 8,
                       detail: bool = False) -> list[dict] | dict:
    """Corpus instances that the registry explicitly maps to this adapter.

    The default return keeps the original list shape (one entry per PMID).
    ``detail=True`` returns the registry's full verdict, including
    ``found=False`` plus a reason when there is no backing. Nothing is widened
    from related-but-different methods: the registry's ``instances`` are the
    only evidence returned, and ``excluded_related`` is passed through for
    transparency.
    """
    limit = max(1, int(limit))
    if method_id not in EXECUTABLE:
        verdict = {
            "method_id": method_id, "found": False, "quality": "not_found",
            "instances": [], "n_instances": 0, "n_papers": 0,
            "excluded_related": [], "mapped_canonical_names": [],
            "reason": f"{method_id!r} is not one of the four executable adapters",
        }
        return verdict if detail else []

    registry = _registry()
    lookup = getattr(registry, "registry_backing", None)
    if lookup is None:
        raise CatalogContractError("method_registry has no registry_backing()")
    raw = lookup(method_id)
    if not isinstance(raw, dict):
        raise CatalogContractError(
            f"registry_backing({method_id!r}) did not return an object")
    instances = [i for i in (raw.get("instances") or []) if isinstance(i, dict)]
    groups, total_groups = _group_instances(instances, limit)
    found = bool(raw.get("found")) and bool(groups)
    detail_instances = instances[:DETAIL_INSTANCE_MAX]
    verdict = {
        "method_id": method_id,
        "found": found,
        "quality": raw.get("quality") or ("exact" if found else "not_found"),
        "n_instances": _int(raw.get("n_instances"), len(instances)),
        "n_papers": _int(raw.get("n_papers"), total_groups),
        "mapped_canonical_names": _str_list(raw.get("mapped_canonical_names")),
        "excluded_related": deepcopy(raw.get("excluded_related") or []),
        "reason": raw.get("reason"),
        "groups": groups,
        "n_groups": total_groups,
        "instances": detail_instances if detail else [],
        "instances_truncated": len(instances) > len(detail_instances),
        "returned_instances": len(instances),
        "groups_truncated": total_groups > len(groups),
    }
    # A registry verdict of `not_found`/`local_adapter` must not be upgraded by
    # a non-empty instance list; that would be the exact overstatement this
    # module exists to prevent.
    if verdict["quality"] in ("not_found", "local_adapter") and found:
        found = verdict["found"] = False
    if detail:
        return verdict
    return groups if found else []


def get_method(method_id: str) -> dict | None:
    return EXECUTABLE.get(method_id)


def _executable_rows() -> list[dict]:
    rows: list[dict] = []
    for method_id in sorted(EXECUTABLE):
        row = deepcopy(EXECUTABLE[method_id])
        try:
            verdict = literature_backing(method_id, detail=True)
            # The catalog is a listing, not the anchor-inspection tool: keep
            # counts/groups/quality/reason, but do not embed up to 100 verbatim
            # anchors per adapter on every call. Full anchors stay available via
            # literature_backing(detail=True) and method_registry.registry_get().
            had_instances = bool(verdict.get("instances"))
            verdict["instances"] = []
            verdict["instances_omitted"] = had_instances
            verdict["instances_omitted_note"] = (
                "catalog listing omits full anchors; use literature_backing("
                "detail=True) or registry_get(id) for the full evidence")
            row["literature_backing"] = verdict.get("groups") or []
            row["literature_backing_detail"] = verdict
        except (CatalogError, OSError) as exc:
            row["literature_backing"] = []
            row["literature_backing_detail"] = {
                "method_id": method_id, "found": False,
                "quality": "unavailable", "reason": str(exc),
                "instances": [], "groups": [],
                "instances_omitted": False,
            }
            row["backing_status"] = "unavailable"
        rows.append(row)
    return rows


def catalog_summary(executable_only: bool = False,
                    limit: int | None = None,
                    offset: int | None = 0,
                    query: str | None = None,
                    stage: str | list[str] | None = None) -> dict:
    """Two-layer catalog with a bounded, pageable discovered section.

    The executable layer is always at most four entries. The discovered layer
    is a page (default 50, hard cap 500) with explicit ``total``/``has_more``;
    ``limit=None`` does not mean "return everything". Full dumps are for file
    artifacts via :func:`discovered_methods`.
    """
    if not executable_only:
        page_limit, page_offset, clamped = _page_bounds(limit, offset)
    else:
        page_limit, page_offset, clamped = 0, 0, False

    out: dict = {
        "status": "ok",
        "executable": _executable_rows(),
        "n_executable": len(EXECUTABLE),
        "authority": ("EXECUTABLE is the only authoritative adapter list; a method is "
                      "executable only if r/adapters.R wires it and it has run"),
        "scope_note": SCOPE_NOTE,
    }
    if executable_only:
        out["note"] = ("executable only: no literature page was requested; "
                       "discovered methods are available with executable_only=False")
        return out

    stages = None
    if isinstance(stage, str):
        stages = [stage] if stage.strip() else None
    elif isinstance(stage, (list, tuple)):
        stages = [str(s) for s in stage if str(s).strip()]
    q = (query or "").strip() or None

    try:
        page = _registry().registry_search(query=q, stages=stages,
                                           limit=page_limit, offset=page_offset,
                                           kinds="literature_method")
    except (CatalogError, OSError) as exc:
        out.update({
            "status": "unavailable",
            "error": str(exc),
            "discovered": [],
            "n_discovered": 0,
            "discovered_total": None,
            "page": {"limit": page_limit, "offset": page_offset, "returned": 0,
                     "limit_clamped": clamped, "has_more": None,
                     "next_offset": None},
            "truncated": None,
            "note": "registry unavailable; no discovered methods were read. "
                    "This is not an empty corpus.",
        })
        return out

    if not isinstance(page, dict):
        raise CatalogContractError("registry_search did not return an object")
    rows: list[dict] = []
    for record in page.get("methods") or []:
        entry = _method_entry(record)
        detail = supported_by_detail(entry["canonical_name"])
        entry["supported_detail"] = detail
        entry["supported_by"] = [h["method_id"] for h in detail]
        rows.append(entry)

    total = page.get("total")
    if total is None:
        total = page_offset + len(rows)
    total = _int(total)
    returned = len(rows)
    has_more = page.get("has_more")
    if has_more is None:
        has_more = page_offset + returned < total
    next_offset = page.get("next_offset")
    if next_offset is None and has_more:
        next_offset = page_offset + returned

    try:
        registry_stats = _registry().registry_stats()
    except (CatalogError, OSError):
        registry_stats = {}
    if not isinstance(registry_stats, dict):
        registry_stats = {}

    scope_meta = page.get("scope")
    scope_meta = deepcopy(scope_meta) if isinstance(scope_meta, dict) else {}
    corpus_meta = page.get("corpus")
    corpus_meta = deepcopy(corpus_meta) if isinstance(corpus_meta, dict) else {}
    stats_scope = (registry_stats.get("scope") or {})
    if not isinstance(stats_scope, dict):
        stats_scope = {}
    stats_counts = registry_stats.get("counts") or {}
    if not isinstance(stats_counts, dict):
        stats_counts = {}
    filtered = bool(q or stages)
    global_in_scope = stats_counts.get("n_methods_literature")
    in_scope_count = (global_in_scope if filtered and global_in_scope is not None
                      else total)
    out.update({
        "discovered": rows,
        "n_discovered": total,          # legacy key: total, not page length
        "discovered_total": total,
        "discovered_returned": returned,
        "filtered": filtered,
        "n_discovered_in_scope": in_scope_count,
        "page": {
            "limit": page_limit,
            "offset": page_offset,
            "returned": returned,
            "total": total,
            "has_more": bool(has_more),
            "next_offset": next_offset if has_more else None,
            "limit_clamped": clamped,
        },
        "truncated": bool(has_more),
        "limit": page_limit,
        "offset": page_offset,
        "has_more": bool(has_more),
        "next_offset": next_offset if has_more else None,
        "query": q,
        "stages": stages,
        "scope": {
            "n_included_pmids": scope_meta.get("n_included_pmids"),
            "n_effective_pmids": scope_meta.get("n_effective_pmids"),
            "retracted_pmids": scope_meta.get("retracted_pmids") or [],
            "pmid_list_path": stats_scope.get("pmid_list_path"),
            "pmid_list_sha256": stats_scope.get("pmid_list_sha256"),
            "pmid_list_matches_known": stats_scope.get("pmid_list_matches_known"),
            "corpus_sha256": corpus_meta.get("sha256"),
            "record_rule": scope_meta.get("record_rule"),
            "corpus_rule": scope_meta.get("corpus_rule"),
            "incomplete": True,
        },
        "registry": {
            "scope": scope_meta,
            "corpus": corpus_meta,
            "stats": deepcopy(registry_stats),
            "notes": deepcopy(page.get("notes") or []),
        },
        "incomplete": bool(page.get("incomplete", True)),
        "note": ("discovered = found in the frozen literature corpus and 422-scoped "
                 "(may be unsupported); executable = has a wired adapter. The two "
                 "layers are never merged. The discovered list is paged; use "
                 "limit/offset for more, and each row's id/registry_id addresses the "
                 "full record (examples here are capped at three). Extraction is "
                 "incomplete; absence is not evidence that a method does not exist in "
                 "the literature."),
    })
    return out


def discovered_instances(limit: int | None = None, offset: int = 0) -> list[dict]:
    """Raw 422-scoped method instances (not aggregated).

    Uses the registry's evidence view. If the registry exposes only example
    records, this is a partial view, not a claim of completeness.
    """
    registry = _registry()
    loader = getattr(registry, "load_registry", None)
    if loader is None:
        raise CatalogContractError("method_registry has no load_registry()")
    loaded = loader(include_evidence=True)
    records = loaded.get("methods") if isinstance(loaded, dict) else loaded
    out: list[dict] = []
    for record in records or []:
        if record.get("kind") == "executable_adapter":
            continue
        for inst in (record.get("instances") or record.get("evidence")
                     or record.get("examples") or []):
            if isinstance(inst, dict):
                out.append(deepcopy(inst))
    start = max(0, int(offset))
    if limit is None:
        return out[start:]
    return out[start:start + max(0, int(limit))]
