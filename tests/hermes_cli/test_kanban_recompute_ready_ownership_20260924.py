"""RED->GREEN tests for the 2026-09-24 recompute_ready ownership correction
(TurnerBook canary incident: local mirror t_fk_b2bee83f / canonical issue
fk_3635595e-5839-ee47-0b75-a5ce673f9bb0, "Synthetic Grok A2A Fleet proof —
do not execute" was auto-promoted BLOCKED rev1 -> READY rev2 by this node's
own recompute_ready(), which had no ownership check at all).

Exact required matrix (TB-cndr, 2026-09-24 authorization):
  1. foreign Blocked/Todo (owned by a different node) — unchanged, no
     'promoted' event.
  2. manually-blocked local task with no causal parents — unchanged
     (sticky-block guard, pre-existing behavior, must not regress).
  3. local dependency wait in todo, all parents done — still promotes
     (legitimate local dependency progress must not regress).
  4. explicit unblock (promote_task / unblock_task) still works — those
     are operator-driven paths, untouched by this correction, covered
     here only as a non-regression check.
  5. unknown owner (issue_map row with NULL current_node AND NULL
     source_node) — safe (not promoted) AND visible (logged).
  6. a plain, non-Fleet-adapted board — legacy semantics fully preserved
     (recompute_ready behaves exactly as before this correction).

This file is run against UNMODIFIED baseline first (RED — reproduces the
live incident) and then against the candidate in this sibling worktree
(GREEN). See the worktree/hash reporting in the accompanying maker report
for the exact frozen commit this was verified against.
"""
from __future__ import annotations

import sqlite3
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPTS = Path(
    os.environ.get(
        "FLEET_KANBAN_DEPLOY_SCRIPTS",
        str(Path.home() / ".hermes/fleet-kanban-deploy-5f8e0275/scripts"),
    )
)
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(DEPLOY_SCRIPTS))

from hermes_cli import kanban_db as kb  # noqa: E402
# 0.21.5 moved connect() to hermes_cli.kanban_db_connect; kanban_db.connect is
# a plugin-compat pointer in-tree code may not use.
from hermes_cli import kanban_db_connect as kbc  # noqa: E402
import fleet_kanban_sqlite as adapter  # noqa: E402
from fleet_kanban_home import refresh_assignee_homes  # noqa: E402


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _minimal_board_schema(conn: sqlite3.Connection) -> None:
    cols = ", ".join(f"{c} TEXT" for c in adapter.TASK_COLUMNS if c != "id")
    conn.executescript(
        f"""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, {cols},
            claim_lock TEXT, claim_expires INTEGER, worker_pid INTEGER,
            worker_started_at TEXT,
            current_run_id INTEGER, last_heartbeat_at INTEGER,
            current_step_key TEXT, block_kind TEXT
        );
        CREATE TABLE task_comments (
            id INTEGER PRIMARY KEY, task_id TEXT, author TEXT,
            body TEXT, created_at INTEGER
        );
        CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT,
            step_key TEXT, status TEXT, outcome TEXT, summary TEXT,
            error TEXT, metadata TEXT, started_at INTEGER, ended_at INTEGER,
            claim_lock TEXT, claim_expires INTEGER, worker_pid INTEGER,
            max_runtime_seconds INTEGER
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT,
            payload TEXT, run_id INTEGER, created_at INTEGER
        );
        """
    )


def _make_fleet_board(conn: sqlite3.Connection, *, node_id: str = "snowdrop") -> None:
    _minimal_board_schema(conn)
    adapter.install_adapter_schema(
        conn, board_slug="fleet", node_id=node_id, source_profile="snow-cndr",
    )
    refresh_assignee_homes(
        conn, {"snow-cndr": "snowdrop", "tb-cndr": "turnerbook"}, source="test fixture",
    )


def _events(conn, task_id):
    return [
        dict(r) for r in conn.execute(
            "SELECT kind, payload, created_at FROM task_events "
            "WHERE task_id = ? ORDER BY id", (task_id,),
        ).fetchall()
    ]


