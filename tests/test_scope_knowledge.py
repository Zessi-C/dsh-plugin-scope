"""Consumer tests for the SCOPE corpus retrieval module.

The tests build small synthetic corpora that reproduce the failure modes the
retrieval layer must not hide:

* a retracted PMID, with and without a usable ``pmid`` field;
* a record that only mentions the retracted PMID in its text;
* records outside the authoritative PMID list;
* extraction-inference fields that must not be reported as author statements;
* unanchored records that must be hidden unless explicitly requested;
* repair records that supersede older shard records for the same PMID;
* bounded pagination and neighbour context.

One integration test uses the real frozen corpus when it is present. It is
skipped when the corpus or the 422-PMID list is unavailable.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import scope_knowledge as SK  # noqa: E402

PMID = "11111111"
SHARD_ONLY = "33333333"
OUT_OF_SCOPE = "22222222"
RETRACTED = "36450980"


# --------------------------------------------------------------------- fixtures


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
        + "\n",
        encoding="utf-8",
    )


def default_workflows() -> list[dict]:
    return [
        {
            "workflow_id": f"{PMID}:wf1",
            "pmid": PMID,
            "question": {
                "text": "Does treatment change the myeloid compartment?",
                "evidence": [
                    {
                        "locator": "abstract:L1:P1:S1",
                        "verbatim": "Treatment changed the myeloid compartment.",
                    }
                ],
            },
            "comparison": "treated vs control",
            "cell_level": "myeloid cells",
            "steps": [
                {
                    "observation": "The myeloid fraction fell after treatment",
                    "locator": "results:L2:P1:S1",
                    "verbatim": "The myeloid fraction decreased after treatment.",
                    "next_analysis": "test whether the loss reflects egress",
                    "rationale": (
                        "a fall in local frequency can reflect egress rather than death"
                    ),
                },
                {
                    "observation": "A second observation",
                    "locator": "results:L2:P1:S2",
                    "verbatim": "A second sentence anchors the second step.",
                    "next_analysis": "repeat in an independent sample",
                    "rationale": (
                        "replication distinguishes biology from batch"
                    ),
                },
            ],
            "claims": [
                {
                    "claim": "Treatment reduces myeloid cells",
                    "locator": "results:L2:P1:S1",
                    "verbatim": "The myeloid fraction decreased after treatment.",
                    "evidence_type": "count",
                    "direction": "supports",
                }
            ],
            "gaps": [
                {
                    "text": "Only one time point was sampled",
                    "locator": "results:L3:P1:S1",
                }
            ],
            "author_focus_groups": [
                {"name": "ZEBRA_FOCUS_GROUP", "locator": "results:L2:P1:S1"}
            ],
            "no_prior_warning": (
                "author_focus_groups are recorded for context only; they MUST NOT "
                "seed candidate sets"
            ),
        },
        {
            "workflow_id": f"{RETRACTED}:wf1",
            "pmid": RETRACTED,
            "question": {
                "text": "Retracted question",
                "evidence": [
                    {
                        "locator": "results:L9:P1:S1",
                        "verbatim": "RETRACTED_UNIQUE_TOKEN appears in the retracted paper.",
                    }
                ],
            },
            "comparison": "retracted comparison",
            "cell_level": "retracted cells",
            "steps": [
                {
                    "observation": "RETRACTED_UNIQUE_TOKEN",
                    "locator": "results:L9:P1:S1",
                    "verbatim": "RETRACTED_UNIQUE_TOKEN appears in the retracted paper.",
                    "next_analysis": "none",
                    "rationale": "none",
                }
            ],
            "claims": [],
            "gaps": [],
            "author_focus_groups": [],
            "no_prior_warning": "",
        },
        {
            # No pmid field: the retracted PMID is only in the workflow_id.
            "workflow_id": f"{RETRACTED}:wf9",
            "question": {
                "text": "Missing-pmid retracted question",
                "evidence": [
                    {
                        "locator": "results:L1:P1:S1",
                        "verbatim": "MISSING_PMID_RETRACTED_TOKEN.",
                    }
                ],
            },
            "comparison": "retracted comparison",
            "cell_level": "retracted cells",
            "steps": [],
            "claims": [],
            "gaps": [],
            "author_focus_groups": [],
            "no_prior_warning": "",
        },
        {
            "workflow_id": f"{OUT_OF_SCOPE}:wf1",
            "pmid": OUT_OF_SCOPE,
            "question": {
                "text": "Out of scope question",
                "evidence": [
                    {
                        "locator": "results:L1:P1:S1",
                        "verbatim": "OUT_OF_SCOPE_TOKEN.",
                    }
                ],
            },
            "comparison": "out of scope comparison",
            "cell_level": "out of scope cells",
            "steps": [],
            "claims": [],
            "gaps": [],
            "author_focus_groups": [],
            "no_prior_warning": "",
        },
    ]


def default_methods() -> list[dict]:
    return [
        {
            "instance_id": f"{PMID}:methods:L9:P1:S1",
            "pmid": PMID,
            "locator": "methods:L9:P1:S1",
            "verbatim": "We used a two-tailed Wilcoxon rank-sum test.",
            "mentioned_name": "Wilcoxon",
            "canonical_name": "two-tailed Wilcoxon rank-sum test",
            "package": None,
            "operation": "per-cell two-group difference test",
            "pipeline_stage": "de",
            "analysis_object": "cells",
            "inputs": ["per-cell values"],
            "outputs": ["p-values"],
            "purpose": "characterization",
            "constraints": ["cells treated as independent"],
            "version_in_text": None,
            "method_class": "computational",
            "in_scope": True,
            "scope_reason": "used on single-cell results",
            "rerunnable_role": "rerunnable",
            "params": {"test": "wilcox"},
        }
    ]


def default_shards() -> list[dict]:
    return [
        {
            # Superseded by repair_a for the same PMID.
            "pmid": PMID,
            "status": "unresolved",
            "question": "Old question",
            "patterns": [
                {
                    "observation": "OLD_SHARD_OBSERVATION",
                    "ask_next": "old next",
                    "rationale_source": "inferred",
                    "limitations": "old limitation",
                    "source_locator": "results:L2:P1:S1",
                    "verbatim": "The myeloid fraction decreased after treatment.",
                    "prompt_lesson": "old lesson",
                }
            ],
        },
        {
            "pmid": SHARD_ONLY,
            "status": "reviewed",
            "question": "Shard-only question",
            "patterns": [
                {
                    "observation": "SHARD_ONLY_OBSERVATION",
                    "ask_next": "shard next",
                    "rationale_source": "inferred",
                    "limitations": "shard limitation",
                    "source_locator": "results:L4:P1:S1",
                    "verbatim": "Shard-only quote.",
                    "prompt_lesson": "SHARD_LESSON_TOKEN",
                },
                {
                    "observation": "UNVERIFIED_PATTERN_TOKEN",
                    "ask_next": "no anchor",
                    "rationale_source": "inferred",
                    "limitations": "none",
                    "source_locator": None,
                    "verbatim": None,
                    "prompt_lesson": "no anchor lesson",
                },
                {
                    "observation": "UC samples were compared.",
                    "ask_next": "lesson two",
                    "rationale_source": "inferred",
                    "limitations": "none",
                    "source_locator": "results:L4:P1:S2",
                    "verbatim": "UC samples were compared.",
                    "prompt_lesson": "lesson two",
                },
                {
                    "observation": "AUCell was used for scoring.",
                    "ask_next": "lesson three",
                    "rationale_source": "inferred",
                    "limitations": "none",
                    "source_locator": "results:L4:P1:S3",
                    "verbatim": "AUCell was used for scoring.",
                    "prompt_lesson": "lesson three",
                },
                {
                    "observation": "CD4+ T cells were enriched.",
                    "ask_next": "lesson four",
                    "rationale_source": "inferred",
                    "limitations": "none",
                    "source_locator": "results:L4:P1:S4",
                    "verbatim": "CD4+ T cells were enriched.",
                    "prompt_lesson": "lesson four",
                },
            ],
        },
        {
            # No pmid and no record id: a raw mention of the retracted PMID.
            "status": "reviewed",
            "patterns": [
                {
                    "observation": "RAW_REFERENCE_TOKEN cites PMID 36450980",
                    "ask_next": "none",
                    "rationale_source": "inferred",
                    "limitations": "none",
                    "source_locator": None,
                    "verbatim": None,
                    "prompt_lesson": "raw lesson",
                }
            ],
        },
        {
            # No pmid and no retracted reference: cannot be attributed.
            "status": "reviewed",
            "patterns": [
                {
                    "observation": "UNATTRIBUTED_TOKEN",
                    "ask_next": "none",
                    "rationale_source": "inferred",
                    "limitations": "none",
                    "source_locator": "results:L1:P1:S1",
                    "verbatim": "Unattributed quote.",
                    "prompt_lesson": "unattributed lesson",
                }
            ],
        },
    ]


def default_repairs() -> list[dict]:
    return [
        {
            "pmid": PMID,
            "status": "reviewed",
            "citation": "Test citation for PMID 11111111.",
            "question": "Repair question",
            "patterns": [
                {
                    "observation": "NEW_REPAIR_OBSERVATION",
                    "ask_next": "repair next",
                    "rationale_source": "explicit",
                    "limitations": "repair limitation",
                    "source_locator": "results:L2:P1:S1",
                    "verbatim": "The myeloid fraction decreased after treatment.",
                    "prompt_lesson": (
                        "PROMPT_LESSON_TOKEN is a design lesson, not paper evidence."
                    ),
                    "provenance": "added",
                },
                {
                    "observation": "PDF observation",
                    "ask_next": "pdf next",
                    "rationale_source": "inferred",
                    "limitations": "pdf limitation",
                    "source_locator": None,
                    "verbatim": None,
                    "pdf_page": 9,
                    "pdf_quote": "PDF_ONLY_QUOTE_TOKEN appears on the page.",
                    "prompt_lesson": "pdf lesson",
                },
            ],
        },
        {
            "pmid": RETRACTED,
            "status": "reviewed",
            "citation": "Retracted citation PMID 36450980.",
            "question": "Retracted repair question",
            "retraction": {"pmid": RETRACTED, "retraction_pmid": "40634621"},
            "patterns": [
                {
                    "observation": "RETRACTED_REPAIR_TOKEN",
                    "ask_next": "none",
                    "rationale_source": "inferred",
                    "limitations": "none",
                    "source_locator": "results:L1:P1:S1",
                    "verbatim": "RETRACTED_REPAIR_TOKEN.",
                    "prompt_lesson": "retracted lesson",
                }
            ],
        },
    ]


def write_corpus(
    root: Path,
    *,
    workflows: list[dict] | None = None,
    methods: list[dict] | None = None,
    shards: list[dict] | None = None,
    repairs: list[dict] | None = None,
    included: tuple[str, ...] = (PMID, RETRACTED, SHARD_ONLY),
) -> tuple[Path, list[SK.SourceSpec], Path]:
    research = root / "research"
    learn = research / "scope_learn"
    learn.mkdir(parents=True, exist_ok=True)
    write_jsonl(
        research / "workflows.jsonl",
        default_workflows() if workflows is None else workflows,
    )
    write_jsonl(
        research / "methods.jsonl",
        default_methods() if methods is None else methods,
    )
    write_jsonl(
        learn / "shard_6.jsonl",
        default_shards() if shards is None else shards,
    )
    write_jsonl(
        learn / "repair_a.jsonl",
        default_repairs() if repairs is None else repairs,
    )
    pmid_list = root / "included_pmids_422.txt"
    pmid_list.write_text("\n".join(included) + "\n", encoding="utf-8")
    specs = [
        SK.SourceSpec("workflow", research / "workflows.jsonl", "workflows"),
        SK.SourceSpec("method", research / "methods.jsonl", "methods"),
        SK.SourceSpec("pattern", learn / "shard_6.jsonl", "shard_6"),
        SK.SourceSpec("pattern", learn / "repair_a.jsonl", "repair_a"),
    ]
    return research, specs, pmid_list


def make_index(
    root: Path,
    *,
    workflows: list[dict] | None = None,
    methods: list[dict] | None = None,
    shards: list[dict] | None = None,
    repairs: list[dict] | None = None,
    included: tuple[str, ...] = (PMID, RETRACTED, SHARD_ONLY),
    extra_retractions=(),
    prefer_repairs: bool = True,
) -> SK.ScopeKnowledge:
    research, specs, pmid_list = write_corpus(
        root,
        workflows=workflows,
        methods=methods,
        shards=shards,
        repairs=repairs,
        included=included,
    )
    return SK.ScopeKnowledge(
        research_dir=research,
        pmid_list_path=pmid_list,
        source_files=specs,
        extra_retractions=extra_retractions,
        prefer_repairs=prefer_repairs,
    )


@pytest.fixture
def index(tmp_path) -> SK.ScopeKnowledge:
    return make_index(tmp_path)


# ------------------------------------------------------------------ retraction


def test_retracted_pmid_is_excluded_from_search_and_full_record(index):
    page = index.search("RETRACTED_UNIQUE_TOKEN", limit=50)
    assert page["hits"] == []

    stats = index.stats()
    assert stats["excluded"]["retracted_records"] >= 1

    record = index.record(RETRACTED)
    assert record["status"] == "excluded"
    assert record["reason"] == "retracted"
    assert record["records"] == []
    assert record["retraction"]["retraction_pmid"] == "40634621"


def test_retraction_survives_missing_pmid_field(index):
    page = index.search("MISSING_PMID_RETRACTED_TOKEN", limit=50)
    assert page["hits"] == []
    assert index.stats()["excluded"]["retracted_records"] >= 2


def test_record_mentioning_retracted_pmid_is_excluded(index):
    page = index.search("RAW_REFERENCE_TOKEN", limit=50)
    assert page["hits"] == []
    assert index.stats()["excluded"]["retracted_reference_records"] >= 1


def test_retraction_registry_is_extensible_and_append_only(tmp_path):
    extra = SK.RetractionNotice(
        pmid=OUT_OF_SCOPE,
        retraction_pmid=None,
        note="test-only retraction",
    )
    index = make_index(tmp_path, extra_retractions=[extra])

    assert OUT_OF_SCOPE in index.retractions
    assert RETRACTED in index.retractions
    assert index.search("OUT_OF_SCOPE_TOKEN", limit=50)["hits"] == []
    record = index.record(OUT_OF_SCOPE)
    assert record["status"] == "excluded"
    assert record["retraction"]["pmid"] == OUT_OF_SCOPE

    # Adding detail for an existing retraction cannot remove it.
    index.retractions.add(
        SK.RetractionNotice(pmid=RETRACTED, note="more detail")
    )
    assert index.retractions.get(RETRACTED).pmid == RETRACTED

    # A notice added after indexing is enforced at query time.
    hit = index.search("SHARD_ONLY_OBSERVATION", limit=10)["hits"][0]
    index.retractions.add(
        SK.RetractionNotice(pmid=SHARD_ONLY, note="late retraction")
    )
    assert index.search("SHARD_ONLY_OBSERVATION", limit=10)["hits"] == []
    assert index.get(hit["evidence_id"])["status"] == "excluded"
    assert index.record(SHARD_ONLY)["status"] == "excluded"


# ----------------------------------------------------------------------- scope


def test_pmid_list_is_authoritative(index):
    assert index.search("OUT_OF_SCOPE_TOKEN", limit=50)["hits"] == []
    assert index.stats()["excluded"]["out_of_scope_records"] >= 1

    page = index.search(
        "decreased after treatment",
        source_kinds="workflow",
        granularities="step",
        limit=10,
    )
    assert page["hits"]
    assert all(hit["in_scope"] is True for hit in page["hits"])

    assert index.record(OUT_OF_SCOPE)["status"] == "out_of_scope"


def test_missing_pmid_list_fails_closed(tmp_path, monkeypatch):
    research, specs, _ = write_corpus(tmp_path)
    monkeypatch.setattr(
        SK, "DEFAULT_PMID_LIST_CANDIDATES", (tmp_path / "absent.txt",)
    )
    monkeypatch.delenv(SK.PMID_LIST_ENV, raising=False)

    index = SK.ScopeKnowledge(research_dir=research, source_files=specs)
    with pytest.raises(SK.CorpusConfigError):
        index.build()


def test_missing_pmid_list_can_be_explicitly_unfiltered(tmp_path, monkeypatch):
    research, specs, _ = write_corpus(tmp_path)
    monkeypatch.setattr(
        SK, "DEFAULT_PMID_LIST_CANDIDATES", (tmp_path / "absent.txt",)
    )
    monkeypatch.delenv(SK.PMID_LIST_ENV, raising=False)

    index = SK.ScopeKnowledge(
        research_dir=research,
        source_files=specs,
        allow_missing_pmid_list=True,
    )
    stats = index.build()
    assert stats["pmid_list"]["scope_mode"] == "unfiltered"
    assert stats["pmid_list"]["n_pmids"] == 0

    # Retraction still wins when the scope list is unavailable.
    assert index.search("RETRACTED_UNIQUE_TOKEN", limit=50)["hits"] == []
    page = index.search("decreased after treatment", granularities="step")
    assert page["hits"]
    assert page["hits"][0]["in_scope"] is None


# ----------------------------------------------------------------------- tiers


def test_verbatim_and_inference_tiers_are_separated(index):
    verbatim = index.search(
        "decreased after treatment",
        source_kinds="workflow",
        granularities="step",
        limit=10,
    )["hits"][0]
    assert verbatim["tier"] == SK.TIER_VERBATIM
    assert verbatim["tier_scope"] == "query"
    assert verbatim["usable_as_evidence"] is False
    assert verbatim["requires_verification"] is True
    assert verbatim["claim_support"] == "not_asserted"
    assert verbatim["citation_status"] == "unknown"
    assert verbatim["anchor_status"] == "not_checked"

    rationale = index.search(
        "egress rather than death",
        source_kinds="workflow",
        granularities="step",
        limit=10,
    )["hits"][0]
    assert rationale["tier"] == SK.TIER_EXTRACTION
    assert "rationale" in rationale["matched_fields"]
    assert rationale["usable_as_evidence"] is False
    assert rationale["requires_verification"] is True
    assert rationale["claim_support"] == "not_asserted"

    method_inference = index.search(
        "per-cell two-group difference test",
        source_kinds="method",
        limit=10,
    )["hits"][0]
    assert method_inference["tier"] == SK.TIER_EXTRACTION
    assert "operation" in method_inference["matched_fields"]
    assert method_inference["verbatim_available"] is True
    assert method_inference["usable_as_evidence"] is False

    design = index.search("PROMPT_LESSON_TOKEN", source_kinds="pattern", limit=10)[
        "hits"
    ][0]
    assert design["tier"] == SK.TIER_DESIGN
    assert "prompt_lesson" in design["matched_fields"]
    assert "prompt_lesson" in design["design_inference_fields"]


def test_ascii_query_terms_respect_token_boundaries(index):
    uc_hits = index.search("UC", source_kinds="pattern", limit=50)["hits"]
    assert any(
        "UC samples were compared." in json.dumps(hit) for hit in uc_hits
    )
    assert not any("AUCell" in json.dumps(hit) for hit in uc_hits)

    aucell_hits = index.search("AUCell", source_kinds="pattern", limit=50)["hits"]
    assert aucell_hits
    assert all("AUCell" in json.dumps(hit) for hit in aucell_hits)

    plus_hits = index.search("CD4+", source_kinds="pattern", limit=50)["hits"]
    assert plus_hits
    assert all("CD4+" in json.dumps(hit) for hit in plus_hits)
    assert index.search("CD4", source_kinds="pattern", limit=50)["hits"]


def test_context_fields_are_searchable_but_not_evidence(index):
    hit = index.search("treated vs control", source_kinds="workflow", limit=10)[
        "hits"
    ][0]
    assert hit["tier"] == SK.TIER_EXTRACTION
    assert "comparison" in hit["matched_fields"]
    assert hit["context_only"] is True
    assert hit["usable_as_evidence"] is False
    assert hit["claim_support"] == "not_asserted"
    assert "comparison" in hit["record_context"]


def test_author_focus_is_not_searchable_or_candidate_evidence(index):
    assert index.search("ZEBRA_FOCUS_GROUP", limit=50)["hits"] == []
    assert (
        index.search(
            "ZEBRA_FOCUS_GROUP", include_unverified=True, limit=50
        )["hits"]
        == []
    )

    record = index.record(PMID, record_id=f"{PMID}:wf1")
    assert record["status"] == "ok"
    serialized = json.dumps(record, ensure_ascii=False)
    assert "ZEBRA_FOCUS_GROUP" not in serialized
    focus = record["records"][0]["record"]["author_focus_groups"]
    assert focus["redacted"] is True

    audit = index.record(
        PMID, record_id=f"{PMID}:wf1", include_author_focus=True
    )
    assert (
        audit["records"][0]["record"]["author_focus_groups"][0]["name"]
        == "ZEBRA_FOCUS_GROUP"
    )


# ------------------------------------------------------------------- unverified


def test_erratum_is_unknown_impact_not_retraction(tmp_path):
    methods = [
        {
            "instance_id": "41162704:methods:L1:P1:S1",
            "pmid": "41162704",
            "locator": "methods:L1:P1:S1",
            "verbatim": "ERRATUM_NOTE_TOKEN method sentence.",
            "canonical_name": "erratum test method",
            "operation": "erratum test",
            "pipeline_stage": "other",
            "analysis_object": "cells",
            "inputs": [],
            "outputs": [],
            "purpose": "background",
            "constraints": [],
            "method_class": "computational",
            "in_scope": True,
            "scope_reason": "test",
            "rerunnable_role": "rerunnable",
        }
    ]
    index = make_index(
        tmp_path,
        workflows=[],
        methods=methods,
        shards=[],
        repairs=[],
        included=(PMID, RETRACTED, SHARD_ONLY, "41162704"),
    )
    assert index.retractions.get("41162704") is None

    page = index.search("ERRATUM_NOTE_TOKEN", source_kinds="method", limit=10)
    assert page["hits"]
    hit = page["hits"][0]
    assert hit["citation_status"] == "unknown"
    assert hit["retraction_status"] == "not_excluded_by_frozen_registry"
    assert hit["correction_status"] == "recorded_impact_unknown"
    assert hit["editorial_notices"]
    notice = hit["editorial_notices"][0]
    assert notice["kind"] == "erratum"
    assert notice["impact"] == "unknown"
    assert "2026" in notice["notice_citation"]

    record = index.record("41162704")
    assert record["status"] == "ok"
    assert record["citation_status"] == "unknown"
    assert record["retraction_status"] == "not_excluded_by_frozen_registry"
    assert record["correction_status"] == "recorded_impact_unknown"
    assert record["editorial_notices"][0]["impact"] == "unknown"

    stats = index.stats()
    assert stats["editorial_notices"][0]["pmid"] == "41162704"
    assert "live PubMed" in stats["citation_checks"]


# ------------------------------------------------------------------- unverified


def test_unverified_records_are_hidden_by_default(index):
    assert index.search("Only one time point", limit=50)["hits"] == []

    page = index.search("Only one time point", include_unverified=True, limit=50)
    gap_hits = [hit for hit in page["hits"] if hit["granularity"] == "gap"]
    assert gap_hits
    gap = gap_hits[0]
    assert gap["tier"] == SK.TIER_UNVERIFIED
    assert gap["locator"] is not None
    assert gap["verbatim"] is None
    assert gap["usable_as_evidence"] is False

    assert index.get(gap["evidence_id"])["status"] == "unverified_hidden"
    visible = index.get(gap["evidence_id"], include_unverified=True)
    assert visible["status"] == "ok"
    assert visible["hit"]["tier"] == SK.TIER_UNVERIFIED
    assert visible["hit"]["usable_as_evidence"] is False


def test_unverified_pattern_is_hidden_until_requested(index):
    assert index.search("UNVERIFIED_PATTERN_TOKEN", limit=50)["hits"] == []

    page = index.search(
        "UNVERIFIED_PATTERN_TOKEN", include_unverified=True, limit=50
    )
    assert page["hits"]
    hit = page["hits"][0]
    assert hit["tier"] == SK.TIER_UNVERIFIED
    assert hit["locator"] is None
    assert hit["verbatim"] is None


# ------------------------------------------------------------------------- PDF


def test_pdf_page_anchor_is_a_distinct_locator(index):
    page = index.search("PDF_ONLY_QUOTE_TOKEN", source_kinds="pattern", limit=10)
    assert page["hits"]
    hit = page["hits"][0]
    assert hit["locator"] == f"pdf:{PMID}:p9"
    assert hit["locator_type"] == "pdf"
    assert hit["locator_section"] == "pdf"
    assert hit["tier"] == SK.TIER_VERBATIM
    assert hit["tier_scope"] == "query"
    assert hit["usable_as_evidence"] is False
    assert hit["claim_support"] == "not_asserted"
    assert hit["verbatim"] == "PDF_ONLY_QUOTE_TOKEN appears on the page."


# ------------------------------------------------------------------- pagination


def make_pagination_methods() -> list[dict]:
    return [
        {
            "instance_id": f"{PMID}:methods:L{index}:P1:S1",
            "pmid": PMID,
            "locator": f"methods:L{index}:P1:S1",
            "verbatim": f"PAGINATION_TOKEN record {index}.",
            "canonical_name": "pagination method",
            "operation": "pagination test",
            "pipeline_stage": "other",
            "analysis_object": "cells",
            "inputs": [],
            "outputs": [],
            "purpose": "background",
            "constraints": [],
            "method_class": "computational",
            "in_scope": True,
            "scope_reason": "test",
            "rerunnable_role": "rerunnable",
        }
        for index in range(7)
    ]


def test_pagination_is_explicit_and_bounded(tmp_path):
    index = make_index(
        tmp_path,
        workflows=[],
        methods=make_pagination_methods(),
        shards=[],
        repairs=[],
    )
    first = index.search("PAGINATION_TOKEN", limit=2, offset=0)
    assert first["page"] == {
        "offset": 0,
        "limit": 2,
        "total": 7,
        "returned": 2,
        "has_more": True,
        "next_offset": 2,
    }
    last = index.search("PAGINATION_TOKEN", limit=2, offset=6)
    assert last["page"]["returned"] == 1
    assert last["page"]["has_more"] is False
    assert last["page"]["next_offset"] is None

    beyond = index.search("PAGINATION_TOKEN", limit=2, offset=99)
    assert beyond["page"]["returned"] == 0
    assert beyond["page"]["has_more"] is False

    with pytest.raises(SK.QueryError):
        index.search(limit=0)
    with pytest.raises(SK.QueryError):
        index.search(limit=SK.MAX_PAGE_SIZE + 1)
    with pytest.raises(SK.QueryError):
        index.search(offset=-1)
    with pytest.raises(SK.QueryError):
        index.search("!!!")


# ------------------------------------------------------------ direct retrieval


def test_get_and_context_return_fine_grained_units(index):
    step_id = f"workflow:{PMID}:wf1:step:1"
    got = index.get(step_id)
    assert got["status"] == "ok"
    assert got["hit"]["granularity"] == "step"
    assert got["hit"]["unit_index"] == 1
    assert got["hit"]["content"]["verbatim"] == (
        "A second sentence anchors the second step."
    )
    # A read without matched fields must not choose a best tier.
    assert got["hit"]["tier"] is None
    assert got["hit"]["tier_scope"] == "unscoped"
    assert got["hit"]["usable_as_evidence"] is False
    assert got["hit"]["claim_support"] == "not_asserted"
    assert got["hit"]["field_tiers"]["verbatim"] == SK.TIER_VERBATIM

    scoped_inference = index.get(step_id, matched_fields=["rationale"])
    assert scoped_inference["hit"]["tier"] == SK.TIER_EXTRACTION
    assert scoped_inference["hit"]["tier_scope"] == "query"
    assert scoped_inference["hit"]["usable_as_evidence"] is False

    scoped_verbatim = index.get(step_id, matched_fields=["verbatim"])
    assert scoped_verbatim["hit"]["tier"] == SK.TIER_VERBATIM
    assert scoped_verbatim["hit"]["usable_as_evidence"] is False
    assert scoped_verbatim["hit"]["requires_anchor_verification"] is True

    context = index.context(step_id, before=1, after=0)
    assert context["status"] == "ok"
    assert all(hit["tier"] is None for hit in context["hits"])
    ids = [hit["evidence_id"] for hit in context["hits"]]
    assert step_id in ids
    assert f"workflow:{PMID}:wf1:step:0" in ids

    scoped_context = index.context(
        step_id, before=1, after=0, matched_fields=["rationale"]
    )
    assert all(
        hit["tier"] == SK.TIER_EXTRACTION for hit in scoped_context["hits"]
    )
    assert all(
        hit["usable_as_evidence"] is False for hit in scoped_context["hits"]
    )

    assert index.get("does-not-exist")["status"] == "not_found"
    with pytest.raises(SK.QueryError):
        index.context(step_id, before=SK.MAX_CONTEXT + 1)


def test_full_record_retrieval_is_sanitized_and_navigable(index):
    record = index.record(PMID, record_id=f"{PMID}:wf1")
    assert record["status"] == "ok"
    assert len(record["records"]) == 1
    item = record["records"][0]
    assert item["source_kind"] == "workflow"
    assert item["source_sha256"]
    assert item["record"]["comparison"] == "treated vs control"
    assert item["record"]["cell_level"] == "myeloid cells"
    assert any("step:0" in evidence_id for evidence_id in item["evidence_ids"])

    assert index.record(PMID, record_id="nope")["status"] == "not_found"
    methods = index.record(PMID, source_kind="method")
    assert methods["status"] == "ok"
    assert len(methods["records"]) == 1
    assert methods["records"][0]["source_kind"] == "method"


# ---------------------------------------------------------------- repair rules


def test_repair_record_supersedes_older_shard(index):
    assert index.search("OLD_SHARD_OBSERVATION", limit=50)["hits"] == []
    assert index.search("NEW_REPAIR_OBSERVATION", limit=50)["hits"]
    assert index.stats()["excluded"]["superseded_records"] >= 1

    # A shard record without a repair counterpart remains available.
    assert index.search("SHARD_ONLY_OBSERVATION", limit=50)["hits"]


def test_repair_precedence_can_be_disabled(tmp_path):
    index = make_index(tmp_path / "both", prefer_repairs=False)
    assert index.search("OLD_SHARD_OBSERVATION", limit=50)["hits"]


# ------------------------------------------------------------- corpus integrity


def test_build_is_read_only(tmp_path):
    research, specs, pmid_list = write_corpus(tmp_path)
    paths = [
        research / "workflows.jsonl",
        research / "methods.jsonl",
        research / "scope_learn" / "shard_6.jsonl",
        research / "scope_learn" / "repair_a.jsonl",
        pmid_list,
    ]
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    index = SK.ScopeKnowledge(
        research_dir=research,
        pmid_list_path=pmid_list,
        source_files=specs,
    )
    index.build()
    after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    assert before == after


def test_malformed_line_is_reported_not_indexed(tmp_path):
    research, specs, pmid_list = write_corpus(tmp_path)
    workflows_path = research / "workflows.jsonl"
    with workflows_path.open("a", encoding="utf-8") as handle:
        handle.write("{this is not json}\n")

    index = SK.ScopeKnowledge(
        research_dir=research,
        pmid_list_path=pmid_list,
        source_files=specs,
    )
    stats = index.build()
    assert stats["excluded"]["malformed_records"] >= 1
    assert stats["warning_count"] >= 1
    assert any("malformed_json" in warning for warning in stats["warnings"])
    assert index.search("decreased after treatment", limit=10)["hits"]


def test_corpus_fingerprint_is_stable_across_rebuilds(index):
    first = index.stats()
    index.build(force=True)
    second = index.stats()
    assert first["corpus_fingerprint"] == second["corpus_fingerprint"]


# --------------------------------------------------------------- real corpus


REAL_RESEARCH = ROOT / "research"
REAL_PMID_LIST = Path(
    "/data/disk1/zeiss/singlecell-quant/data/included_pmids_422.txt"
)


@pytest.mark.skipif(
    not (REAL_RESEARCH / "workflows.jsonl").is_file()
    or not REAL_PMID_LIST.is_file(),
    reason="real frozen corpus or 422-PMID list unavailable",
)
def test_real_frozen_corpus_contract():
    index = SK.ScopeKnowledge(
        research_dir=REAL_RESEARCH,
        pmid_list_path=REAL_PMID_LIST,
    )
    stats = index.build()
    assert stats["pmid_list"]["n_pmids"] == 422
    assert stats["n_hits_indexed"] > 10_000
    assert stats["excluded"]["retracted_records"] >= 1
    assert stats["excluded"]["out_of_scope_records"] > 0

    labels = {source["label"] for source in stats["sources"]}
    assert {"workflows", "methods", "shard_0", "repair_a"} <= labels

    page = index.search("pseudobulk", source_kinds="method", limit=5)
    assert page["status"] == "ok"
    assert page["page"]["total"] > 0
    assert all(hit["pmid"] != RETRACTED for hit in page["hits"])

    assert index.record(RETRACTED)["status"] == "excluded"
    assert index.record("32997960")["status"] == "ok"

    correction_hit = index.search("", pmids="41162704", limit=1)["hits"][0]
    assert correction_hit["citation_status"] == "unknown"
    assert any(
        notice["kind"] == "erratum" and notice["impact"] == "unknown"
        for notice in correction_hit["editorial_notices"]
    )
    correction_record = index.record("41162704")
    assert correction_record["status"] == "ok"
    assert correction_record["correction_status"] == "recorded_impact_unknown"
