"""TUI smoke test: the screen really composes, fills and runs a frozen plan.

Uses Textual's own pilot, so this exercises the real widget tree rather than a
mock. It proves the plan list is reachable from the TUI and that `run` executes a
frozen plan with no approval key in between.
"""
from __future__ import annotations

import asyncio

import pytest

from sctda import protocol as P
from sctda.tui import SctdaApp

pytest.importorskip("textual")


def test_tui_composes_and_populates(tmp_path, synthetic_object):
    from sctda.ingest import import_dataset
    root = tmp_path / "tui"
    import_dataset(root, synthetic_object, annotation_column="celltype",
                   sample_column="orig.ident", condition_column="group")

    async def scenario():
        app = SctdaApp(str(root))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            cand = app.query_one("#cand")
            assert cand.row_count == 2, "both annotated groups must be listed"
            prot = app.query_one("#prot")
            assert prot.row_count == 0          # nothing planned yet
            app.action_refresh()
            await pilot.pause()
            return True

    assert asyncio.run(scenario())


def test_tui_has_no_approval_action(tmp_path, synthetic_object):
    """A plan is frozen when written; the screen has no release step to offer."""
    from sctda.ingest import import_dataset
    root = tmp_path / "tui2"
    import_dataset(root, synthetic_object, annotation_column="celltype",
                   sample_column="orig.ident", condition_column="group")
    P.build_plan(root, methods=["composition"])
    app = SctdaApp(str(root))
    assert not hasattr(app, "action_approve")
    keys = [b[0] if isinstance(b, tuple) else b.key for b in app.BINDINGS]
    assert "a" not in keys, keys


def test_tui_runs_a_frozen_plan_and_updates_status(tmp_path, synthetic_object):
    from sctda.ingest import import_dataset
    root = tmp_path / "tui3"
    import_dataset(root, synthetic_object, annotation_column="celltype",
                   sample_column="orig.ident", condition_column="group")
    P.build_plan(root, methods=["composition"])

    async def scenario():
        app = SctdaApp(str(root))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_refresh()
            await pilot.pause()
            assert app.query_one("#prot").row_count == 1
            app.action_run()                    # frozen -> completed, no approval
            await pilot.pause()
            statuses = [p["status"] for p in app.state.get("protocols", [])]
            assert "completed" in statuses, statuses
            assert app.state["counts"]["artifacts"] > 0
        return True

    assert asyncio.run(scenario())
