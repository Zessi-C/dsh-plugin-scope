"""Behavior tests for research-process tools at the kernel tool boundary.

Every call goes through `invoke_tool`, the same door `sctda call` and the DSH
manifest proxy use, so a pass here covers argument validation, the error
envelope, the persisted rows, and what a restarted parent reads back. No test
starts a subagent: the tools only record what a separate DSH child did, and the
reclaim path is exercised with an explicit clock override rather than a sleep.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from sctda.harness import TOOL_DIRECTIVE, TOOLS, invoke_tool, tool_manifest  # noqa: E402


# ---------------------------------------------------------------- helpers
def call(name: str, project: Path, **args) -> dict:
    return invoke_tool(name, {"project": str(project), **args})


def ok(name: str, project: Path, **args) -> dict:
    out = call(name, project, **args)
    assert out["status"] == "ok", out
    return out


def error(name: str, project: Path, fragment: str | None = None, **args) -> dict:
    out = call(name, project, **args)
    assert out["status"] == "error", out
    # The kernel's existing error envelope names the tool with `tool`; `_tool`
    # is only added on the success path. Assert the original shape instead of
    # changing the shared boundary for these tools.
    assert out.get("tool") == name, out
    if fragment is not None:
        assert fragment in out["error"], out
    return out


def start(project: Path, question: str = "Which annotated subsets carry a targetable axis?",
          **kwargs) -> dict:
    return ok("sctda_research_start", project, question=question, **kwargs)


def later(seconds: int = 3600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def add_entry(project: Path, run_id: str, kind: str, statement: str, **kwargs) -> dict:
    return ok("sctda_entry_add", project, run_id=run_id, kind=kind,
              statement=statement, **kwargs)


def create_delegation(project: Path, run_id: str, *, role: str = "explorer",
                      task: str = "Inspect marker specificity", **kwargs) -> dict:
    return ok("sctda_delegation_create", project, run_id=run_id, role=role,
              task=task, **kwargs)


# ------------------------------------------------------------ research runs
def test_start_is_idempotent_per_retry_key_and_each_new_run_gets_a_version(tmp_path):
    project = tmp_path / "proj"
    first = start(project, dedup_key="boot", parent_session_id="sess_parent")
    assert first["created"] is True
    run = first["run"]
    assert run["status"] == "running" and run["version"] == 1
    assert run["question"].startswith("Which annotated subsets")
    assert run["parent_session_id"] == "sess_parent"
    assert run["counts"] == {"entries": 0, "open_entries": 0,
                             "delegations": 0, "open_delegations": 0}
    assert run["is_open"] is True and run["is_terminal"] is False

    retry = start(project, dedup_key="boot", parent_session_id="sess_parent")
    assert retry["created"] is False
    assert retry["run"]["run_id"] == run["run_id"]
    assert retry["run"]["version"] == 1

    second = start(project, question="A different question")
    assert second["run"]["version"] == 2
    assert second["run"]["run_id"] != run["run_id"]

    error("sctda_research_start", project, "ConflictError",
          question="A different question", dedup_key="boot")


def test_start_rejects_missing_empty_and_reused_identities(tmp_path):
    project = tmp_path / "proj"
    missing = invoke_tool("sctda_research_start", {"project": str(project)})
    assert missing["status"] == "error" and "missing required 'question'" in missing["error"]

    error("sctda_research_start", project, "ValidationError", question="   ")

    run = start(project)["run"]
    reused = start(project, run_id=run["run_id"])
    assert reused["created"] is False and reused["run"]["run_id"] == run["run_id"]
    error("sctda_research_start", project, "ConflictError",
          question="A different question", run_id=run["run_id"])
    error("sctda_research_start", project, "ConflictError", question="Another", version=1)


def test_heartbeat_only_applies_to_a_running_run(tmp_path):
    project = tmp_path / "proj"
    run_id = start(project)["run"]["run_id"]
    beat = ok("sctda_research_heartbeat", project, run_id=run_id)
    assert beat["changed"] is True and beat["run"]["is_open"] is True
    ok("sctda_research_finish", project, run_id=run_id)
    error("sctda_research_heartbeat", project, "InvalidTransitionError", run_id=run_id)


def test_finish_refuses_while_a_child_is_open_and_returns_open_entry_count(tmp_path):
    project = tmp_path / "proj"
    run_id = start(project)["run"]["run_id"]
    add_entry(project, run_id, "question", "Is Alpha expanded?")
    delegation = create_delegation(project, run_id)["delegation"]

    blocked = error("sctda_research_finish", project, "InvalidTransitionError", run_id=run_id)
    assert delegation["delegation_id"] in blocked["error"]

    ok("sctda_delegation_complete", project, delegation_id=delegation["delegation_id"],
       result={"finding": "markers are shared"})
    finished = ok("sctda_research_finish", project, run_id=run_id,
                  outcome={"answer": "insufficient evidence"})
    assert finished["run"]["status"] == "completed"
    assert finished["run"]["is_completed"] is True
    assert finished["open_entries"] == 1          # open entries do not block closing
    assert finished["run"]["outcome"] == {"answer": "insufficient evidence"}
    error("sctda_research_finish", project, "InvalidTransitionError", run_id=run_id)


def test_fail_cancels_open_delegations_and_keeps_the_error(tmp_path):
    project = tmp_path / "proj"
    run_id = start(project)["run"]["run_id"]
    pending = create_delegation(project, run_id, role="reviewer")["delegation"]
    running = create_delegation(project, run_id, role="explorer")["delegation"]
    ok("sctda_delegation_claim", project, delegation_id=running["delegation_id"],
       child_id="child_a", session_id="sess_a")

    failed = ok("sctda_research_fail", project, run_id=run_id,
                error="input object became unreadable")
    assert failed["run"]["status"] == "failed"
    assert failed["run"]["error"] == "input object became unreadable"
    assert failed["cancelled_delegations"] == 2

    listed = ok("sctda_delegations", project, run_id=run_id)["delegations"]
    assert {row["delegation_id"]: row["status"] for row in listed} == {
        pending["delegation_id"]: "cancelled",
        running["delegation_id"]: "cancelled",
    }
    error("sctda_delegation_complete", project, "InvalidTransitionError",
          delegation_id=pending["delegation_id"], result={"late": True})
    error("sctda_research_fail", project, "InvalidTransitionError",
          run_id=run_id, error="again")


def test_run_listing_filters_and_pages(tmp_path):
    project = tmp_path / "proj"
    first = start(project, question="First")["run"]
    second = start(project, question="Second")["run"]
    ok("sctda_research_finish", project, run_id=first["run_id"])

    running = ok("sctda_research_list", project, status="running")
    assert running["total"] == 1 and running["count"] == 1
    assert running["runs"][0]["run_id"] == second["run_id"]

    page = ok("sctda_research_list", project, limit=1, offset=0)
    assert page["count"] == 1 and page["total"] == 2
    assert page["runs"][0]["run_id"] == second["run_id"]
    error("sctda_research_list", project, "ValidationError", limit=0)


# ---------------------------------------------------------------- entries
def test_entries_keep_provenance_and_survive_a_retried_append(tmp_path):
    project = tmp_path / "proj"
    run_id = start(project)["run"]["run_id"]
    question = add_entry(project, run_id, "question", "Is Alpha expanded in UC?",
                         source_type="current_data", source_ref="art_composition")
    hypothesis = add_entry(
        project, run_id, "hypothesis", "Alpha expansion reflects an interferon axis",
        source_type="paper_observation", source_ref="PMID:12345",
        source_details={"locator": "results:L10"}, details={"markers": ["ACKR1"]},
        author="lead", dedup_key="h1")
    assert hypothesis["created"] is True
    entry = hypothesis["entry"]
    assert entry["source"] == {"type": "paper_observation", "ref": "PMID:12345",
                               "details": {"locator": "results:L10"}}
    assert entry["details"] == {"markers": ["ACKR1"]}
    assert entry["run_status"] == "running" and entry["is_open"] is True
    assert entry["links"] == [] and entry["delegation_id"] is None

    retry = add_entry(project, run_id, "hypothesis",
                      "Alpha expansion reflects an interferon axis",
                      source_type="paper_observation", source_ref="PMID:12345",
                      source_details={"locator": "results:L10"},
                      details={"markers": ["ACKR1"]}, author="lead", dedup_key="h1")
    assert retry["created"] is False
    assert retry["entry"]["entry_id"] == entry["entry_id"]

    error("sctda_entry_add", project, "ConflictError", run_id=run_id,
          kind="hypothesis", statement="A different statement",
          source_type="paper_observation", source_ref="PMID:12345",
          source_details={"locator": "results:L10"}, details={"markers": ["ACKR1"]},
          author="lead", dedup_key="h1")

    listed = ok("sctda_entries", project, run_id=run_id)
    assert [row["entry_id"] for row in listed["entries"]] == [
        question["entry"]["entry_id"], entry["entry_id"]]
    assert listed["total"] == 2

    only_hypotheses = ok("sctda_entries", project, run_id=run_id, kind="hypothesis")
    assert only_hypotheses["count"] == 1 and only_hypotheses["total"] == 1
    page = ok("sctda_entries", project, run_id=run_id, limit=1, offset=1)
    assert page["count"] == 1 and page["total"] == 2
    assert page["entries"][0]["entry_id"] == entry["entry_id"]
    error("sctda_entries", project, "ValidationError", run_id=run_id, limit=0)


def test_entry_links_are_typed_directed_and_idempotent(tmp_path):
    project = tmp_path / "proj"
    run_id = start(project)["run"]["run_id"]
    question = add_entry(project, run_id, "question", "Why is Alpha expanded?")["entry"]
    hypothesis = add_entry(project, run_id, "hypothesis", "Interferon drives it")["entry"]
    other_run = start(project, question="Another run")["run"]
    foreign = add_entry(project, other_run["run_id"], "hypothesis", "Elsewhere")["entry"]

    link = ok("sctda_entry_link", project, run_id=run_id,
              from_entry_id=hypothesis["entry_id"], to_entry_id=question["entry_id"],
              relation="addresses", note="hypothesis answers the question")
    assert link["created"] is True and link["link"]["relation"] == "addresses"

    duplicate = ok("sctda_entry_link", project, run_id=run_id,
                   from_entry_id=hypothesis["entry_id"],
                   to_entry_id=question["entry_id"], relation="addresses")
    assert duplicate["created"] is False
    assert duplicate["link"]["link_id"] == link["link"]["link_id"]

    got = ok("sctda_entry_get", project, entry_id=question["entry_id"])["entry"]
    assert [item["direction"] for item in got["links"]] == ["in"]
    assert got["links"][0]["other_entry_id"] == hypothesis["entry_id"]
    assert got["links"][0]["other_kind"] == "hypothesis"
    assert got["links"][0]["other_statement"] == "Interferon drives it"

    error("sctda_entry_link", project, "ValidationError", run_id=run_id,
          from_entry_id=question["entry_id"], to_entry_id=question["entry_id"],
          relation="addresses")
    error("sctda_entry_link", project, "ValidationError", run_id=run_id,
          from_entry_id=question["entry_id"], to_entry_id=foreign["entry_id"],
          relation="informs")


def test_entry_status_transitions_are_bounded_by_a_running_run(tmp_path):
    project = tmp_path / "proj"
    run_id = start(project)["run"]["run_id"]
    entry = add_entry(project, run_id, "hypothesis", "Interferon drives Alpha")["entry"]

    supported = ok("sctda_entry_set_status", project, entry_id=entry["entry_id"],
                   status="supported", note="supported by DE artifact")
    assert supported["changed"] is True
    assert supported["entry"]["status"] == "supported"
    assert supported["entry"]["is_terminal"] is False

    retired = ok("sctda_entry_set_status", project, entry_id=entry["entry_id"],
                 status="retired")
    assert retired["entry"]["is_terminal"] is True
    error("sctda_entry_set_status", project, "InvalidTransitionError",
          entry_id=entry["entry_id"], status="supported")

    second = add_entry(project, run_id, "observation", "Alpha is 12% of cells")["entry"]
    ok("sctda_research_finish", project, run_id=run_id)
    error("sctda_entry_set_status", project, "InvalidTransitionError",
          entry_id=second["entry_id"], status="resolved")
    error("sctda_entry_add", project, "InvalidTransitionError",
          run_id=run_id, kind="gap", statement="No matched stromal control")


def test_entry_attribution_to_a_delegation_must_stay_inside_the_run(tmp_path):
    project = tmp_path / "proj"
    run_id = start(project)["run"]["run_id"]
    delegation = create_delegation(project, run_id)["delegation"]
    entry = add_entry(project, run_id, "observation", "Child found shared markers",
                      delegation_id=delegation["delegation_id"])["entry"]
    assert entry["delegation_id"] == delegation["delegation_id"]

    listed = ok("sctda_delegations", project, run_id=run_id)["delegations"]
    assert listed[0]["note_count"] == 1

    by_child = ok("sctda_entries", project, run_id=run_id,
                  delegation_id=delegation["delegation_id"])
    assert [row["entry_id"] for row in by_child["entries"]] == [entry["entry_id"]]
    assert by_child["total"] == 1

    other = start(project, question="Another run")["run"]
    error("sctda_entry_add", project, "ValidationError",
          run_id=other["run_id"], kind="observation", statement="Wrong run",
          delegation_id=delegation["delegation_id"])


# ------------------------------------------------------------ delegations
def test_delegation_lifecycle_binds_dsh_ids_and_retries_are_idempotent(tmp_path):
    project = tmp_path / "proj"
    run_id = start(project)["run"]["run_id"]
    created = create_delegation(project, run_id, dedup_key="spawn-1",
                                request={"question": "marker specificity"})
    assert created["created"] is True
    delegation = created["delegation"]
    assert delegation["status"] == "pending" and delegation["is_open"] is True
    assert delegation["request"] == {"question": "marker specificity"}
    assert delegation["child_id"] is None and delegation["session_id"] is None

    retry = create_delegation(project, run_id, dedup_key="spawn-1",
                              request={"question": "marker specificity"})
    assert retry["created"] is False
    assert retry["delegation"]["delegation_id"] == delegation["delegation_id"]

    claimed = ok("sctda_delegation_claim", project,
                 delegation_id=delegation["delegation_id"],
                 child_id="child_abc", session_id="sess_abc")
    assert claimed["delegation"]["status"] == "running"
    assert claimed["delegation"]["child_id"] == "child_abc"
    assert claimed["delegation"]["session_id"] == "sess_abc"
    assert claimed["delegation"]["attempt"] == 1
    assert claimed["reclaimed"] is False

    again = ok("sctda_delegation_claim", project,
               delegation_id=delegation["delegation_id"],
               child_id="child_abc", session_id="sess_abc")
    assert again["changed"] is False and again["reclaimed"] is False
    assert again["delegation"]["attempt"] == 1

    error("sctda_delegation_claim", project, "ConflictError",
          delegation_id=delegation["delegation_id"], child_id="child_other")

    completed = ok("sctda_delegation_complete", project,
                   delegation_id=delegation["delegation_id"],
                   result={"markers": ["ACKR1"]}, child_id="child_abc")
    assert completed["changed"] is True
    assert completed["delegation"]["status"] == "completed"
    assert completed["delegation"]["result"] == {"markers": ["ACKR1"]}
    assert completed["delegation"]["is_terminal"] is True

    repeat = ok("sctda_delegation_complete", project,
                delegation_id=delegation["delegation_id"],
                result={"markers": ["ACKR1"]}, child_id="child_abc")
    assert repeat["changed"] is False

    error("sctda_delegation_complete", project, "ConflictError",
          delegation_id=delegation["delegation_id"], result={"markers": ["OTHER"]})
    error("sctda_delegation_claim", project, "InvalidTransitionError",
          delegation_id=delegation["delegation_id"], child_id="child_abc")


def test_a_stale_lease_is_reported_not_mutated_and_can_be_reclaimed(tmp_path):
    project = tmp_path / "proj"
    run_id = start(project)["run"]["run_id"]
    delegation_id = create_delegation(project, run_id)["delegation"]["delegation_id"]
    ok("sctda_delegation_claim", project, delegation_id=delegation_id,
       child_id="child_a", session_id="sess_a", lease_seconds=60)

    future = later()
    first_read = ok("sctda_delegations", project, run_id=run_id, as_of_utc=future)
    stale = first_read["delegations"][0]
    assert stale["is_stale"] is True
    assert stale["status"] == "running" and stale["child_id"] == "child_a"
    assert stale["attempt"] == 1

    second_read = ok("sctda_delegations", project, run_id=run_id, as_of_utc=future)
    assert second_read["delegations"][0]["status"] == "running"
    assert second_read["delegations"][0]["child_id"] == "child_a"

    reclaimed = ok("sctda_delegation_claim", project, delegation_id=delegation_id,
                   child_id="child_b", session_id="sess_b", as_of_utc=future)
    assert reclaimed["reclaimed"] is True
    assert reclaimed["delegation"]["attempt"] == 2
    assert reclaimed["delegation"]["child_id"] == "child_b"

    live = error("sctda_delegation_claim", project, "ConflictError",
                 delegation_id=delegation_id, child_id="child_c")
    assert "child_b" in live["error"]


def test_failed_and_missing_results_are_never_success(tmp_path):
    project = tmp_path / "proj"
    run_id = start(project)["run"]["run_id"]
    delegation_id = create_delegation(project, run_id, role="reviewer")["delegation"]["delegation_id"]

    error("sctda_delegation_complete", project, "ValidationError",
          delegation_id=delegation_id, result=None)
    failed = ok("sctda_delegation_fail", project, delegation_id=delegation_id,
                error="child session ended without output")
    assert failed["delegation"]["status"] == "failed"
    assert failed["delegation"]["is_failed"] is True
    assert failed["delegation"]["result"] is None
    assert failed["delegation"]["error"] == "child session ended without output"

    repeat = ok("sctda_delegation_fail", project, delegation_id=delegation_id,
                error="child session ended without output")
    assert repeat["changed"] is False
    error("sctda_delegation_fail", project, "ConflictError",
          delegation_id=delegation_id, error="a different error")
    error("sctda_delegation_complete", project, "InvalidTransitionError",
          delegation_id=delegation_id, result={"late": True})


def test_delegation_listing_and_filters(tmp_path):
    project = tmp_path / "proj"
    run_id = start(project)["run"]["run_id"]
    finished = create_delegation(project, run_id, role="explorer")["delegation"]
    open_one = create_delegation(project, run_id, role="reviewer")["delegation"]
    ok("sctda_delegation_complete", project,
       delegation_id=finished["delegation_id"], result={"done": True})

    all_rows = ok("sctda_delegations", project, run_id=run_id)
    assert all_rows["count"] == 2 and all_rows["total"] == 2
    assert all_rows["run_status"] == "running"
    assert {row["role"] for row in all_rows["delegations"]} == {"explorer", "reviewer"}

    open_rows = ok("sctda_delegations", project, run_id=run_id, include_finished=False)
    assert [row["delegation_id"] for row in open_rows["delegations"]] == [
        open_one["delegation_id"]]

    by_status = ok("sctda_delegations", project, run_id=run_id, status="completed")
    assert by_status["count"] == 1 and by_status["total"] == 1
    error("sctda_delegations", project, "ValidationError", run_id=run_id, limit=0)


def test_dsh_start_event_binds_a_pending_delegation_and_end_settles_it(tmp_path):
    project = tmp_path / "proj"
    run_id = start(project, parent_session_id="sess_parent")["run"]["run_id"]
    pending = create_delegation(project, run_id, role="explorer",
                                dedup_key="spawn-1")["delegation"]

    started = ok("sctda_subagent_start", project,
                 parent_session_id="sess_parent", provider="dsh",
                 dsh_run_id="dshrun_1", child_id="child_1", label="explorer-1",
                 task_prompt="Inspect marker specificity", session_id="sess_child",
                 role="explorer", task="Inspect marker specificity",
                 dedup_key="spawn-1")
    assert started["created"] is False and started["reclaimed"] is False
    delegation = started["delegation"]
    assert delegation["delegation_id"] == pending["delegation_id"]
    assert delegation["status"] == "running"
    assert delegation["child_id"] == "child_1"
    assert delegation["session_id"] == "sess_child"
    assert delegation["dsh_run_id"] == "dshrun_1"
    assert delegation["parent_session_id"] == "sess_parent"
    assert delegation["provider"] == "dsh" and delegation["label"] == "explorer-1"
    assert delegation["task_prompt"] == "Inspect marker specificity"
    assert delegation["role"] == "explorer"

    found = ok("sctda_delegation_find", project, child_id="child_1")["delegation"]
    assert found["delegation_id"] == delegation["delegation_id"]
    assert found["note_count"] == 0

    ended = ok("sctda_subagent_end", project, child_id="child_1", status="settled",
               output_excerpt="markers are shared", output_sha256="sha-abc")
    assert ended["changed"] is True
    assert ended["delegation"]["status"] == "completed"
    assert ended["delegation"]["result"] == {"last_assistant_message": "markers are shared"}
    assert ended["delegation"]["output_excerpt"] == "markers are shared"
    assert ended["delegation"]["output_sha256"] == "sha-abc"

    repeat = ok("sctda_subagent_end", project, child_id="child_1", status="settled",
                output_excerpt="markers are shared", output_sha256="sha-abc")
    assert repeat["changed"] is False
    error("sctda_subagent_end", project, "ConflictError", child_id="child_1",
          status="settled", output_excerpt="a different ending")


def test_dsh_start_event_can_create_the_row_and_end_keeps_failure_and_loss(tmp_path):
    project = tmp_path / "proj"
    run_id = start(project, parent_session_id="sess_parent")["run"]["run_id"]

    created = ok("sctda_subagent_start", project,
                 parent_session_id="sess_parent", provider="dsh",
                 dsh_run_id="dshrun_2", child_id="child_2", label="reviewer-1")
    assert created["created"] is True
    assert created["delegation"]["role"] == "subagent"
    assert created["delegation"]["task"] == "reviewer-1"

    error("sctda_subagent_end", project, "ValidationError",
          child_id="child_2", status="completed")
    failed = ok("sctda_subagent_end", project, child_id="child_2", status="failed",
                error="child crashed")
    assert failed["delegation"]["status"] == "failed"
    assert failed["delegation"]["error"] == "child crashed"

    ok("sctda_subagent_start", project, parent_session_id="sess_parent",
       provider="dsh", dsh_run_id="dshrun_3", child_id="child_3", label="explorer-2")
    lost = ok("sctda_subagent_end", project, child_id="child_3", status="lost",
              stop_reason="session vanished")
    assert lost["delegation"]["status"] == "lost"
    assert lost["delegation"]["is_lost"] is True and lost["delegation"]["is_terminal"] is True
    assert lost["delegation"]["stop_reason"] == "session vanished"
    assert "lost" in lost["delegation"]["error"]

    listed = ok("sctda_delegations", project, run_id=run_id)
    assert {row["status"] for row in listed["delegations"]} == {"failed", "lost"}


def test_delegation_find_requires_a_selector_and_refuses_ambiguity(tmp_path):
    project = tmp_path / "proj"
    run_id = start(project)["run"]["run_id"]
    first = create_delegation(project, run_id, role="explorer")["delegation"]
    second = create_delegation(project, run_id, role="reviewer")["delegation"]
    ok("sctda_delegation_claim", project, delegation_id=first["delegation_id"],
       child_id="child_a", session_id="sess_shared")
    ok("sctda_delegation_claim", project, delegation_id=second["delegation_id"],
       child_id="child_b", session_id="sess_shared")

    error("sctda_delegation_find", project, "ValidationError")
    by_id = ok("sctda_delegation_find", project,
               delegation_id=second["delegation_id"])["delegation"]
    assert by_id["delegation_id"] == second["delegation_id"]
    error("sctda_delegation_find", project, "ConflictError", session_id="sess_shared")
    error("sctda_delegation_find", project, "NotFoundError", child_id="child_missing")


def test_attach_binds_late_dsh_ids_without_rewriting_the_row(tmp_path):
    project = tmp_path / "proj"
    run_id = start(project)["run"]["run_id"]
    delegation = create_delegation(project, run_id, role="explorer",
                                   dedup_key="late-1")["delegation"]

    attached = ok("sctda_subagent_attach", project,
                  delegation_id=delegation["delegation_id"],
                  child_id="child_late", session_id="sess_late", provider="dsh",
                  label="explorer-late", parent_session_id="sess_parent")
    assert attached["started"] is True
    assert attached["delegation"]["status"] == "running"
    assert attached["delegation"]["child_id"] == "child_late"
    assert attached["delegation"]["session_id"] == "sess_late"
    assert attached["delegation"]["label"] == "explorer-late"
    assert attached["delegation"]["parent_session_id"] == "sess_parent"

    again = ok("sctda_subagent_attach", project,
               delegation_id=delegation["delegation_id"], child_id="child_late",
               dsh_run_id="dshrun_late")
    assert again["started"] is False
    assert again["delegation"]["dsh_run_id"] == "dshrun_late"
    assert again["delegation"]["session_id"] == "sess_late"

    error("sctda_subagent_attach", project, "ConflictError",
          delegation_id=delegation["delegation_id"], provider="another-provider")

    ok("sctda_subagent_end", project, delegation_id=delegation["delegation_id"],
       status="completed", result={"done": True})
    error("sctda_subagent_attach", project, "InvalidTransitionError",
          delegation_id=delegation["delegation_id"], child_id="child_late")


def test_a_restarted_session_takes_over_only_when_rebind_is_requested(tmp_path):
    """A new DSH session owns a different id, so start events must be movable."""
    project = tmp_path / "proj"
    run_id = start(project, parent_session_id="sess_old")["run"]["run_id"]

    refused = error("sctda_subagent_start", project,
                    parent_session_id="sess_new", provider="dsh",
                    dsh_run_id="dshrun_r1", child_id="child_r1",
                    label="explorer-new", run_id=run_id)
    assert "rebind_parent_session" in refused["error"]

    rebound = ok("sctda_subagent_start", project,
                 parent_session_id="sess_new", provider="dsh",
                 dsh_run_id="dshrun_r1", child_id="child_r1",
                 label="explorer-new", run_id=run_id,
                 rebind_parent_session=True)
    assert rebound["delegation"]["parent_session_id"] == "sess_new"
    assert rebound["delegation"]["status"] == "running"
    got = ok("sctda_research_get", project, run_id=run_id)["run"]
    assert got["parent_session_id"] == "sess_new"

    attached = ok("sctda_subagent_attach", project,
                  child_id="child_r1", parent_session_id="sess_newer",
                  rebind_parent_session=True)
    assert attached["delegation"]["parent_session_id"] == "sess_newer"


def test_run_start_and_delegation_create_forward_rebind(tmp_path):
    project = tmp_path / "proj"
    run = start(project, parent_session_id="sess_old", rebind_parent_session=True,
                dedup_key="boot")["run"]
    again = start(project, parent_session_id="sess_old2", rebind_parent_session=True,
                  dedup_key="boot")["run"]
    assert again["run_id"] == run["run_id"]
    assert again["parent_session_id"] == "sess_old2"

    delegation = ok("sctda_delegation_create", project, run_id=run["run_id"],
                    role="explorer", task="Inspect markers",
                    parent_session_id="sess_old3",
                    rebind_parent_session=True)["delegation"]
    assert delegation["parent_session_id"] == "sess_old3"


def test_claim_honours_the_heartbeat_window_it_is_given(tmp_path):
    """resume calls a row stale by heartbeat age; claim agrees only when told.

    The lease is deliberately longer than the elapsed clock, so lease-only
    staleness is false and only the heartbeat window can classify the row.
    """
    project = tmp_path / "proj"
    run_id = start(project)["run"]["run_id"]
    delegation_id = create_delegation(project, run_id)["delegation"]["delegation_id"]
    ok("sctda_delegation_claim", project, delegation_id=delegation_id,
       child_id="child_a", session_id="sess_a", lease_seconds=7200)

    future = later(3600)
    stale = ok("sctda_resume", project, run_id=run_id, as_of_utc=future,
               stale_after_seconds=1)
    assert stale["runs"][0]["counts"]["stale_delegations"] == 1

    refused = error("sctda_delegation_claim", project, "ConflictError",
                    delegation_id=delegation_id, child_id="child_b",
                    as_of_utc=future)
    assert "live lease" in refused["error"]

    reclaimed = ok("sctda_delegation_claim", project, delegation_id=delegation_id,
                   child_id="child_b", as_of_utc=future, stale_after_seconds=1)
    assert reclaimed["reclaimed"] is True
    assert reclaimed["delegation"]["attempt"] == 2


def test_delegation_listing_can_span_the_project(tmp_path):
    project = tmp_path / "proj"
    first_run = start(project, question="First run")["run"]
    second_run = start(project, question="Second run")["run"]
    first = create_delegation(project, first_run["run_id"], role="explorer")["delegation"]
    second = create_delegation(project, second_run["run_id"], role="reviewer")["delegation"]
    ok("sctda_delegation_complete", project,
       delegation_id=first["delegation_id"], result={"done": True})

    all_rows = ok("sctda_delegations", project)
    assert all_rows["run_id"] is None and all_rows["run_status"] is None
    assert all_rows["total"] == 2
    assert {row["delegation_id"] for row in all_rows["delegations"]} == {
        first["delegation_id"], second["delegation_id"]}

    open_only = ok("sctda_delegations", project, include_finished=False)
    assert [row["delegation_id"] for row in open_only["delegations"]] == [
        second["delegation_id"]]
    completed = ok("sctda_delegations", project, status="completed")
    assert [row["delegation_id"] for row in completed["delegations"]] == [
        first["delegation_id"]]


# ---------------------------------------------------------------- recovery
def test_resume_reports_open_work_without_mutating_it(tmp_path):
    project = tmp_path / "proj"
    finished = start(project, question="Closed run")["run"]
    ok("sctda_research_finish", project, run_id=finished["run_id"])

    live = start(project, question="Interrupted run")["run"]
    add_entry(project, live["run_id"], "gap", "No matched stromal control")
    delegation_id = create_delegation(project, live["run_id"])["delegation"]["delegation_id"]
    ok("sctda_delegation_claim", project, delegation_id=delegation_id,
       child_id="child_a", session_id="sess_a")

    snapshot = ok("sctda_resume", project)
    assert snapshot["n_runs"] == 1
    recovered = snapshot["runs"][0]
    assert recovered["run"]["run_id"] == live["run_id"]
    assert [item["kind"] for item in recovered["open_entries"]] == ["gap"]
    assert [item["delegation_id"] for item in recovered["open_delegations"]] == [
        delegation_id]
    assert recovered["counts"] == {"open_entries": 1, "open_delegations": 1,
                                   "stale_delegations": 0}

    with_finished = ok("sctda_resume", project, include_finished=True)
    assert with_finished["n_runs"] == 2
    assert {item["run"]["run_id"] for item in with_finished["runs"]} == {
        finished["run_id"], live["run_id"]}

    single = ok("sctda_resume", project, run_id=finished["run_id"])
    assert single["n_runs"] == 1
    assert single["runs"][0]["run"]["status"] == "completed"
    assert single["runs"][0]["open_entries"] == []

    stale = ok("sctda_resume", project, as_of_utc=later())
    assert stale["runs"][0]["counts"]["stale_delegations"] == 1
    after = ok("sctda_delegations", project, run_id=live["run_id"])
    assert after["delegations"][0]["status"] == "running"
    assert after["delegations"][0]["child_id"] == "child_a"


def test_resume_with_an_explicit_run_id_can_read_a_failed_run(tmp_path):
    project = tmp_path / "proj"
    run_id = start(project)["run"]["run_id"]
    ok("sctda_research_fail", project, run_id=run_id, error="object unreadable")
    out = ok("sctda_resume", project, run_id=run_id)
    assert out["runs"][0]["run"]["status"] == "failed"
    assert out["runs"][0]["run"]["error"] == "object unreadable"
    error("sctda_resume", project, "NotFoundError", run_id="run_missing")


# ------------------------------------------------------- envelope and manifest
def test_unknown_ids_and_arguments_fail_in_the_same_envelope_as_other_tools(tmp_path):
    project = tmp_path / "proj"
    error("sctda_research_get", project, "NotFoundError", run_id="run_missing")
    error("sctda_entry_get", project, "NotFoundError", entry_id="ent_missing")
    error("sctda_delegation_complete", project, "NotFoundError",
          delegation_id="del_missing", result={})

    missing = invoke_tool("sctda_entry_add",
                          {"project": str(project), "run_id": "run_x", "statement": "x"})
    assert missing["status"] == "error"
    assert "missing required 'kind'" in missing["error"]

    unknown = invoke_tool("sctda_not_a_tool", {"project": str(project)})
    assert unknown["status"] == "error" and "unknown tool" in unknown["error"]
    assert "sctda_research_start" in unknown["available"]

    success = ok("sctda_research_list", project)
    assert success["_tool"] == "sctda_research_list"
    assert success["_mutating"] is False


def test_manifest_publishes_research_tools_and_registration_metadata():
    expected = {
        "sctda_research_start", "sctda_research_get", "sctda_research_list",
        "sctda_research_finish", "sctda_research_fail", "sctda_research_heartbeat",
        "sctda_entry_add", "sctda_entry_get", "sctda_entries",
        "sctda_entry_set_status", "sctda_entry_link",
        "sctda_delegation_create", "sctda_delegation_claim",
        "sctda_delegation_heartbeat", "sctda_delegation_complete",
        "sctda_delegation_fail", "sctda_delegations",
        "sctda_delegation_find", "sctda_subagent_start", "sctda_subagent_attach",
        "sctda_subagent_end", "sctda_resume",
    }
    manifest = tool_manifest()
    names = {tool["name"] for tool in manifest}
    assert expected <= names
    assert not any(verb in name for name in names
                   for verb in ("approve", "authorize", "confirm", "grant"))

    for tool in manifest:
        assert isinstance(tool["internal"], bool)
        assert isinstance(tool["roles"], list) and tool["roles"]
        assert set(tool["roles"]) <= {"lead", "worker"}
        assert tool["mutating"] is (TOOLS[tool["name"]].mutating)
        json.dumps(tool)                       # the manifest is printable as-is

    read_only = {
        "sctda_research_get", "sctda_research_list", "sctda_entry_get",
        "sctda_entries", "sctda_delegations", "sctda_delegation_find",
        "sctda_resume",
    }
    for name in read_only:
        assert TOOLS[name].mutating is False, name

    assert TOOLS["sctda_research_start"].roles == ("lead",)
    assert TOOLS["sctda_entry_add"].roles == ("lead", "worker")
    assert TOOLS["sctda_research_get"].roles == ("lead", "worker")
    # Delegation rows carry full prompts/results: never worker-visible, and the
    # recovery surface stays lead-only.
    for name in ("sctda_delegations", "sctda_delegation_find", "sctda_resume"):
        assert TOOLS[name].roles == ("lead",), name
    worker_visible = {name for name, tool in TOOLS.items() if "worker" in tool.roles}
    assert "sctda_delegations" not in worker_visible
    assert "sctda_delegation_find" not in worker_visible
    assert "sctda_resume" not in worker_visible
    assert "sctda_entry_add" in worker_visible

    internal = {name for name, tool in TOOLS.items() if tool.internal}
    assert internal == {
        "sctda_delegation_create", "sctda_delegation_claim",
        "sctda_delegation_heartbeat", "sctda_delegation_complete",
        "sctda_delegation_fail", "sctda_subagent_start",
        "sctda_subagent_attach", "sctda_subagent_end",
    }
    for name in internal:
        assert TOOLS[name].roles == ("lead",)
        assert TOOLS[name].mutating is True


