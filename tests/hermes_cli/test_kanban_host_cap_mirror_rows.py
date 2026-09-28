"""Capacity caps count only CLAIMED running rows; a cap hold names its cause.

t_b19ce8b9: a synced fleet board holds ``running`` MIRROR rows for cards other
nodes are working (no ``claim_lock``, no ``worker_pid``). ``count_running_tasks``
counted them, so 9 foreign rows filled a ``max_in_progress`` of 2 and the node
spawned nothing, every tick, with no logged reason.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_ops

_FENCE = "verified execution lease required before running"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda sample=None: "ok")
    kb.init_db()
    return home


def _spawner(spawns: list):
    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42
    return fake_spawn


def _add_mirror_rows(conn: sqlite3.Connection, n: int, assignee: str = "remote") -> list[str]:
    """``running`` rows with no claim, fenced like the fleet adapter fences them.

    The fence (a lifecycle-guard trigger) is what keeps the orphan reconciler
    from requeueing a mirror row on a real fleet board, so the rows stay
    ``running`` through the dispatch tick exactly as they do on Snowdrop.
    """
    ids = []
    for i in range(n):
        tid = kb.create_task(conn, title=f"mirror-{i}", assignee=assignee)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'running', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL WHERE id = ?",
                (tid,),
            )
        ids.append(tid)
    conn.execute("CREATE TABLE IF NOT EXISTS _mirror_ids (id TEXT PRIMARY KEY)")
    conn.executemany("INSERT OR IGNORE INTO _mirror_ids VALUES (?)", [(t,) for t in ids])
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS _mirror_fence BEFORE UPDATE ON tasks "
        "WHEN OLD.id IN (SELECT id FROM _mirror_ids) "
        f"BEGIN SELECT RAISE(ABORT, '{_FENCE}'); END"
    )
    conn.commit()
    return ids


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0]


def test_mirror_rows_do_not_fill_host_cap(kanban_home, all_assignees_spawnable):
    """Snowdrop shape: 9 mirror rows + ready local cards spawn up to the cap."""
    spawns: list = []
    with kbc.connect() as conn:
        mirrors = _add_mirror_rows(conn, 9)
        local = [kb.create_task(conn, title=f"local-{i}", assignee="alice") for i in range(3)]
        assert kbd.count_running_tasks(conn) == 0
        res = kbd.dispatch_once(conn, spawn_fn=_spawner(spawns), max_in_progress=2)
        assert all(_status(conn, t) == "running" for t in mirrors)

    assert len(spawns) == 2
    assert set(spawns) <= set(local)
    assert res.capacity_held is None


def test_mirror_rows_on_other_board_do_not_count(kanban_home, all_assignees_spawnable):
    kb.create_board("fleet")
    with kbc.connect(board="fleet") as conn:
        _add_mirror_rows(conn, 5)
        claimed = kb.create_task(conn, title="real", assignee="alice")
        assert kb.claim_task(conn, claimed) is not None
    assert kbd.count_running_tasks_other_boards() == 1

    spawns: list = []
    with kbc.connect() as conn:
        for i in range(3):
            kb.create_task(conn, title=f"a{i}", assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=_spawner(spawns), max_in_progress=3)

    assert len(spawns) == 2  # 1 claimed elsewhere + 2 here = 3
    assert res.capacity_held is None


def test_mirror_rows_do_not_fill_board_cap(kanban_home, all_assignees_spawnable):
    spawns: list = []
    with kbc.connect() as conn:
        _add_mirror_rows(conn, 4)
        kb.create_task(conn, title="local", assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=_spawner(spawns), max_spawn=1)
    assert len(spawns) == 1
    assert res.capacity_held is None


def test_mirror_rows_do_not_fill_per_profile_cap(kanban_home, all_assignees_spawnable):
    """A mirror of another node's card for the same profile name is not our worker."""
    spawns: list = []
    with kbc.connect() as conn:
        _add_mirror_rows(conn, 3, assignee="alice")
        kb.create_task(conn, title="local", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_spawner(spawns), max_in_progress_per_profile=1,
        )
    assert len(spawns) == 1
    assert not res.skipped_per_profile_capped


def test_host_cap_hold_is_reported_and_logged(
    kanban_home, all_assignees_spawnable, caplog,
):
    spawns: list = []
    with kbc.connect() as conn:
        _add_mirror_rows(conn, 4)
        for i in range(2):
            tid = kb.create_task(conn, title=f"busy-{i}", assignee="alice")
            assert kb.claim_task(conn, tid) is not None
        kb.create_task(conn, title="waiting", assignee="alice")
        with caplog.at_level(logging.INFO):
            res = kbd.dispatch_once(conn, spawn_fn=_spawner(spawns), max_in_progress=2)

    assert not spawns
    assert res.capacity_held == "host cap: 2 running of 2"
    assert "host cap: 2 running of 2" in caplog.text
    assert "host cap: 2 running of 2" in kbd.describe_suppression([res])


def test_board_cap_hold_is_reported(kanban_home, all_assignees_spawnable):
    spawns: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, tid) is not None
        kb.create_task(conn, title="waiting", assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=_spawner(spawns), max_spawn=1)
    assert not spawns
    assert res.capacity_held == "board cap: 1 running of 1"


def test_live_orphan_with_pid_still_counts(kanban_home, all_assignees_spawnable):
    """A local worker whose claim was wiped but whose pid is alive keeps its slot.

    ``reconcile_orphaned_running`` defers such a row while the pid lives, so the
    caps must still see it or the dispatcher would over-spawn beside it.
    """
    import os

    spawns: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="orphan", assignee="alice")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'running', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = ? WHERE id = ?",
                (os.getpid(), tid),
            )
        kb.create_task(conn, title="waiting", assignee="alice")
        assert kbd.count_running_tasks(conn) == 1
        res = kbd.dispatch_once(conn, spawn_fn=_spawner(spawns), max_in_progress=1)
        assert _status(conn, tid) == "running"
    assert not spawns
    assert res.capacity_held == "host cap: 1 running of 1"


def test_no_hold_reason_when_under_cap(kanban_home, all_assignees_spawnable):
    with kbc.connect() as conn:
        kb.create_task(conn, title="a", assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=_spawner([]), max_in_progress=4)
    assert res.capacity_held is None
    assert kbd.describe_suppression([res]) == ""


def test_dispatch_cli_shows_capacity_hold(kanban_home, monkeypatch, capsys):
    monkeypatch.setattr(
        kanban_ops.kbd, "dispatch_once",
        lambda *a, **k: kbd.DispatchResult(capacity_held="host cap: 10 running of 2"),
    )
    args = dict(dry_run=True, max=None, failure_limit=kbd.DEFAULT_FAILURE_LIMIT)
    assert kanban_ops._cmd_dispatch(SimpleNamespace(json=True, **args)) == 0
    assert json.loads(capsys.readouterr().out)["capacity_held"] == "host cap: 10 running of 2"
    assert kanban_ops._cmd_dispatch(SimpleNamespace(json=False, **args)) == 0
    assert "Held (host cap: 10 running of 2)" in capsys.readouterr().out
