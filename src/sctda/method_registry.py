"""Unified method registry: literature methods + wired adapters in one list.

What this module is
-------------------
``research/corpus/methods.jsonl`` (built by ``tools/build_corpus.py`` from the
extraction shards) is an open-vocabulary extraction: it contains method
instances that are only mentioned, methods used only for QC, upstream
quantification steps, instruments, and so on. This registry applies the *record
layer* of the project scope and aggregates the survivors by canonical name:

  record kept  iff  pmid is in included_pmids_405.txt
                    (current upstream inclusion set; the 422 list is a
                    superseded snapshot kept for historical reproduction)
                    and the pmid is not retracted (DEFAULT_RETRACTIONS)
                    and record["in_scope"] is True
                    and record["method_class"] == "computational"

`rerunnable_role` is preserved, not used as a filter. In particular
``upstream_background`` instances stay in the registry and remain visible as
``rerunnable_roles`` / ``upstream_background``; dropping them would erase the
distinction the extraction recorded.

Deliberate non-features
-----------------------
* No frequency threshold. Every aggregated canonical name is listed, including
  the single-paper long tail. Ordering is by match quality, then a canonical-name
  hit, then a whole-query exact-name hit, then normalized name; never by paper
  count. Each result carries ``match`` with ``kind``, ``terms``, ``fields``,
  ``name_matched`` and ``name_exact``.
* No claim of exhaustiveness. The extraction is known to be incomplete; every
  response carries ``incomplete: true``.
* No invented installation state. Adapters are read from
  ``catalog.EXECUTABLE``; this module never probes or asserts that a package is
  installed. ``support.status`` is only ``executable`` (wired in the catalog) or
  ``corpus_only`` (literature only).
* No copied adapter definitions. ``EXECUTABLE`` is imported lazily at call time.

Normalization is conservative on purpose: NFKC, case-folding, and collapsing
runs of non-word characters. Tokens and version strings are not removed, so
AUCell != UCell, inferCNV != CopyKAT != infercnvpy, Seurat FindMarkers !=
Seurat FindAllMarkers, and "Wilcoxon rank-sum test" merges only with its own
spacing/case variants.

Public API
----------
    registry_search(query=None, stages=None, limit=20, offset=0, kinds=None) -> dict
    registry_get(registry_id) -> dict | None
    registry_stats() -> dict
    registry_backing(method_id, limit=None) -> dict
    registry_adapters_for(canonical_name) -> list[dict]
    load_registry(include_evidence=False) -> dict
    build_registry(methods_path=None, pmid_list_path=None,
                   retractions=None, executable=None) -> MethodRegistry
    resolve_pmid_list(pmid_list_path=None) -> Path
    read_pmid_list(pmid_list_path=None) -> dict
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent

# The built corpus, produced by tools/build_corpus.py, plus its provenance
# manifest. The extraction inputs (research/methods.jsonl and the shard files)
# stay on disk for audit but are not read at runtime.
CORPUS_DIR = ROOT / "research" / "corpus"
METHODS_PATH = CORPUS_DIR / "methods.jsonl"
CORPUS_MANIFEST_PATH = CORPUS_DIR / "manifest.json"
CORPUS_MANIFEST_SCHEMA = "sctda.corpus_manifest/1"
DEFAULT_LIMIT = 20
MAX_LIMIT = 500
SCHEMA = "sctda.method_registry/1"
SEARCH_SCHEMA = "sctda.method_registry.search/1"
STATS_SCHEMA = "sctda.method_registry.stats/1"
LOAD_SCHEMA = "sctda.method_registry.load/1"
BACKING_SCHEMA = "sctda.method_registry.backing/1"

# Explicit adapter -> corpus mapping. Keys are the registry's normalized
# canonical names; they are exact equality matches, never substrings. The
# adapter definitions themselves live only in catalog.EXECUTABLE.
#
# `quality`:
#   exact      the corpus entry is the wired operation/tool itself
#   operation  a specific operation wired by the adapter, or the exact test the
#              adapter runs (caveat recorded when the surrounding frame differs)
# Optional `stages` is an instance-level predicate: only extracted instances
# whose pipeline_stage is in that tuple count as adapter backing. A name can map
# at the method level while some of its instances are non-backing (e.g. Wilcoxon
# used as a metric rather than for DE).
EXECUTABLE_CORPUS_MAP: dict[str, tuple[dict[str, Any], ...]] = {
    "composition": (),
    "de_vs_rest_wilcox": (
        {"key": "seurat findmarkers wilcoxon", "quality": "operation",
         "stages": ("de",),
         "note": "Seurat FindMarkers run with the Wilcoxon test; only DE-stage instances count"},
        {"key": "seurat findmarkers wilcoxon test", "quality": "operation",
         "stages": ("de",),
         "note": "same wired operation with the test named explicitly; only DE-stage instances count"},
        {"key": "wilcoxon rank sum test", "quality": "operation",
         "stages": ("de",),
         "note": "the statistical test the adapter runs; only DE-stage instances count, "
                 "and these corpus instances may use another toolkit and a different "
                 "grouping frame"},
    ),
    "signature_score_aucell": (
        {"key": "aucell", "quality": "exact",
         "note": "the adapter is AUCell scoring"},
        {"key": "aucell gene set scoring", "quality": "operation",
         "note": "AUCell used for gene-set scoring"},
        {"key": "aucell signature scoring", "quality": "operation",
         "note": "AUCell signature scoring"},
        {"key": "aucell signature scores", "quality": "operation",
         "note": "AUCell signature scores"},
        {"key": "aucell scoring", "quality": "operation",
         "note": "AUCell scoring"},
        {"key": "aucell tumor reactivity score", "quality": "operation",
         "note": "AUCell used for a signature score"},
        {"key": "aucell stemness scoring", "quality": "operation",
         "note": "AUCell used for a signature score"},
        {"key": "aucell regulon activity", "quality": "operation",
         "note": "AUCell regulon activity"},
        {"key": "aucell regulon scoring", "quality": "operation",
         "note": "AUCell regulon scoring"},
        {"key": "aucell eregulon scoring", "quality": "operation",
         "note": "AUCell eRegulon scoring"},
    ),
    "cnv_infercnv": (
        {"key": "infercnv", "quality": "exact",
         "note": "the adapter's CNV inference is inferCNV"},
        {"key": "infercnv reference design", "quality": "operation",
         "note": "inferCNV reference configuration"},
        {"key": "infercnv slide window filtering", "quality": "operation",
         "note": "inferCNV sliding-window configuration"},
        {"key": "infercnv i6 hmm", "quality": "operation",
         "note": "inferCNV HMM model i6"},
        {"key": "infercnv random trees permutation test", "quality": "operation",
         "note": "inferCNV permutation-based configuration"},
    ),
}

# Methods that look adjacent but are deliberately NOT backing for an adapter.
# These are checked by exact normalized name only; each present entry is
# reported in registry_backing(...)["excluded_related"] with the reason.
RELATED_BUT_NOT_BACKING: dict[str, tuple[tuple[str, str], ...]] = {
    "composition": (),
    "de_vs_rest_wilcox": (
        ("seurat findmarkers", "FindMarkers without a stated test; the wired adapter is specifically Wilcoxon"),
        ("findmarkers wilcoxon test", "does not name the Seurat implementation; toolkit unverified"),
        ("seurat findallmarkers", "all-markers DE, not one-group-vs-rest"),
        ("seurat findallmarkers wilcoxon test", "all-markers DE, not one-group-vs-rest"),
        ("seurat findmarkers logistic regression", "logistic-regression test, not Wilcoxon"),
        ("wilcoxon signed rank test", "paired signed-rank test, not rank-sum"),
        ("deseq2", "bulk/pseudobulk count model, not a Wilcoxon rank-sum test"),
        ("mast", "different DE model (hurdle), not Wilcoxon"),
        ("edger", "bulk count model, not Wilcoxon"),
        ("limma", "linear-model framework, not Wilcoxon"),
        ("bimod", "Seurat bimod likelihood-ratio test, not Wilcoxon"),
    ),
    "signature_score_aucell": (
        ("ucell", "different scoring algorithm; AUCell is not UCell"),
        ("aucell explorethresholds", "threshold selection on AUCell scores, not scoring"),
        ("spatial spot aucell enrichment", "spatial-spot variant, outside the per-cell adapter"),
        ("gsva", "different gene-set scoring framework"),
        ("ssgsea", "different gene-set scoring method"),
        ("gsea", "rank-based enrichment, not per-cell scoring"),
        ("fgsea", "rank-based enrichment, not per-cell scoring"),
        ("seurat addmodulescore", "Seurat module score, not AUCell"),
        ("gsva and aucell", "combined naming; assigns one support to two methods"),
        ("aucell and ssgsea pathway scoring", "combined naming; assigns one support to two methods"),
    ),
    "cnv_infercnv": (
        ("copykat", "different CNV caller; inferCNV is not CopyKAT"),
        ("copykat malignant cell assignment", "CopyKAT-specific assignment"),
        ("infercnvpy", "Python reimplementation, not the R infercnv adapter"),
        ("infercnv and copykat", "combined naming; assigns one support to two methods"),
        ("copykat infercnv", "combined naming; assigns one support to two methods"),
        ("infercnv style cnv inference", "style imitation without a named package"),
        ("infercnv subclusters", "downstream clustering of CNV profiles, not CNV inference"),
        ("infercnv malignant assignment", "downstream malignant assignment, not CNV inference"),
        ("infercnv per cell cnv classification", "downstream classification, not CNV inference"),
    ),
}

PMID_LIST_ENV = "SCOPE_INCLUDED_PMIDS"

# Public fields of every registry record. Internal search/evidence fields start
# with an underscore and are never serialized.
_PUBLIC_FIELDS = (
    "id", "kind", "name", "canonical_name", "normalized_key", "aliases",
    "stages", "packages", "operations", "purposes", "rerunnable_roles",
    "upstream_background", "in_scope", "method_class", "scope", "corpus",
    "completeness", "support", "adapters", "name_source",
)


# --------------------------------------------------------------------- helpers
def _normalize_key(value: Any) -> str:
    """Conservative key: NFKC, casefold, collapse non-word runs, strip.

    Unicode letters/digits are kept (``\\W`` is Unicode-aware), so non-Latin
    method names do not collapse to the empty string. Hyphens, slashes,
    parentheses and punctuation become spaces; nothing else is removed.
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    # Keep C++ distinct from C instead of reducing both to "c".
    text = text.replace("+", " plus ")
    text = re.sub(r"[\W_]+", " ", text)
    return " ".join(text.split())


