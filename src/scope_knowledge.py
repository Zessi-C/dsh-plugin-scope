"""SCOPE corpus knowledge retrieval.

Read-only, dependency-free access to the frozen single-cell paper corpus that
backs SCOPE. It is deliberately separate from the legacy ``sctda`` kernel: it
does not import ``sctda``, does not touch the project database, and never writes
to the corpus.

What it reads
-------------
The corpus lives in ``<research>/corpus`` and is produced by
``tools/build_corpus.py``; the extraction shards under ``research/shards`` and
``research/scope_learn`` are build inputs retained for audit, never read here.

``<research>/corpus/workflows.jsonl``
    One record per paper: question, comparison, cell level, ordered steps
    (observation -> next analysis -> rationale), claims and gaps.
``<research>/corpus/methods.jsonl``
    One record per method instance with locator, verbatim sentence, operation,
    stage and stated constraints.
``<research>/corpus/patterns.jsonl``
    One pattern record per PMID with a brief locator or a PDF page anchor.
    The shard -> repair precedence is already applied when the file is built, so
    this module indexes exactly one record per paper and does no precedence
    selection. ``manifest.json`` records which source files were merged and how
    many records a repair superseded; that provenance is reported in ``stats``.
``<research>/corpus/manifest.json``
    Per-file sha256, byte size, record count and source files, plus the pattern
    merge provenance. Every source's sha256 is checked against it; a mismatch is
    a warning, never silently ignored.

Hard rules
----------
1. The user's 405-PMID inclusion list is authoritative (the current upstream
   set; ``included_pmids_422.txt`` is a superseded snapshot kept only for
   reproducing pre-2026-11-13 numbers). If the list cannot be read, construction
   fails closed unless ``allow_missing_pmid_list=True`` is passed explicitly for
   an audit. Records whose PMID is not in the list are dropped.
   ``include_out_of_scope=True`` is likewise an audit-only escape hatch; it
   never overrides the retraction rule.
2. Retracted papers are hard-excluded before any search. PMID 36450980 is in
   the default registry; the registry can grow through ``extra_retractions``
   but cannot be emptied. Exclusion also applies when the ``pmid`` field is
   missing: the PMID is recovered from record IDs, and as a last resort the
   raw record is scanned for a retracted PMID. A record that merely mentions a
   retracted PMID is also dropped, because this frozen corpus is not a citation
   graph and a conservative miss is safer than surfacing a retracted fragment.
3. A hit is one fine-grained evidence unit, never a paper-level summary. It
   carries source file + SHA-256, PMID, locator, and verbatim text, or it is
   labelled ``unverified`` and hidden by default. ``rationale``, ``next_analysis``,
   ``observation``, ``claim``, method metadata and pattern lessons are labelled
   ``extraction_inference`` or ``design_inference``; they are never upgraded to
   author statements.
4. Retrieval never certifies evidence. ``usable_as_evidence`` is false on every
   hit and ``claim_support`` is ``not_asserted``: a retrievable quote is not
   proof that it supports a queried claim. ``get``/``context`` return ``tier``
   null unless the caller supplies ``matched_fields``; they never fall back to
   a best-tier shortcut. Anchor and semantic checks remain the caller's job.
5. Citation status is ``unknown`` for returned hits: this module applies a
   frozen retraction registry, not a live PubMed check. PMID 41162704 carries
   an erratum whose impact is unverified; it is not retracted, and its impact is
   not guessed.
6. ``author_focus_groups`` is not searchable and is redacted from full-record
   output unless the caller explicitly opts in for an audit. Paper-level
   ``cell_level`` and ``comparison`` are ``context_fields``: useful for locating
   a paper, never candidate evidence. No hit is candidate-eligible.
7. Ordering is deterministic (PMID, then source order). There is no relevance
   score, document-frequency weight, citation count or hit-count ranking.

The module does not re-verify locators against the brief/PDF text. Each hit says
``anchor_status="not_checked"``; anchor verification remains the caller's job.

Typical use
-----------
    from scope_knowledge import ScopeKnowledge

    idx = ScopeKnowledge(research_dir="/data/.../research",
                         pmid_list_path="/data/.../included_pmids_405.txt")
    page = idx.search("pseudobulk", source_kinds=["method"], limit=20)
    hit = page["hits"][0]
    detail = idx.get(hit["evidence_id"], matched_fields=hit["matched_fields"])
    near = idx.context(hit["evidence_id"], before=1, after=1,
                       matched_fields=hit["matched_fields"])
    full = idx.record(hit["pmid"])            # whole frozen extract for one paper

    # Tests index a hand-built corpus instead of the shipped one:
    idx = ScopeKnowledge(research_dir=root,
                         source_files=[SourceSpec("pattern", path, "patterns")])
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "CorpusConfigError",
    "DEFAULT_EDITORIAL_NOTICES",
    "DEFAULT_RETRACTIONS",
    "EditorialNotice",
    "EditorialNoticeRegistry",
    "KnowledgeHit",
    "MAX_CONTEXT",
    "MAX_PAGE_SIZE",
    "QueryError",
    "RetractionNotice",
    "RetractionRegistry",
    "ScopeKnowledge",
    "ScopeKnowledgeError",
    "SourceSpec",
    "TIER_DESIGN",
    "TIER_EXTRACTION",
    "TIER_UNVERIFIED",
    "TIER_VERBATIM",
    "corpus_dir",
    "default_index",
    "locator_section",
    "make_pdf_locator",
    "normalize_pmid",
    "search_corpus",
]

# --------------------------------------------------------------------- constants

TIER_VERBATIM = "verbatim"
TIER_EXTRACTION = "extraction_inference"
TIER_DESIGN = "design_inference"
TIER_UNVERIFIED = "unverified"
EVIDENCE_TIERS = (TIER_VERBATIM, TIER_EXTRACTION, TIER_DESIGN, TIER_UNVERIFIED)
_TIER_STRENGTH = {
    TIER_UNVERIFIED: 0,
    TIER_DESIGN: 1,
    TIER_EXTRACTION: 2,
    TIER_VERBATIM: 3,
}

SOURCE_KINDS = ("workflow", "method", "pattern")
GRANULARITIES = (
    "question_evidence",
    "step",
    "claim",
    "gap",
    "method_instance",
    "pattern",
    "record_context",
)
LOCATOR_SECTIONS = (
    "abstract",
    "subheadings",
    "figure_legends",
    "results",
    "methods",
    "other",
    "pdf",
)

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 200
MAX_CONTEXT = 20
MAX_WARNINGS = 200

PMID_LIST_ENV = "SCOPE_INCLUDED_PMIDS"
RESEARCH_DIR_ENV = "SCOPE_RESEARCH_DIR"
DEFAULT_RESEARCH_DIR = Path(__file__).resolve().parent.parent / "research"
CORPUS_SUBDIR = "corpus"
CORPUS_MANIFEST_NAME = "manifest.json"
CORPUS_SCHEMA = "sctda.corpus_manifest/1"
# The three extraction copies plus the merged pattern file. Names are the
# contract with ``tools/build_corpus.py``; nothing globs this directory, so a
# stray file cannot enter the index by accident.
CORPUS_FILES = (
    ("workflow", "workflows.jsonl", "workflows"),
    ("method", "methods.jsonl", "methods"),
    ("pattern", "patterns.jsonl", "patterns"),
)
DEFAULT_PMID_LIST_CANDIDATES = (
    # Only the current inclusion set is a default candidate. The superseded 422
    # list is deliberately not a fallback: a missing current list fails closed
    # instead of silently falling back to an obsolete scope.
    Path("/data/disk1/zeiss/singlecell-quant/data/included_pmids_405.txt"),
    Path("/home/zeiss/data/singlecell-quant/data/included_pmids_405.txt"),
    Path.home() / "data/singlecell-quant/data/included_pmids_405.txt",
)

# SHA-256 recorded for included_pmids_405.txt on 2026-11-17 (current upstream
# inclusion set: 422 minus the 17 papers excluded on 2026-11-13). A different
# hash is reported as a warning, not an error: an explicit custom list is
# legitimate, and the upstream snapshot may move again.
KNOWN_PMID_LIST_SHA256 = (
    "835beea7c489393e9e92ea17c8e0f69cb4f53a9af0d88e6f89f8b3e01f76fb90"
)

# Snapshots that have been characterised before. The current hash above is the
# only one that counts as a match; the older entry is named in stats and in the
# warning so that passing a superseded list for historical reproduction does not
# look like an unknown file. An explicit ``pmid_list_path`` / ``SCOPE_INCLUDED_PMIDS``
# always wins over the defaults; this table never overrides the configured list.
KNOWN_PMID_LIST_SNAPSHOTS = {
    KNOWN_PMID_LIST_SHA256: {
        "label": "included_pmids_405",
        "n_pmids": 405,
        "status": "current",
        "recorded": "2026-11-17",
        "upstream_rule": "422 minus 17 (2026-11-13 ruling)",
    },
    "cd7f602bff388be3021cfed2d9c4d0d1fa6f989aaaf9a681d95bfa9cbecf2635": {
        "label": "included_pmids_422",
        "n_pmids": 422,
        "status": "superseded",
        "recorded": "2026-11-12",
        "superseded": "2026-11-13",
        "superseded_by": "included_pmids_405",
        "note": (
            "historical scope; pass it explicitly only to reproduce "
            "pre-2026-11-13 numbers"
        ),
    },
}

AUTHOR_FOCUS_POLICY = (
    "author_focus_groups records what the authors studied. It is context only and "
    "must not seed candidate sets, marker whitelists or ranking weights."
)

POLICY = {
    "scope_list": "The configured PMID list is authoritative; unreadable list fails closed.",
    "retractions": "Retracted papers are hard-excluded before search; the registry only grows.",
    "citation_status": (
        "Returned hits are not proven clean: status is 'unknown' unless a live "
        "PubMed retraction/correction check is performed. A correction or erratum "
        "is reported with impact='unknown' until its effect is verified."
    ),
    "evidence_use": (
        "usable_as_evidence is always false here: a quote may be retrievable and "
        "anchorable, but neither its anchor nor its semantics for a queried claim "
        "have been checked."
    ),
    "read_tiers": (
        "get/context return tier=null unless matched_fields is supplied; there is "
        "no best-tier shortcut from a query hit to a verbatim read. tier_scope: "
        "query=matched to query terms, unit=descriptive with no query, "
        "anchor=unanchored, unscoped=read without matched fields."
    ),
    "candidate_eligibility": "No retrieval hit is candidate-eligible; paper focus never ranks candidates.",
    "independent_sources": (
        "Count unique (pmid, locator) pairs; repeated hits at one locator are "
        "not independent replication."
    ),
    "ordering": "Deterministic by PMID and source order; no frequency, citation or relevance score.",
    "anchors": "locator+verbatim are passed through; this module does not re-verify anchors.",
    "corpus": (
        "Reads the built corpus under <research>/corpus only. The shard -> repair "
        "precedence is applied at build time, so patterns.jsonl holds one record "
        "per paper; per-file sha256 and merge provenance come from manifest.json "
        "and a mismatch is reported as a warning, never silently accepted."
    ),
    "tiers": {
        TIER_VERBATIM: "The quoted sentence itself states this.",
        TIER_EXTRACTION: "Extractor summary/reconstruction; cite as 'the extraction records ... (unverified)'.",
        TIER_DESIGN: "A system-level lesson or design judgement, not paper evidence.",
        TIER_UNVERIFIED: "No usable locator+verbatim pair; never evidence.",
    },
}

_BRIEF_LOCATOR_RE = re.compile(
    r"^(?P<section>abstract|subheadings|figure_legends|results|methods|other)"
    r":L\d+:P\d+:S\d+(?:#\d+)?$"
)
_PDF_LOCATOR_RE = re.compile(r"^pdf:(?P<pmid>\d{1,9}):p(?P<page>\d+)$")
_QUOTED_RE = re.compile(r'"([^"]+)"')
_QUERY_SPLIT_RE = re.compile(r"[\s,;，；]+")
_PMID_ID_RE = re.compile(r"^\s*(\d{1,9})\s*[:/]")
_PMID_CITATION_RE = re.compile(r"\bPMID\s*:?\s*(\d{1,9})\b", re.IGNORECASE)


class ScopeKnowledgeError(RuntimeError):
    """Base error for configuration, corpus and query failures."""


class CorpusConfigError(ScopeKnowledgeError):
    """The corpus or the authoritative PMID list is unusable."""


class QueryError(ScopeKnowledgeError):
    """A retrieval argument is invalid."""


# ----------------------------------------------------------------------- helpers


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten_text(item) for item in value)
    if isinstance(value, dict):
        return " ".join(f"{key} {_flatten_text(item)}" for key, item in value.items())
    return str(value)


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", _flatten_text(value)).strip().casefold()


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"\s*\d+\s*", value):
        return int(value.strip())
    return None


def normalize_pmid(value: Any) -> str | None:
    """Return a bare PMID string, or ``None`` when the value is not one."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    text = str(value).strip()
    text = re.sub(r"^PMID\s*:?\s*", "", text, flags=re.IGNORECASE)
    if re.fullmatch(r"\d{1,9}", text):
        return text
    return None


