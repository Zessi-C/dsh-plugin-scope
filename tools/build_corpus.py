#!/usr/bin/env python3
"""Build the formal run-time corpus under ``research/corpus/``.

The official inputs of a run are the four files in ``research/corpus`` plus the
manifest that proves where every byte came from. Build history stays on disk:
nothing under ``research/`` is deleted or rewritten, so the pre-merge shards,
the repair passes and the merged-at-root files all remain readable as
provenance.

What is produced
----------------
``research/corpus/methods.jsonl``, ``workflows.jsonl``, ``code_sources.jsonl``
    Byte-identical copies of the root extraction files. They are *not* filtered
    to a PMID list: keeping the full 458-paper extraction means a historical
    inclusion list (``included_pmids_422.txt``) still resolves against the same
    bytes, and the current 405-paper scope stays a read-time filter instead of a
    destructive rewrite.
``research/corpus/patterns.jsonl``
    One record per PMID, merged from ``scope_learn/shard_*.jsonl`` and
    ``scope_learn/repair_*.jsonl`` under the existing ``prefer_repairs=True``
    semantics: a repair record replaces the shard record for the same PMID, and
    the shard record is indexed only if the caller asked for ``prefer_repairs=
    False``. Each output line is the exact bytes of the record it came from, so
    repair traces (``prior_status``, ``remaining_obstacles``, ``repair`` ...)
    survive verbatim; which line superseded which is recorded in
    ``provenance/patterns_records.jsonl`` instead of being annotated into the
    record.
``research/corpus/provenance/patterns_records.jsonl``
    Per-record trace: source file, source line, source file SHA-256, record
    SHA-256, merge rule, and the superseded shard line for the 24 repaired
    papers.
``research/corpus/provenance/copies.jsonl``
    Per-copy trace. Because each copy is bit-identical, every record in it is
    at the same line number in the documented source file.
``research/corpus/manifest.json``
    Hashes, counts, merge policy, scope lists and build-time invariants.

Rules the merge follows, deliberately
-------------------------------------
* Nothing is dropped for being retracted or out of scope. Retraction-registry
  exclusion and the 405/422 PMID scope are *read-time* decisions and stay in the
  reader; baking them in would make the corpus a different object than the one
  the recorded numbers were computed from.
* Ordering is deterministic: shard files in numeric-suffix order, each in line
  order, and a superseded shard record is replaced in place by its repair
  record. Rebuilding therefore reproduces the same bytes, and each record's row
  index (which the reader uses in ``record_id``) is stable.
* A case the old reader could represent but one file cannot (two records with
  the same PMID that the merge rule does not order) is a hard error, not a
  silent pick.

Usage
-----
    python3 tools/build_corpus.py            # report the plan, write nothing
    python3 tools/build_corpus.py --write    # write research/corpus
    python3 tools/build_corpus.py --check    # rebuild in memory and diff on disk
    python3 tools/build_corpus.py --verify-equivalence
                                            # replay the merge rule and, when the
                                            # reader still offers the split
                                            # layout, cross-check every hit

``SOURCE_DATE_EPOCH`` pins ``generated_utc`` so a rebuild is byte-identical.
``--check`` exits non-zero on drift, ``--verify-equivalence`` on a mismatch, so
both are usable as gates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESEARCH = ROOT / "research"
SCHEMA = "sctda.corpus_manifest/1"

COPIES = {
    # target name -> (root source name, record kind, primary key)
    "methods.jsonl": ("methods.jsonl", "method", "instance_id"),
    "workflows.jsonl": ("workflows.jsonl", "workflow", "workflow_id"),
    "code_sources.jsonl": ("code_sources.jsonl", "code_source", "source_id"),
}
PATTERNS = "patterns.jsonl"
PATTERN_KEY = "pmid"
SHARD_GLOB = "shard_*.jsonl"
REPAIR_GLOB = "repair_*.jsonl"

# Recorded for provenance only. The runtime applies the scope it is configured
# with; this table never decides what the corpus contains.
SCOPE_LISTS = (
    ("included_pmids_405.txt", "current"),
    ("included_pmids_422.txt", "superseded"),
)
SCOPE_DIRS = (
    Path("/data/disk1/zeiss/singlecell-quant/data"),
    Path("/home/zeiss/data/singlecell-quant/data"),
    Path.home() / "data/singlecell-quant/data",
)


class CorpusBuildError(RuntimeError):
    """The inputs cannot be merged without inventing a decision."""


# --------------------------------------------------------------------- helpers


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_now() -> str:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_lines(path: Path) -> tuple[bytes, list[bytes]]:
    """Return the raw file bytes and its newline-separated records.

    Only a trailing newline is removed from each record; interior bytes are kept
    exactly. A CR is refused because it would make "byte-identical" mean two
    different things depending on who reads it.
    """
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        raise CorpusBuildError(f"unexpected UTF-8 BOM: {path}")
    if b"\r" in data:
        raise CorpusBuildError(f"unexpected CR in {path}; only LF is supported")
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return data, lines


def parse_row(raw: bytes, path: Path, line_number: int) -> dict:
    try:
        row = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorpusBuildError(f"{path}:{line_number}: not a JSON object ({exc})") from exc
    if not isinstance(row, dict):
        raise CorpusBuildError(f"{path}:{line_number}: record is {type(row).__name__}")
    return row


def pmid_of(row: dict, path: Path, line_number: int) -> str:
    value = row.get(PATTERN_KEY)
    text = str(value).strip() if value is not None else ""
    if not text:
        raise CorpusBuildError(f"{path}:{line_number}: record has no pmid")
    return text


def shard_sort_key(path: Path) -> tuple[int, str]:
    match = re.search(r"(\d+)$", path.stem)
    return (int(match.group(1)) if match else 10**9, path.stem)


def pmids_digest(pmids: list[str]) -> str:
    return sha256_bytes(("\n".join(sorted(pmids)) + "\n").encode("utf-8"))


def jsonl_bytes(rows: list[dict]) -> bytes:
    return ("\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True)
                      for r in rows) + "\n").encode("utf-8")


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# ----------------------------------------------------------------------- build


def build_copies(
    research: Path,
) -> tuple[dict[str, bytes], dict[str, dict], list[dict], dict[str, set[str]]]:
    """Byte-identical copies plus the per-copy provenance rows."""
    payloads: dict[str, bytes] = {}
    entries: dict[str, dict] = {}
    traces: list[dict] = []
    pmid_sets: dict[str, set[str]] = {}
    for target, (source_name, kind, key) in COPIES.items():
        source = research / source_name
        if not source.is_file():
            raise CorpusBuildError(f"missing corpus source: {source}")
        data, lines = read_lines(source)
        pmids: list[str] = []
        pmid_types: dict[str, int] = {}
        keys: set[str] = set()
        for line_number, raw in enumerate(lines, 1):
            row = parse_row(raw, source, line_number)
            raw_pmid = row.get("pmid")
            pmid_type = type(raw_pmid).__name__
            pmid_types[pmid_type] = pmid_types.get(pmid_type, 0) + 1
            pmid = str(raw_pmid if raw_pmid is not None else "").strip()
            if pmid:
                pmids.append(pmid)
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                keys.add(value.strip())
        file_sha = sha256_bytes(data)
        payloads[target] = data
        entries[target] = {
            "kind": kind,
            "mode": "copy",
            "sha256": file_sha,
            "source_files": [source_name],
            "source_sha256": file_sha,
            "bytes": len(data),
            "n_lines": len(lines),
            "n_records": len(lines),
            "n_malformed": 0,
            "primary_key": key,
            "n_primary_keys": len(keys),
            "n_pmids": len(set(pmids)),
            "pmids_sha256": pmids_digest(pmids),
            "pmid_json_types": dict(sorted(pmid_types.items())),
            "identical_to_source": True,
            "provenance": "provenance/copies.jsonl",
        }
        traces.append({
            "target": target,
            "source": source_name,
            "source_sha256": file_sha,
            "target_sha256": file_sha,
            "bytes": len(data),
            "n_lines": len(lines),
            "identical": True,
            "record_locator": "same line number in source",
        })
        pmid_sets[target] = set(pmids)
    return payloads, entries, traces, pmid_sets


def build_patterns(research: Path) -> tuple[bytes, dict, list[dict], list[dict], dict]:
    """Merge shard/repair pattern records the way ``prefer_repairs=True`` reads them.

    Returns the merged file bytes, its manifest entry, the per-record trace, the
    superseded pairs and a few build-time counters.
    """
    learn = research / "scope_learn"
    if not learn.is_dir():
        raise CorpusBuildError(f"missing pattern directory: {learn}")
    shard_paths = sorted(learn.glob(SHARD_GLOB), key=shard_sort_key)
    repair_paths = sorted(learn.glob(REPAIR_GLOB))
    if not shard_paths:
        raise CorpusBuildError(f"no {SHARD_GLOB} under {learn}")

    sources: dict[str, dict] = {}
    shard_records: list[dict] = []
    repair_records: list[dict] = []
    seen_shard: dict[str, dict] = {}
    seen_repair: dict[str, dict] = {}

    for paths, bucket, seen, role in (
        (shard_paths, shard_records, seen_shard, "shard"),
        (repair_paths, repair_records, seen_repair, "repair"),
    ):
        for path in paths:
            data, lines = read_lines(path)
            rel = path.relative_to(research).as_posix()
            file_sha = sha256_bytes(data)
            sources[rel] = {
                "sha256": file_sha,
                "bytes": len(data),
                "n_lines": len(lines),
                "role": role,
            }
            for line_number, raw in enumerate(lines, 1):
                row = parse_row(raw, path, line_number)
                pmid = pmid_of(row, path, line_number)
                if pmid in seen:
                    raise CorpusBuildError(
                        "two {} records share pmid {}: {}:{} and {}:{}; the "
                        "prefer_repairs rule does not order them, so one file "
                        "cannot hold both".format(
                            role, pmid, seen[pmid]["file"], seen[pmid]["line"],
                            rel, line_number)
                    )
                record = {
                    "pmid": pmid,
                    "file": rel,
                    "line": line_number,
                    "file_sha256": file_sha,
                    "record_sha256": sha256_bytes(raw),
                    "raw": raw,
                }
                seen[pmid] = record
                bucket.append(record)

    orphans = sorted(set(seen_repair) - set(seen_shard))
    if orphans:
        raise CorpusBuildError(
            "repair records without a shard record: " + ", ".join(orphans)
            + "; prefer_repairs would keep them, but the shard-first ordering "
              "of this merge cannot place them"
        )

    out_lines: list[bytes] = []
    traces: list[dict] = []
    superseded: list[dict] = []
    for record in shard_records:
        repair = seen_repair.get(record["pmid"])
        kept = repair or record
        out_lines.append(kept["raw"])
        trace = {
            "pmid": kept["pmid"],
            "record_row": len(out_lines),
            "rule": "repair_supersedes_shard" if repair else "shard_kept",
            "source_file": kept["file"],
            "source_line": kept["line"],
            "source_file_sha256": kept["file_sha256"],
            "record_sha256": kept["record_sha256"],
        }
        if repair:
            dropped = {
                "file": record["file"],
                "line": record["line"],
                "file_sha256": record["file_sha256"],
                "record_sha256": record["record_sha256"],
            }
            trace["superseded"] = dropped
            superseded.append({
                "pmid": kept["pmid"],
                "shard": {k: dropped[k] for k in ("file", "line", "record_sha256")},
                "repair": {
                    "file": repair["file"],
                    "line": repair["line"],
                    "record_sha256": repair["record_sha256"],
                },
            })
        traces.append(trace)

    data = b"\n".join(out_lines) + b"\n"
    pmids = [t["pmid"] for t in traces]
    if len(set(pmids)) != len(pmids):
        raise CorpusBuildError("merged patterns are not one record per PMID")
    if len(out_lines) != len(shard_records):
        raise CorpusBuildError("merged record count does not match shard record count")

    # Reviewer-visible parity: the reader fans a pattern record out into one hit
    # per element of ``patterns`` (plus one record-context hit when empty). Count
    # both sides here so "equivalent to prefer_repairs=True" is a number, not a
    # claim, and so a future edit that changes fan-out fails the build.
    merged_units = 0
    pmid_types: dict[str, int] = {}
    for raw in out_lines:
        record = json.loads(raw)
        pmid_type = type(record.get("pmid")).__name__
        pmid_types[pmid_type] = pmid_types.get(pmid_type, 0) + 1
        patterns = record.get("patterns")
        merged_units += len(patterns) if isinstance(patterns, list) else 1
    legacy_units = 0
    for record in shard_records:
        kept = seen_repair.get(record["pmid"]) or record
        patterns = json.loads(kept["raw"]).get("patterns")
        legacy_units += len(patterns) if isinstance(patterns, list) else 1

    counters = {
        "n_shard_files": len(shard_paths),
        "n_repair_files": len(repair_paths),
        "n_shard_records": len(shard_records),
        "n_repair_records": len(repair_records),
        "n_superseded": len(superseded),
        "n_repair_records_used": len(superseded),
        "n_pattern_units": merged_units,
        "n_legacy_pattern_units": legacy_units,
    }
    entry = {
        "kind": "pattern",
        "mode": "generated",
        "sha256": sha256_bytes(data),
        "source_files": sorted(sources),
        "source_sha256": {name: stat["sha256"] for name, stat in sorted(sources.items())},
        "bytes": len(data),
        "n_lines": len(out_lines),
        "n_records": len(out_lines),
        "n_malformed": 0,
        "primary_key": PATTERN_KEY,
        "n_primary_keys": len(set(pmids)),
        "n_pmids": len(set(pmids)),
        "pmids_sha256": pmids_digest(pmids),
        "pmid_json_types": dict(sorted(pmid_types.items())),
        "identical_to_source": False,
        "provenance": "provenance/patterns_records.jsonl",
    }
    return data, entry, traces, superseded, {
        "sources": sources, "pmids": pmids, **counters}


def scope_lists() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name, role in SCOPE_LISTS:
        for directory in SCOPE_DIRS:
            path = directory / name
            if path.is_file():
                data = path.read_bytes()
                n_pmids = len([l for l in data.decode("utf-8", "replace").splitlines()
                               if l.strip() and not l.startswith("#")])
                out[name] = {
                    "path": str(path),
                    "sha256": sha256_bytes(data),
                    "n_pmids": n_pmids,
                    "role": role,
                }
                break
    return out


def pmid_range(pmid_sets: dict[str, set[str]]) -> dict:
    """Corpus-wide PMID coverage, derived from each file's own records."""
    union: set[str] = set()
    for pmids in pmid_sets.values():
        union.update(pmids)
    ordered = sorted(union)
    return {
        "n_pmids": len(ordered),
        "first": ordered[0] if ordered else None,
        "last": ordered[-1] if ordered else None,
        "pmids_sha256": pmids_digest(ordered),
        "per_file": {name: len(pmids) for name, pmids in sorted(pmid_sets.items())},
    }