def _slug(value: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:60].strip("-") or "method"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _corpus_manifest_entry(
    methods_path: Path, corpus_sha: str
) -> dict[str, Any]:
    """Relate the methods file actually read to ``corpus/manifest.json``.

    Provenance only: a missing, unreadable or non-matching manifest does not
    block the registry, it is reported. An explicit alternate ``methods_path``
    (tests, historical snapshots) is only checked against a manifest sitting in
    its own directory, so a temp corpus never inherits the shipped hashes.
    """
    out: dict[str, Any] = {
        "status": "absent",
        "path": str(methods_path.parent / CORPUS_MANIFEST_PATH.name),
        "schema": None,
        "generated_utc": None,
        "file_entry": None,
        "sha256_match": None,
        "patterns": None,
        "error": None,
    }
    path = out["path"]
    if not Path(path).is_file():
        return out
    try:
        data = json.loads(Path(path).read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError) as exc:
        out["status"] = "unreadable"
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    if not isinstance(data, dict):
        out["status"] = "unreadable"
        out["error"] = f"manifest is {type(data).__name__}, not an object"
        return out
    files = data.get("files")
    entry = files.get(methods_path.name) if isinstance(files, dict) else None
    patterns = data.get("patterns")
    out.update({
        "status": "ok",
        "schema": data.get("schema"),
        "generated_utc": data.get("generated_utc"),
        "file_entry": dict(entry) if isinstance(entry, dict) else None,
        "patterns": dict(patterns) if isinstance(patterns, dict) else None,
    })
    if out["schema"] != CORPUS_MANIFEST_SCHEMA:
        out["status"] = "schema_mismatch"
    if isinstance(entry, dict):
        out["sha256_match"] = entry.get("sha256") == corpus_sha
        if not out["sha256_match"]:
            warnings_note = (
                "the methods file read is not the manifest snapshot "
                f"(manifest {entry.get('sha256')}, actual {corpus_sha})"
            )
            out["warning"] = warnings_note
    return out


def _pmid_sort_key(pmid: str) -> tuple[int, str]:
    return (len(pmid), pmid)


def _pmid_list(pmids: Iterable[str]) -> list[str]:
    return sorted({str(p) for p in pmids if p}, key=_pmid_sort_key)


def _flatten_purposes(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if isinstance(v, str) and v.strip()]
    return []


def _read_jsonl(path: Path) -> tuple[list[dict], int, int]:
    """Return (records, physical_line_count, parse_error_count)."""
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    records: list[dict] = []
    errors = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            errors += 1
            continue
        if isinstance(rec, dict):
            records.append(rec)
        else:
            errors += 1
    return records, len(lines), errors


def resolve_pmid_list(pmid_list_path: str | os.PathLike | None = None) -> Path:
    """Resolve the authoritative PMID inclusion list to a readable file.

    Order: an explicit ``pmid_list_path``, then ``SCOPE_INCLUDED_PMIDS``, then
    the current inclusion-set defaults. A superseded snapshot is never a
    fallback: a missing or unreadable configuration raises
    ``FileNotFoundError`` instead of answering from an obsolete scope.
    """
    if pmid_list_path is not None:
        path = Path(pmid_list_path)
        if not path.is_file():
            raise FileNotFoundError(f"included PMID list not found: {path}")
        return path

    env = os.environ.get(PMID_LIST_ENV)
    if env:
        path = Path(env)
        if not path.is_file():
            raise FileNotFoundError(
                f"{PMID_LIST_ENV} points at a missing included PMID list: {path}"
            )
        return path

    from scope_knowledge import DEFAULT_PMID_LIST_CANDIDATES

    for candidate in DEFAULT_PMID_LIST_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "included PMID list not found; set "
        f"{PMID_LIST_ENV} or pass pmid_list_path. Tried: "
        + ", ".join(str(c) for c in DEFAULT_PMID_LIST_CANDIDATES)
    )