def test_foreign_blocked_task_no_parents_not_promoted(monkeypatch) -> None:
    """Exact shape of the live incident: a Fleet-mirrored task owned by a
    DIFFERENT node (current_node != this board's installed node_id),
    zero local dependency parents, sitting in 'blocked' with no sticky
    block event at all (matching t_fk_b2bee83f's real event history: no
    'blocked' event ever recorded locally). Before the fix this promotes
    straight to 'ready' — the exact incident. After the fix: completely
    untouched, no 'promoted' event, no outbox mutation attempt.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _make_fleet_board(conn, node_id="snowdrop")

    conn.execute("UPDATE fleet_kanban_apply_context SET origin='remote' WHERE singleton=1")
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, tenant) "
        "VALUES ('t_foreign_blocked', 'Synthetic Grok A2A Fleet proof — "
        "do not execute', 'blocked', 'tb-cndr', 'turnerbook')"
    )
    conn.execute("UPDATE fleet_kanban_apply_context SET origin='local' WHERE singleton=1")
    # Stamp current_node/source_node exactly as the real incident row had
    # them: both 'turnerbook', neither 'snowdrop'.
    conn.execute(
        "UPDATE fleet_kanban_issue_map SET current_node='turnerbook', "
        "source_node='turnerbook' WHERE local_task_id='t_foreign_blocked'"
    )
    conn.commit()

    promoted = kb.recompute_ready(conn)

    assert promoted == 0, "a foreign-owned mirror must never be counted as promoted"
    row = conn.execute(
        "SELECT status FROM tasks WHERE id='t_foreign_blocked'"
    ).fetchone()
    assert row["status"] == "blocked", "foreign row must stay exactly as-is"
    assert _events(conn, "t_foreign_blocked") == [], (
        "no 'promoted' (or any other) event may be written for a "
        "foreign-owned row — zero local activity"
    )


def test_foreign_todo_task_satisfied_parents_not_promoted(monkeypatch) -> None:
    """Same ownership check, 'todo' status branch (not just 'blocked') —
    the trigger condition in recompute_ready is status IN
    ('todo','blocked'), both branches must be covered.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _make_fleet_board(conn, node_id="snowdrop")

    conn.execute("UPDATE fleet_kanban_apply_context SET origin='remote' WHERE singleton=1")
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, tenant) "
        "VALUES ('t_foreign_todo', 'remote todo', 'todo', 'tb-cndr', 'turnerbook')"
    )
    conn.execute("UPDATE fleet_kanban_apply_context SET origin='local' WHERE singleton=1")
    conn.execute(
        "UPDATE fleet_kanban_issue_map SET current_node='max', "
        "source_node='max' WHERE local_task_id='t_foreign_todo'"
    )
    conn.commit()

    promoted = kb.recompute_ready(conn)

    assert promoted == 0
    row = conn.execute("SELECT status FROM tasks WHERE id='t_foreign_todo'").fetchone()
    assert row["status"] == "todo"
    assert _events(conn, "t_foreign_todo") == []


