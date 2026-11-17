"""Shared fixtures: one imported project over the synthetic object."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from sctda.ingest import import_dataset  # noqa: E402
from sctda.project import Project  # noqa: E402

SYNTH = ROOT / "tests/data/synthetic.rds"


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
