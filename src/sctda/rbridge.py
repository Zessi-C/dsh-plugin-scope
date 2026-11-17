"""Run R workers and read their JSON output.

Python orchestrates and stores; R does the native Seurat work. Each call is
fully logged so a failed run leaves the real R error behind instead of a
synthesised success.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .project import PKG_ROOT, now


#: Environment variable a host may set to bound every unbounded R call. The dsh
#: plugin derives it from its own kernel-call deadline so the R batch fails
#: first, with a recorded error, instead of being killed from outside mid-write.
R_TIMEOUT_ENV = "SCTDA_R_TIMEOUT_SECONDS"


class RBridgeError(RuntimeError):
    pass


@dataclass
class RResult:
    ok: bool
    payload: dict
    stdout: str
    stderr: str
    returncode: int
    log_path: Path | None


def rscript() -> str:
    return "Rscript"


def _env_timeout_seconds() -> int | None:
    """Read the host-provided R deadline, ignoring anything unusable."""
    raw = os.environ.get(R_TIMEOUT_ENV, "").strip()
    if not raw:
        return None
    try:
        value = int(float(raw))
    except ValueError:
        return None
    return value if value > 0 else None


def _write_log(log_path: Path, cmd: list[str], returncode: int,
               stdout: str, stderr: str, note: str = "") -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        f"# {now()}\n# cmd: {' '.join(cmd)}\n"
        f"# returncode: {returncode}\n"
        f"{note}"
        f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n",
        encoding="utf-8")


def _pdeathsig_preexec() -> None:
    """Ask Linux to SIGKILL the R process when its parent dies.

    `run_r` puts R in its own session so a deadline can kill R's process group
    without killing the Python process. That isolation has a cost: an outer
    process-group kill aimed at the kernel CLI would no longer reach R. A parent
    death signal closes that hole, so the orphan case is covered on both paths.

    Only meaningful on Linux; silently a no-op elsewhere because the caller runs
    this inside the freshly forked child, where raising would be invisible.
    """
    if not sys.platform.startswith("linux"):
        return
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        # PR_SET_PDEATHSIG = 1
        libc.prctl(1, signal.SIGKILL, 0, 0, 0)
        # The parent may already have died between fork and prctl, which clears
        # the death signal; in that case the child must not keep running.
        if os.getppid() == 1:
            os._exit(1)
    except Exception:
        return


def _kill_group(proc: subprocess.Popen) -> str:
    """Kill the R process group; report which mechanism actually ran.

    ``Rscript`` may spawn its own children, so killing only the direct child
    leaves writers behind. The child leads a new session (see the ``Popen``
    call), which makes its pid a process-group id; a process that already
    exited is not an error here.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return "group"
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
            return "child"
        except OSError:
            return "none"


def run_r(script: str | Path, args: list[str], log_path: Path | None = None,
          timeout: int | None = None) -> RResult:
    """Run one R worker and return its parsed output.

    ``timeout`` is the caller's explicit deadline. When it is ``None`` the
    environment's ``SCTDA_R_TIMEOUT_SECONDS`` applies, and when that is unset
    too the batch runs without a deadline, exactly as before. There is
    deliberately no built-in default: a scientific batch of unknown size must
    not be killed by a number nobody chose for it.

    A deadline is not an exception: it is a failed result with ``payload["error"]``
    populated, because ``execution.run_protocol`` records that message in every
    pending task's ``reason`` and ``protocol.design_table`` raises it.
    """
    script = Path(script)
    if not script.is_absolute():
        script = PKG_ROOT / "r" / script
    if not script.exists():
        raise RBridgeError(f"R script not found: {script}")
    effective = timeout if timeout is not None else _env_timeout_seconds()
    cmd = [rscript(), str(script), *args]

    # start_new_session gives the worker its own process group, so a deadline can
    # reach R's grandchildren. text/PIPE keep the previous capture semantics.
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True, preexec_fn=_pdeathsig_preexec)
    timed_out = False
    kill_note = ""
    try:
        stdout, stderr = proc.communicate(timeout=effective)
    except subprocess.TimeoutExpired:
        timed_out = True
        strategy = _kill_group(proc)
        # Reap after the kill so the pipes are drained and the returncode is set.
        stdout, stderr = proc.communicate()
        kill_note = (f"# timeout: exceeded {effective} s; "
                     f"process tree killed ({strategy})\n")
    returncode = proc.returncode if proc.returncode is not None else -signal.SIGKILL

    if log_path:
        _write_log(log_path, cmd, returncode, stdout or "", stderr or "", kill_note)

    if timed_out:
        # Deliberately a result, not a raise: the caller records a task failure
        # instead of unwinding with a traceback, and the log keeps the evidence.
        return RResult(
            ok=False,
            payload={"ok": False,
                     "error": f"R batch exceeded its {effective} s deadline and was killed "
                              f"({kill_note.strip().lstrip('# ').strip()})"},
            stdout=stdout or "", stderr=stderr or "", returncode=returncode,
            log_path=log_path)

    payload: dict = {}
    for blob in (stdout, stderr):
        if not blob:
            continue
        # R workers print one JSON object; take the last balanced one
        blob = blob.strip()
        if not blob.startswith("{"):
            i = blob.find("{")
            if i >= 0:
                blob = blob[i:]
        try:
            payload = json.loads(blob)
        except json.JSONDecodeError:
            continue
    ok = returncode == 0 and payload.get("ok") is True
    if returncode != 0 and not payload:
        payload = {"ok": False, "error": (stderr or stdout or "").strip()[-4000:]}
    return RResult(ok=ok, payload=payload, stdout=stdout or "", stderr=stderr or "",
                   returncode=returncode, log_path=log_path)


def inspect_object(object_path: Path, out_path: Path, object_name: str | None = None,
                   log_path: Path | None = None, timeout: int = 3600) -> RResult:
    args = ["--object", str(object_path), "--out", str(out_path)]
    if object_name:
        args += ["--object-name", object_name]
    return run_r("inspect_object.R", args, log_path=log_path, timeout=timeout)