def read_pmid_list(
    pmid_list_path: str | os.PathLike | None = None,
) -> dict[str, Any]:
    """Read the configured PMID list with the registry's exact rules.

    This is the shared scope source for ``MethodRegistry`` and
    ``method_sources.code_sources``: ordered, de-duplicated numeric PMIDs,
    ``#``/blank lines ignored, and a non-numeric line raising ``ValueError``,
    plus the file SHA-256 and its known-snapshot classification. Callers must
    treat a raised error as a hard failure, never as permission to answer
    without scope. An empty file is returned as zero PMIDs (the registry then
    excludes every record); ``code_sources`` rejects it explicitly instead of
    answering ``out_of_scope`` for every paper.
    """
    path = resolve_pmid_list(pmid_list_path)
    raw = path.read_bytes()
    sha = _sha256(raw)
    included: list[str] = []
    seen: set[str] = set()
    duplicate_pmids: list[str] = []
    for line_number, line in enumerate(
        raw.decode("utf-8", errors="replace").splitlines(), 1
    ):
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        if not value.isdigit():
            # Deliberately no line text: this helper is also reachable with a
            # caller-supplied path, and an error must not echo file contents.
            raise ValueError(
                f"{path}:{line_number}: non-numeric PMID line"
            )
        if value in seen:
            duplicate_pmids.append(value)
            continue
        seen.add(value)
        included.append(value)

    from scope_knowledge import KNOWN_PMID_LIST_SHA256, KNOWN_PMID_LIST_SNAPSHOTS

    known_snapshot = KNOWN_PMID_LIST_SNAPSHOTS.get(sha)
    return {
        "path": str(path),
        "sha256": sha,
        "sha256_16": sha[:16],
        "known_sha256": KNOWN_PMID_LIST_SHA256,
        "known_snapshot": (
            dict(known_snapshot) if known_snapshot is not None else None
        ),
        "matches_known_snapshot": sha == KNOWN_PMID_LIST_SHA256,
        "pmids": included,
        "pmid_set": frozenset(included),
        "n_pmids": len(included),
        "n_duplicates": len(duplicate_pmids),
        "duplicate_pmids": duplicate_pmids,
    }


def _retraction_registry(extra: Iterable[Any] | None):
    from scope_knowledge import RetractionRegistry

    return RetractionRegistry(extra=extra or ())


def _load_executable(override: Mapping[str, Mapping] | None) -> dict:
    if override is not None:
        return dict(override)
    from .catalog import EXECUTABLE

    return EXECUTABLE


def _evidence_for_row(entry: dict, row: Mapping) -> list[dict]:
    """Instances of an aggregated entry that satisfy a mapping row predicate."""
    stages = tuple(row.get("stages") or ())
    evidence = entry.get("_evidence") or []
    if not stages:
        return list(evidence)
    return [e for e in evidence if e.get("pipeline_stage") in stages]


