"""The embedded dispatcher releases board WAL keepalives it must not keep pinning.

A keepalive reader holds the board's -wal/-shm (and the file's inode) open. A board quarantined
as corrupt must drop it — or the replaced/restored file is never reopened cleanly — and so must a
dispatcher that gives up its singleton lock (its successor opens its own).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hermes_cli import kanban_db_connect as kbc


def _settings():
    from gateway.kanban_watchers_dispatcher import _DispatcherSettings
    return _DispatcherSettings(interval=60.0, max_spawn=None, max_in_progress=None, failure_limit=5,
                               stale_timeout_seconds=0, reconcile_orphans=False, default_assignee=None,
                               max_in_progress_per_profile=None)


def _board(tmp_path: Path) -> Path:
    db_path = tmp_path / "kanban.db"
    kbc.init_db(db_path)
    return db_path


def test_corrupt_board_quarantine_closes_its_wal_keepalive(tmp_path, monkeypatch):
    from gateway.kanban_watchers_dispatcher import _KanbanDispatcher

    db_path = _board(tmp_path)

    class Kb:
        def kanban_db_path(self, slug):
            return db_path

    def corrupt_connect(*_args, **_kwargs):
        raise sqlite3.DatabaseError("file is not a database")

    try:
        assert kbc.ensure_wal_keepalive(db_path) is not None
        monkeypatch.setattr(kbc, "connect", corrupt_connect)
        dispatcher = _KanbanDispatcher(Kb(), _settings())
        assert dispatcher.tick_once_for_board("quarantined") is None
        assert "quarantined" in dispatcher.disabled_corrupt_boards
        assert not kbc.is_wal_keepalive_active(db_path)
    finally:
        kbc.close_all_wal_keepalives()


def test_dispatcher_lock_release_closes_all_wal_keepalives(tmp_path):
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin

    db_path = _board(tmp_path)

    class Runner(GatewayKanbanWatchersMixin):
        _kanban_dispatcher_lock_handle = None

    try:
        assert kbc.ensure_wal_keepalive(db_path) is not None
        Runner()._release_kanban_dispatcher_lock()
        assert not kbc.is_wal_keepalive_active(db_path)
    finally:
        kbc.close_all_wal_keepalives()


def test_healthy_tick_leaves_keepalive_alone(tmp_path, monkeypatch):
    """Only the quarantine path closes: a normal tick never tears down a live keepalive."""
    from gateway import kanban_watchers_dispatcher as kwd

    db_path = _board(tmp_path)

    class Kb:
        def kanban_db_path(self, slug):
            return db_path

    monkeypatch.setattr(kbc, "connect", lambda *a, **k: sqlite3.connect(db_path))
    monkeypatch.setattr(kwd, "_kbd", lambda: type("Kbd", (), {"dispatch_once": staticmethod(lambda conn, **kw: "ok")}))
    try:
        assert kbc.ensure_wal_keepalive(db_path) is not None
        assert kwd._KanbanDispatcher(Kb(), _settings()).tick_once_for_board("healthy") == "ok"
        assert kbc.is_wal_keepalive_active(db_path)
    finally:
        kbc.close_all_wal_keepalives()