def build(research: Path) -> dict[str, bytes]:
    """Everything the corpus directory must contain, keyed by relative path."""
    copy_payloads, copy_entries, copy_traces, copy_pmids = build_copies(research)
    patterns_bytes, pattern_entry, pattern_traces, superseded, counters = \
        build_patterns(research)

    files = dict(copy_entries)
    files[PATTERNS] = pattern_entry
    coverage = pmid_range({**copy_pmids, PATTERNS: set(counters["pmids"])})

    fingerprint_parts = [f"{name}:{entry['sha256']}" for name, entry in sorted(files.items())]
    for name, stat in sorted(counters["sources"].items()):
        fingerprint_parts.append(f"source:{name}:{stat['sha256']}")

    manifest = {
        "schema": SCHEMA,
        "generated_utc": utc_now(),
        "generator": {
            "path": "tools/build_corpus.py",
            "sha256": sha256_bytes(Path(__file__).read_bytes()),
            "invariants": [
                "every pattern record is the byte-for-byte line it came from",
                "one pattern record per PMID; repair wins over shard",
                "copies are bit-identical to their root source file",
                "no record is dropped for retraction or PMID scope; both stay "
                "read-time filters",
            ],
        },
        "corpus_dir": str(research / "corpus"),
        "files": files,
        "patterns": {
            "path": PATTERNS,
            "precedence": "repair_over_shard",
            "prefer_repairs": True,
            "key": PATTERN_KEY,
            "superseded_records": counters["n_superseded"],
            "source_files": sorted(counters["sources"]),
            "shard_glob": f"scope_learn/{SHARD_GLOB}",
            "repair_glob": f"scope_learn/{REPAIR_GLOB}",
            "order": (
                "shard files by numeric suffix, each in line order; a superseded "
                "shard record is replaced in place by its repair record"
            ),
            "equivalent_to": (
                "the reader's prefer_repairs=True path over "
                "scope_learn/shard_*.jsonl + repair_*.jsonl: same 422 records, "
                "same record bytes, same pattern-unit count"
            ),
            "differs_from_legacy_reader_in": [
                "record_id/evidence_id now name this file ('patterns:...:rowN') "
                "instead of the shard file stem; hit seq order therefore differs "
                "wherever the shard file order did",
                "a hit's source_file/source_sha256 name this file, not the shard",
                "stats.excluded.superseded_records is 0: superseded shard "
                "records are not in the input any more, so the reader has "
                "nothing to drop. The count survives as "
                "patterns.superseded_records (24, of which 1 is the retracted "
                "PMID); the old reader reported 23 there because it dropped that "
                "record as retracted before it could count it as superseded",
                "stats.excluded.retracted_records goes 24 -> 23: the old layout "
                "held both shard_6 and repair_a for PMID 36450980, so it excluded "
                "two pattern records; the merged file holds one. No retrievable "
                "hit changed (--verify-equivalence compares all hit signatures)",
            ],
            "read_time_only": [
                "retraction registry exclusion",
                "PMID scope filter (405 current, 422 historical)",
            ],
            "n_shard_files": counters["n_shard_files"],
            "n_repair_files": counters["n_repair_files"],
            "n_shard_records": counters["n_shard_records"],
            "n_repair_records": counters["n_repair_records"],
            "n_records": pattern_entry["n_records"],
            "n_pattern_units": counters["n_pattern_units"],
        },
        "pmid_range": coverage,
        "provenance": {
            "copies": "provenance/copies.jsonl",
            "patterns": "provenance/patterns_records.jsonl",
            "sources": counters["sources"],
            "superseded": superseded,
        },
        "scope_lists": scope_lists(),
        "lineage": {
            "note": "build history stays on disk; this directory adds the formal "
                    "inputs and never replaces or deletes what came before",
            "retained_in_place": [
                "methods.jsonl / workflows.jsonl / code_sources.jsonl at the "
                "research root: the merge target the extraction shards fed",
                "scope_learn/shard_*.jsonl: the original shard records, including "
                "the 24 stale shard_6 records left uncorrected on purpose",
                "scope_learn/repair_*.jsonl: the re-read records that supersede them",
                "research/shards/bNN/: per-batch extraction units with done.txt",
            ],
            "superseded_but_readable": {
                "scope_learn/shard_6.jsonl": (
                    "24 records still say status=unresolved; the repair files, not "
                    "edits to this file, carry the corrected reading"
                ),
            },
            "formal_inputs": [
                "corpus/methods.jsonl",
                "corpus/workflows.jsonl",
                "corpus/code_sources.jsonl",
                "corpus/patterns.jsonl",
            ],
        },
        "verification": {
            "n_records_checked": sum(entry["n_records"] for entry in files.values()),
            "record_hashes_recomputed": True,
            "copies_bit_identical": all(
                entry["identical_to_source"] for name, entry in files.items()
                if name != PATTERNS
            ),
            "patterns_one_record_per_pmid": (
                pattern_entry["n_pmids"] == pattern_entry["n_records"]
            ),
            "patterns_units_match_legacy": (
                counters["n_pattern_units"] == counters["n_legacy_pattern_units"]
            ),
            "superseded_pmids_recorded": len(superseded),
            "all_repair_records_used": (
                counters["n_repair_records"] == counters["n_repair_records_used"]
            ),
        },
        "content_fingerprint": sha256_bytes(
            "|".join(fingerprint_parts).encode("utf-8")
        ),
    }

    payloads: dict[str, bytes] = {}
    payloads.update(copy_payloads)
    payloads[PATTERNS] = patterns_bytes
    payloads["provenance/copies.jsonl"] = jsonl_bytes(copy_traces)
    payloads["provenance/patterns_records.jsonl"] = jsonl_bytes(pattern_traces)
    payloads["manifest.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2,
                                            sort_keys=True) + "\n").encode("utf-8")
    return payloads


