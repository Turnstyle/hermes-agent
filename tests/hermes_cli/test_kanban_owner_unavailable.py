"""A ready card whose assignee profile is missing must leave ONE durable,
deduped ``owner_unavailable`` event instead of sitting silently in ``ready``;
configured control-plane lanes and profiles another home owns stay quiet; and
the explicit recovery (``reassign_task(reason=...)``) keeps its reason on the
``assigned`` event.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

TICKS = 6


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def only_worker_exists(monkeypatch):
    """Exactly one runnable profile (``worker``); every other name is missing."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "worker")


def _events(conn, tid, kind=None):
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id", (tid,),
    ).fetchall()
    out = [(r["kind"], json.loads(r["payload"]) if r["payload"] else None) for r in rows]
    return [e for e in out if kind is None or e[0] == kind]


def _tick(conn):
    return kbd.dispatch_once(conn, dry_run=False, spawn_fn=lambda _t, _w: None)


def test_missing_owner_records_one_deduped_event(kanban_home, only_worker_exists):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="owner down", assignee="owner-down")
        results = [_tick(conn) for _ in range(TICKS)]
        ev = _events(conn, tid, "owner_unavailable")
        status = kb.get_task(conn, tid).status
    assert all(tid in r.skipped_nonspawnable for r in results)
    assert len(ev) == 1, f"expected one owner_unavailable event after {TICKS} ticks, got {ev}"
    assert ev[0][1]["assignee"] == "owner-down"
    assert ev[0][1]["reason"] == "profile_not_found"
    assert status == "ready"  # no automatic reassignment / blocking
    assert results[0].owner_unavailable == [(tid, "owner-down")]
    assert all(r.owner_unavailable == [(tid, "owner-down")] for r in results)


def test_dry_run_does_not_write(kanban_home, only_worker_exists):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="owner down", assignee="owner-down")
        res = kbd.dispatch_once(conn, dry_run=True)
        ev = _events(conn, tid, "owner_unavailable")
    assert res.owner_unavailable == [(tid, "owner-down")]
    assert ev == []


def test_control_plane_lane_stays_quiet(kanban_home, only_worker_exists):
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  control_plane_lanes:\n    - orion-cc\n    - 'snow-*'\n", encoding="utf-8",
    )
    with kbc.connect() as conn:
        lane = kb.create_task(conn, title="lane", assignee="orion-cc")
        pat = kb.create_task(conn, title="pattern lane", assignee="snow-cndr")
        missing = kb.create_task(conn, title="missing", assignee="owner-down")
        results = [_tick(conn) for _ in range(TICKS)]
        lane_ev = _events(conn, lane, "owner_unavailable")
        pat_ev = _events(conn, pat, "owner_unavailable")
        miss_ev = _events(conn, missing, "owner_unavailable")
    assert all({lane, pat, missing} <= set(r.skipped_nonspawnable) for r in results)
    assert lane_ev == [] and pat_ev == []
    assert len(miss_ev) == 1
    assert all(t != lane and t != pat for r in results for (t, _a) in r.owner_unavailable)


def test_foreign_profile_outside_dispatch_profiles_stays_quiet(kanban_home, only_worker_exists):
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  dispatch_profiles:\n    - worker\n", encoding="utf-8",
    )
    with kbc.connect() as conn:
        foreign = kb.create_task(conn, title="other home", assignee="default")
        for _ in range(TICKS):
            _tick(conn)
        assert _events(conn, foreign, "owner_unavailable") == []


def test_reassign_keeps_reason_and_new_episode_dedupes_per_assignee(
    kanban_home, only_worker_exists,
):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="owner down", assignee="owner-down")
        _tick(conn)
        _tick(conn)
        why = "owner-down unavailable; single recovery claimant"
        assert kb.reassign_task(conn, tid, "also-missing", reason=why) is True
        assigned = _events(conn, tid, "assigned")
        _tick(conn)
        _tick(conn)
        ev = _events(conn, tid, "owner_unavailable")
    assert assigned[-1][1] == {"assignee": "also-missing", "from": "owner-down", "reason": why}
    # One event per unavailability episode: one for each missing assignee.
    assert [e[1]["assignee"] for e in ev] == ["owner-down", "also-missing"]


