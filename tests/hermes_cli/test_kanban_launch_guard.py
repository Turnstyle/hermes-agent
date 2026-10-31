"""ESTOP / drain launch admission: no waiter starts while new work is paused, and the card is not charged.

Rewritten onto the owner-managed spawn: ``_default_spawn`` checks ``kanban_launch.pause_reason``
right before ``Popen`` of ``kanban_worker_client``; a refusal is host-level
(``LaunchDeferred`` is a ``RestartSafeScopeUnavailable``), so the dispatcher releases the claim
without counting a failure, exactly as it books an unplaceable restart-safe scope.
"""
from __future__ import annotations

import subprocess

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_launch


@pytest.fixture
def board(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)
    launched = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, *a, **k: launched.append(cmd) or pytest.fail("worker started"))
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield root, profile, conn, launched


def _pause(root, profile, kind):
    if kind == "estop-profile":
        (profile / "ESTOP").write_text("{}")
    elif kind == "estop-root":
        (root / "ESTOP").write_text("{}")
    else:
        from gateway.drain_control import write_drain_request
        write_drain_request(home=profile if kind == "drain-profile" else root)


@pytest.mark.parametrize("kind", ["estop-profile", "estop-root", "drain-profile", "drain-root"])
def test_paused_dispatch_starts_no_waiter_and_charges_nothing(board, kind):
    root, profile, conn, launched = board
    tid = kb.create_task(conn, title="paused", assignee="worker")
    _pause(root, profile, kind)
    assert kanban_launch.pause_reason()

    result = kbd.dispatch_once(conn)

    assert launched == [] and result.spawned == []
    task = kb.get_task(conn, tid)
    assert (task.status, task.consecutive_failures, task.claim_lock) == ("ready", 0, None)
    # Pre-claim: the paused tick never claimed the card, so no run was opened and released.
    assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0] == 0


def test_paused_default_spawn_raises_host_level_deferral(board):
    root, profile, conn, launched = board
    tid = kb.create_task(conn, title="direct", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    _pause(root, profile, "estop-profile")
    from tools.process_registry import RestartSafeScopeUnavailable
    with pytest.raises(kanban_launch.LaunchDeferred) as deferred:
        kbd._default_spawn(claimed, str(root))
    assert isinstance(deferred.value, RestartSafeScopeUnavailable)
    assert launched == []


def test_stale_drain_marker_does_not_pause(board, monkeypatch):
    root, profile, _conn, _launched = board
    from gateway import drain_control
    # Pin both epochs: without /proc (macOS) the real epoch is "" and disables the check.
    monkeypatch.setattr(drain_control, "current_instantiation_epoch", lambda: "earlier-boot")
    drain_control.write_drain_request(home=profile)
    monkeypatch.setattr(drain_control, "current_instantiation_epoch", lambda: "another-boot")
    assert kanban_launch.pause_reason() is None


def test_unreadable_pause_state_fails_closed(board, monkeypatch):
    import agent.estop as estop
    monkeypatch.setattr(estop, "is_engaged", lambda: (_ for _ in ()).throw(PermissionError("denied")))
    assert "failed" in kanban_launch.pause_reason()


def test_unpaused_launch_is_admitted(board):
    assert kanban_launch.pause_reason() is None
    kanban_launch.require_launch()