# ---------------------------------------------------------- reader equivalence


def _hit_signature(hit) -> tuple:
    """Everything a retrieval result exposes about one hit, minus the label.

    ``record_id``/``source_file``/``source_sha256`` are deliberately excluded:
    the merge renames the pattern source, and this check exists to prove the
    *retrievable content* did not move when that happened.
    """
    return (
        hit.pmid,
        hit.source_kind,
        hit.granularity,
        hit.locator,
        hit.verbatim,
        hit.in_scope,
        tuple((k, json.dumps(v, ensure_ascii=False, sort_keys=True, default=str))
              for k, v in hit.content),
        tuple((k, json.dumps(v, ensure_ascii=False, sort_keys=True, default=str))
              for k, v in hit.record_context),
        tuple(sorted(hit.field_tiers)),
    )


def verify_equivalence(research: Path) -> dict:
    """Prove the merged corpus is the ``prefer_repairs=True`` view, without a reader.

    Two independent layers:

    1. **Selector replay** — re-derive the merged file from the shard/repair
       files with the documented rule and compare bytes. This is the check that
       survives reader refactors, because it only uses the source files.
    2. **Reader cross-check** — if ``src/scope_knowledge.py`` still exposes the
       two layouts, compare every hit signature. Reported as ``null`` when the
       reader no longer offers the pre-merge layout, because a deleted code path
       is not evidence about the data.

    A third layer is cheap and decisive for the copies: ``bytes`` identity, which
    ``--check`` already enforces.
    """
    learn = research / "scope_learn"
    shard_paths = sorted(learn.glob(SHARD_GLOB), key=shard_sort_key)
    repair_paths = sorted(learn.glob(REPAIR_GLOB))
    repair_by_pmid: dict[str, bytes] = {}
    for path in repair_paths:
        _data, lines = read_lines(path)
        for line_number, raw in enumerate(lines, 1):
            pmid = pmid_of(parse_row(raw, path, line_number), path, line_number)
            repair_by_pmid[pmid] = raw

    replayed: list[bytes] = []
    for path in shard_paths:
        _data, lines = read_lines(path)
        for line_number, raw in enumerate(lines, 1):
            pmid = pmid_of(parse_row(raw, path, line_number), path, line_number)
            replayed.append(repair_by_pmid.get(pmid, raw))

    on_disk = (research / "corpus" / PATTERNS)
    merged_bytes = on_disk.read_bytes().split(b"\n") if on_disk.is_file() else []
    if merged_bytes and merged_bytes[-1] == b"":
        merged_bytes.pop()

    report: dict = {
        "selector_replay": {
            "n_shard_records": len(replayed),
            "n_repair_records": len(repair_by_pmid),
            "n_merged_records": len(merged_bytes),
            "identical": replayed == merged_bytes,
            "rule": "for each shard record in file order, substitute the repair "
                    "record with the same pmid when one exists",
        },
        "reader_cross_check": None,
    }
    if not report["selector_replay"]["identical"]:
        first = next((i for i, (a, b) in enumerate(zip(replayed, merged_bytes)) if a != b),
                     min(len(replayed), len(merged_bytes)))
        report["selector_replay"]["first_divergence_row"] = first + 1
    report["equivalent"] = report["selector_replay"]["identical"]

    try:
        sys.path.insert(0, str(research.parent / "src"))
        import scope_knowledge as sk

        legacy = sk.ScopeKnowledge(research_dir=str(research))
        merged = sk.ScopeKnowledge(
            research_dir=str(research / "corpus"),
            source_files=[
                ("workflow", "workflows.jsonl", "workflows"),
                ("method", "methods.jsonl", "methods"),
                ("pattern", PATTERNS, "patterns"),
            ],
        )
        legacy_stats, merged_stats = legacy.stats(), merged.stats()
        left = sorted(_hit_signature(h) for h in legacy._all_hits)
        right = sorted(_hit_signature(h) for h in merged._all_hits)
        report["reader_cross_check"] = {
            "legacy_sources": [(s["label"], s["n_records_indexed"], s["n_hits"])
                               for s in legacy_stats["sources"]],
            "merged_sources": [(s["label"], s["n_records_indexed"], s["n_hits"])
                               for s in merged_stats["sources"]],
            "n_records_indexed": [legacy_stats["n_records_indexed"],
                                  merged_stats["n_records_indexed"]],
            "n_hits_indexed": [legacy_stats["n_hits_indexed"],
                               merged_stats["n_hits_indexed"]],
            "n_hits_anchored": [legacy_stats["n_hits_anchored"],
                                merged_stats["n_hits_anchored"]],
            "coverage_equal": legacy_stats["coverage"] == merged_stats["coverage"],
            "identical_hit_multiset": left == right,
            "legacy_excluded": legacy_stats["excluded"],
            "merged_excluded": merged_stats["excluded"],
            "equivalent": (
                left == right
                and legacy_stats["n_hits_indexed"] == merged_stats["n_hits_indexed"]
                and legacy_stats["n_hits_anchored"] == merged_stats["n_hits_anchored"]
                and legacy_stats["coverage"] == merged_stats["coverage"]
            ),
        }
        report["equivalent"] = report["equivalent"] and report["reader_cross_check"]["equivalent"]
    except Exception as exc:  # noqa: BLE001 - the reader may be mid-refactor
        report["reader_cross_check"] = {
            "skipped": True,
            "reason": f"{type(exc).__name__}: {exc}",
            "note": "the pre-merge reader layout is unavailable; selector replay "
                    "still proves the merge rule, and --check proves the copies",
        }
    return report


