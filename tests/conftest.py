"""Shared fixtures: one imported project over the synthetic object."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from scope_corpus import DB_ENV, RESEARCH_DIR_ENV, resolve_db_path  # noqa: E402
from sctda.ingest import import_dataset  # noqa: E402
from sctda.method_registry import PMID_LIST_ENV, read_pmid_list  # noqa: E402
from sctda.project import Project  # noqa: E402

SYNTH = ROOT / "tests/data/synthetic.rds"


@pytest.fixture(scope="session")
def corpus_405_scope():
    """Skip absent private defaults, not broken explicit configuration."""
    db = resolve_db_path()
    if not db.exists() and not (os.environ.get(DB_ENV) or os.environ.get(RESEARCH_DIR_ENV)):
        pytest.skip("private SQLite corpus not provisioned")
    assert db.is_file(), f"configured SQLite corpus is not a file: {db}"
    try:
        scope = read_pmid_list()
    except FileNotFoundError as exc:
        if os.environ.get(PMID_LIST_ENV):
            raise
        pytest.skip(str(exc))
    assert scope["matches_known_snapshot"], "live tests require the frozen 405-PMID list"
    return scope


@pytest.fixture(scope="session")
def synthetic_object() -> Path:
    if not SYNTH.exists():
        pytest.skip("synthetic object missing; run Rscript tests/make_synthetic.R")
    return SYNTH


@pytest.fixture
def project(tmp_path, synthetic_object) -> Path:
    root = tmp_path / "proj"
    out = import_dataset(root, synthetic_object, annotation_column="celltype",
                         sample_column="orig.ident", condition_column="group")
    assert out["status"] == "imported", out
    return root


@pytest.fixture
def bare_project(tmp_path, synthetic_object) -> Path:
    """Imported with no mappings at all: the ambiguous-column path."""
    root = tmp_path / "bare"
    out = import_dataset(root, synthetic_object)
    assert out["status"] == "needs_mapping", out
    return root


@pytest.fixture
def conn(project):
    c = Project(project).connect()
    yield c
    c.close()
