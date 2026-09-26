"""Health telemetry counts only cards this Fleet node owns (t_46ff0060).

``has_spawnable_ready`` / ``has_spawnable_review`` feed the gateway's
"kanban dispatcher stuck" warning. On a Fleet board every node mirrors every
other node's cards; the lease fence refuses foreign ones by design, so a node
holding only foreign ready cards must read as idle, not stuck. Ownership uses
the same rule as ``kanban_db._is_foreign_fleet_mirror``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

NODE = "snowdrop"

_MAP_DDL = """CREATE TABLE IF NOT EXISTS fleet_kanban_issue_map (
    local_task_id TEXT PRIMARY KEY,
    issue_id TEXT,
    raw_title TEXT,
    canonical_body TEXT,
    source_node TEXT,
    current_node TEXT,
    source_profile TEXT
)"""

# Shape of the live adapter trigger: the node id is the 5th VALUES literal.
_TRIGGER_DDL = f"""CREATE TRIGGER fleet_kanban_task_insert
    AFTER INSERT ON tasks
    BEGIN
        INSERT INTO fleet_kanban_issue_map(
            local_task_id,issue_id,raw_title,canonical_body,source_node,current_node,source_profile
        ) VALUES(NEW.id,'fk_' || lower(hex(randomblob(4))),NEW.title,NEW.body,'{NODE}',NULL,'p');
    END"""


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def local_profiles(monkeypatch):
    """Only ``here`` is a profile on this node."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "here")


def _install_adapter(conn):
    conn.execute(_MAP_DDL)
    conn.execute(_TRIGGER_DDL)
    conn.commit()


def _set_owner(conn, tid, *, source, current):
    conn.execute(
        "UPDATE fleet_kanban_issue_map SET source_node = ?, current_node = ? "
        "WHERE local_task_id = ?", (source, current, tid),
    )
    conn.commit()


def _only(conn, status, source, current, assignee="here"):
    tid = kb.create_task(conn, title="t", assignee=assignee)
    if status == "review":
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        conn.commit()
    _set_owner(conn, tid, source=source, current=current)
    return tid


def _probe(conn, status):
    return kbd.has_spawnable_ready(conn) if status == "ready" else kbd.has_spawnable_review(conn)


@pytest.mark.parametrize("status", ["ready", "review"])
@pytest.mark.parametrize(
    "source,current,expected",
    [
        (NODE, None, True),          # local: created here, never moved
        ("max", NODE, True),         # moved here
        ("max", None, False),        # foreign: homed on another node
        (NODE, "max", False),        # moved away
        (NODE, "", False),           # blank current_node is the owner -> unknown -> foreign
        ("", None, False),           # blank source, NULL current -> unknown -> foreign
        (None, None, False),         # no owner at all -> foreign
        (NODE, " \t\n", False),      # whitespace-only owner -> foreign
    ],
    ids=["local", "moved_here", "foreign", "moved_away", "blank_current",
         "blank_source", "null_owner", "whitespace_current"],
)
def test_ownership_gates_spawnable(kanban_home, local_profiles, status, source, current, expected):
    with kbc.connect() as conn:
        _install_adapter(conn)
        _only(conn, status, source, current)
        assert _probe(conn, status) is expected


@pytest.mark.parametrize("status", ["ready", "review"])
def test_unmapped_row_is_local(kanban_home, local_profiles, status):
    """No map row = the task predates the adapter = local."""
    with kbc.connect() as conn:
        _install_adapter(conn)
        tid = _only(conn, status, NODE, None)
        conn.execute("DELETE FROM fleet_kanban_issue_map WHERE local_task_id = ?", (tid,))
        conn.commit()
        assert _probe(conn, status) is True


@pytest.mark.parametrize("status", ["ready", "review"])
def test_owned_card_without_local_profile_not_spawnable(kanban_home, local_profiles, status):
    with kbc.connect() as conn:
        _install_adapter(conn)
        _only(conn, status, NODE, None, assignee="elsewhere")
        assert _probe(conn, status) is False


def test_foreign_cards_do_not_mask_owned_one(kanban_home, local_profiles):
    """Many foreign cards for a local profile plus one owned card -> spawnable."""
    with kbc.connect() as conn:
        _install_adapter(conn)
        for _ in range(5):
            _only(conn, "ready", "max", None)
        assert kbd.has_spawnable_ready(conn) is False
        _only(conn, "ready", NODE, None)
        assert kbd.has_spawnable_ready(conn) is True


def test_no_adapter_board_keeps_legacy_semantics(kanban_home, local_profiles):
    with kbc.connect() as conn:
        kb.create_task(conn, title="plain", assignee="here")
        assert kbd.has_spawnable_ready(conn) is True


def test_unparseable_trigger_fails_closed(kanban_home, local_profiles):
    """Trigger present but unreadable -> every mapped row is foreign (quiet)."""
    with kbc.connect() as conn:
        conn.execute(_MAP_DDL)
        conn.execute(
            "CREATE TRIGGER fleet_kanban_task_insert AFTER INSERT ON tasks BEGIN "
            "INSERT INTO fleet_kanban_issue_map(local_task_id, source_node) "
            f"VALUES(NEW.id, '{NODE}'); END"
        )
        conn.commit()
        kb.create_task(conn, title="t", assignee="here")
        assert kbd.has_spawnable_ready(conn) is False


def test_gateway_ready_nonempty_quiet_on_foreign_only(kanban_home, local_profiles, monkeypatch):
    """Through the gateway probe that drives the "dispatcher stuck" warning:
    foreign-only cards -> no stuck signal; one owned spawnable card -> signal."""
    from gateway import kanban_watchers_dispatcher as kwd

    monkeypatch.setattr(kwd._KanbanDispatcher, "_board_slugs", lambda self: ["default"])
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: True)
    disp = kwd._KanbanDispatcher(kb, settings=None)
    with kbc.connect() as conn:
        _install_adapter(conn)
        _only(conn, "ready", "max", None)
        _only(conn, "review", "max", None)
    assert disp.ready_nonempty() is False
    with kbc.connect() as conn:
        _only(conn, "ready", NODE, None)
    assert disp.ready_nonempty() is True
