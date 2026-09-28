"""A Kanban ``chat -q`` worker refused on MAX_CONCURRENT_SESSIONS must actually exit.

Live (Sheldon, 2026-09-28): refused workers printed the busy marker and
``[kanban-worker-exit] rc=75`` but never exited (0% CPU for 19 min to 2h42m, one
holding a merge card): ``sys.exit`` ran ``Py_FinalizeEx``, which blocked on the
import lock / a non-daemon thread held by background work. The worker path now
hard-exits after its cleanup, like the ``-z`` oneshot path (#30387, #43055).
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

_CHILD = textwrap.dedent(
    """
    import sys, threading, types, importlib.abc, importlib.machinery
    sys.path.insert(0, {repo!r})
    import cli
    from hermes_cli.active_sessions import MAX_CONCURRENT_SESSIONS

    # A background import that never finishes: a non-daemon thread parked inside a
    # module's top-level code, holding that module's import lock.
    gate = threading.Event()

    class _Loader(importlib.abc.Loader):
        def create_module(self, spec):
            return None
        def exec_module(self, module):
            started.set()
            gate.wait()  # never set

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name == "_hermes_stuck_import":
                return importlib.machinery.ModuleSpec(name, _Loader())
            return None

    sys.meta_path.insert(0, _Finder())
    started = threading.Event()
    threading.Thread(target=lambda: __import__("_hermes_stuck_import"), daemon=False).start()
    started.wait(5)

    cli._should_seed_interactive = lambda *a, **k: False
    stub = types.SimpleNamespace(_active_session_refusal_reason=MAX_CONCURRENT_SESSIONS)
    stub._claim_active_session = lambda *a, **k: False
    import time
    print(f"REFUSAL_AT={{time.time()}}", file=sys.stderr, flush=True)
    cli._run_single_query_mode(stub, "work kanban task t_x", None, False, True)
    """
)


def _run_child(tmp_path, *, kanban: bool):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_KANBAN_")}
    env["HERMES_HOME"] = str(tmp_path / "hermes")
    (tmp_path / "hermes").mkdir(exist_ok=True)
    if kanban:
        env["HERMES_KANBAN_TASK"] = "t_hang_probe"
        env["HERMES_KANBAN_RUN_ID"] = "4242"
    script = tmp_path / "child.py"
    script.write_text(_CHILD.format(repo=str(REPO)), encoding="utf-8")
    t0 = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, str(script)], env=env, cwd=str(tmp_path),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return proc, t0


@pytest.mark.real_kanban_hard_exit
def test_kanban_worker_refused_on_capacity_exits_despite_stuck_import(tmp_path):
    from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE
    from hermes_cli.quiet_single_query import KANBAN_WORKER_BUSY_MARKER, KANBAN_WORKER_EXIT_TRAILER

    proc, t0 = _run_child(tmp_path, kanban=True)
    try:
        # Cold import of ``cli`` is the slow part; the exit itself must follow within 5 s.
        out, err = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        pytest.fail(f"kanban worker hung on exit\nstderr tail:\n{err[-2000:]}")
    assert proc.returncode == KANBAN_RATE_LIMIT_EXIT_CODE, err[-2000:]
    assert f"{KANBAN_WORKER_BUSY_MARKER}MAX_CONCURRENT_SESSIONS run=4242" in err
    assert f"{KANBAN_WORKER_EXIT_TRAILER}{KANBAN_RATE_LIMIT_EXIT_CODE}" in err
    exited_at = time.time()
    refusal_at = float(next(l for l in err.splitlines() if l.startswith("REFUSAL_AT=")).split("=", 1)[1])
    assert exited_at - refusal_at < 5, f"exit took {exited_at - refusal_at:.1f}s after the refusal"


@pytest.mark.real_kanban_hard_exit
def test_non_kanban_one_shot_keeps_normal_interpreter_exit(tmp_path):
    """Outside Kanban the ``sys.exit`` path is unchanged: the same stuck non-daemon
    thread still holds the process open (proves the probe reproduces the hang)."""
    proc, _t0 = _run_child(tmp_path, kanban=False)
    try:
        proc.communicate(timeout=25)
        pytest.fail(f"expected the non-Kanban one-shot to block in finalization (rc={proc.returncode})")
    except subprocess.TimeoutExpired:
        pass
    finally:
        proc.kill()
        proc.communicate()
