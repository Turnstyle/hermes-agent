"""Tests for H24: prevent duplicate lanes on one worktree/dir workspace."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _make_ready_task(conn, title: str, workspace_kind: str, workspace_path: str | None) -> str:
    tid = kb.create_task(
        conn, title=title, workspace_kind=workspace_kind,
        workspace_path=workspace_path,
    )
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    return tid


def test_duplicate_workspace_claim_rejected(kanban_home: Path, tmp_path: Path):
    """Claim A on path P succeeds, claim B on P is rejected with workspace_busy,
    claim C on different path succeeds, B is claimable once A completes."""
    path_p = str(tmp_path / "worktree_p")
    path_c = str(tmp_path / "worktree_c")

    with kbc.connect_closing() as conn:
        task_a = _make_ready_task(conn, "Task A", "worktree", path_p)
        task_b = _make_ready_task(conn, "Task B", "worktree", path_p)
        task_c = _make_ready_task(conn, "Task C", "worktree", path_c)

        # 1. Claim A on path P succeeds
        claimed_a = kb.claim_task(conn, task_a, claimer="worker-a")
        assert claimed_a is not None
        assert claimed_a.status == "running"

        # 2. Claim B on path P is rejected with workspace_busy
        claimed_b = kb.claim_task(conn, task_b, claimer="worker-b")
        assert claimed_b is None
        task_b_record = kb.get_task(conn, task_b)
        assert task_b_record.status == "ready"

        rows = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? AND kind = 'claim_rejected' ORDER BY id",
            (task_b,),
        ).fetchall()
        assert len(rows) >= 1
        payload = json.loads(rows[-1]["payload"])
        assert payload.get("reason") == "workspace_busy"

        # 3. Claim C on a different path succeeds
        claimed_c = kb.claim_task(conn, task_c, claimer="worker-c")
        assert claimed_c is not None
        assert claimed_c.status == "running"

        # 4. Once A completes, B is claimable
        assert kb.complete_task(conn, task_a, summary="done")
        claimed_b2 = kb.claim_task(conn, task_b, claimer="worker-b")
        assert claimed_b2 is not None
        assert claimed_b2.status == "running"


def test_scratch_and_unspecified_workspaces_unaffected(kanban_home: Path, tmp_path: Path):
    """Scratch workspaces and tasks with no workspace_path are not blocked by running worktrees."""
    path_p = str(tmp_path / "worktree_p")

    with kbc.connect_closing() as conn:
        task_a = _make_ready_task(conn, "Task A", "worktree", path_p)
        task_scratch = _make_ready_task(conn, "Task Scratch", "scratch", path_p)
        task_no_path = _make_ready_task(conn, "Task No Path", "worktree", None)

        claimed_a = kb.claim_task(conn, task_a, claimer="worker-a")
        assert claimed_a is not None

        # Scratch task claiming succeeds despite path_p matching
        claimed_scratch = kb.claim_task(conn, task_scratch, claimer="worker-scratch")
        assert claimed_scratch is not None
        assert claimed_scratch.status == "running"

        # Task with no path claiming succeeds
        claimed_no_path = kb.claim_task(conn, task_no_path, claimer="worker-nopath")
        assert claimed_no_path is not None
        assert claimed_no_path.status == "running"


def test_dir_workspace_kind_also_guarded(kanban_home: Path, tmp_path: Path):
    """workspace_kind='dir' also blocks duplicate claims on the same directory."""
    path_dir = str(tmp_path / "shared_dir")

    with kbc.connect_closing() as conn:
        task_1 = _make_ready_task(conn, "Task 1", "dir", path_dir)
        task_2 = _make_ready_task(conn, "Task 2", "dir", path_dir)

        assert kb.claim_task(conn, task_1, claimer="worker-1") is not None
        assert kb.claim_task(conn, task_2, claimer="worker-2") is None
        assert kb.get_task(conn, task_2).status == "ready"
