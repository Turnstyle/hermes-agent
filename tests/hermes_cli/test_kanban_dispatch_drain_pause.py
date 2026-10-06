"""The embedded Kanban dispatcher pauses while a drain marker is active.

Defect (Sheldon coordinated restart r-20261006-141811-f1f1): the restart script
wrote ``~/.hermes/.drain_request.json`` and the gateway refused new chat turns,
but the dispatcher kept spawning Kanban workers, which the restart deadline then
cut. ``_kanban_dispatch_allowed()`` now also returns False while an ACTIVE
(present, same-epoch, unexpired) marker sits at the machine root or in the
gateway's own HERMES_HOME. Running workers, Board Ops and the zombie reaper are
untouched; a stale marker never pauses dispatch.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import gateway.drain_control as dc
import gateway.kanban_watchers_common as kwc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kwc, "_drain_pause_logged_for", None, raising=False)
    kb.init_db()
    return home


def _write_marker(home: Path, *, age_s: float = 0.0, epoch: str | None = None) -> None:
    at = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    body = {"action": "drain", "requested_at": at.isoformat(), "principal": "coordinated-restart:r-test",
            "epoch": dc.current_instantiation_epoch() if epoch is None else epoch, "suppress_notification": False}
    (home / ".drain_request.json").write_text(json.dumps(body), encoding="utf-8")


def _run_watcher(monkeypatch, between_ticks) -> list[str]:
    """Drive the REAL ``_kanban_dispatcher_watcher`` loop; ``between_ticks(n)`` runs after tick n (1-based)."""
    from gateway.run import GatewayRunner
    import gateway.kanban_board_ops as board_ops
    import gateway.kanban_watchers_dispatcher as kwd
    import hermes_cli.config as cfg_mod
    from hermes_cli import kanban_db_dispatch as kbd

    spawned: list[str] = []
    decomposed: list[int] = []

    def fake_spawn(task, workspace, *, board=None):
        spawned.append(task.id)
        return os.getpid()  # alive, so the crash detector leaves the claim alone

    async def no_board_ops(runner, kb_mod):
        return None

    async def fast_sleep(_delay):
        return None

    monkeypatch.setattr(kbd, "_default_spawn", fake_spawn)
    monkeypatch.setattr(board_ops, "tick", no_board_ops)
    monkeypatch.setattr(kwd._KanbanDispatcher, "auto_decompose_tick",
                        lambda self, n: decomposed.append(n) or 0)
    monkeypatch.setattr(cfg_mod, "load_config", lambda: {"kanban": {
        "dispatch_in_gateway": True, "dispatch_interval_seconds": 1, "auto_decompose": True}})
    monkeypatch.setattr("gateway.kanban_watchers.asyncio.sleep", fast_sleep)

    runner = object.__new__(GatewayRunner)
    runner._running = True
    ticks = {"n": 0}

    async def between(interval):
        ticks["n"] += 1
        between_ticks(ticks["n"], runner, spawned, decomposed)

    runner._sleep_between_ticks = between
    asyncio.run(asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=20.0))
    return spawned


def test_ready_card_not_claimed_while_marker_present_then_claimed_after_clear(kanban_home, monkeypatch,
                                                                             all_assignees_spawnable):
    """Required test (card t_ad6438cf decision 1)."""
    _write_marker(kanban_home)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="made ready during the drain", assignee="alice")
        assert kb.get_task(conn, tid).status == "ready"

    seen = {}

    def between(n, runner, spawned, decomposed):
        with kbc.connect() as conn:
            seen[n] = (kb.get_task(conn, tid).status, list(spawned), list(decomposed))
        if n == 1:
            (kanban_home / ".drain_request.json").unlink()  # the restart script clears the marker
        else:
            runner._running = False

    _run_watcher(monkeypatch, between)

    status1, spawned1, decomposed1 = seen[1]
    assert status1 == "ready" and spawned1 == [] and decomposed1 == []  # paused: no claim, no auto-decompose
    status2, spawned2, decomposed2 = seen[2]
    assert status2 == "running" and spawned2 == [tid] and decomposed2  # first tick after the clear claims it


def test_stale_marker_does_not_pause_dispatch(kanban_home, monkeypatch, all_assignees_spawnable):
    _write_marker(kanban_home, age_s=dc.DRAIN_REQUEST_MAX_AGE_SECONDS + 60)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="stale marker must not hold me", assignee="alice")

    def between(n, runner, spawned, decomposed):
        runner._running = False

    assert _run_watcher(monkeypatch, between) == [tid]


def test_wrong_epoch_marker_does_not_pause(kanban_home, monkeypatch):
    monkeypatch.setattr(dc, "current_instantiation_epoch", lambda: "boot-now:1")
    _write_marker(kanban_home, epoch="boot-before:1")
    assert kwc._kanban_dispatch_allowed() is True
    _write_marker(kanban_home, epoch="boot-now:1")
    assert kwc._kanban_dispatch_allowed() is False


def test_profile_home_gateway_sees_machine_root_marker(kanban_home, monkeypatch):
    prof = kanban_home / "profiles" / "shld-x"
    prof.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(prof))
    assert kwc._kanban_dispatch_allowed() is True
    _write_marker(kanban_home)  # the restart script writes the ROOT marker, not the profile's
    assert kwc._kanban_dispatch_allowed() is False
    (kanban_home / ".drain_request.json").unlink()
    assert kwc._kanban_dispatch_allowed() is True


def test_pause_logged_once_per_engagement(kanban_home, caplog):
    _write_marker(kanban_home)
    with caplog.at_level(logging.INFO, logger="gateway.run"):
        for _ in range(4):
            assert kwc._kanban_dispatch_allowed() is False
        (kanban_home / ".drain_request.json").unlink()
        assert kwc._kanban_dispatch_allowed() is True
        assert kwc._kanban_dispatch_allowed() is True
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("PAUSED by drain marker" in m for m in msgs) == 1
    assert sum("dispatch resumes" in m for m in msgs) == 1


def test_reader_error_fails_open(kanban_home, monkeypatch):
    _write_marker(kanban_home)

    def boom(**_k):
        raise RuntimeError("reader broke")

    monkeypatch.setattr(dc, "drain_requested", boom)
    assert kwc._kanban_dispatch_allowed() is True
