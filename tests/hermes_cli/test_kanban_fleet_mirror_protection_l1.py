"""L1: _is_protected_fleet_mirror fails closed on mapped fleet rows (hermetic)."""

from __future__ import annotations

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

HERMETIC_SCRATCH.mkdir(parents=True, exist_ok=True)
_ISOLATED_HOME = Path(
    tempfile.mkdtemp(prefix=f"kanban-l1-{uuid.uuid4().hex[:8]}-", dir=str(HERMETIC_SCRATCH))
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


_FLEET_MAP_DDL = """CREATE TABLE IF NOT EXISTS fleet_kanban_issue_map (
    local_task_id TEXT PRIMARY KEY,
    issue_id TEXT,
    source_node TEXT,
    current_node TEXT,
    deferred_snapshot INTEGER,
    canonical_status TEXT
)"""


class FleetMirrorProtectionL1Tests(unittest.TestCase):
    def setUp(self):
        self._test_home = Path(
            tempfile.mkdtemp(prefix="l1-case-", dir=str(HERMETIC_SCRATCH))
        ).resolve()
        os.environ["HERMES_HOME"] = str(self._test_home)
        os.environ["HERMES_KANBAN_HOME"] = str(self._test_home)
        os.environ["HERMES_KANBAN_DB"] = str(self._test_home / "kanban.db")
        os.environ.pop("HERMES_KANBAN_BOARD", None)
        from hermes_cli import kanban_db as kb

        kb.init_db()

    def _insert_map_row(
        self,
        conn,
        local_task_id: str,
        *,
        source_node: str = "",
        current_node: str = "",
    ) -> None:
        conn.execute(_FLEET_MAP_DDL)
        conn.execute(
            """INSERT INTO fleet_kanban_issue_map(
                local_task_id, issue_id, source_node, current_node,
                deferred_snapshot, canonical_status
            ) VALUES(?,?,?,?,?,?)""",
            (local_task_id, "fk_issue", source_node, current_node, 0, "triage"),
        )
        conn.commit()

    def test_mapped_other_node_actor_unset_excluded(self):
        from hermes_cli import kanban_decompose
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        env = os.environ.copy()
        env.pop("FLEET_KANBAN_ACTOR_NODE", None)
        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title="foreign", assignee="orch", triage=True)
            self._insert_map_row(conn, tid, source_node="turnerbook", current_node="max")
        with mock.patch.dict(os.environ, env, clear=True):
            os.environ.update(
                {
                    "HERMES_HOME": str(self._test_home),
                    "HERMES_KANBAN_HOME": str(self._test_home),
                    "HERMES_KANBAN_DB": str(self._test_home / "kanban.db"),
                }
            )
            ids = kanban_decompose.list_triage_ids()
        self.assertNotIn(tid, ids)

    def test_mapped_other_node_actor_whitespace_excluded(self):
        from hermes_cli import kanban_decompose
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title="foreign ws", assignee="orch", triage=True)
            self._insert_map_row(conn, tid, source_node="turnerbook", current_node="max")
        with mock.patch.dict(os.environ, {"FLEET_KANBAN_ACTOR_NODE": "  \t  "}):
            ids = kanban_decompose.list_triage_ids()
        self.assertNotIn(tid, ids)

    def test_mapped_owned_by_actor_included(self):
        from hermes_cli import kanban_decompose
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title="mine", assignee="orch", triage=True)
            self._insert_map_row(conn, tid, source_node="turnerbook", current_node="turnerbook")
        with mock.patch.dict(os.environ, {"FLEET_KANBAN_ACTOR_NODE": "turnerbook"}):
            ids = kanban_decompose.list_triage_ids()
        self.assertIn(tid, ids)

    def test_unmapped_local_actor_unset_included(self):
        from hermes_cli import kanban_decompose
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        env = os.environ.copy()
        env.pop("FLEET_KANBAN_ACTOR_NODE", None)
        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title="local only", assignee="orch", triage=True)
            conn.execute(_FLEET_MAP_DDL)
            conn.commit()
        with mock.patch.dict(os.environ, env, clear=True):
            os.environ.update(
                {
                    "HERMES_HOME": str(self._test_home),
                    "HERMES_KANBAN_HOME": str(self._test_home),
                    "HERMES_KANBAN_DB": str(self._test_home / "kanban.db"),
                }
            )
            ids = kanban_decompose.list_triage_ids()
        self.assertIn(tid, ids)

    def test_mapped_lookup_db_error_excluded_with_warning(self):
        from hermes_cli import kanban_decompose
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title="db err", assignee="orch", triage=True)
            self._insert_map_row(conn, tid, source_node="turnerbook", current_node="turnerbook")
            original_execute = conn.execute

            def _boom(sql, params=()):
                if "fleet_kanban_issue_map" in str(sql) and "local_task_id" in str(sql):
                    raise RuntimeError("simulated db failure")
                return original_execute(sql, params)

            conn.execute = _boom  # type: ignore[method-assign]
            with self.assertLogs("hermes_cli.kanban_decompose", level="WARNING") as logs:
                protected = kanban_decompose._is_protected_fleet_mirror(conn, tid)
            self.assertTrue(protected)
            self.assertTrue(
                any("lookup failed" in m for m in logs.output),
                logs.output,
            )

    def test_no_fleet_map_table_included(self):
        from hermes_cli import kanban_decompose
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title="no fleet sync", assignee="orch", triage=True)
            conn.execute("DROP TABLE IF EXISTS fleet_kanban_issue_map")
            conn.commit()
        ids = kanban_decompose.list_triage_ids()
        self.assertIn(tid, ids)

    def test_mapped_empty_owner_nodes_excluded(self):
        from hermes_cli import kanban_decompose
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title="empty owner", assignee="orch", triage=True)
            self._insert_map_row(conn, tid, source_node="", current_node="")
        with mock.patch.dict(os.environ, {"FLEET_KANBAN_ACTOR_NODE": "turnerbook"}):
            ids = kanban_decompose.list_triage_ids()
        self.assertNotIn(tid, ids)


if __name__ == "__main__":
    unittest.main()
