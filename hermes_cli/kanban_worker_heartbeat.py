"""Process-local liveness for silent Kanban one-shot workers (not a TTL policy).

Only the dispatcher-owned process gets a timer. No child process or external
scheduler survives it. The timer pins the board and attempt at startup, uses
bounded SQLite waits, and never initializes/migrates a board or renews a lease.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)
INTERVAL_SECONDS = 60


class WorkerHeartbeat:
    def __init__(self, path: Path, task_id: str, run_id: int, claim_lock: str):
        self.path = path
        self.task_id = task_id
        self.run_id = run_id
        self.claim_lock = claim_lock
        self.pid = os.getpid()
        self.stopped = threading.Event()
        self.thread: threading.Thread | None = None

    def beat(self) -> float | None:
        """Seconds to next due beat; None means the pinned attempt is over."""
        if self.stopped.is_set() or os.getpid() != self.pid:
            return None
        from hermes_cli import kanban_db as kb, kanban_db_dispatch as kbd
        from hermes_cli.sqlite_safe_read import connect_tracked

        # mode=rw refuses a deleted board rather than silently recreating one.
        # Do not call connect(): periodic liveness must not trigger migrations,
        # board sync, an integrity scan, or the default long busy timeout.
        with contextlib.closing(connect_tracked(
            self.path.as_uri() + "?mode=rw", connect_fn=sqlite3.connect,
            # write_txn already retries busy boundaries with jitter. SQLite's
            # own busy sleeps multiply that retry budget (and can oversleep
            # badly on a loaded host). Liveness can skip a contended minute.
            uri=True, timeout=0, isolation_level=None,
        )) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT worker_pid FROM tasks WHERE id = ?", (self.task_id,)).fetchone()
            if row is None or row["worker_pid"] not in (None, self.pid):
                return None
            if self.stopped.is_set():
                return None
            if not kbd.heartbeat_worker(
                conn, self.task_id, expected_run_id=self.run_id,
                expected_claim_lock=self.claim_lock, min_interval_seconds=INTERVAL_SECONDS,
            ):
                return None
            last = conn.execute(
                "SELECT last_heartbeat_at FROM tasks WHERE id = ?", (self.task_id,),
            ).fetchone()
            age = time.time() - (kb._to_epoch(last[0]) or time.time())
            # Activity may have beaten just before this timer woke. Revisit when
            # THAT beat becomes due, not a full minute after this no-op check.
            return max(0.1, min(INTERVAL_SECONDS, INTERVAL_SECONDS - age))

    def run(self) -> None:
        try:
            while not self.stopped.is_set() and os.getpid() == self.pid:
                started = time.monotonic()
                try:
                    delay = self.beat()
                except PermissionError:
                    break  # a descendant fence is permanent, not a retry condition
                except Exception:
                    logger.warning("Kanban timer heartbeat failed for task %s; retrying next minute", self.task_id)
                    delay = INTERVAL_SECONDS
                if delay is None:
                    break
                # A delayed writer does not accumulate drift or catch-up bursts.
                if self.stopped.wait(max(0.1, started + delay - time.monotonic())):
                    break
        finally:
            self.stopped.set()

    def start(self) -> WorkerHeartbeat:
        ctx = contextvars.copy_context()
        self.thread = threading.Thread(
            target=lambda: ctx.run(self.run), name="kanban-worker-heartbeat", daemon=True,
        )
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stopped.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)


def start_worker_heartbeat() -> WorkerHeartbeat | None:
    """Start once for a one-shot worker; never for a chat, cron or delegate."""
    from agent.delegation_context import is_dispatcher_owned_worker_context
    if not is_dispatcher_owned_worker_context():
        return None
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
    try:
        run_id = int(os.environ.get("HERMES_KANBAN_RUN_ID", ""))
    except ValueError:
        return None
    if not task_id or not claim_lock or run_id <= 0:
        return None
    try:
        from hermes_cli import kanban_db as kb
        return WorkerHeartbeat(kb.kanban_db_path().resolve(), task_id, run_id, claim_lock).start()
    except Exception:
        logger.warning("Could not start Kanban timer heartbeat for task %s", task_id)
        return None