def _extract_pmid(record: Mapping[str, Any]) -> str | None:
    for key in ("pmid", "PMID", "paper_id"):
        pmid = normalize_pmid(record.get(key))
        if pmid:
            return pmid
    for key in (
        "workflow_id",
        "instance_id",
        "source_id",
        "record_id",
        "evidence_id",
    ):
        value = record.get(key)
        if isinstance(value, str):
            match = _PMID_ID_RE.match(value)
            if match:
                return match.group(1)
    citation = record.get("citation")
    if isinstance(citation, str):
        match = _PMID_CITATION_RE.search(citation)
        if match:
            return match.group(1)
    return None


def make_pdf_locator(pmid: Any, page: Any) -> str | None:
    """Build a physical-PDF-page anchor, kept separate from brief locators."""
    pmid_text = normalize_pmid(pmid)
    page_number = _as_int(page)
    if not pmid_text or page_number is None or page_number <= 0:
        return None
    return f"pdf:{pmid_text}:p{page_number}"


def locator_section(locator: str | None) -> str | None:
    """Return the brief section or ``pdf`` for a known locator form."""
    if not locator:
        return None
    text = str(locator).strip()
    match = _BRIEF_LOCATOR_RE.match(text)
    if match:
        return match.group("section")
    if _PDF_LOCATOR_RE.match(text):
        return "pdf"
    return None


def _locator_type(locator: str | None) -> str | None:
    if not locator:
        return None
    text = str(locator).strip()
    if _BRIEF_LOCATOR_RE.match(text):
        return "brief"
    if _PDF_LOCATOR_RE.match(text):
        return "pdf"
    return None


def _best_tier(tiers: Iterable[str]) -> str:
    best = TIER_UNVERIFIED
    for tier in tiers:
        if _TIER_STRENGTH.get(tier, 0) > _TIER_STRENGTH[best]:
            best = tier
    return best


def _matched_field_list(
    value: str | Iterable[str] | None,
) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    try:
        return [str(item) for item in value]
    except TypeError as exc:
        raise QueryError("matched_fields must be a string or iterable of strings") from exc


def _scoped_tier(
    hit: "KnowledgeHit",
    matched_fields: Sequence[str] | None,
) -> tuple[str | None, str]:
    """Tier for a query-scoped read; never infer a best tier for an unscoped read."""
    if not hit.is_anchored:
        return TIER_UNVERIFIED, "anchor"
    if not matched_fields:
        return None, "unscoped"
    tiers = hit.tiers
    present = [name for name in matched_fields if name in tiers]
    if not present:
        return None, "field_not_present"
    return _best_tier(tiers[name] for name in present), "query"


_ASCII_WORD = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
)


def _term_matcher(term: str) -> re.Pattern[str]:
    """Compile a term so ASCII identifiers match as tokens, not substrings.

    ``UC`` must not match ``AUCell``; ``CD3E`` must not match ``CD3EAP``.  The
    boundary is ASCII-only on purpose: a term such as ``UC`` still matches
    ``UC患者`` and ``CD4+``, while CJK terms remain plain substrings.  Gene
    symbols containing hyphens or plus signs keep their punctuation.
    """
    prefix = r"(?<![A-Za-z0-9_])" if term and term[0] in _ASCII_WORD else ""
    suffix = r"(?![A-Za-z0-9_])" if term and term[-1] in _ASCII_WORD else ""
    return re.compile(prefix + re.escape(term) + suffix)


def _parse_query_terms(query: str) -> list[str]:
    phrases = [match.group(1) for match in _QUOTED_RE.finditer(query)]
    rest = _QUOTED_RE.sub(" ", query)
    tokens = [token for token in _QUERY_SPLIT_RE.split(rest) if token]
    terms: list[str] = []
    for item in phrases + tokens:
        term = _normalize_text(item)
        if not term or term in terms:
            continue
        if not any(character.isalnum() for character in term):
            continue
        terms.append(term)
    return terms


def _choice_set(value: str | Iterable[str] | None) -> set[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return {value.casefold()}
    return {str(item).casefold() for item in value}


def _validate_choice(name: str, values: set[str] | None, allowed: Sequence[str]) -> None:
    if values is None:
        return
    unknown = sorted(values - set(allowed))
    if unknown:
        raise QueryError(f"unknown {name}: {unknown}; allowed: {list(allowed)}")


def _page_bounds(limit: int, offset: int) -> tuple[int, int]:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise QueryError("limit must be an integer")
    if limit < 1 or limit > MAX_PAGE_SIZE:
        raise QueryError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise QueryError("offset must be an integer")
    if offset < 0:
        raise QueryError("offset must be >= 0")
    return limit, offset


def _page_payload(total: int, offset: int, returned: int, limit: int) -> dict:
    has_more = offset + returned < total
    return {
        "offset": offset,
        "limit": limit,
        "total": total,
        "returned": returned,
        "has_more": has_more,
        "next_offset": offset + returned if has_more else None,
    }


def _sanitize(value: Any, include_author_focus: bool) -> Any:
    """Deep-copy a record, redacting author-focus fields unless explicitly kept."""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if key in ("author_focus_groups", "author_focus") and not include_author_focus:
                cleaned[key] = {"redacted": True, "policy": AUTHOR_FOCUS_POLICY}
            else:
                cleaned[key] = _sanitize(item, include_author_focus)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, include_author_focus) for item in value]
    return copy.deepcopy(value)


# --------------------------------------------------------------------- retraction


@dataclass(frozen=True)
class RetractionNotice:
    """One retraction relationship. Adding a notice is the only mutation."""

    pmid: str
    retraction_pmid: str | None = None
    notice_url: str | None = None
    notice_date: str | None = None
    reason: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        pmid = normalize_pmid(self.pmid)
        if pmid is None:
            raise ValueError(f"retraction notice has no valid PMID: {self.pmid!r}")
        object.__setattr__(self, "pmid", pmid)
        if self.retraction_pmid is not None:
            notice_pmid = normalize_pmid(self.retraction_pmid)
            if notice_pmid is None:
                raise ValueError(
                    f"retraction notice PMID is invalid: {self.retraction_pmid!r}"
                )
            object.__setattr__(self, "retraction_pmid", notice_pmid)

    def as_dict(self) -> dict:
        return {
            "pmid": self.pmid,
            "retraction_pmid": self.retraction_pmid,
            "notice_url": self.notice_url,
            "notice_date": self.notice_date,
            "reason": self.reason,
            "note": self.note,
        }


DEFAULT_RETRACTIONS: tuple[RetractionNotice, ...] = (
    RetractionNotice(
        pmid="36450980",
        retraction_pmid="40634621",
        notice_url="https://pubmed.ncbi.nlm.nih.gov/40634621/",
        notice_date="2025-07-09",
        reason=(
            "Retraction Note: Human fetal cerebellar cell atlas informs "
            "medulloblastoma origin and oncogenesis (Nature 2025;644(8075):290)"
        ),
        note=(
            "Hard-excluded from retrieval. Source records remain on disk for audit; "
            "their conclusions are not evidence or method recommendations."
        ),
    ),
)