def test_reassign_to_runnable_profile_dispatches(kanban_home, only_worker_exists):
    spawned = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="owner down", assignee="owner-down")
        _tick(conn)
        assert kb.reassign_task(conn, tid, "worker", reason="recovery") is True
        res = kbd.dispatch_once(
            conn, dry_run=False, spawn_fn=lambda t, _w: spawned.append(t.id) or 4242,
        )
        ev = _events(conn, tid, "owner_unavailable")
    assert spawned == [tid]
    assert res.owner_unavailable == []
    assert len(ev) == 1


def test_assign_without_reason_payload_unchanged(kanban_home, only_worker_exists):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
        assert kb.reassign_task(conn, tid, "owner-down") is True
        assigned = _events(conn, tid, "assigned")
    assert assigned[-1][1] == {"assignee": "owner-down", "from": "worker"}


def test_claim_starts_new_episode(kanban_home, only_worker_exists):
    """A ``claimed`` event after the first flag ends that episode, so a later
    skip for the same missing assignee is recorded again (once)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="owner down", assignee="owner-down")
        _tick(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "claimed", {"lock": "test"})
        _tick(conn)
        _tick(conn)
        ev = _events(conn, tid, "owner_unavailable")
    assert [e[1]["assignee"] for e in ev] == ["owner-down", "owner-down"]
    assert ev[0][1]["node"]


def test_stale_row_is_not_flagged(kanban_home, only_worker_exists):
    """The tick reads rows once; if the card was reassigned since, the write
    re-check refuses to flag the old assignee."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="owner down", assignee="owner-down")
        assert kb.reassign_task(conn, tid, "worker", reason="moved") is True
        assert kbd._record_owner_unavailable(conn, tid, "owner-down") is False
        assert _events(conn, tid, "owner_unavailable") == []


def test_refused_write_does_not_abort_tick(kanban_home, only_worker_exists, monkeypatch):
    """A board fence refusing the owner_unavailable write is logged and
    skipped; the same tick still dispatches the runnable card."""
    import sqlite3

    real_append = kb._append_event

    def _append(conn, task_id, kind, payload=None, **kw):
        if kind == "owner_unavailable":
            raise sqlite3.IntegrityError("verified execution lease required before running")
        return real_append(conn, task_id, kind, payload, **kw)

    monkeypatch.setattr(kbd._kb, "_append_event", _append)
    spawned = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="owner down", assignee="owner-down")
        ok = kb.create_task(conn, title="runnable", assignee="worker")
        res = kbd.dispatch_once(
            conn, dry_run=False, spawn_fn=lambda t, _w: spawned.append(t.id) or 4242,
        )
        ev = _events(conn, tid, "owner_unavailable")
    assert res.owner_unavailable == [(tid, "owner-down")]
    assert spawned == [ok]
    assert ev == []


def test_reassign_with_reclaim_first_keeps_reason(kanban_home, only_worker_exists):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="owner-down")
        assert kb.reassign_task(conn, tid, "worker", reclaim_first=True, reason="recover") is True
        assigned = _events(conn, tid, "assigned")
    assert assigned[-1][1] == {"assignee": "worker", "from": "owner-down", "reason": "recover"}


@pytest.mark.parametrize("as_json", [False, True])
def test_dispatch_reports_claim_and_reclaim_refusals(kanban_home, monkeypatch, capsys, caplog, as_json):
    import argparse
    from hermes_cli import kanban_ops
    from gateway.kanban_watchers_dispatcher import _log_spawn_results

    kbd._fence_logged.clear()
    result = kbd.DispatchResult(
        claim_errors=[("t_claim", "claim: fixture fence")],
        reclaim_errors=[("t_reclaim", "reclaim: fixture fence")],
    )
    monkeypatch.setattr(kbd, "dispatch_once", lambda *args, **kwargs: result)
    args = argparse.Namespace(dry_run=True, max=0, failure_limit=2, json=as_json)
    assert kanban_ops._cmd_dispatch(args) == 0
    output = capsys.readouterr().out
    if as_json:
        result_json = json.loads(output)
        assert result_json["claim_errors"] == [{"task_id": "t_claim", "error": "claim: fixture fence"}]
        assert result_json["reclaim_errors"] == [{"task_id": "t_reclaim", "error": "reclaim: fixture fence"}]
    else:
        assert "Claim refused (t_claim): claim: fixture fence" in output
        assert "Reclaim refused (t_reclaim): reclaim: fixture fence" in output
    assert _log_spawn_results([("fixture", result)]) is False
    assert "t_claim: claim: fixture fence" in caplog.text
    assert "t_reclaim: reclaim: fixture fence" in caplog.text


