"""Behavioral tests for the projected runtime corpus (builder + reader).

These cover the edges an ordinary rebuild cannot show: projection whitelists,
retraction bookkeeping at build time, read-only refuses-to-create behaviour,
schema/identity checks, FTS prefilter breadth (ASCII token boundaries, CJK
substrings, punctuation gene names, short terms), and ordering parity with the
in-memory JSONL reader.

The tests do not need the shipped corpus: every case builds a synthetic
``research/corpus`` and a small database with ``tools/build_runtime_corpus.py``.
The real-corpus parity test runs only when the 405 list and the corpus exist.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import scope_corpus as SC  # noqa: E402
import scope_knowledge as SK  # noqa: E402
import build_runtime_corpus as BR  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _shared_derivation() -> None:
    """Bridge the builder to ``scope_knowledge``'s unit builders when absent.

    The production builder refuses to run without
    ``scope_knowledge.derive_record_hits`` (it must not own a second derivation).
    Until that shared entry point exists on the reader side, the tests install a
    thin adapter that calls the *same* private builders and context helper, so
    the parity assertions still compare against one implementation of the logic.
    """
    if hasattr(SK, "derive_record_hits"):
        yield
        return

    def derive_record_hits(
        *, kind, record, pmid, record_id, record_uid, source_file, source_label,
        source_sha256, in_scope, start_seq=0, evidence_ids=None,
    ):
        if kind == "workflow":
            units = SK._workflow_units(record)
        elif kind == "method":
            units = SK._method_units(record)
        else:
            units = SK._pattern_units(record, pmid)
        context_pairs, context_fields = SK._record_context(kind, record)
        rows = []
        seq = start_seq
        for unit in units:
            content = [
                (name, value)
                for name, value in unit["content"]
                if not SK._is_empty(value)
            ]
            tiers = {
                name: unit["tiers"].get(name, SK.TIER_EXTRACTION) for name, _ in content
            }
            for name, _ in context_pairs:
                tiers[name] = SK.TIER_EXTRACTION
            fields = [
                [name, SK._normalize_text(value)]
                for name, value in (*content, *context_pairs)
            ]
            locator = unit.get("locator")
            locator = locator.strip() or None if isinstance(locator, str) else None
            verbatim = unit.get("verbatim")
            verbatim = (
                verbatim.strip()
                if isinstance(verbatim, str) and verbatim.strip()
                else None
            )
            rows.append(
                {
                    "evidence_id": (
                        f"method:{record_id}" if kind == "method"
                        else f"{kind}:{record_id}:{unit['suffix']}"
                    ),
                    "seq": seq,
                    "unit_index": unit.get("unit_index", 0),
                    "granularity": unit["granularity"],
                    "locator": locator,
                    "locator_type": SK._locator_type(locator),
                    "verbatim": verbatim,
                    "content": [list(item) for item in content],
                    "record_context": [list(item) for item in context_pairs],
                    "field_tiers": tiers,
                    "context_fields": list(context_fields),
                    "search_fields": fields,
                    "search_text": "\n".join(text for _, text in fields),
                    "is_anchored": bool(locator and SK._locator_type(locator) and verbatim),
                }
            )
            seq += 1
        return rows

    SK.derive_record_hits = derive_record_hits
    yield
    del SK.derive_record_hits

PMID = "11111111"
OTHER = "22222222"
OUT_OF_SCOPE = "33333333"
RETRACTED = "36450980"
REAL_CORPUS = ROOT / "research" / "corpus"
REAL_PMID_LIST = Path("/data/disk1/zeiss/singlecell-quant/data/included_pmids_405.txt")


# --------------------------------------------------------------------- fixtures


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def workflow(pmid: str, token: str, **overrides) -> dict:
    record = {
        "workflow_id": f"{pmid}:wf1",
        "pmid": pmid,
        "question": {
            "text": f"{token} question",
            "evidence": [
                {"locator": "abstract:L1:P1:S1", "verbatim": f"{token} quote."}
            ],
        },
        "comparison": "treated vs control",
        "cell_level": "myeloid cells",
        "steps": [
            {
                "observation": f"{token} observation",
                "locator": "results:L2:P1:S1",
                "verbatim": "The CD4+ fraction fell; PD-1 rose in UC患者 samples.",
                "next_analysis": "test egress",
                "rationale": "egress can explain a local fall",
            }
        ],
        "claims": [
            {
                "claim": f"{token} claim",
                "locator": "results:L2:P1:S1",
                "verbatim": "The CD4+ fraction fell.",
                "evidence_type": "count",
                "direction": "supports",
            }
        ],
        "gaps": [{"text": "One time point", "locator": "results:L3:P1:S1"}],
        "author_focus_groups": [{"name": "FOCUS", "locator": "results:L2:P1:S1"}],
        "no_prior_warning": "context only",
    }
    record.update(overrides)
    return record


def method(pmid: str, instance: str, meaningful: str = "AUCell") -> dict:
    return {
        "instance_id": f"{pmid}:{instance}",
        "pmid": pmid,
        "locator": "methods:L9:P1:S1",
        "verbatim": f"We scored cells with {meaningful}.",
        "mentioned_name": meaningful,
        "canonical_name": meaningful,
        "package": meaningful,
        "operation": "per-cell scoring",
        "pipeline_stage": "signature",
        "analysis_object": "cells",
        "inputs": ["matrix"],
        "outputs": ["scores"],
        "purpose": "characterization",
        "constraints": [],
        "version_in_text": None,
        "method_class": "computational",
        "in_scope": True,
        "scope_reason": "used on single-cell results",
        "rerunnable_role": "rerunnable",
        "params": {"auc_max_rank": 5},
    }


def pattern(pmid: str, token: str) -> dict:
    return {
        "pmid": pmid,
        "status": "reviewed",
        "question": "Which program tracks the phenotype?",
        "patterns": [
            {
                "observation": f"{token} observation",
                "ask_next": "test the program",
                "rationale_source": "inferred",
                "limitations": "one cohort",
                "source_locator": "results:L42:P3:S1",
                "verbatim": f"{token} verbatim sentence.",
                "prompt_lesson": "ask structure first",
                "provenance": "inferred",
            },
            {
                "observation": "no anchor observation",
                "ask_next": "none",
                "rationale_source": "inferred",
                "limitations": "none",
                "source_locator": None,
                "verbatim": None,
                "prompt_lesson": "unanchored",
            },
        ],
        "author_focus_groups": [],
        # A dropped key: repair bookkeeping must not reach the database.
        "repair": {"what_changed": "SECRET_REPAIR_NOTE"},
        "prior_status": "unresolved",
    }


def code_source(pmid: str) -> dict:
    return {
        "source_id": f"{pmid}:cs1",
        "pmid": pmid,
        "url": "https://github.com/example/repo",
        "url_kind": "repository",
        "ownership": "author",
        "locator": "methods:L117:P6:S1",
        "verbatim": "Code is at the repository.",
        "resolves_to": None,
        "inputs": [],
        "outputs": ["script"],
        "params_found": [],
        "status": "author_repo_from_text",
        "checked_utc": None,
        "notes": "not verified",
    }


@pytest.fixture()
def corpus(tmp_path: Path) -> tuple[Path, Path]:
    """A synthetic research tree plus the PMID list it is scoped to."""
    research = tmp_path / "research"
    corpus_dir = research / "corpus"
    write_jsonl(
        corpus_dir / "workflows.jsonl",
        [
            workflow(PMID, "IN_SCOPE"),
            workflow(OUT_OF_SCOPE, "OUT_OF_SCOPE_TOKEN"),
            workflow(RETRACTED, "RETRACTED_TOKEN"),
            workflow(OTHER, "MENTIONS", workflow_id=f"{OTHER}:wf1",
                     question={
                         "text": "mentions a retracted paper",
                         "evidence": [
                             {
                                 "locator": "abstract:L1:P1:S1",
                                 "verbatim": "As shown previously (PMID 36450980).",
                             }
                         ],
                     }),
        ],
    )
    write_jsonl(
        corpus_dir / "methods.jsonl",
        [method(PMID, "methods:L9:P1:S1"), method(OTHER, "methods:L9:P1:S2")],
    )
    write_jsonl(
        corpus_dir / "patterns.jsonl",
        [pattern(PMID, "PATTERN"), pattern(OUT_OF_SCOPE, "SCOPE_PATTERN")],
    )
    write_jsonl(corpus_dir / "code_sources.jsonl", [code_source(PMID)])
    pmid_list = tmp_path / "included.txt"
    pmid_list.write_text(f"{PMID}\n{OTHER}\n{RETRACTED}\n", encoding="utf-8")
    return research, pmid_list


# ------------------------------------------------------------------ projection


def test_projection_drops_non_whitelisted_keys() -> None:
    record = pattern(PMID, "TOKEN")
    projected = SC.project_record("pattern", record)
    assert "repair" not in projected
    assert "prior_status" not in projected
    assert projected["patterns"][0]["verbatim"] == "TOKEN verbatim sentence."
    # Unknown nested keys are dropped too, absent keys stay absent.
    noisy = {"pmid": PMID, "patterns": [{"verbatim": "q", "junk": "x"}]}
    assert SC.project_record("pattern", noisy)["patterns"] == [{"verbatim": "q"}]
    assert "citation" not in SC.project_record("pattern", {"pmid": PMID})


def test_projection_keeps_parser_inputs() -> None:
    record = workflow(PMID, "TOKEN")
    projected = SC.project_record("workflow", record)
    # Everything _workflow_units/_record_context read must survive.
    assert projected["question"]["text"] == record["question"]["text"]
    assert projected["question"]["evidence"][0]["locator"] == "abstract:L1:P1:S1"
    assert set(projected["steps"][0]) == {
        "observation", "locator", "verbatim", "next_analysis", "rationale"
    }
    assert set(projected["claims"][0]) == {
        "claim", "locator", "verbatim", "evidence_type", "direction"
    }
    assert projected["gaps"] == record["gaps"]
    assert projected["comparison"] == record["comparison"]
    assert projected["cell_level"] == record["cell_level"]
    method_projected = SC.project_record("method", method(PMID, "methods:L9:P1:S1"))
    for key in ("mentioned_name", "canonical_name", "package", "operation",
                "pipeline_stage", "purpose", "method_class", "in_scope",
                "rerunnable_role", "params", "locator", "verbatim"):
        assert key in method_projected


# ---------------------------------------------------------------------- build


def test_build_and_read_roundtrip(corpus) -> None:
    research, pmid_list = corpus
    db = research / "corpus" / "records.sqlite"
    report = BR.build(research, db, pmid_list_path=pmid_list)
    assert report["status"] == "built"
    assert db.is_file()
    assert db.stat().st_mode & 0o222 == 0, "published database must be read-only on disk"

    conn = SC.open_db(db)
    try:
        meta = SC.read_meta(conn)
        assert meta["schema_id"] == SC.SCHEMA_ID
        assert meta["schema_version"] == SC.SCHEMA_VERSION
        assert meta["fts_enabled"] is True
        assert meta["pmid_list"]["sha256"]
        assert meta["n_records"] == int(meta["n_records"])
        # Scope is not final at build: out-of-scope rows are stored with
        # in_scope=0 and the reader decides. Only retracted/unattributed rows
        # never reach the database.
        records = list(SC.iter_record_rows(conn))
        by_pmid = {}
        for row in records:
            by_pmid.setdefault(row["pmid"], set()).add(row["in_scope"])
        assert by_pmid[PMID] == {True}
        assert by_pmid[OUT_OF_SCOPE] == {False}
        assert RETRACTED not in by_pmid
        # Build-time exclusions are counted, incl. the retracted paper.
        assert meta["excluded"]["retracted_records"] > 0
        assert meta["excluded"]["retracted_reference_records"] == 1
        assert meta["excluded"]["out_of_scope_records"] > 0
        assert meta["excluded_by_kind"]["workflow"]["retracted_reference_records"]["records"] == 1
    finally:
        conn.close()


def test_retracted_reference_rows_are_marked_not_dropped(corpus) -> None:
    research, pmid_list = corpus
    db = research / "corpus" / "records.sqlite"
    BR.build(research, db, pmid_list_path=pmid_list)
    conn = SC.open_db(db)
    try:
        mentioning = [
            row for row in SC.iter_record_rows(conn, pmid=OTHER, source_kind="workflow")
        ]
        assert len(mentioning) == 1
        assert mentioning[0]["retracted_ref"] is True
        clean = list(SC.iter_record_rows(conn, pmid=PMID, source_kind="workflow"))
        assert clean and clean[0]["retracted_ref"] is False
        # The mentioning record's units are hidden by default; the paper's other
        # records are not, because the JSONL reader only drops the record.
        default_rows = SC.hit_rows(conn, pmids=[OTHER])
        all_rows = SC.hit_rows(conn, pmids=[OTHER], exclude_retracted_ref=False)
        assert default_rows and len(all_rows) > len(default_rows)
        assert all(row["source_kind"] != "workflow" for row in default_rows)
        assert any(row["source_kind"] == "workflow" for row in all_rows)
    finally:
        conn.close()


def test_pmid_refs_capture_every_pmid_shaped_token(corpus) -> None:
    """The cross-reference table must scan the RAW line, not the projection.

    A retraction announced after the build has to reach a row that cites the
    paper — including a citation that lives in a field the projection drops.
    """
    research, pmid_list = corpus
    db = research / "corpus" / "records.sqlite"
    BR.build(research, db, pmid_list_path=pmid_list)
    conn = SC.open_db(db)
    try:
        refs = SC.pmid_refs(conn)
        assert refs and all(set(row) == {"ref_pmid", "owner", "owner_key"} for row in refs)
        assert SC.counts(conn)["pmid_refs"] == len(refs)
        assert SC.read_meta(conn)["n_pmid_refs"] == len(refs)
        owners = {row["owner"] for row in refs}
        assert owners == {"record", "code_source"}
        # owner_key values resolve to real rows on both sides.
        record_uids = {
            row["record_uid"] for row in SC.iter_record_rows(conn)
        }
        code_keys = {str(row["rowid"]) for row in SC.code_source_rows(conn)}
        assert all(
            row["owner_key"] in (record_uids if row["owner"] == "record" else code_keys)
            for row in refs
        )
        # A record's own PMID and the PMID it cites are both present.
        mentioning = SC.pmids_mentioning(conn, [RETRACTED])
        assert mentioning, "the record citing the retracted paper must be indexed"
        assert all(key.startswith("workflow:") for key in mentioning)
    finally:
        conn.close()


def test_pmid_refs_carry_citations_from_dropped_fields(corpus) -> None:
    """A PMID that only survives in a dropped field must still be cross-referenced."""
    research, pmid_list = corpus
    corpus_dir = research / "corpus"
    record = workflow(PMID, "ONLY_DROPPED_FIELD")
    # ``repair`` is dropped by the projection; the citation lives only there.
    record["repair"] = {"note": "citing 99999999, PMID:321 and 1654"}
    raw = json.dumps(record).replace("PMID:321", r"PMID:\u003321")
    (corpus_dir / "workflows.jsonl").write_text(raw + "\n", encoding="utf-8")
    db = research / "corpus" / "records.sqlite"
    BR.build(research, db, pmid_list_path=pmid_list)
    conn = SC.open_db(db)
    try:
        projected = SC.record_row(conn, f"workflow:workflows:{PMID}:wf1")
        assert "repair" not in projected["record_payload"]
        assert f"workflow:workflows:{PMID}:wf1" in SC.pmids_mentioning(conn, ["99999999"])
        assert f"workflow:workflows:{PMID}:wf1" in SC.pmids_mentioning(conn, ["321"])
        assert not SC.pmids_mentioning(conn, ["654"])
    finally:
        conn.close()


def test_live_retractions_precede_scope_in_retrieval_counts(corpus) -> None:
    research, pmid_list = corpus
    corpus_dir = research / "corpus"
    write_jsonl(corpus_dir / "workflows.jsonl", [
        workflow(PMID, "OWN_RETRACTION"),
        workflow(OTHER, "VISIBLE_REFERENCE", repair={"citation": PMID}),
        workflow(OUT_OF_SCOPE, "OUTSIDE_REFERENCE", repair={"citation": PMID}),
    ])
    db = corpus_dir / "records.sqlite"
    BR.build(research, db, pmid_list_path=pmid_list)
    with SK.ScopeKnowledge(research_dir=research, db_path=db, pmid_list_path=pmid_list) as before:
        evidence_id = before.search("VISIBLE_REFERENCE")["hits"][0]["evidence_id"]
        assert before.context(evidence_id)["status"] == "ok"
    index = SK.ScopeKnowledge(
        research_dir=research, db_path=db, pmid_list_path=pmid_list,
        extra_retractions=[{"pmid": PMID, "reason": "withdrawn after build"}],
    )
    excluded = index.stats()["excluded"]
    assert excluded["retracted_records"] == 3  # Workflow, method, and pattern.
    assert excluded["retracted_reference_records"] == 2  # Includes outside reference.
    assert excluded["out_of_scope_records"] == 1  # Only the unrelated pattern.
    assert index.record(PMID)["status"] == "excluded"
    assert index.search("VISIBLE_REFERENCE")["page"]["total"] == 0
    for result in (index.get(evidence_id), index.context(evidence_id),
                   index.record(OTHER, source_kind="workflow"),
                   index.record(OUT_OF_SCOPE, source_kind="workflow")):
        assert result["status"] == "excluded"
        assert result["reason"] == "retracted_reference"
        assert not ({"hit", "center", "hits"} & result.keys())
        assert not result.get("records")
    assert index.get("missing-evidence-id")["status"] == "not_found"
    assert index.record(OTHER, record_id="missing-record-id")["status"] == "not_found"


def test_code_sources_distinguishes_live_retraction_from_a_miss(corpus, monkeypatch):
    from sctda import method_sources

    research, pmid_list = corpus
    corpus_dir = research / "corpus"
    write_jsonl(corpus_dir / "code_sources.jsonl", [
        dict(code_source(OTHER), repair={"reference": PMID}),
    ])
    db = corpus_dir / "records.sqlite"
    BR.build(research, db, pmid_list_path=pmid_list)
    monkeypatch.setenv(SC.DB_ENV, str(db))
    before = method_sources.code_sources(OTHER, pmid_list_path=pmid_list)
    assert before["status"] == "ok"
    assert "repair" not in before["records"][0]
    assert method_sources.code_sources(PMID, pmid_list_path=pmid_list)["status"] == "not_found"

    monkeypatch.setattr(SK, "DEFAULT_RETRACTIONS", (
        *SK.DEFAULT_RETRACTIONS, SK.RetractionNotice(pmid=PMID, reason="fixture"),
    ))
    for pmid, reason in ((PMID, "retracted"), (OTHER, "retracted_reference")):
        result = method_sources.code_sources(pmid, pmid_list_path=pmid_list)
        assert (result["status"], result["reason"], result["records"]) == ("excluded", reason, [])


def test_open_db_refuses_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SC.CorpusDbMissing):
        SC.open_db(tmp_path / "nope.sqlite")
    assert not (tmp_path / "nope.sqlite").exists(), "open must never create a database"


def test_open_db_rejects_foreign_schema(tmp_path: Path) -> None:
    path = tmp_path / "foreign.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta VALUES ('schema_id', '\"other/9\"')")
    conn.commit()
    conn.close()
    with pytest.raises(SC.CorpusDbSchemaError):
        SC.open_db(path)


def test_open_db_rejects_row_tampering(corpus) -> None:
    research, pmid_list = corpus
    db = research / "corpus" / "records.sqlite"
    BR.build(research, db, pmid_list_path=pmid_list)
    tampered = research / "tampered.sqlite"
    tampered.write_bytes(db.read_bytes())
    conn = sqlite3.connect(tampered)
    conn.execute("DELETE FROM hits WHERE rowid = 1")
    conn.commit()
    conn.close()
    with pytest.raises(SC.CorpusDbSchemaError):
        SC.open_db(tampered)


def test_build_requires_pmid_list(tmp_path: Path, monkeypatch) -> None:
    research = tmp_path / "research"
    write_jsonl(research / "corpus" / "workflows.jsonl", [workflow(PMID, "X")])
    monkeypatch.delenv(SK.PMID_LIST_ENV, raising=False)
    # The workstation has the real 405 list; hide it so "no list" is testable.
    monkeypatch.setattr(SK, "DEFAULT_PMID_LIST_CANDIDATES", ())
    with pytest.raises(BR.BuildError, match="pmid_list_missing"):
        BR.build(research, research / "corpus" / "records.sqlite")


# ------------------------------------------------------------------------ FTS


def test_fts_prefilter_is_a_superset_of_token_boundary_matches(corpus) -> None:
    research, pmid_list = corpus
    db = research / "corpus" / "records.sqlite"
    BR.build(research, db, pmid_list_path=pmid_list)
    conn = SC.open_db(db)
    try:
        rows = SC.hit_rows(conn)
        for term, expected in (("uc", True), ("uc患者", True), ("aucell", True),
                               ("cd4+", True), ("pd-1", True), ("aucel", True),
                               ("no-such-term-anywhere", False)):
            candidates = SC.search_hit_rowids(conn, [term])
            blob_hits = {
                row["rowid"] for row in rows
                if term in "\n".join(text for _, text in row["search_fields"])
            }
            assert blob_hits <= (candidates or set()), term
            assert bool(blob_hits) is expected, term
            # The regex matcher (ASCII token boundaries) is the real decision.
            matcher = SK._term_matcher(term)
            regex_hits = {
                row["rowid"] for row in rows
                if matcher.search("\n".join(text for _, text in row["search_fields"]))
            }
            assert regex_hits <= (candidates or set()), term
    finally:
        conn.close()


def test_fts_short_terms_still_prefilter(corpus) -> None:
    research, pmid_list = corpus
    db = research / "corpus" / "records.sqlite"
    BR.build(research, db, pmid_list_path=pmid_list)
    conn = SC.open_db(db)
    try:
        assert SC.search_hit_rowids(conn, []) is None
        short = SC.search_hit_rowids(conn, ["uc"])
        assert short, "two-character terms must still narrow via LIKE"
        both = SC.search_hit_rowids(conn, ["uc", "cd4+"], match="all")
        assert both <= short
        either = SC.search_hit_rowids(conn, ["nope", "cd4+"], match="any")
        assert either
    finally:
        conn.close()


# ------------------------------------------------------------------- ordering


def expected_hits_from_jsonl(
    research: Path, included: set[str], *, retracted: set[str] | None = None
) -> list[dict]:
    """Replay the build contract on the JSONL files, with no database involved.

    This is the oracle the database must match: same per-record order
    (workflows, methods, patterns), same drop rules (no attributable PMID, own
    PMID retracted, record mentions a retracted PMID), same unit derivation
    (``scope_knowledge.derive_record_hits``).
    """
    retracted = set(retracted or ())
    mention = re.compile(
        "|".join(rf"(?<!\d){re.escape(pmid)}(?!\d)" for pmid in retracted)
    ) if retracted else None
    out: list[dict] = []
    seq = 0
    for kind, name, label in BR.SOURCES:
        if kind not in BR.HIT_KINDS:
            continue
        path = research / "corpus" / name
        if not path.is_file():
            continue
        for row_number, record in enumerate(
            (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()),
            1,
        ):
            pmid = SK._extract_pmid(record)  # noqa: SLF001 - shared helper
            if pmid is None or pmid in retracted:
                continue
            if mention is not None and mention.search(
                json.dumps(record, ensure_ascii=False, sort_keys=True)
            ):
                continue
            projected = SC.project_record(kind, record)
            record_id = SK.ScopeKnowledge._record_id(  # noqa: SLF001
                SK.SourceSpec(kind, path, label), record, pmid, row_number
            )
            hits = SK.derive_record_hits(
                kind=kind,
                record=projected,
                pmid=pmid,
                record_id=record_id,
                record_uid=f"{kind}:{label}:{record_id}",
                source_file=str(path),
                source_label=label,
                source_sha256="",
                in_scope=pmid in included,
                start_seq=seq,
            )
            for hit in hits:
                out.append({**hit, "pmid": pmid})
                seq = hit["seq"] + 1
    return out


def test_hit_order_and_text_match_the_derivation_contract(corpus) -> None:
    research, pmid_list = corpus
    db = research / "corpus" / "records.sqlite"
    BR.build(research, db, pmid_list_path=pmid_list)
    included = {PMID, OTHER, RETRACTED}

    expected = expected_hits_from_jsonl(research, included, retracted={RETRACTED})
    conn = SC.open_db(db)
    try:
        rows = SC.hit_rows(conn)
        by_id = {row["evidence_id"]: row for row in rows}
        # ``hit_rows`` orders by (pmid, seq). ``seq`` numbers are allocated over
        # *stored* rows, so a record the default read hides (it mentions a
        # retracted PMID) leaves gaps; only the relative order is comparable.
        stored_order = [(row["pmid"], row["evidence_id"]) for row in rows]
        expected_order = [
            (hit["pmid"], hit["evidence_id"])
            for hit in sorted(expected, key=lambda hit: (hit["pmid"], hit["seq"]))
        ]
        assert stored_order == expected_order
        for hit in expected:
            row = by_id[hit["evidence_id"]]
            assert row["pmid"] == hit["pmid"]
            assert row["field_tiers"] == hit["field_tiers"]
            assert row["content"] == hit["content"]
            assert row["record_context"] == hit["record_context"]
            assert row["context_fields"] == hit["context_fields"]
            assert row["search_fields"] == hit["search_fields"]
            assert row["is_anchored"] is bool(hit["is_anchored"])
            assert row["granularity"] == hit["granularity"]
    finally:
        conn.close()


def test_hit_rows_expose_every_documented_field(corpus) -> None:
    """Structural guard: a column selected in SQL must reach the returned dict.

    ``unit_index`` was selected but not returned once, and a caller silently got
    zero. Assert the full contract on a real row instead of trusting the dict.
    """
    research, pmid_list = corpus
    db = research / "corpus" / "records.sqlite"
    BR.build(research, db, pmid_list_path=pmid_list)
    conn = SC.open_db(db)
    try:
        rows = SC.hit_rows(conn)
        assert rows
        required = {
            "rowid", "evidence_id", "record_uid", "source_kind", "source_label",
            "source_file", "source_sha256", "pmid", "record_id", "seq",
            "unit_index", "granularity", "locator", "locator_type", "verbatim",
            "content", "record_context", "field_tiers", "context_fields",
            "search_fields", "search_text", "is_anchored", "in_scope",
            "retracted_ref",
        }
        for row in rows:
            assert required <= set(row), sorted(required - set(row))
        # ``unit_index`` is the unit's position inside its record, not a constant:
        # the matching record must agree between the row and the reader's contract.
        for kind in ("workflow", "pattern"):
            for row in [item for item in rows if item["source_kind"] == kind]:
                expected = SK.derive_record_hits(
                    kind=kind,
                    record=SC.record_row(conn, row["record_uid"])["record_payload"],
                    pmid=row["pmid"],
                    record_id=row["record_id"],
                    record_uid=row["record_uid"],
                    source_file=row["source_file"],
                    source_label=row["source_label"],
                    source_sha256=row["source_sha256"],
                    in_scope=True,
                )
                match = next(
                    (hit for hit in expected if hit["evidence_id"] == row["evidence_id"]),
                    None,
                )
                assert match is not None, row["evidence_id"]
                assert row["unit_index"] == match["unit_index"], row["evidence_id"]
    finally:
        conn.close()


def test_record_reads_return_the_projection_only(corpus) -> None:
    research, pmid_list = corpus
    db = research / "corpus" / "records.sqlite"
    BR.build(research, db, pmid_list_path=pmid_list)
    conn = SC.open_db(db)
    try:
        rows = [row for row in SC.iter_record_rows(conn, source_kind="pattern")]
        assert rows
        payload = rows[0]["record_payload"]
        assert "repair" not in payload and "prior_status" not in payload
        assert "SECRET_REPAIR_NOTE" not in json.dumps(payload)
    finally:
        conn.close()


def test_resolve_db_path_precedence(tmp_path: Path, monkeypatch) -> None:
    research = tmp_path / "research"
    monkeypatch.delenv(SC.DB_ENV, raising=False)
    assert SC.resolve_db_path(research) == research / "corpus" / "records.sqlite"
    monkeypatch.setenv(SC.DB_ENV, "alt/path.sqlite")
    assert SC.resolve_db_path(research) == research / "alt" / "path.sqlite"
    explicit = tmp_path / "explicit.sqlite"
    assert SC.resolve_db_path(research, explicit) == explicit


def test_exclusions_and_source_counts_match_a_jsonl_walk(corpus) -> None:
    """``meta.excluded_by_kind`` must equal an independent walk of the same bytes.

    Build-time drops (retracted, unattributed) cannot be recounted at read time,
    so these counters are the only record of what never reached the database.
    """
    research, pmid_list = corpus
    db = research / "corpus" / "records.sqlite"
    BR.build(research, db, pmid_list_path=pmid_list)
    included = {PMID, OTHER, RETRACTED}
    retracted = {RETRACTED}

    expected = {
        kind: {
            "retracted_records": 0,
            "retracted_reference_records": 0,
            "out_of_scope_records": 0,
            "unattributed_records": 0,
            "malformed_records": 0,
            "n_records_source": 0,
        }
        for kind in ("workflow", "method", "pattern", "code_source")
    }
    pattern = __import__("re").compile(rf"(?<!\d){RETRACTED}(?!\d)")
    for kind, name, _label in BR.SOURCES:
        path = research / "corpus" / name
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            expected[kind]["n_records_source"] += 1
            pmid = SK._extract_pmid(record)  # noqa: SLF001
            blob = json.dumps(record, ensure_ascii=False, sort_keys=True)
            mentions = bool(pattern.search(blob))
            if pmid is None:
                expected[kind]["unattributed_records"] += 1
                continue
            if pmid in retracted:
                expected[kind]["retracted_records"] += 1
                continue
            if pmid not in included:
                expected[kind]["out_of_scope_records"] += 1
            if mentions:
                expected[kind]["retracted_reference_records"] += 1

    conn = SC.open_db(db)
    try:
        meta = SC.read_meta(conn)
        per_kind = meta["excluded_by_kind"]
        reasons = [name for name in next(iter(expected.values()))
                   if name != "n_records_source"]
        for kind, values in expected.items():
            got = {reason: per_kind[kind][reason]["records"] for reason in reasons}
            assert got == {reason: values[reason] for reason in reasons}, kind
            assert per_kind[kind]["n_records_source"] == values["n_records_source"]
        totals = {
            reason: sum(block[reason] for block in expected.values())
            for reason in next(iter(expected.values()))
            if reason != "n_records_source"
        }
        for reason, value in totals.items():
            assert meta["excluded"][reason] == value, reason
        # The stored rows are exactly the records the walk keeps. Code sources
        # live in their own table, so compare them separately.
        stored = {
            (row["source_kind"], row["record_id"])
            for row in SC.iter_record_rows(conn)
        }
        stored_code = {
            (row["source_id"], row["pmid"]) for row in SC.code_source_rows(conn)
        }
        kept = {
            (kind, SK.ScopeKnowledge._record_id(  # noqa: SLF001
                SK.SourceSpec(kind, research / "corpus" / name, name.split(".")[0]),
                json.loads(line),
                SK._extract_pmid(json.loads(line)),  # noqa: SLF001
                index,
            ))
            for kind, name, _label in BR.SOURCES
            if kind != "code_source"
            for index, line in enumerate(
                (text for text in path_text(research, name) if text.strip()), 1
            )
            if (pmid := SK._extract_pmid(json.loads(line))) is not None  # noqa: SLF001
            and pmid not in retracted
        }
        assert stored == kept
        assert len(stored_code) == expected["code_source"]["n_records_source"] - (
            expected["code_source"]["retracted_records"]
            + expected["code_source"]["unattributed_records"]
        )
    finally:
        conn.close()


def path_text(research: Path, name: str) -> list[str]:
    path = research / "corpus" / name
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


# -------------------------------------------------------------- real corpus


@pytest.mark.skipif(
    not (REAL_CORPUS / "methods.jsonl").is_file() or not REAL_PMID_LIST.is_file(),
    reason="real corpus or 405 PMID list missing",
)
def test_real_corpus_projection_and_counts(tmp_path: Path) -> None:
    db = tmp_path / "records.sqlite"
    report = BR.build(ROOT / "research", db, pmid_list_path=REAL_PMID_LIST)
    assert report["n_hits"] > 1000
    conn = SC.open_db(db)
    try:
        meta = SC.read_meta(conn)
        assert meta["schema_id"] == SC.SCHEMA_ID
        assert meta["n_records"] == report["n_records"]
        assert SC.counts(conn)["hits"] == meta["n_hits"]
        assert meta["excluded"]["superseded_records"] == 24
        assert "verbatim" in SC.field_names(conn)
        # Field vocabulary must cover every key the shared derivation emits.
        derived: set[str] = set()
        for kind, name, _label in BR.SOURCES:
            if kind not in BR.HIT_KINDS:
                continue
            path = ROOT / "research" / "corpus" / name
            for line in path.read_text(encoding="utf-8").splitlines()[:50]:
                if not line.strip():
                    continue
                record = json.loads(line)
                pmid = SK._extract_pmid(record)  # noqa: SLF001
                if pmid is None:
                    continue
                for hit in SK.derive_record_hits(
                    kind=kind,
                    record=SC.project_record(kind, record),
                    pmid=pmid,
                    record_id="probe",
                    record_uid="probe",
                    source_file=name,
                    source_label=name,
                    source_sha256="",
                    in_scope=True,
                ):
                    derived.update(hit["field_tiers"])
        assert derived <= set(SC.field_names(conn))
    finally:
        conn.close()
