"""Contract tests for the R bridge's deadline.

The review found that a timed-out tool call killed only the Python process,
leaving an R batch writing into the task directories the next attempt would
quarantine. `run_r` now owns a deadline of its own:

  * the caller's explicit `timeout` wins;
  * otherwise `SCTDA_R_TIMEOUT_SECONDS` (set by the dsh plugin from its kernel
    deadline) applies, then a conservative default;
  * expiry is a failed `RResult` whose `payload["error"]` is populated, because
    `execution.run_protocol` writes that text into every pending task's
    `reason` and `protocol.design_table` raises it;
  * the whole R process group dies, and the log still records the deadline.

These tests drive a real `Rscript`; they assert the transport contract only.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sctda import rbridge
from sctda.rbridge import R_TIMEOUT_ENV, run_r


def _r(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_an_explicit_timeout_still_wins_over_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(R_TIMEOUT_ENV, "1")
    script = _r(tmp_path, "ok.R", 'cat(\'{"ok": true, "n": 7}\\n\')\n')
    result = run_r(script, [], timeout=60)
    assert result.ok is True
    assert result.payload["n"] == 7


def test_the_environment_deadline_applies_when_the_caller_passes_none(tmp_path, monkeypatch):
    monkeypatch.setenv(R_TIMEOUT_ENV, "1")
    script = _r(tmp_path, "slow.R", "Sys.sleep(20)\n")
    started = time.monotonic()
    result = run_r(script, [])
    assert time.monotonic() - started < 10
    assert result.ok is False
    assert "deadline" in result.payload["error"]
    assert result.returncode < 0


def test_a_deadline_is_a_result_not_an_exception_and_the_log_keeps_the_evidence(tmp_path, monkeypatch):
    monkeypatch.delenv(R_TIMEOUT_ENV, raising=False)
    script = _r(tmp_path, "slow.R", "Sys.sleep(20)\n")
    log = tmp_path / "logs" / "run.log"
    result = run_r(script, [], log_path=log, timeout=1)
    assert result.ok is False
    assert result.payload["ok"] is False
    assert "1 s deadline" in result.payload["error"]
    assert result.log_path == log
    text = log.read_text(encoding="utf-8")
    assert "# cmd: Rscript" in text
    assert "# timeout: exceeded 1 s" in text
    assert "process tree killed" in text


def test_the_whole_process_group_dies_with_the_batch(tmp_path, monkeypatch):
    monkeypatch.delenv(R_TIMEOUT_ENV, raising=False)
    marker = tmp_path / "grandchild.marker"
    script = _r(
        tmp_path, "spawn.R",
        f'system("sh -c \'sleep 6; touch {marker}\'", wait=False)\n'
        "Sys.sleep(20)\n",
    )
    result = run_r(script, [], timeout=1)
    assert result.ok is False
    # A surviving grandchild writes the marker when the sleep finishes.
    time.sleep(8)
    assert not marker.exists(), "a grandchild of Rscript outlived the deadline"


def test_an_unset_environment_means_no_deadline_by_default(tmp_path, monkeypatch):
    """No argument and no environment value leaves the batch unbounded.

    There is deliberately no built-in default: a scientific batch of unknown size
    must not be killed by a number nobody chose for it. The test proves the run
    is not silently bounded by a short value (a real long batch is covered by the
    deadline tests above).
    """
    monkeypatch.delenv(R_TIMEOUT_ENV, raising=False)
    assert rbridge._env_timeout_seconds() is None
    script = _r(tmp_path, "ok.R", 'cat(\'{"ok": true}\\n\')\n')
    result = run_r(script, [])
    assert result.ok is True
    assert not hasattr(rbridge, "DEFAULT_R_TIMEOUT_SECONDS")


def test_an_unusable_environment_value_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv(R_TIMEOUT_ENV, "not-a-number")
    assert rbridge._env_timeout_seconds() is None
    monkeypatch.setenv(R_TIMEOUT_ENV, "-5")
    assert rbridge._env_timeout_seconds() is None
    monkeypatch.setenv(R_TIMEOUT_ENV, "0")
    assert rbridge._env_timeout_seconds() is None
    monkeypatch.setenv(R_TIMEOUT_ENV, "90")
    assert rbridge._env_timeout_seconds() == 90


def test_inspect_object_keeps_its_own_explicit_deadline():
    import inspect

    signature = inspect.signature(rbridge.inspect_object)
    assert signature.parameters["timeout"].default == 3600
    # Keyword compatibility with the execution test doubles.
    run_signature = inspect.signature(run_r)
    assert list(run_signature.parameters) == ["script", "args", "log_path", "timeout"]
    assert run_signature.parameters["log_path"].default is None
    assert run_signature.parameters["timeout"].default is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="parent-death signal is Linux-only")
def test_r_dies_when_its_parent_process_is_killed(tmp_path):
    """An outer process-group kill must still reach R.

    `run_r` puts R in its own session so its deadline can kill R's group. That
    session would also hide R from a kill aimed at the kernel CLI's group, which
    is exactly the orphan the review reported. The parent-death signal closes it:
    R is killed by the kernel when the Python parent dies, however it dies.
    """
    pid_file = tmp_path / "r.pid"
    script = _r(tmp_path, "long.R", f'write(Sys.getpid(), "{pid_file}")\nSys.sleep(30)\n')
    helper = tmp_path / "helper.py"
    helper.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parent.parent / 'src')!r})\n"
        "from pathlib import Path\n"
        "from sctda.rbridge import run_r\n"
        f"run_r({str(script)!r}, [], log_path=Path({str(tmp_path / 'run.log')!r}))\n",
        encoding="utf-8")

    parent = subprocess.Popen([sys.executable, str(helper)])
    deadline = time.monotonic() + 20
    r_pid = None
    while time.monotonic() < deadline and r_pid is None:
        time.sleep(0.2)
        try:
            r_pid = int(pid_file.read_text().strip())
        except (OSError, ValueError):
            r_pid = None
    assert r_pid is not None, "the R worker never started"

    parent.kill()
    parent.wait()
    time.sleep(2)
    assert not _pid_alive(r_pid), f"R worker {r_pid} outlived its parent"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
