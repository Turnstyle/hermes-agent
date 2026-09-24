"""BLOCKED comments are recorded only after block_task succeeds (hermetic)."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

HERMETIC_SCRATCH = Path(
    os.environ.get(
        "DECOMP_HERMETIC_SCRATCH",
        "/Users/turner/.hermes/profiles/tb-cndr/cache/scratch/"
        "decomposition-repair-20260924/hermetic_homes",
    )
).resolve()
LIVE_FLEET_BOARD_DB = Path(
    "/Users/turner/.hermes/kanban/boards/fleet/kanban.db"
).resolve()
REAL_KANBAN_ROOT = (Path.home() / ".hermes").resolve()

HERMETIC_SCRATCH.mkdir(parents=True, exist_ok=True)
_ISOLATED_HOME = Path(
    tempfile.mkdtemp(prefix=f"kanban-test-{uuid.uuid4().hex[:8]}-", dir=str(HERMETIC_SCRATCH))
).resolve()
os.environ["HERMES_HOME"] = str(_ISOLATED_HOME)
os.environ["HERMES_KANBAN_HOME"] = str(_ISOLATED_HOME)
os.environ["HERMES_KANBAN_DB"] = str(_ISOLATED_HOME / "kanban.db")
os.environ.pop("HERMES_KANBAN_BOARD", None)

for _mod_name in list(sys.modules):
    if _mod_name.startswith("hermes_cli.kanban") or _mod_name == "hermes_cli.kanban_db_connect":
        del sys.modules[_mod_name]

from hermes_cli import kanban_db as _kanban_db_preflight  # noqa: E402

_RESOLVED_DB = _kanban_db_preflight.kanban_db_path().expanduser().resolve()
if not str(_RESOLVED_DB).startswith(str(HERMETIC_SCRATCH)):
    raise RuntimeError(
        f"kanban_db_path {_RESOLVED_DB} is not under hermetic scratch {HERMETIC_SCRATCH}"
    )
if _RESOLVED_DB == LIVE_FLEET_BOARD_DB:
    raise RuntimeError(f"kanban_db_path resolved to live fleet board {LIVE_FLEET_BOARD_DB}")


def _refuse_live_db_path_without_open() -> None:
    os.environ["HERMES_KANBAN_DB"] = str(LIVE_FLEET_BOARD_DB)
    for name in list(sys.modules):
        if name.startswith("hermes_cli.kanban"):
            del sys.modules[name]
    from hermes_cli import kanban_db as kb

    resolved = kb.kanban_db_path().expanduser().resolve()
    if resolved != LIVE_FLEET_BOARD_DB:
        raise AssertionError(f"expected live path resolution, got {resolved}")
    try:
        resolved.relative_to(REAL_KANBAN_ROOT)
    except ValueError:
        raise AssertionError("live fleet board is not under ~/.hermes")
    raise RuntimeError(f"refusing live kanban DB path {resolved}")


class KanbanBlockCommentOrderTests(unittest.TestCase):
    def setUp(self):
        self._test_home = Path(
            tempfile.mkdtemp(prefix="case-", dir=str(HERMETIC_SCRATCH))
        ).resolve()
        os.environ["HERMES_HOME"] = str(self._test_home)
        os.environ["HERMES_KANBAN_HOME"] = str(self._test_home)
        os.environ["HERMES_KANBAN_DB"] = str(self._test_home / "kanban.db")
        os.environ.pop("HERMES_KANBAN_BOARD", None)
        from hermes_cli import kanban_db as kb

        kb.init_db()

    def test_resolved_db_is_hermetic_before_writes(self):
        from hermes_cli import kanban_db as kb

        db_path = kb.kanban_db_path().resolve()
        self.assertTrue(str(db_path).startswith(str(HERMETIC_SCRATCH)))
        self.assertNotEqual(db_path, LIVE_FLEET_BOARD_DB)

    def test_refuses_leaked_live_path_without_opening(self):
        prior_db = os.environ.get("HERMES_KANBAN_DB")
        prior_home = os.environ.get("HERMES_KANBAN_HOME")
        try:
            with self.assertRaises(RuntimeError) as ctx:
                _refuse_live_db_path_without_open()
            self.assertIn("refusing live kanban DB path", str(ctx.exception))
        finally:
            if prior_db is not None:
                os.environ["HERMES_KANBAN_DB"] = prior_db
            else:
                os.environ.pop("HERMES_KANBAN_DB", None)
            if prior_home is not None:
                os.environ["HERMES_KANBAN_HOME"] = prior_home
            for name in list(sys.modules):
                if name.startswith("hermes_cli.kanban"):
                    del sys.modules[name]

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


if __name__ == "__main__":
    unittest.main()
