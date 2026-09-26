"""The dispatcher does not try to claim fleet cards homed on another node.

t_26a13f4c (tb-cndr ruling 2026-09-25 20:50): on TurnerBook, 97 ready cards
were "claim refused" every tick. Most were homed on another node, where the
fleet lease fence refuses the claim every time, so the refusal count carried no
signal. A card this node can never lease is skipped before the claim and
counted in its own bucket; a refusal then means a card that should lease here
did not.

The skip is an optimization, never an authority. The fence still decides.
Anything this node cannot positively place on another node (no fleet adapter,
an unreadable adapter, an unknown owner, or a live lease held by this node)
is still attempted, exactly as before.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_connect as kbc

_FENCE = "verified execution lease required before running"
_NODE = "turnerbook"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda sample=None: "ok")
    kb.init_db()
    return home


def _install_fleet_adapter(
    conn: sqlite3.Connection, node: str = _NODE, *, parseable: bool = True, fence: bool = True,
):
    """Minimal fleet-adapter shape: map row per task, home = tenant, lease fence.

    The insert trigger's text matches the adapter's real form, so the
    dispatcher reads the installed node id from it the same way it does on a
    live board. ``parseable=False`` installs a trigger the reader cannot parse.
    """
    conn.executescript(
        """
        CREATE TABLE fleet_kanban_issue_map (
            local_task_id TEXT PRIMARY KEY, issue_id TEXT, raw_title TEXT,
            canonical_body TEXT, source_node TEXT, current_node TEXT
        );
        CREATE TABLE fleet_kanban_verified_leases (
            issue_id TEXT PRIMARY KEY, holder_profile TEXT NOT NULL,
            holder_node TEXT NOT NULL, generation INTEGER NOT NULL,
            lease_revision INTEGER NOT NULL, expires_at INTEGER NOT NULL,
            verified_at INTEGER NOT NULL
        );
        """
    )
    if parseable:
        values = (
            f"VALUES(NEW.id,'fk_'||NEW.id,NEW.title,NEW.body,'{node}',"
            f"COALESCE(NEW.tenant,'{node}'))"
        )
    else:
        values = (
            f"SELECT NEW.id,'fk_'||NEW.id,NEW.title,NEW.body,'{node}',"
            f"COALESCE(NEW.tenant,'{node}')"
        )
    conn.execute(
        "CREATE TRIGGER fleet_kanban_task_insert AFTER INSERT ON tasks BEGIN "
        "INSERT INTO fleet_kanban_issue_map("
        "local_task_id,issue_id,raw_title,canonical_body,source_node,current_node) "
        f"{values}; END"
    )
    if not fence:
        conn.commit()
        return
    # Verbatim condition of the live adapter's fleet_kanban_running_requires_lease
    # (TurnerBook fleet board, 2026-09-25): the lease must be held by the card's
    # HOME node for the card's assignee.
    conn.executescript(
        f"""
        CREATE TABLE fleet_kanban_apply_context (singleton INTEGER PRIMARY KEY, origin TEXT);
        INSERT INTO fleet_kanban_apply_context VALUES (1, 'local');
        CREATE TRIGGER fleet_kanban_running_requires_lease
        BEFORE UPDATE OF status ON tasks
        WHEN (SELECT origin FROM fleet_kanban_apply_context WHERE singleton=1)='local'
         AND OLD.status IS NOT 'running' AND NEW.status='running'
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1
                  FROM fleet_kanban_issue_map im
                  JOIN fleet_kanban_verified_leases lease ON lease.issue_id=im.issue_id
                 WHERE im.local_task_id=NEW.id
                   AND NEW.assignee IS NOT NULL
                   AND lease.holder_profile=NEW.assignee
                   AND lease.holder_node=COALESCE(im.current_node,im.source_node)
                   AND lease.expires_at>CAST(strftime('%s','now') AS INTEGER)
            ) THEN RAISE(ABORT, '{_FENCE}') END;
        END;
        """
    )
    conn.commit()


def _lease(conn, tid, *, holder_node=_NODE, holder_profile="alice", ttl=300):
    issue = conn.execute(
        "SELECT issue_id FROM fleet_kanban_issue_map WHERE local_task_id = ?", (tid,)
    ).fetchone()[0]
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO fleet_kanban_verified_leases VALUES (?,?,?,?,?,?,?)",
        (issue, holder_profile, holder_node, 1, 1, now + ttl, now),
    )
    conn.commit()


def _set_home(conn, tid, node):
    conn.execute(
        "UPDATE fleet_kanban_issue_map SET current_node = ? WHERE local_task_id = ?",
        (node, tid),
    )
    conn.commit()


def _spawner(spawns: list):
    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242
    return fake_spawn


def _refused(res):
    return {tid for tid, _err in res.claim_errors}


def test_foreign_home_card_is_skipped_not_refused(kanban_home, all_assignees_spawnable):
    """RED on base: the claim is attempted and refused every tick."""
    spawns: list = []
    with kbc.connect() as conn:
        _install_fleet_adapter(conn)
        foreign = kb.create_task(conn, title="homed on sheldon", assignee="alice", tenant="sheldon")
        res = kbd.dispatch_once(conn, spawn_fn=_spawner(spawns))
        status = conn.execute("SELECT status FROM tasks WHERE id = ?", (foreign,)).fetchone()[0]

    assert foreign not in _refused(res)
    assert (foreign, "sheldon") in res.skipped_foreign_home
    assert not spawns
    assert status == "ready"


def test_local_home_card_without_lease_is_still_refused(kanban_home, all_assignees_spawnable):
    """The refusal stays for a card that should lease here: that is the signal."""
    with kbc.connect() as conn:
        _install_fleet_adapter(conn)
        local = kb.create_task(conn, title="homed here, no lease", assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=_spawner([]))
    assert local in _refused(res)
    assert not res.skipped_foreign_home


def test_local_home_card_with_lease_spawns(kanban_home, all_assignees_spawnable):
    spawns: list = []
    with kbc.connect() as conn:
        _install_fleet_adapter(conn)
        local = kb.create_task(conn, title="homed here, leased", assignee="alice")
        _lease(conn, local)
        res = kbd.dispatch_once(conn, spawn_fn=_spawner(spawns))
    assert spawns == [local]
    assert not res.claim_errors


def test_mixed_board_counts_only_real_refusals(kanban_home, all_assignees_spawnable):
    """TurnerBook shape: many foreign cards, a few local ones."""
    spawns: list = []
    with kbc.connect() as conn:
        _install_fleet_adapter(conn)
        foreign = [
            kb.create_task(conn, title=f"f{i}", assignee="alice", tenant=node)
            for i, node in enumerate(["sheldon"] * 5 + ["max", "snowdrop"])
        ]
        stuck = kb.create_task(conn, title="local, no lease", assignee="alice")
        leased = kb.create_task(conn, title="local, leased", assignee="alice")
        _lease(conn, leased)
        res = kbd.dispatch_once(conn, spawn_fn=_spawner(spawns))
    assert _refused(res) == {stuck}
    assert {tid for tid, _node in res.skipped_foreign_home} == set(foreign)
    assert spawns == [leased]


def test_foreign_home_card_the_fence_would_accept_is_attempted(kanban_home, all_assignees_spawnable):
    """A live lease held by the HOME node for this assignee passes the fence,
    so the skip must not hide it."""
    spawns: list = []
    with kbc.connect() as conn:
        _install_fleet_adapter(conn)
        tid = kb.create_task(conn, title="homed away, home-node lease", assignee="alice", tenant="sheldon")
        _lease(conn, tid, holder_node="sheldon")
        res = kbd.dispatch_once(conn, spawn_fn=_spawner(spawns))
    assert spawns == [tid]
    assert not res.skipped_foreign_home
    assert not res.claim_errors


def test_leases_the_fence_would_refuse_do_not_unskip(kanban_home, all_assignees_spawnable):
    """Lease held by this node, by another profile, or expired: still refused, so skipped."""
    with kbc.connect() as conn:
        _install_fleet_adapter(conn)
        by_this_node = kb.create_task(conn, title="leased here", assignee="alice", tenant="sheldon")
        _lease(conn, by_this_node, holder_node=_NODE)
        other_profile = kb.create_task(conn, title="other profile", assignee="alice", tenant="sheldon")
        _lease(conn, other_profile, holder_node="sheldon", holder_profile="bob")
        expired = kb.create_task(conn, title="expired", assignee="alice", tenant="sheldon")
        _lease(conn, expired, holder_node="sheldon", ttl=-5)
        res = kbd.dispatch_once(conn, spawn_fn=_spawner([]))
    skipped = {tid for tid, _node in res.skipped_foreign_home}
    assert skipped == {by_this_node, other_profile, expired}
    assert not res.claim_errors


def test_board_with_adapter_but_no_lease_fence_is_unchanged(kanban_home, all_assignees_spawnable):
    """No fence installed: nothing would refuse the claim, so nothing is skipped."""
    spawns: list = []
    with kbc.connect() as conn:
        _install_fleet_adapter(conn, fence=False)
        tid = kb.create_task(conn, title="homed away, no fence", assignee="alice", tenant="sheldon")
        res = kbd.dispatch_once(conn, spawn_fn=_spawner(spawns))
    assert spawns == [tid]
    assert not res.skipped_foreign_home


def test_unknown_owner_is_attempted_not_skipped(kanban_home, all_assignees_spawnable):
    """Blank/NULL home: this node cannot place it elsewhere, so the fence decides."""
    with kbc.connect() as conn:
        _install_fleet_adapter(conn)
        blank = kb.create_task(conn, title="blank home", assignee="alice")
        _set_home(conn, blank, "   ")
        res = kbd.dispatch_once(conn, spawn_fn=_spawner([]))
    assert blank in _refused(res)
    assert not res.skipped_foreign_home


def test_unparseable_adapter_disables_the_skip(kanban_home, all_assignees_spawnable):
    with kbc.connect() as conn:
        _install_fleet_adapter(conn, parseable=False)
        tid = kb.create_task(conn, title="homed away", assignee="alice", tenant="sheldon")
        res = kbd.dispatch_once(conn, spawn_fn=_spawner([]))
    assert tid in _refused(res)
    assert not res.skipped_foreign_home


def test_board_without_fleet_adapter_is_unchanged(kanban_home, all_assignees_spawnable):
    spawns: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="plain board", assignee="alice", tenant="sheldon")
        res = kbd.dispatch_once(conn, spawn_fn=_spawner(spawns))
    assert spawns == [tid]
    assert not res.skipped_foreign_home


def test_unassigned_foreign_card_gets_no_default_assignee_write(
    kanban_home, all_assignees_spawnable,
):
    """The skip comes before kanban.default_assignee writes to a foreign card."""
    with kbc.connect() as conn:
        _install_fleet_adapter(conn)
        tid = kb.create_task(conn, title="unassigned, homed away", tenant="sheldon")
        res = kbd.dispatch_once(conn, spawn_fn=_spawner([]), default_assignee="alice")
        assignee = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    assert assignee is None
    assert tid not in res.auto_assigned_default
    assert (tid, "sheldon") in res.skipped_foreign_home


def test_review_lane_skips_foreign_home_too(kanban_home, all_assignees_spawnable, monkeypatch):
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: True)
    with kbc.connect() as conn:
        _install_fleet_adapter(conn)
        tid = kb.create_task(conn, title="review homed away", assignee="alice", tenant="sheldon")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        conn.commit()
        res = kbd.dispatch_once(conn, spawn_fn=_spawner([]))
    assert tid not in _refused(res)
    assert (tid, "sheldon") in res.skipped_foreign_home


def test_dry_run_reports_the_skip(kanban_home, all_assignees_spawnable):
    with kbc.connect() as conn:
        _install_fleet_adapter(conn)
        tid = kb.create_task(conn, title="homed away", assignee="alice", tenant="max")
        res = kbd.dispatch_once(conn, spawn_fn=_spawner([]), dry_run=True)
    assert (tid, "max") in res.skipped_foreign_home
    assert not res.spawned
