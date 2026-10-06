"""Block comments and decomposition operate on fixture-owned boards."""
import argparse
import os
import unittest
from unittest import mock
import pytest


@pytest.fixture
def isolated_board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    from hermes_cli import kanban_db as kb
    kb.init_db()


@pytest.mark.usefixtures("isolated_board")
class KanbanBlockCommentOrderTests(unittest.TestCase):
    def test_block_does_not_comment_when_block_task_fails(self):
        from hermes_cli import kanban as kanban_cli
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title="running", assignee="worker")
            kb.claim_task(conn, tid)
            stale_run = kb.get_task(conn, tid).current_run_id
            with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": tid}):
                with mock.patch.object(
                    kanban_cli,
                    "_worker_run_id_for",
                    lambda _tid: stale_run + 1 if stale_run is not None else 999,
                ):
                    args = argparse.Namespace(
                        task_id=tid, ids=None, reason=["should", "not", "stick"], kind=None
                    )
                    self.assertNotEqual(kanban_cli._cmd_block(args), 0)
            comments = conn.execute(
                "SELECT body FROM task_comments WHERE task_id=?", (tid,)
            ).fetchall()
            self.assertEqual(comments, [])

    def test_decompose_respects_existing_decomposed_event(self):
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc
        from hermes_cli.kanban_db_graph import decompose_triage_task

        with kbc.connect_closing() as conn:
            root = kb.create_task(conn, title="already fenced", assignee="orch")
            with kb.write_txn(conn):
                kb._append_event(conn, root, "decomposed", {"child_ids": ["t_prior"]})
            children = [{"title": "child-a"}, {"title": "child-b"}]
            self.assertIsNone(
                decompose_triage_task(conn, root, root_assignee="orch", children=children)
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM tasks WHERE id != ?", (root,)).fetchone()[0],
                0,
            )

    def test_list_triage_ids_skips_imported_fleet_mirrors(self):
        from hermes_cli import kanban_decompose
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        with kbc.connect_closing() as conn:
            owned = kb.create_task(conn, title="owned triage", assignee="orch", triage=True)
            conn.execute(
                """INSERT INTO tasks(
                    id,title,body,assignee,status,priority,created_by,created_at,
                    workspace_kind,tenant,max_runtime_seconds,max_retries,goal_mode
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "t_fk_deadbeef",
                    "mirror triage",
                    "",
                    "orch",
                    "triage",
                    0,
                    "remote",
                    1,
                    "scratch",
                    None,
                    3600,
                    2,
                    0,
                ),
            )
            conn.commit()
            ids = kanban_decompose.list_triage_ids()
            self.assertIn(owned, ids)
            self.assertNotIn("t_fk_deadbeef", ids)

    def test_list_triage_ids_skips_reassigned_fleet_ownership(self):
        from hermes_cli import kanban_decompose
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        with kbc.connect_closing() as conn:
            owned = kb.create_task(conn, title="reassigned away", assignee="orch", triage=True)
            conn.execute(
                """CREATE TABLE IF NOT EXISTS fleet_kanban_issue_map (
                    local_task_id TEXT PRIMARY KEY,
                    issue_id TEXT,
                    source_node TEXT,
                    current_node TEXT,
                    deferred_snapshot INTEGER,
                    canonical_status TEXT
                )"""
            )
            conn.execute(
                """INSERT INTO fleet_kanban_issue_map(
                    local_task_id, issue_id, source_node, current_node,
                    deferred_snapshot, canonical_status
                ) VALUES(?,?,?,?,?,?)""",
                (owned, "fk_test_issue", "turnerbook", "max", 0, "triage"),
            )
            conn.commit()
            with mock.patch.dict(os.environ, {"FLEET_KANBAN_ACTOR_NODE": "turnerbook"}):
                ids = kanban_decompose.list_triage_ids()
            self.assertNotIn(owned, ids)