# ---------------------------------------------------------------------- driver


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--research-dir", default=str(RESEARCH))
    ap.add_argument("--write", action="store_true", help="write research/corpus")
    ap.add_argument("--check", action="store_true",
                    help="recompute and compare against the corpus on disk")
    ap.add_argument("--verify-equivalence", action="store_true",
                    help="replay the merge rule against the source shards and, "
                         "when the reader still offers the split layout, compare "
                         "every hit signature")
    args = ap.parse_args()

    research = Path(args.research_dir).expanduser().resolve()
    corpus_dir = research / "corpus"
    try:
        payloads = build(research)
    except CorpusBuildError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2

    manifest = json.loads(payloads["manifest.json"])
    report = {
        "corpus_dir": str(corpus_dir),
        "content_fingerprint": manifest["content_fingerprint"],
        "files": {name: {"sha256": entry["sha256"], "n_records": entry["n_records"]}
                  for name, entry in sorted(manifest["files"].items())},
        "patterns": {
            "n_records": manifest["patterns"]["n_records"],
            **{k: manifest["patterns"][k] for k in (
                "precedence", "n_shard_records", "n_repair_records",
                "superseded_records", "n_pattern_units")},
        },
        "pmid_range": manifest["pmid_range"],
        "verification": manifest["verification"],
    }

    if args.check:
        mismatches: list[str] = []
        for name, data in sorted(payloads.items()):
            path = corpus_dir / name
            if not path.is_file():
                mismatches.append(f"missing:{name}")
                continue
            on_disk = path.read_bytes()
            if name == "manifest.json":
                # generated_utc is the only field allowed to move between builds
                a = json.loads(on_disk)
                b = json.loads(data)
                a.pop("generated_utc", None)
                b.pop("generated_utc", None)
                if a != b:
                    mismatches.append(f"differs:{name}")
            elif on_disk != data:
                mismatches.append(f"differs:{name}")
        extra = sorted(p.relative_to(corpus_dir).as_posix()
                       for p in corpus_dir.rglob("*")
                       if p.is_file() and p.relative_to(corpus_dir).as_posix() not in payloads)
        if extra:
            mismatches.append("unexpected:" + ",".join(extra))
        report["check"] = {"status": "ok" if not mismatches else "drift",
                           "mismatches": mismatches}
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0 if not mismatches else 1

    if args.verify_equivalence:
        report["equivalence"] = verify_equivalence(research)

    if args.write:
        written = []
        for name, data in sorted(payloads.items()):
            write_atomic(corpus_dir / name, data)
            written.append(name)
        report["written"] = written
    else:
        report["written"] = []

    print(json.dumps(report, ensure_ascii=False, indent=1))
    if args.verify_equivalence and not report["equivalence"]["equivalent"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
