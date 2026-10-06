"""Fail-closed admission for card claims and worker handoffs."""
from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from pathlib import Path

from hermes_constants import get_default_hermes_root, get_hermes_home

_log = logging.getLogger(__name__)
_held = threading.local()


class LaunchDeferred(RuntimeError):
    """Admission was refused before a worker started."""


class WorkerHandoffUncertain(RuntimeError):
    """A spawned worker must retain its claim when PID persistence fails."""


def pause_reason() -> str | None:
    """Presence alone pauses launches, including unreadable or partial markers."""
    try:
        home = get_hermes_home()
        root = get_default_hermes_root(home=home.resolve())
        for parent in dict.fromkeys((home, root)):
            for name in ("ESTOP", ".drain_request.json"):
                path = parent / name
                try:
                    path.lstat()
                except FileNotFoundError:
                    continue
                return f"launch paused by {path}"
    except Exception as exc:
        return f"launch pause check failed ({type(exc).__name__})"
    return None


@contextlib.contextmanager
def launch_guard(conn=None, *, board=None, db_path=None, wait_seconds=0):
    """Hold the board lock until the caller records its claim/worker.

    Nested claims in a dispatcher reuse only this thread's admission. Copied
    contexts and forked children must acquire their own lock. The open SQLite
    connection is authoritative for private boards and explicit DB overrides.
    """
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc

    try:
        if conn is not None:
            rows = conn.execute("PRAGMA database_list").fetchall()
            actual = next(file for _, name, file in rows if name == "main")
            if actual:
                db_path = Path(actual)
        path = Path(db_path or kb.kanban_db_path(board=board)).resolve()
    except Exception as exc:
        _log.warning("launch skipped: board path unavailable (%s)", type(exc).__name__)
        yield False
        return

    key = (os.getpid(), str(path))
    owned = getattr(_held, "boards", {})
    nested = key in owned
    deadline = time.monotonic() + wait_seconds
    with contextlib.ExitStack() as stack:
        while True:
            lock = contextlib.nullcontext(True) if nested else kbc._dispatch_tick_lock(path)
            acquired = stack.enter_context(lock)
            if acquired or time.monotonic() >= deadline or pause_reason():
                break
            stack.close()
            time.sleep(0.01)
        reason = pause_reason() or (None if acquired else "board dispatch lock unavailable")
        if reason:
            _log.warning("launch skipped: %s", reason)
            yield False
            return
        if not nested:
            _held.boards = {**owned, key: True}
        try:
            yield True
        finally:
            if not nested:
                _held.boards = owned


@contextlib.contextmanager
def require_launch(conn=None, *, board=None, wait_seconds=0):
    """Spawn callers cannot turn refused admission into a successful handoff."""
    with launch_guard(conn, board=board, wait_seconds=wait_seconds) as admitted:
        if not admitted:
            raise LaunchDeferred("Worker launch deferred: restart pause or board lock unavailable")
        yield