class RetractionRegistry:
    """Append-only retraction set; the built-in notice cannot be removed."""

    def __init__(
        self,
        extra: Iterable[RetractionNotice | Mapping[str, Any]] = (),
    ) -> None:
        self._notices: dict[str, RetractionNotice] = {
            notice.pmid: notice for notice in DEFAULT_RETRACTIONS
        }
        for item in extra:
            self.add(item)

    def add(self, notice: RetractionNotice | Mapping[str, Any]) -> None:
        if not isinstance(notice, RetractionNotice):
            notice = RetractionNotice(**dict(notice))
        existing = self._notices.get(notice.pmid)
        if existing is not None and existing != notice:
            # Keep the built-in facts unless the caller supplies a richer notice.
            merged = replace(
                existing,
                retraction_pmid=notice.retraction_pmid or existing.retraction_pmid,
                notice_url=notice.notice_url or existing.notice_url,
                notice_date=notice.notice_date or existing.notice_date,
                reason=notice.reason or existing.reason,
                note=notice.note or existing.note,
            )
            self._notices[notice.pmid] = merged
        else:
            self._notices[notice.pmid] = notice

    def get(self, pmid: Any) -> RetractionNotice | None:
        key = normalize_pmid(pmid)
        return self._notices.get(key) if key else None

    @property
    def pmids(self) -> frozenset[str]:
        return frozenset(self._notices)

    def as_list(self) -> list[dict]:
        return [self._notices[pmid].as_dict() for pmid in sorted(self._notices)]

    def __contains__(self, pmid: Any) -> bool:
        return self.get(pmid) is not None

    def __len__(self) -> int:
        return len(self._notices)


# --------------------------------------------------------------- editorial notices


@dataclass(frozen=True)
class EditorialNotice:
    """A correction, erratum or expression of concern.

    These notices do not exclude a paper. They mark the record as carrying an
    editorial notice whose impact has not been verified; ``impact`` is never
    inferred as ``none``.
    """

    pmid: str
    kind: str
    notice_pmid: str | None = None
    notice_citation: str | None = None
    notice_url: str | None = None
    notice_date: str | None = None
    impact: str = "unknown"
    note: str = ""

    def __post_init__(self) -> None:
        pmid = normalize_pmid(self.pmid)
        if pmid is None:
            raise ValueError(f"editorial notice has no valid PMID: {self.pmid!r}")
        object.__setattr__(self, "pmid", pmid)
        object.__setattr__(self, "kind", str(self.kind).strip().lower())
        if self.notice_pmid is not None:
            notice_pmid = normalize_pmid(self.notice_pmid)
            if notice_pmid is None:
                raise ValueError(
                    f"editorial notice PMID is invalid: {self.notice_pmid!r}"
                )
            object.__setattr__(self, "notice_pmid", notice_pmid)

    def as_dict(self) -> dict:
        return {
            "pmid": self.pmid,
            "kind": self.kind,
            "notice_pmid": self.notice_pmid,
            "notice_citation": self.notice_citation,
            "notice_url": self.notice_url,
            "notice_date": self.notice_date,
            "impact": self.impact,
            "note": self.note,
        }


DEFAULT_EDITORIAL_NOTICES: tuple[EditorialNotice, ...] = (
    EditorialNotice(
        pmid="41162704",
        kind="erratum",
        notice_citation=(
            "Erratum: Nature. 2026;652(8112):E16. "
            "doi:10.1038/s41586-026-10484-w"
        ),
        notice_url="https://doi.org/10.1038/s41586-026-10484-w",
        impact="unknown",
        note=(
            "Erratum exists. Whether it changes any conclusion in the frozen "
            "extract has not been verified; the paper is not retracted. Do not "
            "assume the erratum is immaterial, and do not assume the extract "
            "already reflects it."
        ),
    ),
)


class EditorialNoticeRegistry:
    """Append-only registry of non-retraction editorial notices."""

    def __init__(
        self,
        extra: Iterable[EditorialNotice | Mapping[str, Any]] = (),
    ) -> None:
        self._by_pmid: dict[str, list[EditorialNotice]] = {}
        for notice in DEFAULT_EDITORIAL_NOTICES:
            self._add(notice)
        for item in extra:
            self.add(item)

    def _add(self, notice: EditorialNotice) -> None:
        existing = self._by_pmid.setdefault(notice.pmid, [])
        if notice not in existing:
            existing.append(notice)

    def add(self, notice: EditorialNotice | Mapping[str, Any]) -> None:
        if not isinstance(notice, EditorialNotice):
            notice = EditorialNotice(**dict(notice))
        self._add(notice)

    def for_pmid(self, pmid: Any) -> tuple[EditorialNotice, ...]:
        key = normalize_pmid(pmid)
        if not key:
            return ()
        return tuple(self._by_pmid.get(key, ()))

    def pmids(self) -> frozenset[str]:
        return frozenset(self._by_pmid)

    def as_list(self) -> list[dict]:
        notices = [
            notice
            for pmid in sorted(self._by_pmid)
            for notice in self._by_pmid[pmid]
        ]
        return [notice.as_dict() for notice in notices]

    def __contains__(self, pmid: Any) -> bool:
        return bool(self.for_pmid(pmid))

    def __len__(self) -> int:
        return sum(len(items) for items in self._by_pmid.values())


# --------------------------------------------------------------------- data model


@dataclass(frozen=True)
class SourceSpec:
    kind: str
    path: Path
    label: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", str(self.kind).strip().lower())
        object.__setattr__(self, "path", Path(self.path).expanduser())
        if not str(self.label).strip():
            object.__setattr__(self, "label", self.path.stem)


