"""Expired-claim release never touches a card this node cannot prove it owns.

The canonical Fleet ownership fence (``kanban_db._fleet_row_ownership``) runs before
any PID probe, signal or write; owner receipts (``owner_reclaim_blocked``) still gate
the rows this node does own.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

NODE = "node-a"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _install_adapter(conn) -> None:
    conn.execute(
        "CREATE TABLE fleet_kanban_issue_map (local_task_id TEXT PRIMARY KEY, issue_id TEXT, "
        "title TEXT, body TEXT, source_node TEXT, current_node TEXT)"
    )
    conn.execute(
        "CREATE TRIGGER fleet_kanban_task_insert AFTER INSERT ON tasks BEGIN "
        "INSERT INTO fleet_kanban_issue_map (local_task_id, issue_id, title, body, source_node) "
        f"VALUES (NEW.id, 'fk_' || NEW.id, NEW.title, NEW.body, '{NODE}'); END"
    )


def _expired_running(conn, title: str) -> str:
    tid = kb.create_task(conn, title=title)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock='otherhost:1', claim_expires=? WHERE id=?",
            (int(time.time()) - 60, tid),
        )
    return tid


@pytest.mark.parametrize("home,released", [(NODE, True), ("node-b", False), ("", False)])
def test_release_stale_claims_requires_local_fleet_ownership(kanban_home, monkeypatch, home, released):
    probed = []
    real = kb._terminate_reclaimed_worker
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", lambda *a, **k: probed.append(a) or real(*a, **k))
    with kbc.connect_closing() as conn:
        _install_adapter(conn)
        tid = _expired_running(conn, "stale claim")
        with kb.write_txn(conn):
            conn.execute("UPDATE fleet_kanban_issue_map SET current_node=? WHERE local_task_id=?", (home, tid))
        before = kb.get_task(conn, tid)
        assert kb.release_stale_claims(conn) == (1 if released else 0)
        after = kb.get_task(conn, tid)
        if released:
            assert after.status == "ready" and after.claim_lock is None
        else:
            assert (after.status, after.claim_lock) == (before.status, before.claim_lock)
            assert probed == []


def test_host_local_claim_is_ownership_evidence_even_with_foreign_map_row(kanban_home):
    with kbc.connect_closing() as conn:
        _install_adapter(conn)
        tid = _expired_running(conn, "our own stale claim")
        host = kb._claimer_id().split(":", 1)[0]
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_lock=? WHERE id=?", (f"{host}:dead", tid))
            conn.execute("UPDATE fleet_kanban_issue_map SET current_node='node-b' WHERE local_task_id=?", (tid,))
        assert kb.release_stale_claims(conn) == 1
        assert kb.get_task(conn, tid).status == "ready"
