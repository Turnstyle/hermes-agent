"""Snowdrop carry: _is_foreign_fleet_mirror resolves owner exactly like TurnerBook.

Owner is SQL COALESCE(current_node, source_node): source_node applies only when
current_node IS NULL. A present-but-blank or whitespace owner is UNKNOWN, so the
task counts as foreign (never decomposed or auto-promoted here) and a warning is
logged. Matches TurnerBook live main and Sheldon, whose helper bodies are identical.
"""
from __future__ import annotations

import logging
import sqlite3

import pytest

from hermes_cli import kanban_db as kb

NODE = "node-a"


def _conn(source, current):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE fleet_kanban_issue_map ("
        "local_task_id TEXT PRIMARY KEY, source_node TEXT, current_node TEXT)"
    )
    conn.execute(
        "INSERT INTO fleet_kanban_issue_map VALUES ('t1', ?, ?)", (source, current)
    )
    return conn


@pytest.mark.parametrize(
    "source,current,foreign,warns",
    [
        (NODE, None, False, False),      # local: created here, never moved
        ("max", NODE, False, False),     # moved here
        ("max", None, True, False),      # homed on another node
        (NODE, "max", True, False),      # moved away
        (NODE, "", True, True),          # blank current_node is the owner -> unknown
        ("", None, True, True),          # blank source, NULL current -> unknown
        (None, None, True, True),        # no owner at all -> unknown
        (NODE, " \t\n", True, True),     # whitespace-only owner -> unknown
    ],
    ids=["local", "moved_here", "foreign", "moved_away", "blank_current",
         "blank_source", "null_owner", "whitespace_current"],
)
def test_owner_resolution_matches_turnerbook(caplog, source, current, foreign, warns):
    conn = _conn(source, current)
    with caplog.at_level(logging.WARNING):
        assert kb._is_foreign_fleet_mirror(conn, "t1", NODE) is foreign
    assert any("UNKNOWN" in r.getMessage() for r in caplog.records) is warns


def test_no_adapter_and_unmapped_rows_stay_local():
    conn = _conn(None, None)
    assert kb._is_foreign_fleet_mirror(conn, "t1", None) is False
    assert kb._is_foreign_fleet_mirror(conn, "not-mapped", NODE) is False