# --------------------------------------------------------------------- registry
class MethodRegistry:
    """Aggregated method list built from one corpus snapshot + scope inputs."""

    def __init__(
        self,
        methods_path: str | os.PathLike | None = None,
        pmid_list_path: str | os.PathLike | None = None,
        retractions: Iterable[Any] | None = None,
        executable: Mapping[str, Mapping] | None = None,
    ) -> None:
        self.methods_path = Path(methods_path) if methods_path is not None else METHODS_PATH
        if not self.methods_path.is_file():
            raise FileNotFoundError(f"methods corpus not found: {self.methods_path}")
        self.pmid_list_path = resolve_pmid_list(pmid_list_path)
        self._executable_override = dict(executable) if executable is not None else None
        self._retractions = _retraction_registry(retractions)
        self._lock = threading.Lock()

        self._entries: dict[str, dict] = {}
        self._by_key: dict[str, dict] = {}
        self._stage_vocabulary: list[str] = []
        self._corpus: dict[str, Any] = {}
        self._scope: dict[str, Any] = {}
        self._excluded: dict[str, dict[str, Any]] = {}
        self._counts: dict[str, Any] = {}
        self._notes: list[str] = []
        self._build()

    # ------------------------------------------------------------------ build
    def _executable(self) -> dict:
        return _load_executable(self._executable_override)

    def _build(self) -> None:
        raw = self.methods_path.read_bytes()
        corpus_sha = _sha256(raw)
        records, n_lines, parse_errors = _read_jsonl(self.methods_path)

        scope_list = read_pmid_list(self.pmid_list_path)
        list_sha = scope_list["sha256"]
        included_set = scope_list["pmid_set"]

        retracted = set(self._retractions.pmids)
        effective = included_set - retracted

        groups: dict[str, dict] = {}
        excluded_counts: Counter[str] = Counter()
        excluded_pmids: dict[str, set[str]] = {}
        n_records_in_scope = 0

        for rec in records:
            pmid = str(rec.get("pmid") or "").strip()
            if not pmid:
                reason = "missing_pmid"
            elif pmid in retracted:
                reason = "retracted"
            elif pmid not in included_set:
                reason = "pmid_not_in_included_list"
            elif rec.get("in_scope") is not True:
                reason = "not_in_scope"
            elif rec.get("method_class") != "computational":
                reason = "not_computational"
            else:
                reason = None

            if pmid and pmid in included_set and pmid not in retracted \
                    and rec.get("in_scope") is True:
                n_records_in_scope += 1

            if reason is not None:
                excluded_counts[reason] += 1
                excluded_pmids.setdefault(reason, set())
                if pmid:
                    excluded_pmids[reason].add(pmid)
                continue

            key = _normalize_key(rec.get("canonical_name") or rec.get("mentioned_name"))
            name_source = "canonical"
            if not key:
                name_source = "mentioned"
                key = _normalize_key(rec.get("mentioned_name"))
            if not key:
                name_source = "instance"
                instance_id = str(rec.get("instance_id") or "unknown-instance")
                key = _normalize_key(f"unknown method {instance_id}") or instance_id.casefold()

            group = groups.get(key)
            if group is None:
                group = groups[key] = {
                    "key": key,
                    "spellings": Counter(),
                    "name_values": set(),
                    "stages": set(),
                    "packages": set(),
                    "operations": set(),
                    "purposes": set(),
                    "roles": Counter(),
                    "pmids": set(),
                    "evidence": [],
                    "mention_values": set(),
                    "has_canonical": False,
                    "has_mentioned": False,
                    "real_name": False,
                }
            name = rec.get("canonical_name")
            if isinstance(name, str) and name.strip():
                group["spellings"][name.strip()] += 1
                group["name_values"].add(_normalize_key(name))
                group["has_canonical"] = True
                group["real_name"] = True
            else:
                mentioned = rec.get("mentioned_name")
                if isinstance(mentioned, str) and mentioned.strip():
                    group["spellings"][mentioned.strip()] += 1
                    group["name_values"].add(_normalize_key(mentioned))
                    group["has_mentioned"] = True
                    group["real_name"] = True
                if name_source == "instance":
                    group["spellings"][f"unknown method ({rec.get('instance_id')})"] = 1
                    group["name_values"].add(_normalize_key(
                        f"unknown method ({rec.get('instance_id')})"
                    ))

            for field, bucket in (("pipeline_stage", "stages"),
                                  ("package", "packages"),
                                  ("operation", "operations")):
                value = rec.get(field)
                if isinstance(value, str) and value.strip():
                    group[bucket].add(value.strip())
            mentioned = rec.get("mentioned_name")
            if isinstance(mentioned, str) and mentioned.strip():
                group["mention_values"].add(_normalize_key(mentioned))
            group["purposes"].update(_flatten_purposes(rec.get("purpose")))
            role = rec.get("rerunnable_role")
            if isinstance(role, str) and role.strip():
                group["roles"][role.strip()] += 1
            group["pmids"].add(pmid)

            verbatim = rec.get("verbatim")
            evidence = {
                "instance_id": rec.get("instance_id"),
                "pmid": pmid,
                "locator": rec.get("locator"),
                "verbatim": verbatim if isinstance(verbatim, str) else None,
                "has_verbatim": isinstance(verbatim, str) and bool(verbatim.strip()),
                "mentioned_name": mentioned if isinstance(mentioned, str) else None,
                "canonical_name": (
                    name.strip() if isinstance(name, str) and name.strip()
                    else (mentioned.strip() if isinstance(mentioned, str)
                          and mentioned.strip() else None)
                ),
                "operation": rec.get("operation"),
                "package": rec.get("package"),
                "pipeline_stage": rec.get("pipeline_stage"),
                "purpose": rec.get("purpose"),
                "rerunnable_role": rec.get("rerunnable_role"),
                "method_class": rec.get("method_class"),
                "version_in_text": rec.get("version_in_text"),
            }
            group["evidence"].append(evidence)

        # Stable IDs: deterministic hash of the normalized key, readable slug.
        used_ids: set[str] = set()
        entries: dict[str, dict] = {}
        by_key: dict[str, dict] = {}
        for key in sorted(groups):
            group = groups[key]
            digest = _sha256(key.encode("utf-8"))
            length = 8
            entry_id = f"lit:{_slug(key)}-{digest[:length]}"
            while entry_id in used_ids:
                length += 4
                entry_id = f"lit:{_slug(key)}-{digest[:length]}"
            used_ids.add(entry_id)

            evidence = sorted(
                group["evidence"],
                key=lambda e: (
                    _pmid_sort_key(str(e["pmid"] or "")),
                    str(e["locator"] or ""),
                    str(e["instance_id"] or ""),
                ),
            )
            pmids = _pmid_list(group["pmids"])
            spellings = sorted(
                group["spellings"],
                key=lambda s: (-group["spellings"][s], s.casefold(), s),
            )
            display_name = spellings[0] if spellings else f"unknown method ({key})"
            aliases = sorted(set(spellings))

            operations = sorted(group["operations"])
            packages = sorted(group["packages"])
            stages = sorted(group["stages"])
            roles = dict(sorted(group["roles"].items()))
            missing = {
                "package": sum(1 for e in evidence if not e["package"]),
                "operation": sum(1 for e in evidence if not e["operation"]),
                "pipeline_stage": sum(1 for e in evidence if not e["pipeline_stage"]),
                "canonical_name": sum(1 for e in evidence if not e["canonical_name"]),
            }
            unknown_fields = [
                field for field, bucket in (
                    ("name", group["real_name"]),
                    ("package", packages),
                    ("operation", operations),
                    ("pipeline_stage", stages),
                ) if not bucket
            ]
            partial_fields = [
                f for f, count in missing.items() if 0 < count < len(evidence)
            ]
            stage_other = bool(stages) and all(s == "other" for s in stages)
            completeness = {
                "unknown_fields": unknown_fields,
                "partial_fields": partial_fields,
                "missing_field_instances": missing,
                "stage_other": stage_other,
                "incomplete": bool(unknown_fields) or stage_other,
            }
            entry_name_source = (
                "canonical" if group["has_canonical"]
                else "mentioned" if group["has_mentioned"]
                else "instance"
            )

            entry = {
                "id": entry_id,
                "kind": "literature_method",
                "name": display_name,
                "canonical_name": display_name,
                "normalized_key": key,
                "aliases": aliases,
                "stages": stages,
                "packages": packages,
                "operations": operations,
                "purposes": sorted(group["purposes"]),
                "rerunnable_roles": roles,
                "upstream_background": bool(roles) and set(roles) == {"upstream_background"},
                "in_scope": True,
                "method_class": "computational",
                "scope": {
                    "in_scope": True,
                    "basis": "included_pmids_405 minus retractions",
                },
                "corpus": {
                    "n_instances": len(evidence),
                    "n_papers": len(pmids),
                    "pmids": pmids,
                    "n_operations": len(operations),
                    "examples": [
                        {
                            "pmid": e["pmid"],
                            "locator": e["locator"],
                            "verbatim": e["verbatim"],
                            "has_verbatim": e["has_verbatim"],
                            "operation": e["operation"],
                            "canonical_name": e["canonical_name"],
                        }
                        for e in evidence[:3]
                    ],
                },
                "completeness": completeness,
                "name_source": entry_name_source,
                "_evidence": evidence,
                "_name_values": sorted(group["name_values"]),
                "_mention_values": sorted(v for v in group["mention_values"] if v),
                "_package_values": [_normalize_key(p) for p in packages],
                "_operation_values": [_normalize_key(o) for o in operations],
                "_stage_values": [_normalize_key(s) for s in stages],
            }
            entry["support"] = self._support_for(entry)
            entry["adapters"] = entry["support"]["adapters"]
            entries[entry_id] = entry
            by_key[key] = entry

        self._entries = entries
        self._by_key = by_key

        n_qualifying = sum(e["corpus"]["n_instances"] for e in entries.values())
        qualifying_pmids = set()
        for entry in entries.values():
            qualifying_pmids.update(entry["corpus"]["pmids"])
        missing_scope_pmids = _pmid_list(effective - qualifying_pmids)
        self._stage_vocabulary = sorted(
            {s for entry in entries.values() for s in entry["stages"]}
        )

        self._corpus = {
            "path": str(self.methods_path),
            "sha256": corpus_sha,
            "sha256_16": corpus_sha[:16],
            "bytes": len(raw),
            "n_lines": n_lines,
            "n_records_parsed": len(records),
            "n_parse_errors": parse_errors,
            "n_records_raw": len(records),
            "n_records_in_scope": n_records_in_scope,
            "n_records_after_scope": n_qualifying,
            "n_records_qualifying": n_qualifying,
            "manifest": _corpus_manifest_entry(self.methods_path, corpus_sha),
        }

        self._scope = {
            "pmid_list_path": scope_list["path"],
            "pmid_list_sha256": list_sha,
            "pmid_list_sha256_16": scope_list["sha256_16"],
            "pmid_list_known_sha256": scope_list["known_sha256"],
            "pmid_list_known_snapshot": (
                dict(scope_list["known_snapshot"])
                if scope_list["known_snapshot"] is not None else None
            ),
            "pmid_list_matches_known": scope_list["matches_known_snapshot"],
            "n_included_pmids": scope_list["n_pmids"],
            "n_duplicate_pmids": scope_list["n_duplicates"],
            "retracted_pmids": sorted(retracted, key=_pmid_sort_key),
            "n_retracted_pmids": len(retracted),
            "n_retracted_in_included_list": len(retracted & included_set),
            "n_effective_pmids": len(effective),
            "n_pmids_with_qualifying_records": len(qualifying_pmids),
            "pmids_without_qualifying_records": missing_scope_pmids,
            "record_rule": "record.in_scope is True and record.method_class == 'computational'",
            "corpus_rule": "record.pmid in the included PMID list minus the retraction registry",
        }

        self._excluded = {
            reason: {
                "records": count,
                "n_pmids": len(excluded_pmids.get(reason, ())),
                "pmids": _pmid_list(excluded_pmids.get(reason, ())),
            }
            for reason, count in sorted(excluded_counts.items())
        }

        self._counts = {
            "n_records_total": len(records),
            "n_records_qualifying": n_qualifying,
            "n_records_excluded": len(records) - n_qualifying,
            "n_methods_literature": len(entries),
            "n_upstream_background_methods": sum(
                1 for e in entries.values() if e["upstream_background"]
            ),
            "n_methods_with_background_instances": sum(
                1 for e in entries.values()
                if "upstream_background" in e["rerunnable_roles"]
            ),
            "n_single_paper_methods": sum(
                1 for e in entries.values() if e["corpus"]["n_papers"] == 1
            ),
            "n_unknown_name_methods": sum(
                1 for e in entries.values() if e["name_source"] == "instance"
            ),
            "records_excluded": self._excluded,
        }

        self._notes = [
            "Corpus extraction is known incomplete: methods.jsonl is a frozen "
            "extraction, not an exhaustive inventory. Absence is not evidence "
            "that a method was unused.",
            "No frequency threshold or top-N cut is applied. Every qualifying "
            "canonical name is listed, including single-paper long-tail methods.",
            "Normalization merges only case/punctuation/whitespace surface "
            "variants. Different algorithms and different wired operations stay "
            "separate (AUCell != UCell, inferCNV != CopyKAT, FindMarkers != "
            "FindAllMarkers).",
            "The display label uses the most frequent exact surface spelling in "
            "the group. That choice affects only the label, never inclusion, "
            "matching, or ordering.",
            "n_papers counts distinct PMIDs among aggregated instances; "
            "n_instances counts extraction records and may repeat within a paper.",
            "rerunnable_role=upstream_background is preserved as a record flag "
            "and count, not used to filter methods out.",
        ]
        manifest = self._corpus["manifest"]
        if manifest["status"] == "absent":
            self._notes.append(
                "corpus/manifest.json was not found next to the methods file; "
                "the file was read as-is and its provenance is unverified."
            )
        elif manifest["status"] != "ok":
            self._notes.append(
                f"corpus/manifest.json is unusable ({manifest['status']}: "
                f"{manifest['error'] or 'schema mismatch'}); the methods file "
                "was read as-is and its provenance is unverified."
            )
        elif manifest["sha256_match"] is False:
            self._notes.append(manifest["warning"])
        if not scope_list["matches_known_snapshot"]:
            known_snapshot = scope_list["known_snapshot"]
            if known_snapshot is not None and known_snapshot.get("status") == "superseded":
                self._notes.append(
                    f"included PMID list is the {known_snapshot.get('label')} "
                    f"snapshot, superseded {known_snapshot.get('superseded')} by "
                    f"{known_snapshot.get('superseded_by')}; the configured list "
                    "is authoritative, so these counts describe historical "
                    "scope, not the current inclusion set."
                )
            else:
                self._notes.append(
                    "included PMID list sha256 differs from the recorded snapshot; "
                    "the configured list is authoritative and the registry scope "
                    "follows it."
                )
        if scope_list["n_duplicates"]:
            self._notes.append(
                "included PMID list contained "
                f"{scope_list['n_duplicates']} duplicate line(s)."
            )

    # --------------------------------------------------------------- adapters
    def _adapter_records(self) -> list[dict]:
        executable = self._executable()
        records: list[dict] = []
        for method_id in sorted(executable):
            spec = executable.get(method_id) or {}
            mapped: list[dict] = []
            for row in EXECUTABLE_CORPUS_MAP.get(method_id, ()):
                entry = self._by_key.get(row["key"])
                row_evidence = _evidence_for_row(entry, row) if entry else []
                row_pmids = _pmid_list(e.get("pmid") for e in row_evidence)
                mapped.append({
                    "normalized_key": row["key"],
                    "resolved": entry is not None,
                    "canonical_name": entry["canonical_name"] if entry else None,
                    "quality": row["quality"],
                    "note": row.get("note"),
                    "required_stages": list(row.get("stages") or ()),
                    "n_papers": len(row_pmids),
                    "n_instances": len(row_evidence),
                    "pmids": row_pmids,
                })
            resolved = [m for m in mapped if m["resolved"]]
            qualified = [m for m in resolved if m["n_instances"] > 0]
            if not mapped:
                quality = "local_adapter"
                reason = ("no corpus canonical name maps to this adapter; it is a "
                          "project-local adapter, not a literature-backed method")
            elif not qualified:
                quality = "local_adapter"
                reason = ("declared corpus mapping resolved but no instance meets "
                          "the row's required pipeline stage; no literature "
                          "backing is claimed")
            else:
                quality = "exact" if any(m["quality"] == "exact" for m in qualified) else "operation"
                reason = (f"{len(qualified)} canonical name(s) with qualifying "
                          f"instances map by exact normalized equality; best "
                          f"quality {quality}")

            name = spec.get("name") or method_id
            package = spec.get("package")
            stage = spec.get("stage")
            literatures = qualified
            record = {
                "id": f"adapter:{method_id}",
                "kind": "executable_adapter",
                "name": name,
                "canonical_name": name,
                "normalized_key": f"adapter {_normalize_key(method_id)}",
                "aliases": [method_id],
                "stages": [stage] if isinstance(stage, str) and stage else [],
                "packages": [package] if isinstance(package, str) and package else [],
                "operations": [name],
                "purposes": [],
                "rerunnable_roles": {},
                "upstream_background": False,
                "in_scope": None,
                "method_class": None,
                "scope": {
                    "in_scope": None,
                    "basis": "catalog.EXECUTABLE (project adapter), not corpus scope",
                },
                "corpus": {
                    "n_instances": 0,
                    "n_papers": 0,
                    "pmids": [],
                    "n_operations": 1,
                    "examples": [],
                },
                "completeness": {
                    "unknown_fields": [],
                    "partial_fields": [],
                    "missing_field_instances": {},
                    "stage_other": False,
                    "incomplete": not literatures,
                    "no_literature_backing": not literatures,
                },
                "support": {
                    "status": "executable",
                    "adapter_method_id": method_id,
                    "mapping_quality": quality,
                    "adapters": [],
                    "reason": reason,
                },
                "literature_links": mapped,
                "adapter": {
                    "path": spec.get("adapter"),
                    "status": spec.get("status"),
                    "requires": spec.get("requires"),
                    "provides": spec.get("provides"),
                    "defaults": spec.get("defaults"),
                    "statistical_notes": spec.get("statistical_notes"),
                },
                "literature_refs": [
                    dict(ref, has_verbatim=bool(ref.get("verbatim")))
                    for ref in (spec.get("literature_refs") or [])
                    if isinstance(ref, Mapping)
                ],
                "name_source": "adapter",
                "_evidence": [],
                "_name_values": [_normalize_key(name), _normalize_key(method_id)],
                "_mention_values": [],
                "_package_values": [_normalize_key(package)] if isinstance(package, str) else [],
                "_operation_values": [_normalize_key(name)],
                "_stage_values": [_normalize_key(stage)] if isinstance(stage, str) else [],
            }
            record["support"]["adapters"] = [self._adapter_ref(method_id, quality, spec)]
            record["adapters"] = record["support"]["adapters"]
            records.append(record)
        return records

    @staticmethod
    def _adapter_ref(method_id: str, quality: str, spec: Mapping) -> dict:
        return {
            "method_id": method_id,
            "quality": quality,
            "wired_operation": spec.get("package") or spec.get("name") or method_id,
        }

    def _entries_for_adapters(self) -> dict[str, dict]:
        """return adapter_id -> record for the current executable snapshot."""
        return {r["id"]: r for r in self._adapter_records()}

    # ------------------------------------------------------------- public rows
    @staticmethod
    def _public(entry: dict) -> dict:
        out = {key: entry[key] for key in _PUBLIC_FIELDS if key in entry}
        # Legacy aggregate keys are also exposed at the top level so existing
        # catalog/report consumers do not have to know the provenance nesting.
        corpus = entry.get("corpus") or {}
        for key in ("n_instances", "n_papers", "pmids", "n_operations", "examples"):
            if key in corpus:
                out[key] = corpus[key]
        out["evidence_count"] = len(entry.get("_evidence") or [])
        return out

    def _compact(self, entry: dict, match: dict | None = None) -> dict:
        out = self._public(entry)
        out["match"] = match or {"kind": "all", "terms": 0, "fields": []}
        if entry["kind"] == "executable_adapter":
            out["literature_links"] = entry["literature_links"]
            out["literature_refs"] = entry["literature_refs"]
            out["adapter"] = entry["adapter"]
        else:
            out["name_source"] = entry.get("name_source", "canonical")
        return out

    def _full(self, entry: dict) -> dict:
        out = self._compact(entry)
        out["evidence"] = [dict(e) for e in entry.get("_evidence") or []]
        if entry["kind"] == "literature_method":
            out["related_adapters"] = self.adapters_for(entry["canonical_name"])
        else:
            out["related_adapters"] = entry["support"]["adapters"]
        return out

    # ------------------------------------------------------------------ search
    def _match(self, entry: dict, query_terms: list[str], full_query: str) -> dict | None:
        field_order = ("name", "mention", "package", "operation", "stage")
        fields = {
            "name": entry.get("_name_values") or [],
            "mention": entry.get("_mention_values") or [],
            "package": entry.get("_package_values") or [],
            "operation": entry.get("_operation_values") or [],
            "stage": entry.get("_stage_values") or [],
        }
        field_priority = {field: index for index, field in enumerate(field_order)}
        best_rank = -1
        matched_fields: set[str] = set()
        name_matched = False

        def match_term(term: str) -> tuple[int, str, bool] | None:
            best: tuple[int, int] | None = None
            best_field: str | None = None
            term_name_hit = False
            for field in field_order:
                for value in fields[field]:
                    if not value:
                        continue
                    if term == value:
                        rank = 0
                    elif value.startswith(term):
                        rank = 1
                    elif re.search(rf"(?<!\w){re.escape(term)}(?!\w)", value):
                        rank = 2
                    else:
                        continue
                    if field == "name":
                        term_name_hit = True
                    candidate = (rank, field_priority[field])
                    if best is None or candidate < best:
                        best = candidate
                        best_field = field
            if best is None or best_field is None:
                return None
            return best[0], best_field, term_name_hit

        for term in query_terms:
            hit = match_term(term)
            if hit is None:
                return None
            if hit[0] > best_rank:
                best_rank = hit[0]
            matched_fields.add(hit[1])
            name_matched = name_matched or hit[2]

        kind_by_rank = {0: "exact", 1: "prefix", 2: "token"}
        kind = kind_by_rank[best_rank]

        # The whole query can be an exact name/alias even when its individual
        # tokens only token-match the name.
        name_exact = bool(full_query and full_query in (entry.get("_name_values") or []))
        if name_exact:
            kind = "exact"
            matched_fields.add("name")
            name_matched = True

        return {
            "kind": kind,
            "terms": len(query_terms),
            "fields": sorted(matched_fields),
            "name_matched": bool(name_matched),
            "name_exact": name_exact,
        }

    @staticmethod
    def _normalize_stages(stages: str | Sequence[str] | None) -> list[str] | None:
        if stages is None:
            return None
        if isinstance(stages, str):
            values = [stages]
        elif isinstance(stages, Sequence):
            values = list(stages)
        else:
            raise TypeError("stages must be a string or a sequence of strings")
        out: list[str] = []
        for value in values:
            if not isinstance(value, str):
                raise TypeError("stages entries must be strings")
            normalized = _normalize_key(value)
            if normalized and normalized not in out:
                out.append(normalized)
        return out or None

    @staticmethod
    def _normalize_kinds(kinds: str | Sequence[str] | None) -> list[str] | None:
        if kinds is None:
            return None
        if isinstance(kinds, str):
            values = [kinds]
        elif isinstance(kinds, Sequence):
            values = list(kinds)
        else:
            raise TypeError("kinds must be a string or a sequence of strings")
        allowed = {"literature_method", "executable_adapter"}
        out: list[str] = []
        for value in values:
            if not isinstance(value, str):
                raise TypeError("kinds entries must be strings")
            if value not in allowed:
                raise ValueError(
                    f"unknown kind {value!r}; expected one of {sorted(allowed)}"
                )
            if value not in out:
                out.append(value)
        return out or None

    def search(
        self,
        query: str | None = None,
        stages: str | Sequence[str] | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        kinds: str | Sequence[str] | None = None,
    ) -> dict:
        if query is not None and not isinstance(query, str):
            raise TypeError("query must be a string or None")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise TypeError("offset must be an integer")
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if offset < 0:
            raise ValueError("offset must be >= 0")

        effective_limit = min(limit, MAX_LIMIT)
        stage_filter = self._normalize_stages(stages)
        kind_filter = self._normalize_kinds(kinds)
        notes = list(self._notes)

        full_query = _normalize_key(query) if query else ""
        query_terms = full_query.split() if full_query else []
        if query is not None and query.strip() and not query_terms:
            raise ValueError("query contains no searchable terms")

        entries = list(self._entries.values()) + self._adapter_records()
        if kind_filter:
            entries = [e for e in entries if e["kind"] in kind_filter]
        stage_values = {
            _normalize_key(s) for e in entries for s in (e.get("stages") or [])
        }
        if stage_filter:
            unknown_stages = [s for s in stage_filter if s not in stage_values]
            if unknown_stages:
                notes.append(
                    "stage filter(s) not present in this registry: "
                    + ", ".join(unknown_stages)
                )

        ranked: list[tuple[int, int, str, dict, dict]] = []
        for entry in entries:
            if stage_filter:
                entry_stages = {
                    _normalize_key(s) for s in (entry.get("stages") or [])
                }
                if not (entry_stages & set(stage_filter)):
                    continue
            match = (
                self._match(entry, query_terms, full_query)
                if query_terms
                else {"kind": "all", "terms": 0, "fields": [],
                      "name_matched": False, "name_exact": False}
            )
            if match is None:
                continue
            rank = {"exact": 0, "prefix": 1, "token": 2, "all": 0}[match["kind"]]
            # Within one match quality: canonical-name hits outrank mentions,
            # packages, operations and stages (otherwise an exact package hit
            # alphabetically buries the method actually named by the query);
            # a whole-query exact name hit outranks a partial name hit.
            name_priority = 0 if match.get("name_matched") else 1
            exact_priority = 0 if match.get("name_exact") else 1
            ranked.append((rank, name_priority, exact_priority,
                           entry.get("normalized_key") or "", entry, match))

        ranked.sort(key=lambda row: (row[0], row[1], row[2], row[3], row[4]["id"]))
        total = len(ranked)
        page = ranked[offset:offset + effective_limit]
        methods = [self._compact(entry, match) for _, _, _, _, entry, match in page]
        returned = len(methods)
        has_more = offset + returned < total
        next_offset = offset + returned if has_more else None

        return {
            "schema": SEARCH_SCHEMA,
            "query": query,
            "stages": stage_filter,
            "kinds": kind_filter,
            "total": total,
            "offset": offset,
            "limit": effective_limit,
            "requested_limit": limit,
            "limit_capped": effective_limit != limit,
            "returned": returned,
            "has_more": has_more,
            "next_offset": next_offset,
            "methods": methods,
            "scope": self._scope_envelope(),
            "corpus": dict(self._corpus),
            "incomplete": True,
            "notes": notes,
        }

    def _scope_envelope(self) -> dict:
        return {
            "pmid_list_path": self._scope["pmid_list_path"],
            "pmid_list_sha256": self._scope["pmid_list_sha256"],
            "pmid_list_sha256_16": self._scope["pmid_list_sha256_16"],
            "pmid_list_known_sha256": self._scope["pmid_list_known_sha256"],
            "pmid_list_known_snapshot": (
                dict(self._scope["pmid_list_known_snapshot"])
                if self._scope["pmid_list_known_snapshot"] is not None else None
            ),
            "pmid_list_matches_known": self._scope["pmid_list_matches_known"],
            "n_included_pmids": self._scope["n_included_pmids"],
            "n_retracted_pmids": self._scope["n_retracted_pmids"],
            "retracted_pmids": list(self._scope["retracted_pmids"]),
            "n_effective_pmids": self._scope["n_effective_pmids"],
            "n_pmids_with_qualifying_records":
                self._scope["n_pmids_with_qualifying_records"],
            "n_records_parsed": self._corpus["n_records_parsed"],
            "n_records_in_scope": self._corpus["n_records_in_scope"],
            "n_records_qualifying": self._corpus["n_records_qualifying"],
            "n_records_excluded": (
                self._corpus["n_records_parsed"] - self._corpus["n_records_qualifying"]
            ),
            "record_rule": self._scope["record_rule"],
            "corpus_rule": self._scope["corpus_rule"],
        }

    def _detail(self, entry: dict) -> dict:
        out = self._full(entry)
        out["registry"] = {
            "corpus_sha256": self._corpus["sha256"],
            "pmid_list_sha256": self._scope["pmid_list_sha256"],
            "n_included_pmids": self._scope["n_included_pmids"],
            "n_effective_pmids": self._scope["n_effective_pmids"],
            "retracted_pmids": list(self._scope["retracted_pmids"]),
            "incomplete": True,
        }
        return out

    # -------------------------------------------------------------------- get
    def get(self, registry_id: str | None) -> dict | None:
        if not isinstance(registry_id, str) or not registry_id.strip():
            return None
        rid = registry_id.strip()
        adapters = self._entries_for_adapters()
        if rid in adapters:
            return self._detail(adapters[rid])
        if rid in self._entries:
            return self._detail(self._entries[rid])

        executable = self._executable()
        if rid in executable:
            record = adapters.get(f"adapter:{rid}")
            if record is not None:
                return self._detail(record)

        key = _normalize_key(rid)
        if key in self._by_key:
            return self._detail(self._by_key[key])
        for record in adapters.values():
            if key in (record.get("_name_values") or []):
                return self._detail(record)
        return None

    # ------------------------------------------------------------ adapter maps
    def adapters_for(self, canonical_name: str | None) -> list[dict]:
        key = _normalize_key(canonical_name)
        if not key:
            return []
        executable = self._executable()
        hits: dict[str, dict] = {}
        for method_id in sorted(executable):
            spec = executable.get(method_id) or {}
            wired = spec.get("package") or spec.get("name") or method_id
            for row in EXECUTABLE_CORPUS_MAP.get(method_id, ()):
                if row["key"] == key:
                    hits[method_id] = {"method_id": method_id,
                                       "quality": row["quality"],
                                       "wired_operation": wired,
                                       "corpus_entry": True}
                    break
            package_root = str(spec.get("package") or "").split("::")[0]
            if package_root and _normalize_key(package_root) == key:
                current = hits.get(method_id)
                if current is None:
                    hits[method_id] = {"method_id": method_id, "quality": "toolkit",
                                       "wired_operation": wired,
                                       "corpus_entry": False}
                elif current["quality"] not in ("exact", "operation"):
                    current["quality"] = "toolkit"

        quality_rank = {"exact": 0, "operation": 1, "toolkit": 2}
        out = sorted(hits.values(), key=lambda h: (quality_rank.get(h["quality"], 9),
                                                    h["method_id"]))
        for hit in out:
            hit.pop("corpus_entry", None)
        return out

    def _support_for(self, entry: dict) -> dict:
        adapters = self.adapters_for(entry["canonical_name"])
        if not adapters:
            return {
                "status": "corpus_only",
                "adapter_method_id": None,
                "mapping_quality": None,
                "adapters": [],
                "reason": "no executable adapter maps to this canonical name by exact normalized equality",
            }
        best = adapters[0]
        return {
            "status": "corpus_only",
            "adapter_method_id": None,
            "mapping_quality": best["quality"],
            "adapters": adapters,
            "reason": (
                f"related adapter {best['method_id']!r} exists at mapping quality "
                f"{best['quality']!r}; the executable definition is the adapter "
                "record, not this corpus entry"
            ),
        }

    def backing(self, method_id: str | None, limit: int | None = None) -> dict:
        if not isinstance(method_id, str) or not method_id.strip():
            raise TypeError("method_id must be a non-empty string")
        method_id = method_id.strip()
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise TypeError("limit must be an integer or None")
            if limit < 0:
                raise ValueError("limit must be >= 0")
        executable = self._executable()
        spec = executable.get(method_id)
        if spec is None:
            return {
                "schema": BACKING_SCHEMA,
                "method_id": method_id,
                "found": False,
                "quality": "not_found",
                "instances": [],
                "instances_returned": 0,
                "n_instances": 0,
                "n_papers": 0,
                "excluded_related": [],
                "mapped_canonical_names": [],
                "reason": f"{method_id!r} is not defined in catalog.EXECUTABLE",
                "notes": [],
            }

        rows = EXECUTABLE_CORPUS_MAP.get(method_id, ())
        mapped_names: list[str] = []
        instances: list[dict] = []
        notes: list[str] = []
        resolved_rows = 0
        qualities_with_hits: set[str] = set()
        for row in rows:
            entry = self._by_key.get(row["key"])
            if entry is None:
                notes.append(
                    f"declared canonical name {row['key']!r} is absent from this corpus snapshot"
                )
                continue
            resolved_rows += 1
            required_stages = tuple(row.get("stages") or ())
            row_evidence = _evidence_for_row(entry, row)
            row_hits = len(row_evidence)
            if row_hits:
                qualities_with_hits.add(row["quality"])
            if row.get("note"):
                notes.append(f"{entry['canonical_name']}: {row['note']}")
            for evidence in row_evidence:
                instances.append({
                    "instance_id": evidence["instance_id"],
                    "pmid": evidence["pmid"],
                    "locator": evidence["locator"],
                    "verbatim": evidence["verbatim"],
                    "has_verbatim": evidence["has_verbatim"],
                    "mentioned_name": evidence.get("mentioned_name"),
                    "canonical_name": evidence.get("canonical_name") or entry["canonical_name"],
                    "operation": evidence["operation"],
                    "pipeline_stage": evidence.get("pipeline_stage"),
                })
            if row_hits:
                mapped_names.append(entry["canonical_name"])
            else:
                notes.append(
                    f"{entry['canonical_name']}: resolved but no instance has the "
                    f"required pipeline stage(s) {required_stages}; not counted as backing"
                )

        instances.sort(key=lambda i: (_pmid_sort_key(str(i["pmid"] or "")),
                                      str(i["locator"] or ""),
                                      str(i["instance_id"] or "")))
        n_instances = len(instances)
        pmids = {str(i["pmid"]) for i in instances if i["pmid"]}

        if not rows:
            quality = "local_adapter"
            reason = (
                "no corpus canonical name maps to this adapter; it is a project-local "
                "adapter, not a literature-backed method. No instances are claimed."
            )
        elif resolved_rows == 0:
            quality = "local_adapter"
            reason = (
                "declared corpus mapping did not resolve against this snapshot; no "
                "literature backing is claimed."
            )
        elif n_instances == 0:
            quality = "local_adapter"
            reason = (
                "declared corpus mapping resolved but no instance meets the row's "
                "required pipeline stage(s); no literature backing is claimed."
            )
        else:
            quality = "exact" if "exact" in qualities_with_hits else "operation"
            reason = (
                f"explicit exact-name mapping to {len(mapped_names)} canonical name(s); "
                f"{n_instances} corpus instance(s) in {len(pmids)} paper(s), filtered "
                "to the row's required pipeline stage(s) where declared. "
                "Adjacent but different methods are listed under excluded_related "
                "and are not support."
            )

        excluded_related = []
        for key, reason in RELATED_BUT_NOT_BACKING.get(method_id, ()):
            entry = self._by_key.get(key)
            if entry is None:
                continue
            excluded_related.append({
                "name": entry["canonical_name"],
                "normalized_key": key,
                "reason": reason,
                "n_papers": entry["corpus"]["n_papers"],
                "n_instances": entry["corpus"]["n_instances"],
            })

        truncated = False
        returned = n_instances
        if limit is not None and n_instances > limit:
            instances = instances[:limit]
            returned = len(instances)
            truncated = True

        out = {
            "schema": BACKING_SCHEMA,
            "method_id": method_id,
            "found": n_instances > 0,
            "quality": quality,
            "instances": instances,
            "instances_returned": returned,
            "n_instances": n_instances,
            "n_papers": len(pmids),
            "excluded_related": excluded_related,
            "mapped_canonical_names": mapped_names,
            "reason": reason,
            "notes": notes,
        }
        if truncated:
            out["instances_truncated"] = True
        return out

    # ------------------------------------------------------------------ stats
    def stats(self) -> dict:
        adapters = self._adapter_records()
        executable = self._executable()
        mapped_ids = {m for m in EXECUTABLE_CORPUS_MAP if m in executable}
        unresolved_keys = sorted({
            row["key"] for method_id in EXECUTABLE_CORPUS_MAP
            for row in EXECUTABLE_CORPUS_MAP[method_id]
            if row["key"] not in self._by_key
        })
        adapters_stats = []
        for record in adapters:
            links = [m for m in record["literature_links"]
                     if m["resolved"] and m["n_instances"] > 0]
            linked_pmids: set[str] = set()
            for link in links:
                linked_pmids.update(link.get("pmids") or [])
            adapters_stats.append({
                "method_id": record["support"]["adapter_method_id"],
                "name": record["canonical_name"],
                "stage": record["stages"][0] if record["stages"] else None,
                "package": record["packages"][0] if record["packages"] else None,
                "mapping_quality": record["support"]["mapping_quality"],
                "n_mapped_canonical_names": len(links),
                "n_mapped_instances": sum(m["n_instances"] for m in links),
                "n_mapped_papers": len(linked_pmids),
                "no_literature_backing": not links,
            })

        stages = []
        for stage in self._stage_vocabulary:
            stages.append({
                "stage": stage,
                "n_methods": sum(1 for e in self._entries.values() if stage in e["stages"]),
                "n_instances": sum(
                    e["corpus"]["n_instances"] for e in self._entries.values()
                    if stage in e["stages"]
                ),
            })

        counts = dict(self._counts)
        counts.update({
            "n_methods_total": len(self._entries) + len(adapters),
            "n_adapters": len(adapters),
            "n_adapters_with_literature": sum(
                1 for a in adapters_stats if not a["no_literature_backing"]
            ),
            "n_adapters_local_only": sum(
                1 for a in adapters_stats if a["no_literature_backing"]
            ),
        })

        return {
            "schema": STATS_SCHEMA,
            "corpus": dict(self._corpus),
            "pmid_list": {
                "path": self._scope["pmid_list_path"],
                "sha256": self._scope["pmid_list_sha256"],
                "sha256_16": self._scope["pmid_list_sha256_16"],
                "known_sha256": self._scope["pmid_list_known_sha256"],
                "matches_known": self._scope["pmid_list_matches_known"],
                "n_pmids": self._scope["n_included_pmids"],
            },
            "retractions": self._retractions.as_list(),
            "scope": dict(self._scope),
            "counts": counts,
            "stages": stages,
            "stage_vocabulary": list(self._stage_vocabulary),
            "adapters": adapters_stats,
            "mapping_gaps": {
                "unresolved_mapping_keys": unresolved_keys,
                "declared_mappings_without_executable": sorted(
                    set(EXECUTABLE_CORPUS_MAP) - mapped_ids
                ),
                "executables_without_mapping": sorted(
                    set(executable) - set(EXECUTABLE_CORPUS_MAP)
                ),
            },
            "definitions": {
                "n_papers": "distinct PMIDs among the aggregated instances of one canonical name",
                "n_instances": "extracted method-instance records; may repeat within one paper",
                "in_scope": "record.in_scope is True and method_class == 'computational', within the included PMID list minus retractions",
            },
            "incomplete": True,
            "notes": list(self._notes),
        }

    # ------------------------------------------------------------------ load
    def load(self, include_evidence: bool = False) -> dict:
        entries = list(self._entries.values()) + self._adapter_records()
        entries.sort(key=lambda e: (e.get("normalized_key") or "", e["id"]))
        methods = [
            self._full(e) if include_evidence
            else self._compact(e, {"kind": "all", "terms": 0, "fields": []})
            for e in entries
        ]
        return {
            "schema": LOAD_SCHEMA,
            "methods": methods,
            "stats": self.stats(),
            "scope": dict(self._scope),
            "corpus": dict(self._corpus),
            "incomplete": True,
        }