def test_local_manual_blocked_no_parents_stays_blocked(monkeypatch) -> None:
    """Explicit/manual local block with zero dependency parents (an
    explicit human/worker kanban_block with no causal parent at all) —
    must remain blocked until an explicit unblock, exactly as before
    this correction (sticky-block guard, pre-existing behavior).
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _make_fleet_board(conn, node_id="snowdrop")

    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, tenant) "
        "VALUES ('t_local_manual_blocked', 'local manual block', 'blocked', "
        "'snow-cndr', 'snowdrop')"
    )
    # Give it a valid lease + running history so it's genuinely LOCAL by
    # ownership (current_node = this board's own node) — isolating this
    # test to the sticky-block guard specifically, not ownership.
    conn.execute(
        "UPDATE fleet_kanban_issue_map SET current_node='snowdrop', "
        "source_node='snowdrop' WHERE local_task_id='t_local_manual_blocked'"
    )
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES ('t_local_manual_blocked', 'blocked', "
        "'{\"reason\": \"needs_input: manual\"}', ?)",
        (int(time.time()),),
    )
    conn.commit()

    promoted = kb.recompute_ready(conn)

    assert promoted == 0, "sticky local block must not regress"
    row = conn.execute(
        "SELECT status FROM tasks WHERE id='t_local_manual_blocked'"
    ).fetchone()
    assert row["status"] == "blocked"


def test_local_dependency_wait_all_parents_done_promotes(monkeypatch) -> None:
    """Local dependency waits use todo, as block_task does for dependency blocks."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _make_fleet_board(conn, node_id="snowdrop")

    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, tenant) "
        "VALUES ('t_parent_done', 'parent', 'done', 'snow-cndr', 'snowdrop')"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, tenant) "
        "VALUES ('t_local_dep_wait', 'local dependency wait', 'todo', "
        "'snow-cndr', 'snowdrop')"
    )
    conn.execute(
        "UPDATE fleet_kanban_issue_map SET current_node='snowdrop', "
        "source_node='snowdrop' WHERE local_task_id='t_local_dep_wait'"
    )
    conn.execute(
        "INSERT INTO task_links (parent_id, child_id) VALUES "
        "('t_parent_done', 't_local_dep_wait')"
    )
    conn.commit()

    promoted = kb.recompute_ready(conn)

    assert promoted == 1
    row = conn.execute(
        "SELECT status FROM tasks WHERE id='t_local_dep_wait'"
    ).fetchone()
    assert row["status"] == "ready"
    events = _events(conn, "t_local_dep_wait")
    assert any(e["kind"] == "promoted" for e in events)