_MAP_DDL = """CREATE TABLE IF NOT EXISTS fleet_kanban_issue_map (
    local_task_id TEXT PRIMARY KEY,
    issue_id TEXT,
    raw_title TEXT,
    canonical_body TEXT,
    source_node TEXT,
    current_node TEXT,
    source_profile TEXT
)"""

_TRIGGER_DDL_SNOWDROP = """CREATE TRIGGER fleet_kanban_task_insert
    AFTER INSERT ON tasks
    BEGIN
        INSERT INTO fleet_kanban_issue_map(
            local_task_id,issue_id,raw_title,canonical_body,source_node,current_node,source_profile
        ) VALUES(NEW.id,'fk_' || lower(hex(randomblob(4))),NEW.title,NEW.body,'snowdrop',NULL,'p');
    END"""


def test_fleet_foreign_assignee_homes_stays_quiet(kanban_home, only_worker_exists):
    with kbc.connect() as conn:
        conn.execute(_MAP_DDL)
        conn.execute(_TRIGGER_DDL_SNOWDROP)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS fleet_kanban_assignee_homes ("
            "assignee TEXT PRIMARY KEY, home_node TEXT)"
        )
        conn.execute(
            "INSERT INTO fleet_kanban_assignee_homes (assignee, home_node) VALUES (?, ?)",
            ("max-lead", "max"),
        )
        conn.commit()

        tid = kb.create_task(conn, title="foreign card", assignee="max-lead")
        for _ in range(TICKS):
            res = _tick(conn)
            assert tid not in [t for t, _ in res.owner_unavailable]

        ev = _events(conn, tid, "owner_unavailable")
        assert ev == []
        assert kbd._nonspawnable_kind("max-lead", conn) == "foreign"


def test_fleet_foreign_issue_map_stays_quiet(kanban_home, only_worker_exists):
    with kbc.connect() as conn:
        conn.execute(_MAP_DDL)
        conn.execute(_TRIGGER_DDL_SNOWDROP)
        conn.commit()

        tid = kb.create_task(conn, title="foreign mapped card", assignee="unregistered-bot")
        conn.execute(
            "UPDATE fleet_kanban_issue_map SET source_node = 'turnerbook', current_node = NULL "
            "WHERE local_task_id = ?",
            (tid,),
        )
        conn.commit()

        for _ in range(TICKS):
            res = _tick(conn)
            assert tid not in [t for t, _ in res.owner_unavailable]

        ev = _events(conn, tid, "owner_unavailable")
        assert ev == []
        assert kbd._nonspawnable_kind("unregistered-bot", conn, task_id=tid) == "foreign"


def test_fleet_local_missing_profile_records_event_with_create_help(kanban_home, only_worker_exists):
    with kbc.connect() as conn:
        conn.execute(_MAP_DDL)
        conn.execute(_TRIGGER_DDL_SNOWDROP)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS fleet_kanban_assignee_homes ("
            "assignee TEXT PRIMARY KEY, home_node TEXT)"
        )
        conn.execute(
            "INSERT INTO fleet_kanban_assignee_homes (assignee, home_node) VALUES (?, ?)",
            ("snow-writer", "snowdrop"),
        )
        conn.commit()

        tid = kb.create_task(conn, title="local card missing profile", assignee="snow-writer")
        res = _tick(conn)
        assert res.owner_unavailable == [(tid, "snow-writer")]

        ev = _events(conn, tid, "owner_unavailable")
        assert len(ev) == 1
        payload = ev[0][1]
        assert payload["assignee"] == "snow-writer"
        assert payload["reason"] == "profile_not_found"
        assert "hermes profile create snow-writer" in payload["detail"]
        assert "reassign_task" in payload["detail"]
        assert kbd._nonspawnable_kind("snow-writer", conn) == "missing"
