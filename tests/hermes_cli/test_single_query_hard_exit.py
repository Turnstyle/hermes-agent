"""A one-shot ``chat -q``/``-Q`` run must leave via ``os._exit`` once its cleanup ran.

Live (Sheldon, 2026-09-28): refused Kanban workers printed ``[kanban-worker-exit] rc=75`` but never
exited (0% CPU for 19 min to 2h42m): ``sys.exit`` ran ``Py_FinalizeEx``, which blocked on the import
lock / a non-daemon thread held by background work. Live again 2026-10-03: a plain Bot Chat DM child
(``chat -Q --query-file``) hung 10h14m in ``Py_FinalizeEx -> gc -> import`` and its parent held the
recipient's turn lock the whole time. Every one-shot now hard-exits after its cleanup, like the ``-z``
oneshot path (#30387, #43055).
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


@pytest.mark.real_single_query_hard_exit
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


@pytest.mark.real_single_query_hard_exit
def test_plain_one_shot_exits_despite_stuck_import_with_its_exit_code(tmp_path):
    """A Bot Chat style one-shot (no Kanban env) with the same stuck non-daemon import thread."""
    proc, _t0 = _run_child(tmp_path, kanban=False)
    try:
        out, err = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        pytest.fail(f"plain one-shot hung on exit\nstderr tail:\n{err[-2000:]}")
    assert proc.returncode == 1, err[-2000:]
    assert "[kanban-worker-exit]" not in err
    exited_at = time.time()
    refusal_at = float(next(l for l in err.splitlines() if l.startswith("REFUSAL_AT=")).split("=", 1)[1])
    assert exited_at - refusal_at < 5, f"exit took {exited_at - refusal_at:.1f}s after the refusal"


@pytest.mark.parametrize("kanban", [False, True])
@pytest.mark.parametrize("code", [0, 1, 75])
def test_single_query_system_exit_becomes_hard_exit(monkeypatch, code, kanban):
    from hermes_cli import quiet_single_query as qsq

    if kanban:
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_x")
    else:
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    seen = []
    monkeypatch.setattr(qsq, "hard_exit_single_query", seen.append)
    cleaned = []
    with pytest.raises(SystemExit) as raised:
        with qsq.single_query_hard_exit():
            try:
                qsq.exit_single_query(code)
            finally:
                cleaned.append("finally")
    assert raised.value.code == code
    assert cleaned == ["finally"] and seen == [code]


_SYNTHETIC = textwrap.dedent(
    """
    import sys, threading, importlib.abc, importlib.machinery
    sys.path.insert(0, {repo!r})
    from hermes_cli.quiet_single_query import hard_exit_single_query

    started = threading.Event()
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
    threading.Thread(target=lambda: __import__("_hermes_stuck_import"), daemon=False).start()
    started.wait(10)
    print("READY", flush=True)
    {exit_call}
    """
)


def _spawn_synthetic(tmp_path, exit_call):
    script = tmp_path / "synthetic.py"
    script.write_text(_SYNTHETIC.format(repo=str(REPO), exit_call=exit_call), encoding="utf-8")
    return subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_hard_exit_returns_promptly_while_a_thread_is_parked_in_an_import(tmp_path):
    proc = _spawn_synthetic(tmp_path, "hard_exit_single_query(3)")
    try:
        out, err = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail("hard exit blocked behind a thread parked in an import")
    assert proc.returncode == 3 and "READY" in out, err[-1000:]


def test_plain_sys_exit_blocks_behind_the_same_parked_import(tmp_path):
    """Contrast: a non-daemon thread is joined in ``threading._shutdown`` forever, so the plain exit never returns."""
    proc = _spawn_synthetic(tmp_path, "sys.exit(3)")
    try:
        assert proc.stdout.readline().strip() == "READY"
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=4)
    finally:
        proc.kill()
        proc.communicate()
