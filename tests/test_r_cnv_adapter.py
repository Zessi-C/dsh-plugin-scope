"""Behaviour of the inferCNV adapter (``r/adapters.R::task_cnv_infercnv``).

``tests/cnv_adapter_driver.R`` replaces the CNV engine with an in-process R
shim, so these tests observe what the adapter itself decides — which package it
demands, which arguments reach ``infercnv::run``, what it writes — while staying
fast, deterministic, and never touching the real engine.

Two defects are pinned here, both silent in production:

  1. the gate accepted CopyKAT as a stand-in for inferCNV although the wired
     run calls ``infercnv::*``; with only CopyKAT installed the gate opened and
     the run died downstream instead of reporting an inferCNV dependency
     problem;
  2. ``window_size`` was computed, echoed in the metrics and never passed on,
     so every run smoothed with inferCNV's own default while the report claimed
     the requested width.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DRIVER = ROOT / "tests" / "cnv_adapter_driver.R"
MISSING_R_PACKAGES = 77


def _drive(mode: str) -> dict:
    rscript = shutil.which("Rscript")
    if rscript is None:
        pytest.skip("Rscript is not installed")
    proc = subprocess.run([rscript, str(DRIVER), mode], cwd=ROOT,
                          capture_output=True, text=True, timeout=900)
    if proc.returncode == MISSING_R_PACKAGES:
        pytest.skip(f"R packages missing: {proc.stdout.strip()}")
    assert proc.returncode == 0, proc.stderr
    # The driver emits one JSON document last; anything R printed before it is
    # incidental chatter that must not break parsing.
    payload = next((line for line in reversed(proc.stdout.splitlines())
                    if line.startswith("{")), None)
    assert payload is not None, proc.stdout
    return json.loads(payload)


@pytest.fixture(scope="module")
def gate() -> dict:
    return _drive("gate")


@pytest.fixture(scope="module")
def windows() -> dict:
    return _drive("window")


def test_copykat_cannot_stand_in_for_infercnv(gate):
    """CopyKAT installed, inferCNV not: the adapter must refuse as inferCNV-less."""
    assert gate["status"] == "insufficient_input", gate
    reason = gate["reason"].lower()
    assert "infercnv" in reason
    assert "copykat" in reason and "not run as a substitute" in reason
    # The refusal must be the dependency gate, not a downstream guard: the
    # driver passes a NULL object and a valid annotation column, so a gate that
    # let CopyKAT through would fail on the metadata lookup instead.
    assert "annotation column" not in reason


def test_the_documented_default_window_is_what_the_run_gets(windows):
    default = windows["default"]
    assert default["status"] == "succeeded", default
    assert default["run_args"]["window_length"] == 101
    assert default["metrics"]["window_size"] == 101


def test_window_size_reaches_infercnv_run_as_window_length(windows):
    """The parameter must reach the engine, and an even width must be usable."""
    custom = windows["custom"]
    assert custom["status"] == "succeeded", custom
    # inferCNV's smoother (caTools::runmean) rejects an even k, so 120 becomes
    # 121 — but it must be 121 at the engine, not merely in the metrics.
    assert custom["run_args"]["window_length"] == 121
    assert custom["metrics"]["window_size"] == 121


def test_the_reported_window_is_the_window_that_ran(windows):
    """Metrics and engine must agree on every run; the old code reported a width it never used."""
    for call in ("default", "custom"):
        run_args = windows[call]["run_args"]
        assert "window_length" in run_args, f"{call}: window_length never reached infercnv::run"
        assert run_args["window_length"] == windows[call]["metrics"]["window_size"]


def test_the_engine_receives_parseable_inputs(windows):
    """Gene order (4 columns) and annotations (one per cell) are written for real."""
    create = windows["custom"]["create"]
    assert create["gene_order_cols"] == 4
    assert create["n_anno"] == create["cells"] == 30
    assert create["genes"] >= 50
    assert create["ref_group_names"] == "Tcell"


def test_scores_separate_the_reference_from_the_query(windows):
    """The shim shifts query cells only; the adapter's scoring must show that."""
    for call in ("default", "custom"):
        outcome = windows[call]
        assert outcome["n_cells_csv"] == 30
        assert outcome["groups"] == ["Epithelial", "Tcell"]
        assert outcome["reference_scores"] == pytest.approx(0.0, abs=1e-12)
        assert outcome["query_scores"] == pytest.approx(0.09, abs=1e-12)
