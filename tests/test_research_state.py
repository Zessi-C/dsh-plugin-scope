"""Behaviour tests for the persistent research-process state.

These tests use a bare project directory: the research tables do not depend on
an imported dataset, so nothing here needs R or the synthetic object. They test
recovery, project/run association, legal and illegal transitions, and the
strict completed/open distinction. They are not a biological benchmark.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from sctda import research_state as R
from sctda.project import Project, get_or_create_project


@pytest.fixture
def root(tmp_path):
    return Project(tmp_path / "research").ensure().root


def _run(root, question="which subset drives the phenotype?", **kwargs):
    return R.start_run(root, question, **kwargs)["run"]


# --------------------------------------------------------------- run lifecycle
def test_run_is_versioned_persisted_and_retry_identical(root):
    first = R.start_run(root, "question one", parent_session_id="session-a")
    assert first["created"] is True
    run = first["run"]
    assert run["version"] == 1
    assert run["status"] == "running"
    assert run["is_open"] is True and run["is_terminal"] is False
    assert run["parent_session_id"] == "session-a"

    second = R.start_run(root, "question two")
    assert second["run"]["version"] == 2

    # a separate connection is the recovery path: no in-memory state is reused
    reopened = Project(root).connect()
    try:
        row = reopened.execute(
            "SELECT question, version FROM research_run WHERE run_id=?",
            (run["run_id"],)).fetchone()
        assert (row["question"], row["version"]) == ("question one", 1)
    finally:
        reopened.close()

    retry = R.start_run(root, "question one", run_id=run["run_id"],
                        parent_session_id="session-a")
    assert retry["created"] is False
    assert retry["run"]["run_id"] == run["run_id"]

    keyed = R.start_run(root, "keyed question", dedup_key="spawn-1")
    again = R.start_run(root, "keyed question", dedup_key="spawn-1")
    assert again["created"] is False
    assert again["run"]["run_id"] == keyed["run"]["run_id"]
    with pytest.raises(R.ConflictError):
        R.start_run(root, "different question", dedup_key="spawn-1")

    listed = R.list_runs(root)
    assert listed["total"] == 3
    assert [r["version"] for r in listed["runs"]] == [3, 2, 1]


def test_run_explicit_version_must_be_new(root):
    _run(root)
    with pytest.raises(R.ConflictError):
        R.start_run(root, "reuse version", version=1)
    newer = R.start_run(root, "later version", version=5)
    assert newer["run"]["version"] == 5


# -------------------------------------------------------------------- entries
def test_entries_keep_source_relations_and_status(root):
    run = _run(root)
    hypothesis = R.append_entry(
        root, run["run_id"], "hypothesis",
        "The rare subset is expanded in cases.",
        source_type="current_data", source_ref="artifact://composition",
        details={"direction": "up"})["entry"]
    observation = R.append_entry(
        root, run["run_id"], "observation",
        "The subset is 4.2% of cases and 1.1% of controls.",
        source_type="current_data", source_ref="artifact://composition",
        details={"cases": 0.042, "controls": 0.011})["entry"]
    gap = R.append_entry(
        root, run["run_id"], "gap", "No matched biopsy for two cases.",
        source_type="model_hypothesis")["entry"]
    assert hypothesis["kind"] == "hypothesis"
    assert hypothesis["status"] == "open" and hypothesis["is_open"] is True
    assert hypothesis["source"]["type"] == "current_data"
    assert hypothesis["source"]["ref"] == "artifact://composition"
    assert hypothesis["details"] == {"direction": "up"}
    assert observation["details"]["cases"] == 0.042
    assert gap["source"]["type"] == "model_hypothesis"

    link = R.link_entries(root, run["run_id"], observation["entry_id"],
                          hypothesis["entry_id"], "supports",
                          note="same direction")
    assert link["created"] is True
    duplicate = R.link_entries(root, run["run_id"], observation["entry_id"],
                               hypothesis["entry_id"], "supports")
    assert duplicate["created"] is False
    assert duplicate["link"]["link_id"] == link["link"]["link_id"]

    read = R.get_entry(root, hypothesis["entry_id"])["entry"]
    assert len(read["links"]) == 1
    assert read["links"][0]["direction"] == "in"
    assert read["links"][0]["relation"] == "supports"
    assert read["links"][0]["other_kind"] == "observation"

    filtered = R.list_entries(root, run["run_id"], kind="observation")
    assert filtered["total"] == 1
    assert filtered["entries"][0]["entry_id"] == observation["entry_id"]

    changed = R.set_entry_status(root, hypothesis["entry_id"], "supported",
                                 note="supported by composition")
    assert changed["changed"] is True
    assert changed["entry"]["status"] == "supported"
    assert changed["entry"]["status_note"] == "supported by composition"

    retired = R.set_entry_status(root, hypothesis["entry_id"], "retired")
    assert retired["entry"]["is_terminal"] is True
    with pytest.raises(R.InvalidTransitionError):
        R.set_entry_status(root, hypothesis["entry_id"], "open")


def test_entry_dedup_is_idempotent_but_payload_conflicts(root):
    run = _run(root)
    first = R.append_entry(root, run["run_id"], "decision", "Use sample-level check.",
                           dedup_key="d-1")
    retry = R.append_entry(root, run["run_id"], "decision", "Use sample-level check.",
                           source_type="model_hypothesis", dedup_key="d-1")
    assert retry["created"] is False
    assert retry["entry"]["entry_id"] == first["entry"]["entry_id"]
    # status is mutable, so a retry after a transition still resolves
    R.set_entry_status(root, first["entry"]["entry_id"], "resolved")
    after = R.append_entry(root, run["run_id"], "decision", "Use sample-level check.",
                           dedup_key="d-1")
    assert after["created"] is False
    assert after["entry"]["status"] == "resolved"
    with pytest.raises(R.ConflictError):
        R.append_entry(root, run["run_id"], "decision", "A different decision.",
                       dedup_key="d-1")


# ------------------------------------------------------- strict completion gate
def test_finish_refuses_open_delegation_and_rolls_back(root):
    run = _run(root)
    open_entry = R.append_entry(root, run["run_id"], "gap",
                                "A matched control cohort is missing.")["entry"]
    delegation = R.create_delegation(root, run["run_id"], "scout",
                                     "check the matched cohort")["delegation"]
    with pytest.raises(R.InvalidTransitionError) as exc:
        R.finish_run(root, run["run_id"])
    assert "open delegation" in str(exc.value)
    # the refused transaction left both rows untouched
    assert R.get_run(root, run["run_id"])["run"]["status"] == "running"
    assert R.get_delegation(root, delegation["delegation_id"])["delegation"]["status"] == "pending"

    settled = R.complete_delegation(root, delegation["delegation_id"],
                                    result={"cohort": "absent"})["delegation"]
    assert settled["status"] == "completed" and settled["is_terminal"] is True
    done = R.finish_run(root, run["run_id"], outcome={"closed": True})
    assert done["run"]["status"] == "completed"
    assert done["run"]["is_completed"] is True
    assert done["open_entries"] == 1
    assert done["run"]["counts"]["entries"] == 1
    assert done["run"]["outcome"] == {"closed": True}
    assert done["delegation_outcomes"] == {"completed": 1}

    with pytest.raises(R.InvalidTransitionError):
        R.finish_run(root, run["run_id"])
    with pytest.raises(R.InvalidTransitionError):
        R.append_entry(root, run["run_id"], "observation", "too late")
    with pytest.raises(R.InvalidTransitionError):
        R.create_delegation(root, run["run_id"], "scout", "too late")
    assert open_entry["status"] == "open"


def test_fail_cancels_open_delegations_atomically(root):
    run = _run(root)
    pending = R.create_delegation(root, run["run_id"], "writer", "draft")["delegation"]
    running = R.create_delegation(root, run["run_id"], "reader", "read")["delegation"]
    R.claim_delegation(root, running["delegation_id"], child_id="child-9",
                       session_id="session-9")
    failed = R.fail_run(root, run["run_id"], error="resource probe failed")
    assert failed["run"]["status"] == "failed"
    assert failed["run"]["is_failed"] is True
    assert failed["cancelled_delegations"] == 2
    for delegation in (pending, running):
        row = R.get_delegation(root, delegation["delegation_id"])["delegation"]
        assert row["status"] == "cancelled"
        assert row["is_terminal"] is True
        assert "resource probe failed" in row["error"]
    with pytest.raises(R.InvalidTransitionError):
        R.fail_run(root, run["run_id"], error="again")
    with pytest.raises(R.InvalidTransitionError):
        R.complete_delegation(root, running["delegation_id"], result={"late": True})


# ----------------------------------------------------------- delegation lifecycle
def test_delegation_lease_reclaim_and_completion(root):
    run = _run(root)
    created = R.create_delegation(root, run["run_id"], "scout", "check markers",
                                  request={"genes": ["A", "B"]},
                                  dedup_key="spawn-x")
    delegation = created["delegation"]
    assert created["created"] is True
    assert delegation["status"] == "pending" and delegation["note_count"] == 0
    retry = R.create_delegation(root, run["run_id"], "scout", "check markers",
                                request={"genes": ["A", "B"]}, dedup_key="spawn-x")
    assert retry["created"] is False
    assert retry["delegation"]["delegation_id"] == delegation["delegation_id"]

    t0 = "2030-01-01T00:00:00+00:00"
    claimed = R.claim_delegation(root, delegation["delegation_id"],
                                 child_id="child-1", session_id="session-1",
                                 lease_seconds=60, as_of_utc=t0)["delegation"]
    assert claimed["status"] == "running"
    assert claimed["child_id"] == "child-1"
    assert claimed["session_id"] == "session-1"
    assert claimed["attempt"] == 1
    assert claimed["is_open"] is True and claimed["is_stale"] is False

    same = R.claim_delegation(root, delegation["delegation_id"],
                              child_id="child-1", lease_seconds=60,
                              as_of_utc=t0)["delegation"]
    assert same["attempt"] == 1
    with pytest.raises(R.ConflictError):
        R.claim_delegation(root, delegation["delegation_id"],
                           child_id="child-2", as_of_utc=t0)

    entry = R.append_entry(root, run["run_id"], "observation",
                           "Marker A is present.", delegation_id=delegation["delegation_id"])["entry"]
    assert entry["delegation_id"] == delegation["delegation_id"]
    assert R.get_delegation(root, delegation["delegation_id"])["delegation"]["note_count"] == 1
    scoped = R.list_entries(root, run["run_id"],
                            delegation_id=delegation["delegation_id"])
    assert scoped["total"] == 1
    assert scoped["entries"][0]["entry_id"] == entry["entry_id"]

    later = "2030-01-01T00:02:00+00:00"       # lease expired at +60s
    recovered = R.recover_run(root, as_of_utc=later)
    assert recovered["n_runs"] == 1
    open_delegation = recovered["runs"][0]["open_delegations"][0]
    assert open_delegation["is_stale"] is True
    assert recovered["runs"][0]["counts"]["stale_delegations"] == 1

    takeover = R.claim_delegation(root, delegation["delegation_id"],
                                  child_id="child-2", session_id="session-2",
                                  as_of_utc=later)
    assert takeover["reclaimed"] is True
    assert takeover["delegation"]["attempt"] == 2
    assert takeover["delegation"]["child_id"] == "child-2"
    assert takeover["delegation"]["is_stale"] is False

    done = R.complete_delegation(root, delegation["delegation_id"],
                                 result={"markers": ["A"]},
                                 child_id="child-2")["delegation"]
    assert done["status"] == "completed"
    assert done["result"] == {"markers": ["A"]}
    assert done["is_terminal"] is True and done["is_completed"] is True
    assert done["note_count"] == 1
    repeat = R.complete_delegation(root, delegation["delegation_id"],
                                   result={"markers": ["A"]})
    assert repeat["changed"] is False
    with pytest.raises(R.ConflictError):
        R.complete_delegation(root, delegation["delegation_id"],
                              result={"markers": ["B"]})
    with pytest.raises(R.InvalidTransitionError):
        R.fail_delegation(root, delegation["delegation_id"], error="too late")


def test_delegation_dedup_and_heartbeat_errors(root):
    run = _run(root)
    delegation = R.create_delegation(root, run["run_id"], "scout", "survey",
                                     dedup_key="k")["delegation"]
    with pytest.raises(R.ConflictError):
        R.create_delegation(root, run["run_id"], "scout", "different task",
                            dedup_key="k")
    with pytest.raises(R.InvalidTransitionError):
        R.heartbeat_delegation(root, delegation["delegation_id"])
    R.claim_delegation(root, delegation["delegation_id"], child_id="c")
    beat = R.heartbeat_delegation(root, delegation["delegation_id"],
                                  lease_seconds=120)
    assert beat["changed"] is True
    assert beat["delegation"]["is_stale"] is False


# --------------------------------------------------------------- DSH event path
def test_start_subagent_binds_parent_session_and_settles(root):
    run = _run(root, parent_session_id="session-A")
    started = R.start_subagent(
        root, parent_session_id="session-A", provider="dsh",
        dsh_run_id="dsh-run-1", child_id="child-1", session_id="child-session-1",
        label="scout", task_prompt="Check the marker panel.", role="scout")
    delegation = started["delegation"]
    assert started["created"] is True
    assert delegation["status"] == "running"
    assert delegation["run_id"] == run["run_id"]
    assert delegation["parent_session_id"] == "session-A"
    assert delegation["provider"] == "dsh"
    assert delegation["dsh_run_id"] == "dsh-run-1"
    assert delegation["child_id"] == "child-1"
    assert delegation["session_id"] == "child-session-1"
    assert delegation["label"] == "scout"
    assert delegation["task_prompt"] == "Check the marker panel."
    assert delegation["task"] == "Check the marker panel."

    assert R.find_delegation(root, child_id="child-1")["delegation"]["delegation_id"] == \
        delegation["delegation_id"]
    assert R.find_delegation(root, dsh_run_id="dsh-run-1")["delegation"]["delegation_id"] == \
        delegation["delegation_id"]

    R.append_entry(root, run["run_id"], "observation", "Panel is expressed.",
                   delegation_id=delegation["delegation_id"])
    settled = R.settle_delegation(
        root, delegation["delegation_id"], status="settled",
        stop_reason="end_turn", output_excerpt="Panel is expressed on the subset.",
        output_sha256="abc123", provider="dsh", label="scout")
    assert settled["delegation"]["status"] == "completed"
    assert settled["delegation"]["stop_reason"] == "end_turn"
    assert settled["delegation"]["output_sha256"] == "abc123"
    assert settled["delegation"]["output_excerpt"].startswith("Panel is expressed")
    assert settled["delegation"]["result"] == {
        "last_assistant_message": "Panel is expressed on the subset."}
    assert settled["delegation"]["note_count"] == 1

    with pytest.raises(R.InvalidTransitionError):
        R.start_subagent(root, parent_session_id="session-A", provider="dsh",
                         dsh_run_id="dsh-run-1", child_id="child-1",
                         label="scout")
    with pytest.raises(R.NotFoundError):
        R.start_subagent(root, parent_session_id="session-missing", provider="dsh",
                         dsh_run_id="dsh-x", child_id="child-x", label="scout")


def test_start_subagent_requires_unambiguous_run(root):
    with pytest.raises(R.NotFoundError):
        R.start_subagent(root, parent_session_id="session-A", provider="dsh",
                         dsh_run_id="dsh-1", child_id="child-1", label="scout")
    run = _run(root, parent_session_id="session-A")
    _run(root, "second question", parent_session_id="session-A")
    with pytest.raises(R.ConflictError):
        R.start_subagent(root, parent_session_id="session-A", provider="dsh",
                         dsh_run_id="dsh-1", child_id="child-1", label="scout")
    # explicit run_id disambiguates
    started = R.start_subagent(root, run_id=run["run_id"],
                               parent_session_id="session-A", provider="dsh",
                               dsh_run_id="dsh-1", child_id="child-1",
                               label="scout")
    assert started["delegation"]["run_id"] == run["run_id"]


def test_start_subagent_binds_precreated_pending_delegation(root):
    run = _run(root, parent_session_id="session-A")
    pre = R.create_delegation(root, run["run_id"], "scout", "survey",
                              dedup_key="spawn-7")["delegation"]
    t0 = "2030-01-01T00:00:00+00:00"
    started = R.start_subagent(root, parent_session_id="session-A", provider="dsh",
                               dsh_run_id="dsh-7", child_id="child-7",
                               label="scout", dedup_key="spawn-7", as_of_utc=t0)
    assert started["created"] is False
    assert started["delegation"]["delegation_id"] == pre["delegation_id"]
    assert started["delegation"]["status"] == "running"
    assert started["delegation"]["attempt"] == 1

    same = R.start_subagent(root, parent_session_id="session-A", provider="dsh",
                            dsh_run_id="dsh-7", child_id="child-7",
                            label="scout", dedup_key="spawn-7", as_of_utc=t0)
    assert same["created"] is False and same["reclaimed"] is False

    with pytest.raises(R.ConflictError):
        R.start_subagent(root, parent_session_id="session-A", provider="dsh",
                         dsh_run_id="dsh-8", child_id="child-8", label="scout",
                         dedup_key="spawn-7", as_of_utc=t0)
    takeover = R.start_subagent(root, parent_session_id="session-A",
                                provider="dsh", dsh_run_id="dsh-8",
                                child_id="child-8", label="scout",
                                dedup_key="spawn-7",
                                as_of_utc="2030-01-01T00:20:00+00:00")
    assert takeover["reclaimed"] is True
    assert takeover["delegation"]["attempt"] == 2
    assert takeover["delegation"]["child_id"] == "child-8"


def test_finish_run_records_lost_child_without_calling_it_complete(root):
    run = _run(root)
    delegation = R.create_delegation(root, run["run_id"], "scout", "survey")["delegation"]
    R.claim_delegation(root, delegation["delegation_id"], child_id="child-lost")
    R.settle_delegation(root, delegation["delegation_id"], status="lost",
                        stop_reason="no_end_event")
    done = R.finish_run(root, run["run_id"])
    assert done["run"]["status"] == "completed"
    assert done["delegation_outcomes"] == {"lost": 1}
    assert done["incomplete_delegations"] == 1
    rows = R.list_delegations(root, run["run_id"])
    assert rows["delegations"][0]["is_completed"] is False
    assert rows["delegations"][0]["is_lost"] is True


def test_attach_binds_late_ids_and_starts_pending(root):
    run = _run(root)
    delegation = R.create_delegation(root, run["run_id"], "scout", "survey")["delegation"]
    attached = R.attach_subagent(
        root, delegation_id=delegation["delegation_id"], child_id="child-late",
        session_id="session-late", dsh_run_id="dsh-late", provider="dsh",
        label="late scout", task_prompt="survey markers")
    row = attached["delegation"]
    assert attached["started"] is True
    assert row["status"] == "running"
    assert row["child_id"] == "child-late"
    assert row["session_id"] == "session-late"
    assert row["dsh_run_id"] == "dsh-late"
    assert row["provider"] == "dsh"
    assert row["label"] == "late scout"
    assert row["task_prompt"] == "survey markers"

    with pytest.raises(R.ConflictError):
        R.attach_subagent(root, delegation_id=delegation["delegation_id"],
                          child_id="another-child")
    settled = R.settle_delegation(root, delegation["delegation_id"],
                                  status="completed", result={"ok": True})
    assert settled["delegation"]["status"] == "completed"
    with pytest.raises(R.InvalidTransitionError):
        R.attach_subagent(root, delegation_id=delegation["delegation_id"],
                          label="too late")

    listed = R.list_delegations(root)
    assert listed["total"] == 1
    assert listed["delegations"][0]["delegation_id"] == delegation["delegation_id"]


def test_lost_child_is_terminal_not_completed(root):
    run = _run(root)
    delegation = R.create_delegation(root, run["run_id"], "scout", "survey")["delegation"]
    R.claim_delegation(root, delegation["delegation_id"], child_id="child-lost")
    lost = R.settle_delegation(root, delegation["delegation_id"], status="lost",
                               stop_reason="no_end_event")
    row = lost["delegation"]
    assert row["status"] == "lost"
    assert row["is_terminal"] is True
    assert row["is_completed"] is False
    assert row["is_lost"] is True
    assert row["error"] == "lost: no_end_event"
    with pytest.raises(R.InvalidTransitionError):
        R.complete_delegation(root, delegation["delegation_id"], result={"x": 1})
    with pytest.raises(R.ConflictError):
        R.settle_delegation(root, delegation["delegation_id"], status="completed",
                            output_excerpt="late")


# ------------------------------------------------------- project association
def test_rows_are_invisible_from_another_project(tmp_path):
    root_a = Project(tmp_path / "a").ensure().root
    root_b = Project(tmp_path / "b").ensure().root
    run_a = _run(root_a)
    entry_a = R.append_entry(root_a, run_a["run_id"], "observation", "A only")["entry"]
    del_a = R.create_delegation(root_a, run_a["run_id"], "scout", "survey")["delegation"]
    with pytest.raises(R.NotFoundError):
        R.get_run(root_b, run_a["run_id"])
    with pytest.raises(R.NotFoundError):
        R.get_entry(root_b, entry_a["entry_id"])
    with pytest.raises(R.NotFoundError):
        R.get_delegation(root_b, del_a["delegation_id"])
    with pytest.raises(R.NotFoundError):
        R.claim_delegation(root_b, del_a["delegation_id"], child_id="c")
    with pytest.raises(R.NotFoundError):
        R.recover_run(root_b, run_a["run_id"])


def test_entries_and_links_cannot_cross_runs(root):
    run_a = _run(root, "question a")
    run_b = _run(root, "question b")
    entry_a = R.append_entry(root, run_a["run_id"], "hypothesis", "A")["entry"]
    entry_b = R.append_entry(root, run_b["run_id"], "hypothesis", "B")["entry"]
    del_b = R.create_delegation(root, run_b["run_id"], "scout", "survey")["delegation"]
    with pytest.raises(R.ValidationError):
        R.append_entry(root, run_a["run_id"], "observation", "wrong child",
                       delegation_id=del_b["delegation_id"])
    with pytest.raises(R.ValidationError):
        R.link_entries(root, run_a["run_id"], entry_a["entry_id"],
                       entry_b["entry_id"], "supports")
    with pytest.raises(R.ValidationError):
        R.link_entries(root, run_a["run_id"], entry_a["entry_id"],
                       entry_a["entry_id"], "supports")


# ------------------------------------------------------- parameter validation
def test_invalid_parameters_raise_validation_error(root):
    with pytest.raises(R.ValidationError):
        R.start_run(root, "   ")
    run = _run(root)
    with pytest.raises(R.ValidationError):
        R.append_entry(root, run["run_id"], "not_a_kind", "x")
    with pytest.raises(R.ValidationError):
        R.append_entry(root, run["run_id"], "observation", "x", status="done")
    with pytest.raises(R.ValidationError):
        R.append_entry(root, run["run_id"], "observation", "x",
                       source_type="rumour")
    with pytest.raises(R.ValidationError):
        R.append_entry(root, run["run_id"], "observation", "x",
                       details={"bad": object()})
    with pytest.raises(R.ValidationError):
        R.list_entries(root, run["run_id"], limit=0)
    with pytest.raises(R.ValidationError):
        R.list_entries(root, run["run_id"], offset=-1)
    with pytest.raises(R.ValidationError):
        R.create_delegation(root, run["run_id"], "", "task")
    with pytest.raises(R.ValidationError):
        R.create_delegation(root, run["run_id"], "scout", "")
    with pytest.raises(R.ValidationError):
        R.claim_delegation(root, "missing", child_id="c", lease_seconds=0)
    with pytest.raises(R.NotFoundError):
        R.set_entry_status(root, "ent_missing", "open")
    with pytest.raises(R.ValidationError):
        R.recover_run(root, entry_limit=0)


def test_completed_delegation_requires_a_result(root):
    run = _run(root)
    delegation = R.create_delegation(root, run["run_id"], "scout", "survey")["delegation"]
    with pytest.raises(R.ValidationError):
        R.complete_delegation(root, delegation["delegation_id"], result=None)
    R.claim_delegation(root, delegation["delegation_id"], child_id="c")
    with pytest.raises(R.ValidationError):
        R.settle_delegation(root, delegation["delegation_id"], status="completed")
    with pytest.raises(R.ValidationError):
        R.settle_delegation(root, delegation["delegation_id"], status="failed")
    # failed still records its actual error
    result = R.settle_delegation(root, delegation["delegation_id"], status="failed",
                                 error="child crashed")
    assert result["delegation"]["status"] == "failed"
    assert result["delegation"]["error"] == "child crashed"


# --------------------------------------------------------------- recovery view
def test_recovery_returns_open_work_and_finished_runs_on_request(root):
    run = _run(root, parent_session_id="session-A")
    open_entry = R.append_entry(root, run["run_id"], "gap", "still open")["entry"]
    closed_entry = R.append_entry(root, run["run_id"], "decision", "settled",
                                  status="resolved")["entry"]
    delegation = R.create_delegation(root, run["run_id"], "scout", "survey")["delegation"]
    R.claim_delegation(root, delegation["delegation_id"], child_id="c-1",
                       session_id="s-1")

    recovered = R.recover_run(root)
    assert recovered["n_runs"] == 1
    snapshot = recovered["runs"][0]
    assert snapshot["run"]["run_id"] == run["run_id"]
    assert [e["entry_id"] for e in snapshot["open_entries"]] == [open_entry["entry_id"]]
    assert [d["delegation_id"] for d in snapshot["open_delegations"]] == [
        delegation["delegation_id"]]
    assert snapshot["counts"]["open_entries"] == 1
    assert snapshot["counts"]["open_delegations"] == 1
    assert closed_entry["status"] == "resolved"

    R.complete_delegation(root, delegation["delegation_id"], result={"done": True})
    R.finish_run(root, run["run_id"])
    assert R.recover_run(root)["n_runs"] == 0
    all_runs = R.recover_run(root, include_finished=True)
    assert all_runs["n_runs"] == 1
    assert all_runs["runs"][0]["run"]["status"] == "completed"
    # an open gap remains visible after the run closed
    assert all_runs["runs"][0]["counts"]["open_entries"] == 1


def test_run_staleness_is_reported_not_mutated(root):
    run = _run(root)
    fresh = R.get_run(root, run["run_id"], stale_after_seconds=3600)["run"]
    assert fresh["is_stale"] is False
    stale = R.get_run(root, run["run_id"], stale_after_seconds=0,
                      as_of_utc="2030-01-01T00:00:00+00:00")["run"]
    assert stale["is_stale"] is True
    assert stale["status"] == "running"
    R.heartbeat_run(root, run["run_id"])
    assert R.get_run(root, run["run_id"])["run"]["status"] == "running"


# --------------------------------------------------------------- concurrency
def test_concurrent_appends_are_serialised_and_deduped(root):
    run = _run(root)

    def add(index: int) -> str:
        return R.append_entry(
            root, run["run_id"], "observation", f"observation {index}",
            dedup_key=f"concurrent-{index}")["entry"]["entry_id"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(add, range(12)))
    assert len(ids) == 12
    assert len(set(ids)) == 12
    assert R.list_entries(root, run["run_id"], limit=100)["total"] == 12

    # the same dedup key from several threads resolves to one entry
    def same(_: int) -> dict:
        return R.append_entry(root, run["run_id"], "decision", "one decision",
                              dedup_key="one-decision")

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(same, range(6)))
    chosen = {outcome["entry"]["entry_id"] for outcome in outcomes}
    assert len(chosen) == 1
    assert sum(1 for outcome in outcomes if outcome["created"]) == 1


# ------------------------------------------------- cross-session recovery/rebind
def test_restarted_session_can_rebind_the_run_only_with_the_explicit_flag(root):
    run = _run(root, parent_session_id="session-old")
    opened = R.create_delegation(root, run["run_id"], "scout", "survey",
                                 parent_session_id="session-old")["delegation"]
    R.complete_delegation(root, opened["delegation_id"], result={"done": True})

    # A new session that learned the run id is refused without the flag...
    with pytest.raises(R.ConflictError):
        R.start_subagent(root, parent_session_id="session-new", provider="dsh",
                         dsh_run_id="dsh-1", child_id="child-1", label="scout",
                         run_id=run["run_id"])
    assert R.get_run(root, run["run_id"])["run"]["parent_session_id"] == "session-old"

    # ...and takes ownership of the next child's row with it.
    started = R.start_subagent(
        root, parent_session_id="session-new", provider="dsh", dsh_run_id="dsh-1",
        child_id="child-1", label="scout", session_id="child-session-1",
        run_id=run["run_id"], rebind_parent_session=True)
    assert started["delegation"]["parent_session_id"] == "session-new"
    rebound = R.get_run(root, run["run_id"])["run"]
    assert rebound["parent_session_id"] == "session-new"
    assert rebound["status"] == "running"

    # The rebound run is what recovery returns for the new session's children.
    snapshot = R.recover_run(root)
    assert snapshot["n_runs"] == 1
    assert [d["child_id"] for d in snapshot["runs"][0]["open_delegations"]] == ["child-1"]


def test_rebind_can_adopt_the_only_running_run_without_a_run_id(root):
    run = _run(root, parent_session_id="session-old")
    # Without a run id and without the flag the new session owns nothing; the
    # answer is an explicit miss, never a silent adoption of the old session's
    # run.
    with pytest.raises(R.NotFoundError):
        R.start_subagent(root, parent_session_id="session-new", provider="dsh",
                         dsh_run_id="dsh-x", child_id="child-x", label="scout")
    assert R.get_run(root, run["run_id"])["run"]["parent_session_id"] == "session-old"
    started = R.start_subagent(
        root, parent_session_id="session-new", provider="dsh", dsh_run_id="dsh-x",
        child_id="child-x", label="scout", rebind_parent_session=True)
    assert started["delegation"]["run_id"] == run["run_id"]
    assert R.get_run(root, run["run_id"])["run"]["parent_session_id"] == "session-new"


def test_rebind_refuses_ambiguity_and_closed_runs(root):
    first = _run(root, "first", parent_session_id="session-old")
    _run(root, "second", parent_session_id="session-old")
    with pytest.raises(R.ConflictError):
        R.start_subagent(root, parent_session_id="session-new", provider="dsh",
                         dsh_run_id="dsh-a", child_id="child-a", label="scout",
                         rebind_parent_session=True)
    R.fail_run(root, first["run_id"], error="stopped")
    with pytest.raises(R.InvalidTransitionError):
        R.start_run(root, first["question"], run_id=first["run_id"],
                    parent_session_id="session-new", rebind_parent_session=True)


def test_start_run_retry_rebinds_instead_of_conflicting(root):
    first = R.start_run(root, "shared question", dedup_key="boot",
                        parent_session_id="session-old")
    with pytest.raises(R.ConflictError):
        R.start_run(root, "shared question", dedup_key="boot",
                    parent_session_id="session-new")
    again = R.start_run(root, "shared question", dedup_key="boot",
                        parent_session_id="session-new", rebind_parent_session=True)
    assert again["created"] is False
    assert again["run"]["run_id"] == first["run"]["run_id"]
    assert again["run"]["parent_session_id"] == "session-new"


def test_attach_rebinds_the_run_and_the_row_only_when_asked(root):
    run = _run(root, parent_session_id="session-old")
    delegation = R.create_delegation(root, run["run_id"], "scout", "survey",
                                     parent_session_id="session-old")["delegation"]
    with pytest.raises(R.ConflictError):
        R.attach_subagent(root, delegation_id=delegation["delegation_id"],
                          parent_session_id="session-new")
    attached = R.attach_subagent(root, delegation_id=delegation["delegation_id"],
                                 child_id="child-late",
                                 parent_session_id="session-new",
                                 rebind_parent_session=True)
    assert attached["delegation"]["parent_session_id"] == "session-new"
    assert R.get_run(root, run["run_id"])["run"]["parent_session_id"] == "session-new"


def test_claim_honours_the_same_staleness_window_as_recovery(root):
    run = _run(root)
    delegation = R.create_delegation(root, run["run_id"], "scout", "survey")["delegation"]
    R.claim_delegation(root, delegation["delegation_id"], child_id="child-a",
                       lease_seconds=600)
    # The lease is long, but the heartbeat is older than the reporting window;
    # recovery calls that stale, so a claim with the same window must agree
    # instead of reporting a contradiction.
    reported = R.list_delegations(root, run["run_id"], stale_after_seconds=1,
                                  as_of_utc=_plus_seconds(120))
    assert reported["delegations"][0]["is_stale"] is True
    with pytest.raises(R.ConflictError):
        R.claim_delegation(root, delegation["delegation_id"], child_id="child-b",
                           as_of_utc=_plus_seconds(120))
    reclaimed = R.claim_delegation(root, delegation["delegation_id"],
                                   child_id="child-b", stale_after_seconds=1,
                                   as_of_utc=_plus_seconds(120))
    assert reclaimed["reclaimed"] is True
    assert reclaimed["delegation"]["attempt"] == 2


def _plus_seconds(seconds: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


# ------------------------------------------------- project identity and migration
def test_two_first_calls_agree_on_one_project_row(tmp_path):
    root = Project(tmp_path / "race").ensure().root

    def first_call(_: int) -> str:
        conn = Project(root).connect()
        try:
            return get_or_create_project(conn, root)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(first_call, range(4)))
    assert len(set(ids)) == 1
    conn = Project(root).connect()
    try:
        assert conn.execute("SELECT COUNT(*) c FROM project").fetchone()["c"] == 1
    finally:
        conn.close()

    # A run written after the race is readable, not "not found".
    run = _run(root)
    assert R.get_run(root, run["run_id"])["run"]["run_id"] == run["run_id"]


def test_duplicate_project_rows_are_refused_instead_of_silently_picked(tmp_path):
    from sctda.project import ProjectStateError
    root = Project(tmp_path / "dup").ensure().root
    conn = Project(root).connect()
    try:
        conn.execute("DROP INDEX IF EXISTS uq_project_root")
        for index, moment in enumerate(("2024-01-01T00:00:00+00:00",
                                        "2024-01-02T00:00:00+00:00")):
            conn.execute(
                "INSERT INTO project(project_id, root, created_utc) VALUES (?,?,?)",
                (f"prj_{index}", str(root), moment))
        conn.commit()
        with pytest.raises(ProjectStateError) as exc:
            get_or_create_project(conn, root)
        assert "2 project rows" in str(exc.value)
        # the connection still works; only the ambiguous identity is refused
        assert conn.execute("SELECT COUNT(*) c FROM project").fetchone()["c"] == 2
    finally:
        conn.close()


def test_legacy_artifact_rebuild_keeps_the_project_openable(tmp_path):
    import sqlite3
    root = (tmp_path / "legacy")
    root.mkdir()
    conn = sqlite3.connect(root / "project.db")
    conn.executescript(f"""
        CREATE TABLE project(project_id TEXT PRIMARY KEY, root TEXT NOT NULL,
                             created_utc TEXT NOT NULL, notes TEXT);
        INSERT INTO project VALUES('prj_old','{root}','2024-01-01T00:00:00+00:00',NULL);
        CREATE TABLE artifact(artifact_id TEXT PRIMARY KEY, task_id TEXT,
            protocol_id TEXT NOT NULL, kind TEXT NOT NULL, path TEXT NOT NULL,
            sha256 TEXT, n_rows INTEGER, n_cols INTEGER, created_utc TEXT NOT NULL);
        CREATE TABLE evidence(evidence_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            claim_id TEXT, source_type TEXT NOT NULL, origin TEXT,
            artifact_id TEXT REFERENCES artifact(artifact_id), locator TEXT, pmid TEXT,
            dedup_key TEXT NOT NULL, payload_json TEXT, created_utc TEXT NOT NULL,
            UNIQUE(project_id, dedup_key));
        INSERT INTO artifact VALUES('art_1',NULL,'prot_1','table','a.csv','deadbeef',
                                    1,1,'2024-01-01T00:00:00+00:00');
        INSERT INTO evidence(evidence_id,project_id,source_type,artifact_id,dedup_key,
                             created_utc)
            VALUES('ev_1','prj_old','current_data','art_1','k1',
                   '2024-01-01T00:00:00+00:00');
    """)
    conn.commit()
    conn.close()

    first = Project(root).connect()
    try:
        columns = {r[1] for r in first.execute("PRAGMA table_info(artifact)")}
        assert "checksum" in columns and "sha256" not in columns
        row = first.execute("SELECT checksum, checksum_algo FROM artifact").fetchone()
        assert (row["checksum"], row["checksum_algo"]) == ("deadbeef", "md5")
        assert first.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        first.close()
    # The failure mode this guards: every later connect reported
    # "table artifact__new already exists" and the project never opened again.
    for _ in range(2):
        Project(root).connect().close()


def test_leftover_staging_table_does_not_lock_the_project(tmp_path):
    import sqlite3
    root = (tmp_path / "staging")
    root.mkdir()
    conn = sqlite3.connect(root / "project.db")
    conn.executescript("""
        CREATE TABLE artifact(artifact_id TEXT PRIMARY KEY, task_id TEXT,
            protocol_id TEXT NOT NULL, kind TEXT NOT NULL, path TEXT NOT NULL,
            sha256 TEXT, n_rows INTEGER, n_cols INTEGER, created_utc TEXT NOT NULL);
        CREATE TABLE artifact__new(artifact_id TEXT PRIMARY KEY);
        INSERT INTO artifact__new VALUES('stale');
        INSERT INTO artifact VALUES('art_1',NULL,'prot_1','table','a.csv','deadbeef',
                                    1,1,'2024-01-01T00:00:00+00:00');
    """)
    conn.commit()
    conn.close()
    conn = Project(root).connect()
    try:
        assert conn.execute("SELECT checksum FROM artifact").fetchone()["checksum"] == "deadbeef"
        assert conn.execute("SELECT COUNT(*) c FROM sqlite_master "
                            "WHERE name='artifact__new'").fetchone()["c"] == 0
    finally:
        conn.close()


def test_idempotent_create_retry_moves_the_row_only_with_rebind(root):
    run = _run(root, parent_session_id="session-old")
    first = R.create_delegation(root, run["run_id"], "scout", "survey",
                                parent_session_id="session-old",
                                dedup_key="spawn-9")["delegation"]
    # Without the flag the retried spawn is refused before it can move anything.
    with pytest.raises(R.ConflictError):
        R.create_delegation(root, run["run_id"], "scout", "survey",
                            parent_session_id="session-new", dedup_key="spawn-9")
    retried = R.create_delegation(root, run["run_id"], "scout", "survey",
                                  parent_session_id="session-new",
                                  dedup_key="spawn-9", rebind_parent_session=True)
    assert retried["created"] is False
    assert retried["delegation"]["delegation_id"] == first["delegation_id"]
    assert retried["delegation"]["parent_session_id"] == "session-new"
    assert R.get_run(root, run["run_id"])["run"]["parent_session_id"] == "session-new"
