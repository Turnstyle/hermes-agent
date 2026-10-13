"""Legacy breaker rows cannot restart the auto-decompose loop.

Current recurrence warnings and Decider handoffs remain unchanged. Real legacy
rows retain block_kind and block_recurrences; bare advisory events are not
operator holds. Release is audited and leaves the spec in triage.
"""

from pathlib import Path
from types import SimpleNamespace
import argparse
import importlib

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose as decompose


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _legacy(conn, kind="capability"):
    tid = kb.create_task(conn, title="Legacy reviewed spec", body="Keep this body", assignee="worker", triage=True)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET block_kind=?, block_recurrences=2 WHERE id=?", (kind, tid))
        kb._append_event(conn, tid, "block_loop_detected", {"reason": "legacy cause", "kind": kind, "recurrences": 2, "limit": 2})
    return tid


def _snapshot(conn, tid):
    return tuple([dict(row) for row in conn.execute(f"SELECT * FROM {table} WHERE {key}=? ORDER BY id", (tid,))]
                 for table, key in [("tasks", "id"), ("task_events", "task_id"), ("task_comments", "task_id")])


def test_legacy_loop_excluded_but_plain_triage_still_eligible(kanban_home):
    with kbc.connect_closing() as conn:
        held = _legacy(conn)
        fresh = kb.create_task(conn, title="Fresh", triage=True)
    ids = decompose.list_triage_ids()
    assert held not in ids
    assert fresh in ids


@pytest.mark.parametrize("event", ["edited", "assigned", "status", "promoted", "promoted_manual", "degree_warning", "comment"])
def test_automatic_or_edit_events_cannot_release_legacy_hold(kanban_home, event):
    with kbc.connect_closing() as conn:
        tid = _legacy(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, event, {"status": "triage"})
    assert tid not in decompose.list_triage_ids()


@pytest.mark.parametrize("kind", ["needs_input", "capability", "transient"])
def test_explicit_unblock_releases_without_promoting_or_mutating_spec(kanban_home, kind):
    with kbc.connect_closing() as conn:
        tid = _legacy(conn, kind)
        before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        assert kb.unblock_task(conn, tid, actor="operator", reason="Cause resolved")
        assert dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) == before
        assert kb.list_events(conn, tid)[-1].payload == {
            "status": "triage", "cleared_hold": "block_loop_detected", "actor": "operator", "reason": "Cause resolved",
        }
        assert kb.list_comments(conn, tid)[-1].author == "operator"
        assert not decompose._held_by_block_loop(conn, tid)
        assert not kb.unblock_task(conn, tid, actor="operator", reason="Duplicate")
    assert tid in decompose.list_triage_ids()


@pytest.mark.parametrize("actor,reason", [(None, "yes"), ("owner", None), (" ", "yes"), ("owner", " ")])
def test_unblock_requires_provenance_and_writes_nothing_on_refusal(kanban_home, actor, reason):
    with kbc.connect_closing() as conn:
        tid = _legacy(conn)
        before = _snapshot(conn, tid)
        with pytest.raises(ValueError, match="non-empty actor and reason"):
            kb.unblock_task(conn, tid, actor=actor, reason=reason)
        assert _snapshot(conn, tid) == before


def test_audit_failure_rolls_back_release(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = _legacy(conn)
        before = _snapshot(conn, tid)
        def fail(*args, **kwargs):
            raise RuntimeError("audit unavailable")
        monkeypatch.setattr(kb, "_insert_comment", fail)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            kb.unblock_task(conn, tid, actor="operator", reason="Resolved")
        assert _snapshot(conn, tid) == before
    assert tid not in decompose.list_triage_ids()


def test_lookup_error_is_fail_closed(kanban_home, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("lookup unavailable")
    monkeypatch.setattr(kb, "_has_unreleased_block_loop", fail)
    with kbc.connect_closing() as conn:
        assert decompose._held_by_block_loop(conn, "unknown")


def test_second_legacy_event_after_release_holds_again(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _legacy(conn)
        assert kb.unblock_task(conn, tid, actor="operator", reason="Resolved")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "block_loop_detected", {"kind": "capability", "recurrences": 3})
    assert tid not in decompose.list_triage_ids()


def test_cli_unblock_clears_in_place_with_reason(kanban_home):
    from hermes_cli import kanban as cli
    with kbc.connect_closing() as conn:
        tid = _legacy(conn)
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(["kanban", "unblock", tid, "--reason", "Cause resolved"])
    assert cli.kanban_command(args) == 0
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "triage"
        assert kb.list_events(conn, tid)[-1].payload["actor"] == cli._profile_author()
    assert tid in decompose.list_triage_ids()


def test_auto_decompose_ticks_skip_legacy_hold_and_process_plain_card(kanban_home, monkeypatch):
    from gateway import kanban_watchers_dispatcher as kwd
    import hermes_cli
    live = importlib.import_module("hermes_cli.kanban_decompose")
    monkeypatch.setattr(hermes_cli, "kanban_decompose", live, raising=False)
    with kbc.connect_closing() as conn:
        held = _legacy(conn)
        fresh = kb.create_task(conn, title="Fresh", triage=True)
    calls = []
    def fake_decompose(tid, author=None):
        calls.append(tid)
        return SimpleNamespace(ok=False, fanout=False, child_ids=None, reason="test stub")
    monkeypatch.setattr(live, "decompose_task", fake_decompose)
    monkeypatch.setattr(kwd, "_board_slugs", lambda _kb: [kb.DEFAULT_BOARD])
    settings = kwd._DispatcherSettings(60.0, None, None, 2, 0, True, None, None)
    dispatcher = kwd._KanbanDispatcher(SimpleNamespace(DEFAULT_BOARD=kb.DEFAULT_BOARD), settings)
    for _ in range(3):
        dispatcher.auto_decompose_tick(10)
    assert held not in calls
    assert fresh in calls
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, held).status == "triage"
        assert kb.unblock_task(conn, held, actor="operator", reason="Resolved")
    dispatcher.auto_decompose_tick(10)
    assert held in calls