@dataclass(frozen=True)
class KnowledgeHit:
    """One fine-grained evidence unit plus its provenance and inference labels."""

    evidence_id: str
    source_kind: str
    source_file: str
    source_label: str
    source_sha256: str
    pmid: str
    record_id: str
    record_uid: str
    granularity: str
    locator: str | None
    locator_type: str | None
    verbatim: str | None
    content: tuple[tuple[str, Any], ...]
    record_context: tuple[tuple[str, Any], ...]
    field_tiers: tuple[tuple[str, str], ...]
    context_fields: tuple[str, ...]
    in_scope: bool | None
    seq: int
    unit_index: int
    search_fields: tuple[tuple[str, str], ...] = field(
        default=(), repr=False, compare=False
    )

    @property
    def tiers(self) -> dict[str, str]:
        return dict(self.field_tiers)

    @property
    def is_anchored(self) -> bool:
        return (
            self.locator is not None
            and self.locator_type is not None
            and self.verbatim is not None
        )

    @property
    def independent_source_key(self) -> str | None:
        if self.locator is None:
            return None
        return f"{self.pmid}:{self.locator}"

    def as_payload(
        self,
        *,
        tier: str | None,
        matched_fields: Sequence[str] = (),
        tier_scope: str = "unit",
        editorial_notices: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict:
        tiers = self.tiers
        matched = [name for name in matched_fields if name in tiers]
        notices = []
        for notice in editorial_notices or ():
            if hasattr(notice, "as_dict"):
                notices.append(notice.as_dict())
            else:
                notices.append(copy.deepcopy(dict(notice)))
        correction_status = (
            "recorded_impact_unknown" if notices else "unknown"
        )
        return {
            "evidence_id": self.evidence_id,
            "source_kind": self.source_kind,
            "source": {
                "file": self.source_file,
                "label": self.source_label,
                "sha256": self.source_sha256,
            },
            "pmid": self.pmid,
            "record_id": self.record_id,
            "record_uid": self.record_uid,
            "granularity": self.granularity,
            "unit_index": self.unit_index,
            "locator": self.locator,
            "locator_type": self.locator_type,
            "locator_section": locator_section(self.locator),
            "verbatim": self.verbatim,
            "verbatim_sha256": (
                _sha256_text(self.verbatim) if self.verbatim is not None else None
            ),
            "tier": tier,
            "tier_scope": tier_scope,
            "field_tiers": tiers,
            "matched_fields": matched,
            "matched_field_tiers": {name: tiers[name] for name in matched},
            "inference_fields": [
                name for name, value in self.field_tiers if value != TIER_VERBATIM
            ],
            "design_inference_fields": [
                name for name, value in self.field_tiers if value == TIER_DESIGN
            ],
            "content": copy.deepcopy(dict(self.content)),
            "record_context": copy.deepcopy(dict(self.record_context)),
            "context_fields": list(self.context_fields),
            "context_only": bool(matched)
            and all(name in self.context_fields for name in matched),
            "in_scope": self.in_scope,
            "candidate_eligible": False,
            "verbatim_available": self.verbatim is not None,
            "usable_as_evidence": False,
            "requires_verification": True,
            "requires_anchor_verification": True,
            "requires_claim_check": True,
            "claim_support": "not_asserted",
            "evidence_status": "unverified_anchor_and_claim",
            "citation_status": "unknown",
            "retraction_status": "not_excluded_by_frozen_registry",
            "live_retraction_check": False,
            "live_correction_check": False,
            "correction_status": correction_status,
            "editorial_notices": notices,
            "independent_source_key": self.independent_source_key,
            "anchor_status": "not_checked",
        }


# ------------------------------------------------------------------- main object


class ScopeKnowledge:
    """Read-only retrieval over the frozen SCOPE corpus."""

    def __init__(
        self,
        research_dir: str | Path | None = None,
        *,
        pmid_list_path: str | Path | None = None,
        source_files: Sequence[SourceSpec | Path | str | tuple] | None = None,
        allow_missing_pmid_list: bool = False,
        expected_pmid_list_sha256: str | None = None,
        extra_retractions: Iterable[RetractionNotice | Mapping[str, Any]] = (),
        retractions: RetractionRegistry | None = None,
        extra_editorial_notices: Iterable[
            EditorialNotice | Mapping[str, Any]
        ] = (),
        editorial_notices: EditorialNoticeRegistry | None = None,
        include_out_of_scope: bool = False,
    ) -> None:
        self.research_dir = Path(
            research_dir
            if research_dir is not None
            else (os.environ.get(RESEARCH_DIR_ENV) or DEFAULT_RESEARCH_DIR)
        ).expanduser()
        self.pmid_list_path = (
            Path(pmid_list_path).expanduser() if pmid_list_path is not None else None
        )
        self.allow_missing_pmid_list = bool(allow_missing_pmid_list)
        self.expected_pmid_list_sha256 = (
            expected_pmid_list_sha256.lower()
            if expected_pmid_list_sha256
            else None
        )
        self.include_out_of_scope = bool(include_out_of_scope)
        self.retractions = (
            retractions
            if retractions is not None
            else RetractionRegistry(extra_retractions)
        )
        if retractions is not None:
            for item in extra_retractions:
                self.retractions.add(item)
        self.editorial_notices = (
            editorial_notices
            if editorial_notices is not None
            else EditorialNoticeRegistry(extra_editorial_notices)
        )
        if editorial_notices is not None:
            for item in extra_editorial_notices:
                self.editorial_notices.add(item)
        self._specs = (
            self._coerce_sources(source_files)
            if source_files is not None
            else _default_source_specs(self.research_dir)
        )
        self._manifest = _read_corpus_manifest(
            self.research_dir, self._specs
        )
        self._lock = threading.RLock()
        self._reset()

    # ---------------------------------------------------------------- lifecycle

    def _reset(self) -> None:
        self._built = False
        self._stats: dict[str, Any] = {}
        self._scope: dict[str, Any] = {}
        self._all_hits: list[KnowledgeHit] = []
        self._hits_by_id: dict[str, KnowledgeHit] = {}
        self._hits_by_record: dict[str, list[KnowledgeHit]] = {}
        self._records_by_pmid: dict[str, list[dict]] = {}
        self._records_by_uid: dict[str, dict] = {}
        self._field_names: set[str] = set()

    def build(self, force: bool = False) -> dict:
        """Build the index and return :meth:`stats`."""
        if force:
            with self._lock:
                self._reset()
                self._build_locked()
        else:
            self._ensure_built()
        return self.stats()

    def _ensure_built(self) -> None:
        if self._built:
            return
        with self._lock:
            if not self._built:
                self._build_locked()

    # ------------------------------------------------------------ configuration

    def _coerce_sources(
        self, source_files: Sequence[SourceSpec | Path | str | tuple]
    ) -> list[SourceSpec]:
        specs: list[SourceSpec] = []
        for item in source_files:
            if isinstance(item, SourceSpec):
                spec = item
            elif isinstance(item, (str, Path)):
                path = Path(item).expanduser()
                spec = SourceSpec(_infer_source_kind(path), path, path.stem)
            elif isinstance(item, tuple):
                if len(item) == 2:
                    kind, path = item
                    label = Path(path).stem
                elif len(item) == 3:
                    kind, path, label = item
                else:
                    raise CorpusConfigError(
                        "source_files tuple must be (kind, path[, label])"
                    )
                spec = SourceSpec(kind, Path(path), str(label))
            else:
                raise CorpusConfigError(f"unsupported source spec: {item!r}")
            if spec.kind not in SOURCE_KINDS:
                raise CorpusConfigError(
                    f"unknown source kind {spec.kind!r}; allowed {list(SOURCE_KINDS)}"
                )
            path = spec.path
            if not path.is_absolute():
                path = self.research_dir / path
            specs.append(SourceSpec(spec.kind, path, spec.label))
        if not specs:
            raise CorpusConfigError("source_files is empty; nothing to index")
        return specs

    # ------------------------------------------------------------------ building

    def _build_locked(self) -> None:
        warnings: list[str] = []
        warning_count = 0

        def warn(message: str) -> None:
            nonlocal warning_count
            warning_count += 1
            if len(warnings) < MAX_WARNINGS:
                warnings.append(message)

        scope = self._load_scope(warn)
        specs = self._specs
        source_stats = [
            {
                "kind": spec.kind,
                "label": spec.label,
                "path": str(spec.path),
                "status": "missing",
                "sha256": None,
                "manifest_status": "unknown",
                "manifest_sha256": None,
                "manifest_n_records": None,
                "n_lines": 0,
                "n_records": 0,
                "n_malformed": 0,
                "n_records_indexed": 0,
                "n_hits": 0,
            }
            for spec in specs
        ]
        entries: list[dict[str, Any]] = []

        for spec_index, spec in enumerate(specs):
            stat = source_stats[spec_index]
            if not spec.path.is_file():
                warn(f"source_missing:{spec.label}:{spec.path}")
                continue
            try:
                data = spec.path.read_bytes()
            except OSError as exc:
                warn(f"source_unreadable:{spec.label}:{exc}")
                continue
            stat["status"] = "ok"
            stat["sha256"] = _sha256_bytes(data)
            manifest_entry = _manifest_entry_for(
                self._manifest, self.research_dir, spec
            )
            if manifest_entry is None:
                stat["manifest_status"] = "not_listed"
                if self._manifest.get("status") == "ok":
                    warn(f"corpus_manifest_missing_file:{spec.label}:{spec.path.name}")
            else:
                stat["manifest_status"] = (
                    "match" if manifest_entry.get("sha256") == stat["sha256"]
                    else "sha256_mismatch"
                )
                stat["manifest_sha256"] = manifest_entry.get("sha256")
                stat["manifest_n_records"] = manifest_entry.get("n_records")
                if stat["manifest_status"] == "sha256_mismatch":
                    # A rebuilt corpus is legitimate; a silently edited one is
                    # not. The index still serves the bytes it read, but the
                    # answer says they are not the manifest's snapshot.
                    warn(
                        "corpus_file_hash_differs_from_manifest:"
                        f"{spec.label}:{spec.path.name}:"
                        f"manifest={manifest_entry.get('sha256')}:"
                        f"actual={stat['sha256']}"
                    )
            text = data.decode("utf-8-sig", errors="replace")
            lines = text.splitlines()
            stat["n_lines"] = len(lines)
            row = 0
            for line_number, line in enumerate(lines, 1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    stat["n_malformed"] += 1
                    warn(f"malformed_json:{spec.label}:L{line_number}:{exc.msg}")
                    continue
                if not isinstance(record, dict):
                    stat["n_malformed"] += 1
                    warn(f"non_object_json:{spec.label}:L{line_number}")
                    continue
                row += 1
                stat["n_records"] += 1
                entries.append(
                    {
                        "spec": spec,
                        "spec_index": spec_index,
                        "row": row,
                        "record": record,
                        "source_file": self._display_path(spec.path),
                        "source_sha256": stat["sha256"],
                    }
                )

        retraction_res = [
            (pmid, re.compile(rf"(?<!\d){re.escape(pmid)}(?!\d)"))
            for pmid in sorted(self.retractions.pmids)
        ]
        included_pmids = set(scope.get("pmids_set") or ())
        excluded = {
            "retracted_records": 0,
            "retracted_reference_records": 0,
            # Read-time only. The shard -> repair precedence is applied when the
            # corpus is built, so patterns.jsonl has one record per paper and
            # nothing is dropped at read time; the build-time merge count (24)
            # is reported under `corpus_manifest.patterns.superseded_records`
            # and must not be added to these counters.
            "superseded_records": 0,
            "unattributed_records": 0,
            "out_of_scope_records": 0,
            "malformed_records": sum(stat["n_malformed"] for stat in source_stats),
        }

        seq = 0
        duplicate_ids: dict[str, int] = {}
        unverified_hits = 0

        for entry in entries:
            spec: SourceSpec = entry["spec"]
            record: dict[str, Any] = entry["record"]
            pmid = _extract_pmid(record)
            entry["pmid"] = pmid

            notice = self.retractions.get(pmid) if pmid else None
            if notice is not None:
                excluded["retracted_records"] += 1
                continue

            blob = json.dumps(
                record, ensure_ascii=False, sort_keys=True, default=str
            )
            retracted_refs = [
                candidate
                for candidate, pattern in retraction_res
                if pattern.search(blob)
            ]
            if retracted_refs:
                # Conservative: a record that carries a retracted PMID anywhere
                # is not allowed to pose as evidence, even if its own pmid is
                # missing or points elsewhere.
                excluded["retracted_reference_records"] += 1
                continue

            if scope["mode"] == "included":
                if pmid is None:
                    excluded["unattributed_records"] += 1
                    continue
                if pmid not in included_pmids and not self.include_out_of_scope:
                    excluded["out_of_scope_records"] += 1
                    continue
                in_scope: bool | None = pmid in included_pmids
            else:
                if pmid is None:
                    excluded["unattributed_records"] += 1
                    continue
                in_scope = None

            record_id = self._record_id(spec, record, pmid, entry["row"])
            record_uid = f"{spec.kind}:{spec.label}:{record_id}"
            prepared = {
                **entry,
                "pmid": pmid,
                "record_id": record_id,
                "record_uid": record_uid,
                "in_scope": in_scope,
            }
            self._records_by_pmid.setdefault(pmid, []).append(prepared)
            self._records_by_uid[record_uid] = prepared

            hits = self._hits_for_record(prepared, start_seq=seq)
            for hit in hits:
                if hit.evidence_id in self._hits_by_id:
                    duplicate_ids[hit.evidence_id] = duplicate_ids.get(
                        hit.evidence_id, 0
                    ) + 1
                    duplicate = duplicate_ids[hit.evidence_id]
                    warn(f"duplicate_evidence_id:{hit.evidence_id}#dup{duplicate}")
                    hit = replace(
                        hit, evidence_id=f"{hit.evidence_id}#dup{duplicate}"
                    )
                self._hits_by_id[hit.evidence_id] = hit
                self._hits_by_record.setdefault(record_uid, []).append(hit)
                self._all_hits.append(hit)
                seq = hit.seq + 1
                if not hit.is_anchored:
                    unverified_hits += 1
                for name, _ in (*hit.content, *hit.record_context):
                    self._field_names.add(name)

            source_stats[entry["spec_index"]]["n_records_indexed"] += 1
            source_stats[entry["spec_index"]]["n_hits"] += len(hits)

        self._all_hits.sort(key=lambda hit: (hit.pmid, hit.seq))
        for uid in self._hits_by_record:
            self._hits_by_record[uid].sort(key=lambda hit: hit.seq)

        fingerprint_parts = [
            f"scope:{scope.get('sha256') or 'none'}:{scope.get('mode')}"
        ]
        for stat in source_stats:
            fingerprint_parts.append(
                f"{stat['label']}:{stat.get('sha256') or 'missing'}:{stat['n_records']}"
            )
        corpus_fingerprint = _sha256_text("|".join(fingerprint_parts))[:32]

        pmids_indexed = set(self._records_by_pmid)
        coverage: dict[str, Any] = {
            "n_pmids_indexed": len(pmids_indexed),
        }
        if scope["mode"] == "included":
            coverage.update(
                {
                    "n_pmids_in_list": len(included_pmids),
                    "n_list_pmids_with_records": len(pmids_indexed & included_pmids),
                    "n_list_pmids_without_records": len(
                        included_pmids - pmids_indexed
                    ),
                }
            )

        self._scope = scope
        self._stats = {
            "status": "built",
            "built_utc": _utc_now(),
            "research_dir": str(self.research_dir),
            "corpus_fingerprint": corpus_fingerprint,
            "pmid_list": {
                "path": scope.get("path"),
                "sha256": scope.get("sha256"),
                "n_pmids": scope.get("n_pmids", 0),
                "scope_mode": scope["mode"],
                "known_sha256": KNOWN_PMID_LIST_SHA256,
                "known_snapshot": copy.deepcopy(scope.get("known_snapshot")),
                "matches_known_snapshot": scope.get("matches_known_snapshot"),
                "n_malformed_lines": scope.get("n_malformed", 0),
                "n_duplicates": scope.get("n_duplicates", 0),
            },
            "sources": source_stats,
            "corpus_manifest": copy.deepcopy(self._manifest),
            "n_records_loaded": len(entries),
            "n_records_indexed": sum(
                1 for entries_for_pmid in self._records_by_pmid.values()
                for _ in entries_for_pmid
            ),
            "n_hits_indexed": len(self._all_hits),
            "n_hits_anchored": len(self._all_hits) - unverified_hits,
            "n_hits_unverified": unverified_hits,
            "n_fields": len(self._field_names),
            "excluded": excluded,
            "coverage": coverage,
            "retractions": self.retractions.as_list(),
            "editorial_notices": self.editorial_notices.as_list(),
            "citation_checks": (
                "frozen retraction/editorial-notice registry only; live PubMed "
                "status was not checked"
            ),
            "policy": copy.deepcopy(POLICY),
            "warnings": warnings,
            "warning_count": warning_count,
            "duplicate_evidence_ids": duplicate_ids,
        }
        self._built = True

    def _load_scope(self, warn) -> dict:
        candidates, explicit = self._pmid_list_candidates()
        existing = [path for path in candidates if path.is_file()]
        if not existing:
            if explicit or not self.allow_missing_pmid_list:
                raise CorpusConfigError(
                    "pmid_list_missing: no readable PMID list at "
                    + ", ".join(str(path) for path in candidates)
                    + (
                        "; pass pmid_list_path explicitly or set "
                        f"{PMID_LIST_ENV}."
                    )
                )
            warn(
                "pmid_list_missing: retrieval is UNFILTERED; the authoritative "
                "405-paper inclusion scope is not applied"
            )
            return {
                "mode": "unfiltered",
                "path": None,
                "sha256": None,
                "n_pmids": 0,
                "pmids_set": frozenset(),
                "known_snapshot": None,
                "matches_known_snapshot": None,
                "n_malformed": 0,
                "n_duplicates": 0,
            }

        path = existing[0]
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise CorpusConfigError(f"pmid_list_unreadable:{path}:{exc}") from exc
        sha256 = _sha256_bytes(data)
        if self.expected_pmid_list_sha256 and sha256 != self.expected_pmid_list_sha256:
            raise CorpusConfigError(
                "pmid_list_hash_mismatch: "
                f"expected {self.expected_pmid_list_sha256}, got {sha256} ({path})"
            )
        if sha256 != KNOWN_PMID_LIST_SHA256:
            snapshot = KNOWN_PMID_LIST_SNAPSHOTS.get(sha256)
            if snapshot is not None and snapshot.get("status") == "superseded":
                warn(
                    "pmid_list_is_superseded_snapshot: "
                    f"{path} sha256={sha256} is {snapshot.get('label')} "
                    f"(superseded {snapshot.get('superseded')} by "
                    f"{snapshot.get('superseded_by')}); retrieval follows the "
                    "configured list, use it only to reproduce historical numbers"
                )
            else:
                warn(
                    "pmid_list_hash_differs_from_known_snapshot: "
                    f"{path} sha256={sha256}"
                )

        pmids: list[str] = []
        seen: set[str] = set()
        malformed = 0
        duplicates = 0
        for line_number, line in enumerate(
            data.decode("utf-8-sig", errors="replace").splitlines(), 1
        ):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            pmid = normalize_pmid(text)
            if pmid is None:
                malformed += 1
                warn(f"pmid_list_malformed_line:{line_number}:{text[:40]}")
                continue
            if pmid in seen:
                duplicates += 1
                continue
            seen.add(pmid)
            pmids.append(pmid)
        if not pmids:
            raise CorpusConfigError(f"pmid_list_empty:{path}")
        return {
            "mode": "included",
            "path": str(path),
            "sha256": sha256,
            "n_pmids": len(pmids),
            "pmids_set": frozenset(pmids),
            "known_snapshot": copy.deepcopy(KNOWN_PMID_LIST_SNAPSHOTS.get(sha256)),
            "matches_known_snapshot": sha256 == KNOWN_PMID_LIST_SHA256,
            "n_malformed": malformed,
            "n_duplicates": duplicates,
        }

    def _pmid_list_candidates(self) -> tuple[list[Path], bool]:
        if self.pmid_list_path is not None:
            return [self.pmid_list_path], True
        env_path = os.environ.get(PMID_LIST_ENV)
        if env_path:
            return [Path(env_path).expanduser()], True
        return list(DEFAULT_PMID_LIST_CANDIDATES), False

    def _display_path(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.research_dir.resolve()))
        except (ValueError, OSError):
            return str(path)

    @staticmethod
    def _record_id(
        spec: SourceSpec,
        record: Mapping[str, Any],
        pmid: str | None,
        row: int,
    ) -> str:
        if spec.kind == "workflow":
            value = record.get("workflow_id")
            if isinstance(value, str) and value.strip():
                return value.strip()
            return f"{pmid or 'unattributed'}:wf{row}"
        if spec.kind == "method":
            value = record.get("instance_id")
            if isinstance(value, str) and value.strip():
                return value.strip()
            return f"{pmid or 'unattributed'}:method:{row}"
        return f"{spec.label}:{pmid or 'unattributed'}:row{row}"

    # -------------------------------------------------------------------- hits

    def _hits_for_record(self, entry: dict[str, Any], start_seq: int) -> list[KnowledgeHit]:
        record = entry["record"]
        kind = entry["spec"].kind
        if kind == "workflow":
            units = _workflow_units(record)
        elif kind == "method":
            units = _method_units(record)
        else:
            units = _pattern_units(record, entry["pmid"])

        context_pairs, context_fields = _record_context(kind, record)
        context_tiers = {name: TIER_EXTRACTION for name, _ in context_pairs}
        hits: list[KnowledgeHit] = []
        seq = start_seq
        for unit in units:
            content_pairs = [
                (name, value)
                for name, value in unit["content"]
                if not _is_empty(value)
            ]
            field_tiers = {
                name: unit["tiers"].get(name, TIER_EXTRACTION)
                for name, _ in content_pairs
            }
            for name, _ in context_pairs:
                field_tiers[name] = context_tiers[name]
            search_fields = tuple(
                (name, _normalize_text(value))
                for name, value in (*content_pairs, *context_pairs)
            )
            evidence_id = self._evidence_id(entry, unit)
            locator = unit.get("locator")
            if isinstance(locator, str):
                locator = locator.strip() or None
            hits.append(
                KnowledgeHit(
                    evidence_id=evidence_id,
                    source_kind=kind,
                    source_file=entry["source_file"],
                    source_label=entry["spec"].label,
                    source_sha256=entry["source_sha256"],
                    pmid=entry["pmid"],
                    record_id=entry["record_id"],
                    record_uid=entry["record_uid"],
                    granularity=unit["granularity"],
                    locator=locator,
                    locator_type=_locator_type(locator),
                    verbatim=(
                        unit["verbatim"].strip()
                        if isinstance(unit.get("verbatim"), str)
                        and unit["verbatim"].strip()
                        else None
                    ),
                    content=tuple(content_pairs),
                    record_context=tuple(context_pairs),
                    field_tiers=tuple(field_tiers.items()),
                    context_fields=tuple(context_fields),
                    in_scope=entry["in_scope"],
                    seq=seq,
                    unit_index=unit.get("unit_index", 0),
                    search_fields=search_fields,
                )
            )
            seq += 1
        return hits

    @staticmethod
    def _evidence_id(entry: dict[str, Any], unit: dict[str, Any]) -> str:
        kind = entry["spec"].kind
        record_id = entry["record_id"]
        suffix = unit["suffix"]
        if kind == "method":
            return f"method:{record_id}"
        return f"{kind}:{record_id}:{suffix}"

    # ------------------------------------------------------------------ search

    def search(
        self,
        query: str | None = None,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
        pmids: str | Iterable[str] | None = None,
        source_kinds: str | Iterable[str] | None = None,
        sections: str | Iterable[str] | None = None,
        granularities: str | Iterable[str] | None = None,
        tiers: str | Iterable[str] | None = None,
        fields: str | Iterable[str] | None = None,
        pipeline_stages: str | Iterable[str] | None = None,
        purposes: str | Iterable[str] | None = None,
        method_names: str | Iterable[str] | None = None,
        method_classes: str | Iterable[str] | None = None,
        in_scope: bool | None = None,
        evidence_types: str | Iterable[str] | None = None,
        directions: str | Iterable[str] | None = None,
        rationale_sources: str | Iterable[str] | None = None,
        include_unverified: bool = False,
        match: str = "all",
    ) -> dict:
        """Search fine-grained evidence units.

        ``match="all"`` requires every query term somewhere in the hit;
        ``match="any"`` requires at least one. Quotes keep a phrase together.
        Results are ordered by PMID then source order, never by relevance or
        corpus frequency. Pagination is explicit: ``page`` reports ``total``,
        ``has_more`` and ``next_offset``.
        """
        limit, offset = _page_bounds(limit, offset)
        if query is not None and not isinstance(query, str):
            raise QueryError("query must be a string or None")
        if match not in ("all", "any"):
            raise QueryError("match must be 'all' or 'any'")
        if in_scope is not None and not isinstance(in_scope, bool):
            raise QueryError("in_scope must be a bool or None")
        terms = _parse_query_terms(query) if query else []
        if query and query.strip() and not terms:
            raise QueryError("query has no searchable terms")
        term_matchers = [_term_matcher(term) for term in terms]

        self._ensure_built()

        pmid_filter = None
        if pmids is not None:
            if isinstance(pmids, str) or (
                isinstance(pmids, int) and not isinstance(pmids, bool)
            ):
                raw_pmids = [pmids]
            else:
                try:
                    raw_pmids = list(pmids)
                except TypeError as exc:
                    raise QueryError("pmids must be a PMID or an iterable of PMIDs") from exc
            pmid_filter = set()
            for item in raw_pmids:
                normalized = normalize_pmid(item)
                if normalized is None:
                    raise QueryError(f"invalid PMID filter: {item!r}")
                pmid_filter.add(normalized)

        source_filter = _choice_set(source_kinds)
        section_filter = _choice_set(sections)
        granularity_filter = _choice_set(granularities)
        tier_filter = _choice_set(tiers)
        field_filter = _choice_set(fields)
        stage_filter = _choice_set(pipeline_stages)
        purpose_filter = _choice_set(purposes)
        method_name_filter = _choice_set(method_names)
        method_class_filter = _choice_set(method_classes)
        evidence_type_filter = _choice_set(evidence_types)
        direction_filter = _choice_set(directions)
        rationale_filter = _choice_set(rationale_sources)

        _validate_choice("source_kinds", source_filter, SOURCE_KINDS)
        _validate_choice("sections", section_filter, LOCATOR_SECTIONS)
        _validate_choice("granularities", granularity_filter, GRANULARITIES)
        _validate_choice("tiers", tier_filter, EVIDENCE_TIERS)
        if field_filter is not None:
            unknown = sorted(field_filter - self._field_names)
            if unknown:
                available = ", ".join(sorted(self._field_names)[:30])
                raise QueryError(
                    f"unknown field(s): {unknown}; available include: {available}"
                )
        if tier_filter and TIER_UNVERIFIED in tier_filter:
            include_unverified = True

        rows: list[tuple[KnowledgeHit, str, list[str], str]] = []
        for hit in self._all_hits:
            if hit.pmid in self.retractions:
                # Applies even if the notice was added after the index was
                # built; the registry is authoritative at query time.
                continue
            if (
                not include_unverified
                and not hit.is_anchored
            ):
                continue
            if pmid_filter is not None and hit.pmid not in pmid_filter:
                continue
            if source_filter is not None and hit.source_kind not in source_filter:
                continue
            if (
                section_filter is not None
                and locator_section(hit.locator) not in section_filter
            ):
                continue
            if (
                granularity_filter is not None
                and hit.granularity not in granularity_filter
            ):
                continue
            if in_scope is not None and hit.in_scope is not in_scope:
                continue
            if stage_filter is not None and not (
                _values_for(hit, "pipeline_stage") & stage_filter
            ):
                continue
            if purpose_filter is not None and not (
                _values_for(hit, "purpose") & purpose_filter
            ):
                continue
            if method_class_filter is not None and not (
                _values_for(hit, "method_class") & method_class_filter
            ):
                continue
            if method_name_filter is not None:
                names = (
                    _values_for(hit, "canonical_name")
                    | _values_for(hit, "mentioned_name")
                    | _values_for(hit, "package")
                )
                if not (names & method_name_filter):
                    continue
            if evidence_type_filter is not None and not (
                _values_for(hit, "evidence_type") & evidence_type_filter
            ):
                continue
            if direction_filter is not None and not (
                _values_for(hit, "direction") & direction_filter
            ):
                continue
            if rationale_filter is not None and not (
                _values_for(hit, "rationale_source") & rationale_filter
            ):
                continue

            considered = hit.search_fields
            if field_filter is not None:
                considered = tuple(
                    (name, text)
                    for name, text in considered
                    if name in field_filter
                )
            if not considered:
                continue
            if terms:
                blob = "\n".join(text for _, text in considered)
                if match == "all":
                    if not all(
                        matcher.search(blob) for matcher in term_matchers
                    ):
                        continue
                elif not any(
                    matcher.search(blob) for matcher in term_matchers
                ):
                    continue
                coverage = [
                    (
                        name,
                        sum(1 for matcher in term_matchers if matcher.search(text)),
                    )
                    for name, text in considered
                ]
                best_coverage = max(count for _, count in coverage)
                matched_fields = [
                    name
                    for name, count in coverage
                    if count == best_coverage and count > 0
                ]
            else:
                best_coverage = 0
                matched_fields = []

            tiers_map = hit.tiers
            if matched_fields:
                tier = _best_tier(
                    tiers_map.get(name, TIER_UNVERIFIED)
                    for name in matched_fields
                )
                # A quote that covers only part of an AND query must not be
                # presented as verbatim support for the whole query.
                if (
                    match == "all"
                    and best_coverage < len(terms)
                    and tier == TIER_VERBATIM
                ):
                    tier = TIER_EXTRACTION
            else:
                tier = _best_tier(tiers_map.values())
            if not hit.is_anchored:
                tier = TIER_UNVERIFIED
                tier_scope = "anchor"
            elif terms:
                tier_scope = "query"
            else:
                tier_scope = "unit"
            if tier_filter is not None and tier not in tier_filter:
                continue
            rows.append((hit, tier, matched_fields, tier_scope))

        rows.sort(key=lambda row: (row[0].pmid, row[0].seq))
        total = len(rows)
        window = rows[offset : offset + limit]

        filters_echo: dict[str, Any] = {}
        for name, value in (
            ("pmids", sorted(pmid_filter) if pmid_filter is not None else None),
            ("source_kinds", sorted(source_filter) if source_filter else None),
            ("sections", sorted(section_filter) if section_filter else None),
            (
                "granularities",
                sorted(granularity_filter) if granularity_filter else None,
            ),
            ("tiers", sorted(tier_filter) if tier_filter else None),
            ("fields", sorted(field_filter) if field_filter else None),
            (
                "pipeline_stages",
                sorted(stage_filter) if stage_filter else None,
            ),
            ("purposes", sorted(purpose_filter) if purpose_filter else None),
            (
                "method_names",
                sorted(method_name_filter) if method_name_filter else None,
            ),
            (
                "method_classes",
                sorted(method_class_filter) if method_class_filter else None,
            ),
            ("in_scope", in_scope),
            (
                "evidence_types",
                sorted(evidence_type_filter) if evidence_type_filter else None,
            ),
            (
                "directions",
                sorted(direction_filter) if direction_filter else None,
            ),
            (
                "rationale_sources",
                sorted(rationale_filter) if rationale_filter else None,
            ),
            ("include_unverified", include_unverified),
        ):
            if value is not None:
                filters_echo[name] = value

        return {
            "status": "ok",
            "retrieved_utc": _utc_now(),
            "query": {
                "text": query,
                "match": match,
                "terms": terms,
                "filters": filters_echo,
                "ordering": "deterministic: pmid, then source/record order; no ranking",
            },
            "page": _page_payload(total, offset, len(window), limit),
            "hits": [
                hit.as_payload(
                    tier=tier,
                    matched_fields=matched,
                    tier_scope=tier_scope,
                    editorial_notices=self.editorial_notices.for_pmid(hit.pmid),
                )
                for hit, tier, matched, tier_scope in window
            ],
            "corpus": self._corpus_metadata(),
            "warnings": list(self._stats.get("warnings") or ()),
            "warning_count": self._stats.get("warning_count", 0),
            "policy": copy.deepcopy(POLICY),
        }

    # ------------------------------------------------------------ direct access

    def get(
        self,
        evidence_id: str,
        *,
        include_unverified: bool = False,
        matched_fields: str | Iterable[str] | None = None,
    ) -> dict:
        """Return one evidence unit by its stable ``evidence_id``.

        ``tier`` is null unless ``matched_fields`` is supplied: without the
        field(s) that made the unit relevant to a query, there is no defensible
        single tier. A read never upgrades an inference hit to verbatim.
        """
        self._ensure_built()
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            raise QueryError("evidence_id must be a non-empty string")
        fields = _matched_field_list(matched_fields)
        hit = self._hits_by_id.get(evidence_id)
        if hit is None:
            return {
                "status": "not_found",
                "evidence_id": evidence_id,
                "note": (
                    "not indexed: retracted, out of scope, unattributed, or absent"
                ),
            }
        notice = self.retractions.get(hit.pmid)
        if notice is not None:
            return {
                "status": "excluded",
                "reason": "retracted",
                "evidence_id": evidence_id,
                "pmid": hit.pmid,
                "retraction": notice.as_dict(),
            }
        if (
            not include_unverified
            and not hit.is_anchored
        ):
            return {
                "status": "unverified_hidden",
                "evidence_id": evidence_id,
                "tier": TIER_UNVERIFIED,
                "note": "pass include_unverified=True to inspect the unanchored record",
            }
        tier, tier_scope = _scoped_tier(hit, fields)
        return {
            "status": "ok",
            "retrieved_utc": _utc_now(),
            "hit": hit.as_payload(
                tier=tier,
                matched_fields=fields or (),
                tier_scope=tier_scope,
                editorial_notices=self.editorial_notices.for_pmid(hit.pmid),
            ),
            "record_uid": hit.record_uid,
        }

    def context(
        self,
        evidence_id: str,
        *,
        before: int = 1,
        after: int = 1,
        include_unverified: bool = False,
        matched_fields: str | Iterable[str] | None = None,
    ) -> dict:
        """Return neighbouring units from the same frozen record.

        As with :meth:`get`, ``tier`` is null unless ``matched_fields`` is
        supplied; neighbours are never ranked by a best-tier shortcut.
        """
        self._ensure_built()
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            raise QueryError("evidence_id must be a non-empty string")
        fields = _matched_field_list(matched_fields)
        for name, value in (("before", before), ("after", after)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise QueryError(f"{name} must be an integer")
            if value < 0 or value > MAX_CONTEXT:
                raise QueryError(f"{name} must be between 0 and {MAX_CONTEXT}")
        hit = self._hits_by_id.get(evidence_id)
        if hit is None:
            return {"status": "not_found", "evidence_id": evidence_id}
        notice = self.retractions.get(hit.pmid)
        if notice is not None:
            return {
                "status": "excluded",
                "reason": "retracted",
                "evidence_id": evidence_id,
                "pmid": hit.pmid,
                "retraction": notice.as_dict(),
            }
        if (
            not include_unverified
            and not hit.is_anchored
        ):
            return {
                "status": "unverified_hidden",
                "evidence_id": evidence_id,
                "tier": TIER_UNVERIFIED,
            }
        siblings = self._hits_by_record.get(hit.record_uid, [])
        siblings = [
            sibling
            for sibling in siblings
            if sibling.pmid not in self.retractions
        ]
        if not include_unverified:
            siblings = [
                sibling for sibling in siblings if sibling.is_anchored
            ]
        try:
            center_index = next(
                index
                for index, sibling in enumerate(siblings)
                if sibling.evidence_id == evidence_id
            )
        except StopIteration:
            return {"status": "not_found", "evidence_id": evidence_id}
        start = max(0, center_index - before)
        end = min(len(siblings), center_index + after + 1)
        window = siblings[start:end]
        payloads = []
        for sibling in window:
            tier, tier_scope = _scoped_tier(sibling, fields)
            payloads.append(
                sibling.as_payload(
                    tier=tier,
                    matched_fields=fields or (),
                    tier_scope=tier_scope,
                    editorial_notices=self.editorial_notices.for_pmid(
                        sibling.pmid
                    ),
                )
            )
        return {
            "status": "ok",
            "retrieved_utc": _utc_now(),
            "center": evidence_id,
            "record_uid": hit.record_uid,
            "window": {"before": before, "after": after},
            "returned": len(window),
            "hits": payloads,
            "note": "neighbours come from the same frozen extract, not from the paper",
        }

    def record(
        self,
        pmid: str,
        *,
        record_id: str | None = None,
        source_kind: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
        include_author_focus: bool = False,
    ) -> dict:
        """Return the full frozen extract record(s) for one PMID.

        This is the extracted record, not the paper full text. ``author_focus_groups``
        is redacted by default; ``include_author_focus=True`` is for audits only.
        Retracted and out-of-scope PMIDs return no content.
        """
        limit, offset = _page_bounds(limit, offset)
        normalized = normalize_pmid(pmid)
        if normalized is None:
            raise QueryError(f"invalid PMID: {pmid!r}")
        if source_kind is not None:
            source_kind = str(source_kind).casefold()
        if source_kind is not None and source_kind not in SOURCE_KINDS:
            raise QueryError(
                f"unknown source_kind: {source_kind!r}; allowed {list(SOURCE_KINDS)}"
            )
        self._ensure_built()

        notices = list(self.editorial_notices.for_pmid(normalized))
        correction_status = (
            "recorded_impact_unknown" if notices else "unknown"
        )
        notice_payload = [notice.as_dict() for notice in notices]
        notice = self.retractions.get(normalized)
        if notice is not None:
            return {
                "status": "excluded",
                "reason": "retracted",
                "pmid": normalized,
                "citation_status": "retracted",
                "retraction_status": "retracted",
                "live_retraction_check": False,
                "live_correction_check": False,
                "correction_status": correction_status,
                "editorial_notices": notice_payload,
                "retraction": notice.as_dict(),
                "records": [],
                "page": _page_payload(0, offset, 0, limit),
                "note": "retracted papers are hard-excluded from retrieval",
            }
        if self._scope.get("mode") == "included":
            if (
                normalized not in self._scope.get("pmids_set", frozenset())
                and not self.include_out_of_scope
            ):
                return {
                    "status": "out_of_scope",
                    "reason": "not in the authoritative PMID list",
                    "pmid": normalized,
                    "records": [],
                    "page": _page_payload(0, offset, 0, limit),
                }

        entries = list(self._records_by_pmid.get(normalized) or ())
        if record_id is not None:
            entries = [
                entry for entry in entries if entry["record_id"] == record_id
            ]
        if source_kind is not None:
            entries = [
                entry
                for entry in entries
                if entry["spec"].kind == source_kind
            ]
        entries.sort(key=lambda entry: (entry["spec_index"], entry["row"]))
        if not entries:
            return {
                "status": "not_found",
                "pmid": normalized,
                "record_id": record_id,
                "records": [],
                "page": _page_payload(0, offset, 0, limit),
                "note": "PMIDs in the list may have no extract in these sources",
            }

        total = len(entries)
        window = entries[offset : offset + limit]
        records = []
        for entry in window:
            hits = self._hits_by_record.get(entry["record_uid"], [])
            records.append(
                {
                    "source_kind": entry["spec"].kind,
                    "source_label": entry["spec"].label,
                    "source_file": entry["source_file"],
                    "source_sha256": entry["source_sha256"],
                    "record_id": entry["record_id"],
                    "record_uid": entry["record_uid"],
                    "evidence_ids": [hit.evidence_id for hit in hits],
                    "record": _sanitize(entry["record"], include_author_focus),
                    "author_focus_included": bool(include_author_focus),
                }
            )
        return {
            "status": "ok",
            "retrieved_utc": _utc_now(),
            "pmid": normalized,
            "citation_status": "unknown",
            "retraction_status": "not_excluded_by_frozen_registry",
            "live_retraction_check": False,
            "live_correction_check": False,
            "correction_status": correction_status,
            "editorial_notices": notice_payload,
            "page": _page_payload(total, offset, len(window), limit),
            "records": records,
            "note": (
                "frozen extraction record, not the paper full text; "
                "unanchored pattern fields are not evidence; "
                "citation status is unknown until a live PubMed check is performed; "
                "author focus is context, never candidate evidence"
            ),
        }

    # ---------------------------------------------------------------- reporting

    def stats(self) -> dict:
        self._ensure_built()
        stats = copy.deepcopy(self._stats)
        # The registries can grow after the index was built; report them live.
        stats["retractions"] = self.retractions.as_list()
        stats["editorial_notices"] = self.editorial_notices.as_list()
        return stats

    def _corpus_metadata(self) -> dict:
        return {
            "research_dir": self._stats["research_dir"],
            "pmid_list": copy.deepcopy(self._stats["pmid_list"]),
            "sources": copy.deepcopy(self._stats["sources"]),
            "corpus_fingerprint": self._stats["corpus_fingerprint"],
            "built_utc": self._stats["built_utc"],
            "n_records_indexed": self._stats["n_records_indexed"],
            "n_hits_indexed": self._stats["n_hits_indexed"],
            "coverage": copy.deepcopy(self._stats["coverage"]),
        }


# ----------------------------------------------------------------- record units


def _record_context(kind: str, record: Mapping[str, Any]) -> tuple[list[tuple[str, Any]], list[str]]:
    pairs: list[tuple[str, Any]] = []
    if kind == "workflow":
        question = record.get("question")
        if isinstance(question, str) and not _is_empty(question):
            pairs.append(("record_question", question))
        elif isinstance(question, Mapping) and not _is_empty(question.get("text")):
            pairs.append(("record_question", question.get("text")))
        for key in ("comparison", "cell_level"):
            if not _is_empty(record.get(key)):
                pairs.append((key, record.get(key)))
    elif kind == "pattern":
        if not _is_empty(record.get("question")):
            pairs.append(("record_question", record.get("question")))
        if not _is_empty(record.get("citation")):
            pairs.append(("citation", record.get("citation")))
    return pairs, [name for name, _ in pairs]


def _workflow_units(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    question = record.get("question")
    question = question if isinstance(question, Mapping) else {}
    evidence = question.get("evidence")
    if isinstance(evidence, list):
        for index, item in enumerate(evidence):
            if not isinstance(item, Mapping):
                continue
            units.append(
                {
                    "suffix": f"question:{index}",
                    "granularity": "question_evidence",
                    "unit_index": index,
                    "locator": item.get("locator"),
                    "verbatim": item.get("verbatim"),
                    "content": [("verbatim", item.get("verbatim"))],
                    "tiers": {"verbatim": TIER_VERBATIM},
                }
            )

    for index, step in enumerate(
        record.get("steps") if isinstance(record.get("steps"), list) else []
    ):
        if not isinstance(step, Mapping):
            continue
        units.append(
            {
                "suffix": f"step:{index}",
                "granularity": "step",
                "unit_index": index,
                "locator": step.get("locator"),
                "verbatim": step.get("verbatim"),
                "content": [
                    ("observation", step.get("observation")),
                    ("verbatim", step.get("verbatim")),
                    ("next_analysis", step.get("next_analysis")),
                    ("rationale", step.get("rationale")),
                ],
                "tiers": {
                    "observation": TIER_EXTRACTION,
                    "verbatim": TIER_VERBATIM,
                    "next_analysis": TIER_EXTRACTION,
                    "rationale": TIER_EXTRACTION,
                },
            }
        )

    for index, claim in enumerate(
        record.get("claims") if isinstance(record.get("claims"), list) else []
    ):
        if not isinstance(claim, Mapping):
            continue
        units.append(
            {
                "suffix": f"claim:{index}",
                "granularity": "claim",
                "unit_index": index,
                "locator": claim.get("locator"),
                "verbatim": claim.get("verbatim"),
                "content": [
                    ("claim", claim.get("claim")),
                    ("verbatim", claim.get("verbatim")),
                    ("evidence_type", claim.get("evidence_type")),
                    ("direction", claim.get("direction")),
                ],
                "tiers": {
                    "claim": TIER_EXTRACTION,
                    "verbatim": TIER_VERBATIM,
                    "evidence_type": TIER_EXTRACTION,
                    "direction": TIER_EXTRACTION,
                },
            }
        )

    for index, gap in enumerate(
        record.get("gaps") if isinstance(record.get("gaps"), list) else []
    ):
        if isinstance(gap, Mapping):
            text = gap.get("text")
            locator = gap.get("locator")
        elif isinstance(gap, str):
            text = gap
            locator = None
        else:
            continue
        units.append(
            {
                "suffix": f"gap:{index}",
                "granularity": "gap",
                "unit_index": index,
                "locator": locator,
                "verbatim": None,
                "content": [("gap", text)],
                "tiers": {"gap": TIER_EXTRACTION},
            }
        )

    if not units:
        units.append(
            {
                "suffix": "record",
                "granularity": "record_context",
                "unit_index": 0,
                "locator": None,
                "verbatim": None,
                "content": [],
                "tiers": {},
            }
        )
    return units


def _method_units(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    keys = (
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
    )
    content = [(key, record.get(key)) for key in keys if key in record]
    tiers = {key: TIER_EXTRACTION for key, _ in content}
    tiers["verbatim"] = TIER_VERBATIM
    content.append(("verbatim", record.get("verbatim")))
    return [
        {
            "suffix": "instance",
            "granularity": "method_instance",
            "unit_index": 0,
            "locator": record.get("locator"),
            "verbatim": record.get("verbatim"),
            "content": content,
            "tiers": tiers,
        }
    ]


def _pattern_units(record: Mapping[str, Any], pmid: str | None) -> list[dict[str, Any]]:
    patterns = record.get("patterns")
    if not isinstance(patterns, list):
        patterns = []
    units: list[dict[str, Any]] = []
    for index, pattern in enumerate(patterns):
        if not isinstance(pattern, Mapping):
            continue
        locator = pattern.get("source_locator")
        verbatim = pattern.get("verbatim")
        pdf_quote = pattern.get("pdf_quote")
        pdf_page = pattern.get("pdf_page")
        locator_ok = isinstance(locator, str) and bool(locator.strip())
        verbatim_ok = isinstance(verbatim, str) and bool(verbatim.strip())
        if not (locator_ok and verbatim_ok) and pdf_quote:
            if locator_ok and _locator_type(locator) == "pdf":
                verbatim = pdf_quote
            else:
                pdf_locator = make_pdf_locator(pmid, pdf_page)
                if pdf_locator:
                    locator = pdf_locator
                    verbatim = pdf_quote
        content = [
            ("observation", pattern.get("observation"), TIER_EXTRACTION),
            ("ask_next", pattern.get("ask_next"), TIER_EXTRACTION),
            ("limitations", pattern.get("limitations"), TIER_EXTRACTION),
            ("prompt_lesson", pattern.get("prompt_lesson"), TIER_DESIGN),
            ("rationale_source", pattern.get("rationale_source"), TIER_EXTRACTION),
            ("provenance", pattern.get("provenance"), TIER_EXTRACTION),
            ("anchor_type", pattern.get("anchor_type"), TIER_EXTRACTION),
            ("verbatim", verbatim, TIER_VERBATIM),
        ]
        if pdf_quote and pdf_quote != verbatim:
            content.append(("pdf_quote", pdf_quote, TIER_VERBATIM))
        for key, tier in (
            ("pdf_page", TIER_EXTRACTION),
            ("pdf_journal_page", TIER_EXTRACTION),
            ("extra_locators", TIER_EXTRACTION),
            ("pdf_quote_note", TIER_EXTRACTION),
            ("anchor_note", TIER_EXTRACTION),
            ("verbatim_unique_in_brief", TIER_EXTRACTION),
            ("verbatim_occurrences", TIER_EXTRACTION),
        ):
            content.append((key, pattern.get(key), tier))
        units.append(
            {
                "suffix": str(index),
                "granularity": "pattern",
                "unit_index": index,
                "locator": locator,
                "verbatim": verbatim,
                "content": [(name, value) for name, value, _ in content],
                "tiers": {name: tier for name, _, tier in content},
            }
        )
    if not units:
        units.append(
            {
                "suffix": "record",
                "granularity": "record_context",
                "unit_index": 0,
                "locator": None,
                "verbatim": None,
                "content": [],
                "tiers": {},
            }
        )
    return units


# ------------------------------------------------------------------ source setup


def corpus_dir(research_dir: str | Path | None = None) -> Path:
    """Return ``<research>/corpus``; never globs, never falls back to shards."""
    base = Path(
        research_dir if research_dir is not None else DEFAULT_RESEARCH_DIR
    ).expanduser()
    return base / CORPUS_SUBDIR


def _infer_source_kind(path: Path) -> str:
    name = path.name.casefold()
    if name.startswith("workflows"):
        return "workflow"
    if name.startswith("methods"):
        return "method"
    return "pattern"


def _default_source_specs(research_dir: Path) -> list[SourceSpec]:
    """The three formal corpus files, in a fixed order.

    Labels are the file stems (``workflows``/``methods``/``patterns``) so that a
    hit's ``source_label`` names the shipped file. Missing files are reported as
    missing sources; no fallback scans the shard directories.
    """
    root = corpus_dir(research_dir)
    return [
        SourceSpec(kind, root / name, label)
        for kind, name, label in CORPUS_FILES
    ]


def _manifest_entry_for(
    manifest: Mapping[str, Any], research_dir: Path, spec: SourceSpec
) -> dict | None:
    """Manifest row for one source, only when it names *this* directory.

    Matching by file name alone would let a test corpus called
    ``patterns.jsonl`` inherit the shipped snapshot's hash and be reported as
    tampered with. The parent directory must be the corpus directory too.
    """
    files = manifest.get("files") or {}
    if not isinstance(files, Mapping):
        return None
    try:
        in_corpus = (
            spec.path.resolve().parent
            == corpus_dir(research_dir).resolve()
        )
    except OSError:
        return None
    if not in_corpus:
        return None
    entry = files.get(spec.path.name)
    return dict(entry) if isinstance(entry, Mapping) else None


def _read_corpus_manifest(
    research_dir: Path, specs: Sequence[SourceSpec]
) -> dict[str, Any]:
    """Read ``<research>/corpus/manifest.json`` and relate it to the sources.

    The manifest is provenance, not a gate: an absent or unreadable manifest is
    reported (``status`` other than ``ok``) and the index is still built from
    the files it can read. When a listed file is actually indexed, its sha256
    and record count are compared against the manifest; the comparison is
    reported per source and by warning, never by silently dropping the source.
    """
    path = corpus_dir(research_dir) / CORPUS_MANIFEST_NAME
    out: dict[str, Any] = {
        "status": "absent",
        "path": str(path),
        "schema": None,
        "generated_utc": None,
        "files": {},
        "patterns": None,
        "pmid_range": None,
        "n_sources_covered": 0,
        "error": None,
    }
    if not path.is_file():
        return out
    try:
        data = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError) as exc:
        out["status"] = "unreadable"
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    if not isinstance(data, dict):
        out["status"] = "unreadable"
        out["error"] = f"manifest is {type(data).__name__}, not an object"
        return out
    declared = data.get("files")
    files = {
        str(name): dict(entry)
        for name, entry in (declared.items() if isinstance(declared, dict) else ())
        if isinstance(entry, dict)
    }
    patterns = data.get("patterns")
    out.update(
        {
            "status": "ok",
            "schema": data.get("schema"),
            "generated_utc": data.get("generated_utc"),
            "files": files,
            "patterns": dict(patterns) if isinstance(patterns, dict) else None,
            "pmid_range": (
                dict(data["pmid_range"])
                if isinstance(data.get("pmid_range"), dict) else None
            ),
        }
    )
    out["n_sources_covered"] = sum(
        1 for spec in specs
        if _manifest_entry_for(out, research_dir, spec) is not None
    )
    if out["schema"] != CORPUS_SCHEMA:
        out["status"] = "schema_mismatch"
        out["error"] = (
            f"manifest schema is {out['schema']!r}, expected {CORPUS_SCHEMA!r}"
        )
    return out


def _values_for(hit: KnowledgeHit, key: str) -> set[str]:
    content = dict(hit.content)
    value = content.get(key)
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        return {str(item).casefold() for item in value}
    if isinstance(value, dict):
        return {str(item).casefold() for item in value.values()}
    return {str(value).casefold()}


# ----------------------------------------------------------------- default index


_DEFAULT_INDEX: ScopeKnowledge | None = None
_DEFAULT_LOCK = threading.Lock()


def default_index() -> ScopeKnowledge:
    """Return a process-wide index configured from the environment defaults."""
    global _DEFAULT_INDEX
    with _DEFAULT_LOCK:
        if _DEFAULT_INDEX is None:
            _DEFAULT_INDEX = ScopeKnowledge()
    return _DEFAULT_INDEX


def search_corpus(query: str | None = None, **kwargs: Any) -> dict:
    """Convenience wrapper around ``default_index().search`` for tool registration."""
    return default_index().search(query, **kwargs)