# ------------------------------------------------------------------ singleton
_DEFAULT: MethodRegistry | None = None
_DEFAULT_LOCK = threading.Lock()


def _default_registry() -> MethodRegistry:
    global _DEFAULT
    if _DEFAULT is None:
        with _DEFAULT_LOCK:
            if _DEFAULT is None:
                _DEFAULT = MethodRegistry()
    return _DEFAULT


def build_registry(
    methods_path: str | os.PathLike | None = None,
    pmid_list_path: str | os.PathLike | None = None,
    retractions: Iterable[Any] | None = None,
    executable: Mapping[str, Mapping] | None = None,
) -> MethodRegistry:
    """Build a registry from an explicit snapshot (tests, alternate corpora)."""
    return MethodRegistry(
        methods_path=methods_path,
        pmid_list_path=pmid_list_path,
        retractions=retractions,
        executable=executable,
    )


def registry_search(
    query: str | None = None,
    stages: str | Sequence[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    kinds: str | Sequence[str] | None = None,
) -> dict:
    return _default_registry().search(query=query, stages=stages,
                                     limit=limit, offset=offset, kinds=kinds)


def registry_get(registry_id: str | None) -> dict | None:
    return _default_registry().get(registry_id)


def registry_stats() -> dict:
    return _default_registry().stats()


def registry_backing(method_id: str | None, limit: int | None = None) -> dict:
    return _default_registry().backing(method_id, limit=limit)


def registry_adapters_for(canonical_name: str | None) -> list[dict]:
    return _default_registry().adapters_for(canonical_name)


def load_registry(include_evidence: bool = False) -> dict:
    return _default_registry().load(include_evidence=include_evidence)