def test_explicit_unblock_still_works_on_foreign_owned_row(monkeypatch) -> None:
    """Explicit operator paths (promote_task / unblock_task) are NOT part
    of this correction — they carry their own deliberate audit trail and
    must keep working exactly as before, including for a Fleet-mirrored
    row (an operator explicitly overriding is a different authority than
    the automatic recompute_ready path this correction scopes).
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _make_fleet_board(conn, node_id="snowdrop")

    conn.execute("UPDATE fleet_kanban_apply_context SET origin='remote' WHERE singleton=1")
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, tenant) "
        "VALUES ('t_explicit_unblock', 'remote but explicitly promoted', "
        "'todo', 'tb-cndr', 'turnerbook')"
    )
    conn.execute("UPDATE fleet_kanban_apply_context SET origin='local' WHERE singleton=1")
    conn.execute(
        "UPDATE fleet_kanban_issue_map SET current_node='turnerbook', "
        "source_node='turnerbook' WHERE local_task_id='t_explicit_unblock'"
    )
    conn.commit()

    ok, reason = kb.promote_task(conn, "t_explicit_unblock", actor="operator-test")
    assert ok is True, f"explicit promote_task must still work, got reason={reason!r}"

    row = conn.execute(
        "SELECT status FROM tasks WHERE id='t_explicit_unblock'"
    ).fetchone()
    assert row["status"] == "ready", "explicit operator promotion is unaffected by this correction"


def test_unknown_owner_safe_and_visible(caplog) -> None:
    """A Fleet-mirrored row whose issue_map has NEITHER current_node NOR
    source_node populated (corrupt/partial mapping row — should not
    normally happen but is not impossible; note the CURRENT adapter
    schema enforces ``source_node NOT NULL``, so this specific
    combination cannot arise through the adapter's own triggers today —
    this test exercises the defensive branch directly in case that
    schema invariant changes or a future/different-node adapter version
    relaxes it) must be treated as foreign (SAFE: never auto-promoted)
    and must emit a visible WARNING log so an operator can find and fix
    the corrupt row.

    Exercises ``_is_foreign_fleet_mirror`` directly against a minimal
    stub connection (rather than through a real board write, which the
    adapter's own NOT NULL constraint on ``source_node`` currently
    forbids, and rather than monkeypatching a real ``sqlite3.Connection``
    instance, whose C-level attributes are read-only) to prove the
    safe/visible behavior of that defensive branch in isolation.
    """
    import logging

    class _NullOwnerRow:
        def __getitem__(self, key):
            return None

    class _Cursor:
        def fetchone(self):
            return _NullOwnerRow()

    class _StubConn:
        def execute(self, sql, params=()):
            assert "fleet_kanban_issue_map" in sql
            return _Cursor()

    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        result = kb._is_foreign_fleet_mirror(_StubConn(), "t_unknown_owner", "snowdrop")

    assert result is True, "unknown ownership must be treated as foreign (safe default)"
    assert any(
        "UNKNOWN" in rec.message and "t_unknown_owner" in rec.message
        for rec in caplog.records
    ), "an unknown-owner row must log a visible WARNING naming the task"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_current_node_is_unknown_owner_not_local(blank, caplog) -> None:
    """Checker finding 2: a PRESENT but blank current_node is not affirmative
    local authority. The adapter resolves ownership with SQL
    COALESCE(current_node, source_node), which keeps the blank value — so
    falling back to a local source_node would promote a row this node cannot
    attribute to itself. Unknown owner: no promotion, no task/event/outbox
    write, and a visible WARNING naming the task."""
    import logging

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _make_fleet_board(conn, node_id="snowdrop")
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, tenant) "
        "VALUES ('t_blank_owner', 'blank owner', 'todo', 'snow-cndr', 'snowdrop')"
    )
    conn.execute(
        "UPDATE fleet_kanban_issue_map SET current_node=?, source_node='snowdrop' "
        "WHERE local_task_id='t_blank_owner'", (blank,),
    )
    conn.commit()
    outbox_before = conn.execute("SELECT COUNT(*) FROM fleet_kanban_outbox").fetchone()[0]

    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        promoted = kb.recompute_ready(conn)

    assert promoted == 0
    assert conn.execute(
        "SELECT status FROM tasks WHERE id='t_blank_owner'"
    ).fetchone()["status"] == "todo"
    assert _events(conn, "t_blank_owner") == []
    assert conn.execute("SELECT COUNT(*) FROM fleet_kanban_outbox").fetchone()[0] == outbox_before
    assert any(
        "UNKNOWN" in rec.message and "t_blank_owner" in rec.message for rec in caplog.records
    ), "a blank current owner must log a visible WARNING naming the task"


def _replace_insert_trigger_with_reformatted_sql(conn, *, parseable: bool) -> None:
    """Recreate fleet_kanban_task_insert with the SAME behavior but
    different formatting. parseable=True: newlines, lowercase keywords,
    spaces after commas (must still parse). parseable=False: the node id
    is not a literal at all (must fail closed)."""
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND name='fleet_kanban_task_insert'"
    ).fetchone()[0]
    if parseable:
        new_sql = (
            sql.replace("INSERT INTO fleet_kanban_issue_map(", "insert into fleet_kanban_issue_map (")
            .replace(") VALUES(NEW.id,", ")\n  values(\n    NEW.id, ")
            .replace(",NEW.title,NEW.body,", ",\n    NEW.title, NEW.body,\n    ")
        )
    else:
        new_sql = sql.replace(
            ",NEW.title,NEW.body,'snowdrop',", ",NEW.title,NEW.body,lower('SNOWDROP'),"
        )
    assert new_sql != sql, "fixture must actually reformat the trigger"
    conn.execute("DROP TRIGGER fleet_kanban_task_insert")  # in-memory test board only
    conn.execute(new_sql)


def _insert_foreign_blocked(conn, task_id: str) -> None:
    conn.execute("UPDATE fleet_kanban_apply_context SET origin='remote' WHERE singleton=1")
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, tenant) "
        f"VALUES ('{task_id}', 'remote blocked', 'blocked', 'tb-cndr', 'turnerbook')"
    )
    conn.execute("UPDATE fleet_kanban_apply_context SET origin='local' WHERE singleton=1")
    conn.execute(
        "UPDATE fleet_kanban_issue_map SET current_node='turnerbook', "
        f"source_node='turnerbook' WHERE local_task_id='{task_id}'"
    )


def test_reformatted_trigger_still_parses_node() -> None:
    """Reviewer finding b: newline after VALUES(, lowercase keywords and
    spaces between NEW.title, NEW.body must still resolve to 'snowdrop'."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _make_fleet_board(conn, node_id="snowdrop")
    _replace_insert_trigger_with_reformatted_sql(conn, parseable=True)

    assert kb._fleet_adapter_installed_node_id(conn) == "snowdrop"

    _insert_foreign_blocked(conn, "t_foreign_reformatted")
    conn.commit()
    assert kb.recompute_ready(conn) == 0
    assert conn.execute(
        "SELECT status FROM tasks WHERE id='t_foreign_reformatted'"
    ).fetchone()["status"] == "blocked"


def test_unparseable_trigger_fails_closed_and_warns(caplog) -> None:
    """Reviewer finding b: trigger EXISTS but node id can't be read ->
    WARNING + every mapped Fleet row treated as foreign (never 'all local').
    Before the fix this returned None and silently promoted the foreign row."""
    import logging

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _make_fleet_board(conn, node_id="snowdrop")
    _replace_insert_trigger_with_reformatted_sql(conn, parseable=False)
    _insert_foreign_blocked(conn, "t_foreign_unparseable")
    conn.commit()

    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        promoted = kb.recompute_ready(conn)

    assert promoted == 0, "unparseable Fleet trigger must fail closed"
    assert conn.execute(
        "SELECT status FROM tasks WHERE id='t_foreign_unparseable'"
    ).fetchone()["status"] == "blocked"
    assert _events(conn, "t_foreign_unparseable") == []
    assert any("could not be parsed" in r.message for r in caplog.records)


def test_plain_non_fleet_board_legacy_semantics_unchanged(kanban_home) -> None:
    """A plain board with NO Fleet adapter installed at all (the common
    case: kb.init_db() alone, no install_adapter_schema call) must
    behave EXACTLY as before this correction — every task is
    unconditionally local, since there is no ownership concept without
    the adapter. Uses the real kb.init_db()-backed board + real
    kb.create_task/kb.write_txn, not the minimal adapter-only fixture,
    to prove this against the actual non-Fleet code path.
    """
    conn = kbc.connect()
    try:
        parent_id = kb.create_task(conn, title="parent")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent_id,))
        child_id = kb.create_task(conn, title="child")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (child_id,))
        kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)

        promoted = kb.recompute_ready(conn)

        assert promoted == 1, "legacy non-Fleet boards must promote exactly as before"
        row = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (child_id,)
        ).fetchone()
        assert row["status"] == "ready"
    finally:
        conn.close()


def test_created_blocked_stays_blocked_until_explicit_unblock(kanban_home) -> None:
    """Sticky initial block (tb-cndr decision 2026-09-24): a card CREATED as
    blocked stays blocked through any number of recompute_ready passes — the
    CLI calls recompute_ready on `kanban list`/complete/archive/etc., which is
    how live card t_6ba914d7 went blocked -> ready 3 s after creation on
    0.21.0 (task_events 117 created{status:blocked} -> 118 promoted). Only an
    explicit unblock_task may release it. Real create_task path, zero parents.
    """
    conn = kbc.connect()
    try:
        task_id = kb.create_task(conn, title="gated card", initial_status="blocked")
        assert conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,)
        ).fetchone()["status"] == "blocked"

        for _ in range(3):
            assert kb.recompute_ready(conn) == 0, "created-blocked card must not auto-promote"
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert row["status"] == "blocked"
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (task_id,)
        )]
        assert "promoted" not in kinds

        assert kb.unblock_task(conn, task_id) is True
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert row["status"] == "ready", "explicit unblock must still release it"
    finally:
        conn.close()


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)
