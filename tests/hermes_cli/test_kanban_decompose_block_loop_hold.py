"""Auto-decompose must not undo the unblock-loop breaker.

A task that trips ``BLOCK_RECURRENCE_LIMIT`` lands in ``triage`` with a
``block_loop_detected`` event so a human can look at it. Before this guard the
gateway's auto-decompose tick listed every triage task and promoted it straight
back to ``todo`` on the next tick, so a Turner-gated card looped forever
(TurnerBook card t_e398fd6b, evidence in t_4a323dd5).

The hold lasts until a human touches the card: an ``edited`` (title/body),
``assigned`` or direct ``status`` event after the latest ``block_loop_detected``
releases it.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_decompose


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    kb.init_db()
    return home


def _make_running(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


def _block_loop_to_triage(conn, title="turner-gated"):
    """block -> unblock -> re-block for the same cause: the breaker routes to triage."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    _make_running(conn, tid)
    assert kb.block_task(conn, tid, reason="needs Turner", kind="needs_input")
    assert kb.unblock_task(conn, tid)
    _make_running(conn, tid)
    assert kb.block_task(conn, tid, reason="needs Turner", kind="needs_input")
    task = kb.get_task(conn, tid)
    assert task.status == "triage"
    assert [e for e in kb.list_events(conn, tid) if e.kind == "block_loop_detected"]
    return tid


def test_block_loop_triage_task_is_not_listed_for_auto_decompose(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        held = _block_loop_to_triage(conn)
        fresh = kb.create_task(conn, title="plain triage", assignee="worker", triage=True)
    ids = kanban_decompose.list_triage_ids()
    assert held not in ids
    assert fresh in ids


def test_human_edit_releases_the_hold(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _block_loop_to_triage(conn)
        assert kb.edit_task(conn, tid, body="Turner answered: go ahead")
    assert tid in kanban_decompose.list_triage_ids()


def test_human_reassign_releases_the_hold(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _block_loop_to_triage(conn)
        assert kb.assign_task(conn, tid, "other-worker")
    assert tid in kanban_decompose.list_triage_ids()


def test_bot_assignment_event_does_not_release_hold(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _block_loop_to_triage(conn)
        with kb.write_txn(conn):
            kb._append_event(
                conn, tid, "assigned",
                {"assignee": "worker", "source": "kanban.default_assignee"},
            )
    assert tid not in kanban_decompose.list_triage_ids()
    with kbc.connect_closing() as conn:
        assert kb.assign_task(conn, tid, "other-worker")
    assert tid in kanban_decompose.list_triage_ids()


def test_dashboard_status_move_releases_the_hold(kanban_home: Path) -> None:
    """The dashboard drag path writes a raw ``status`` event (plugin_api._set_status_direct)."""
    with kbc.connect_closing() as conn:
        tid = _block_loop_to_triage(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "status", {"status": "triage", "requested_status": "triage"})
    assert tid in kanban_decompose.list_triage_ids()


def test_comment_does_not_release_the_hold(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _block_loop_to_triage(conn)
        kb.add_comment(conn, tid, "some-bot", "still waiting")
    assert tid not in kanban_decompose.list_triage_ids()


def test_priority_only_change_does_not_release_the_hold(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _block_loop_to_triage(conn)
        assert kb.edit_task(conn, tid, priority=99)
    assert tid not in kanban_decompose.list_triage_ids()


def test_second_block_loop_after_release_holds_again(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _block_loop_to_triage(conn)
        assert kb.edit_task(conn, tid, body="try again")
        assert kb.specify_triage_task(conn, tid)
        _make_running(conn, tid)
        assert kb.block_task(conn, tid, reason="needs Turner", kind="needs_input")
        assert kb.get_task(conn, tid).status == "triage"
    assert tid not in kanban_decompose.list_triage_ids()


def test_stale_default_assignment_cannot_release_block_loop_hold(kanban_home: Path) -> None:
    """A dispatcher's ready-row snapshot must not assign after another writer trips the breaker."""
    with kbc.connect_closing() as dispatcher, kbc.connect_closing() as worker:
        tid = kb.create_task(dispatcher, title="unassigned", assignee=None)
        with kb.write_txn(dispatcher):
            dispatcher.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))

        # A reads the ready/unassigned row, then B handles the task before A resumes.
        snapshot = kbd._lane_rows(dispatcher, "ready")
        assert [(row["id"], row["assignee"]) for row in snapshot] == [(tid, None)]
        assert kb.assign_task(worker, tid, "worker")
        _make_running(worker, tid)
        assert kb.block_task(worker, tid, reason="needs Turner", kind="needs_input")
        assert kb.unblock_task(worker, tid)
        _make_running(worker, tid)
        assert kb.block_task(worker, tid, reason="needs Turner", kind="needs_input")
        assert kb.get_task(worker, tid).status == "triage"

        applied = kbd._apply_default_assignee(dispatcher, snapshot[0]["id"], "default", dry_run=False)
        events = kb.list_events(worker, tid)
        loop_id = max(event.id for event in events if event.kind == "block_loop_detected")
        bot_assignments = [
            event for event in events
            if event.id > loop_id and event.kind == "assigned"
            and event.payload.get("source") == "kanban.default_assignee"
        ]
        assignee = kb.get_task(worker, tid).assignee
        held = kanban_decompose._held_by_block_loop(worker, tid)
    eligible = tid in kanban_decompose.list_triage_ids()
    assert (applied, len(bot_assignments), assignee, held, eligible) == (
        False, 0, "worker", True, False,
    )


def test_auto_decompose_tick_leaves_block_loop_triage_task_alone(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through the gateway tick: the held card is never handed to decompose_task."""
    from gateway import kanban_watchers_dispatcher as kwd

    with kbc.connect_closing() as conn:
        held = _block_loop_to_triage(conn)
        fresh = kb.create_task(conn, title="plain triage", assignee="worker", triage=True)

    calls: list[str] = []

    def fake_decompose(task_id, author=None):
        calls.append(task_id)
        return SimpleNamespace(ok=False, fanout=False, child_ids=None, reason="test stub")

    # Patch the module object the tick will import: other suites purge
    # hermes_cli.kanban* from sys.modules, so the file-level import can be stale.
    import hermes_cli
    live_decomp = importlib.import_module("hermes_cli.kanban_decompose")
    monkeypatch.setattr(hermes_cli, "kanban_decompose", live_decomp, raising=False)
    monkeypatch.setattr(live_decomp, "decompose_task", fake_decompose)
    monkeypatch.setattr(kwd, "_board_slugs", lambda _kb: [kb.DEFAULT_BOARD])
    settings = kwd._DispatcherSettings(60.0, None, None, 2, 0, True, None, None)
    dispatcher = kwd._KanbanDispatcher(SimpleNamespace(DEFAULT_BOARD=kb.DEFAULT_BOARD), settings)

    for _ in range(3):  # several ticks: the loop showed up tick after tick
        dispatcher.auto_decompose_tick(10)

    assert held not in calls
    assert fresh in calls
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, held).status == "triage"
    assert "hermes_cli.kanban_decompose" in sys.modules
