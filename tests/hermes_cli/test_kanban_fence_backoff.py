"""Lease refusals stay visible without hammering claims or paging during sync."""
from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

_FENCE = "verified execution lease required before running"


@pytest.fixture(autouse=True)
def clear_fences():
    getattr(kbd, "_fence_logged", {}).clear()
    getattr(kbd, "_claim_fence", {}).clear()
    yield
    getattr(kbd, "_fence_logged", {}).clear()
    getattr(kbd, "_claim_fence", {}).clear()


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda name: True)
    kb.init_db()
    with kbc.connect() as conn:
        conn.executescript("""
            CREATE TABLE fleet_kanban_issue_map (
                local_task_id TEXT PRIMARY KEY, issue_id TEXT, raw_title TEXT,
                canonical_body TEXT, source_node TEXT, current_node TEXT,
                source_profile TEXT, canonical_status TEXT, sync_state TEXT
            );
            CREATE TRIGGER fleet_kanban_task_insert AFTER INSERT ON tasks
            BEGIN
                INSERT INTO fleet_kanban_issue_map
                    (local_task_id, issue_id, raw_title, canonical_body,
                     source_node, current_node, source_profile)
                VALUES (NEW.id, 'fk_test', NEW.title, NEW.body, 'sheldon', NULL, 'worker');
            END;
            CREATE TRIGGER test_lease_fence BEFORE UPDATE OF status ON tasks
            WHEN NEW.status = 'running'
            BEGIN
                SELECT RAISE(ABORT, 'verified execution lease required before running');
            END;
        """)
        yield conn


def _tick(conn, task_id):
    row = conn.execute("SELECT id, assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
    result = kbd.DispatchResult()
    kbd._dispatch_lane_task(
        conn, row, row["assignee"], result, lane="ready", dry_run=False,
        ttl_seconds=None, board=None, failure_limit=2,
        spawn_fn=lambda *a, **k: 12345,
        per_profile_cap=None, per_profile_running={},
    )
    return result


def _claim_spy(monkeypatch):
    calls = []
    claim = kb.claim_task

    def spy(*args, **kwargs):
        calls.append(args[1])
        return claim(*args, **kwargs)

    monkeypatch.setattr(kb, "claim_task", spy)
    return calls


def test_fence_log_once_per_hour():
    assert kbd._fence_log_due("claim", "t", now=0)
    assert not kbd._fence_log_due("claim", "t", now=3599)
    assert kbd._fence_log_due("claim", "t", now=3600)
    assert kbd._fence_log_due("ownership", "t", now=3600)
    assert kbd._fence_log_due("claim", "other", now=3600)


def test_node_lease_wait_retries_then_reports_late_stall(fleet, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(kbd.time, "time", lambda: now[0])
    calls = _claim_spy(monkeypatch)
    tid = kb.create_task(fleet, title="lease pending", assignee="worker", tenant="sheldon")
    first = _tick(fleet, tid)
    assert first.claim_errors and first.held_claim_errors
    assert first.eligible_claim_errors == []
    assert not kbd.eligible_work_stalled([first], 1)
    since = kbd._claim_fence[tid]["since"]
    now[0] += 60
    assert _tick(fleet, tid).held_claim_errors
    assert len(calls) == 2
    assert kbd._claim_fence[tid]["since"] == since

    kbd._claim_fence[tid]["since"] = now[0] - 700
    skipped_late = _tick(fleet, tid)
    assert skipped_late.eligible_claim_errors and skipped_late.claim_errors == []
    assert len(calls) == 2
    now[0] += 300
    late = _tick(fleet, tid)
    assert late.eligible_claim_errors and late.held_claim_errors == []
    assert kbd.eligible_work_stalled([late], 1)
    assert len(calls) == 3
    now[0] += 299
    skipped = _tick(fleet, tid)
    assert skipped.claim_errors == []
    assert skipped.eligible_claim_errors
    assert kbd.eligible_work_stalled([skipped], 1)
    assert len(calls) == 3
    now[0] += 1
    assert _tick(fleet, tid).claim_errors
    assert len(calls) == 4


def test_foreign_card_first_claim_then_ten_minute_backoff(fleet, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(kbd.time, "time", lambda: now[0])
    calls = _claim_spy(monkeypatch)
    tid = kb.create_task(fleet, title="foreign", assignee="worker", tenant="max")
    fleet.execute("UPDATE fleet_kanban_issue_map SET current_node = 'max' WHERE local_task_id = ?", (tid,))
    fleet.commit()
    first = _tick(fleet, tid)
    assert first.claim_errors and first.foreign_claim_errors
    assert first.eligible_claim_errors == []
    assert len(calls) == 1
    for elapsed in (60, 599):
        now[0] = kbd._claim_fence[tid]["last"] + elapsed
        skipped = _tick(fleet, tid)
        assert skipped.claim_errors == [] and skipped.foreign_claim_errors
        assert skipped.eligible_claim_errors == []
        assert not kbd.eligible_work_stalled([skipped], 0)
        assert len(calls) == 1
    now[0] = kbd._claim_fence[tid]["last"] + 600
    assert _tick(fleet, tid).foreign_claim_errors
    assert len(calls) == 2
    assert kb.get_task(fleet, tid).status == "ready"


def test_lease_appears_next_tick_clears_backoff(fleet):
    tid = kb.create_task(fleet, title="lease arrives", assignee="worker", tenant="sheldon")
    assert _tick(fleet, tid).held_claim_errors
    fleet.execute("DROP TRIGGER test_lease_fence")
    fleet.commit()
    result = _tick(fleet, tid)
    assert [item[0] for item in result.spawned] == [tid]
    assert result.claim_errors == []
    assert tid not in kbd._claim_fence
    assert kb.get_task(fleet, tid).status == "running"


def test_canonical_hold_retries_without_stalling(fleet, monkeypatch):
    calls = _claim_spy(monkeypatch)
    tid = kb.create_task(fleet, title="unblock syncing", assignee="worker", tenant="sheldon")
    fleet.execute(
        "UPDATE fleet_kanban_issue_map SET canonical_status = 'blocked', sync_state = 'pending' WHERE local_task_id = ?",
        (tid,),
    )
    fleet.commit()
    for _ in range(3):
        result = _tick(fleet, tid)
        assert result.claim_errors and result.held_claim_errors
        assert result.eligible_claim_errors == []
    assert len(calls) == 3
    assert kbd._claim_fence[tid]["kind"] == "held"


def test_no_claim_clears_backoff(fleet, monkeypatch):
    tid = kb.create_task(fleet, title="claim gone", assignee="worker", tenant="sheldon")
    _tick(fleet, tid)
    monkeypatch.setattr(kb, "claim_task", lambda *a, **k: None)
    assert _tick(fleet, tid).spawned == []
    assert tid not in kbd._claim_fence


def test_five_refusals_warn_once(fleet, caplog):
    tid = kb.create_task(fleet, title="repeated", assignee="worker", tenant="sheldon")
    with caplog.at_level(logging.DEBUG):
        for _ in range(5):
            assert _tick(fleet, tid).claim_errors
    refusals = [r for r in caplog.records if "lifecycle fence refused the write" in r.message]
    assert len(refusals) == 5
    assert sum(r.levelno == logging.WARNING for r in refusals) == 1
    assert "check the fleet-kanban-sync log" in refusals[0].message


@pytest.mark.parametrize("field", ["claim_errors", "reclaim_errors"])
def test_repeating_summary_warns_once(field, caplog):
    from gateway.kanban_watchers_dispatcher import _log_spawn_results

    result = kbd.DispatchResult()
    setattr(result, field, [("t", "claim: " + _FENCE)])
    with caplog.at_level(logging.DEBUG):
        for _ in range(10):
            assert not _log_spawn_results([("fleet", result)])
    summaries = [r for r in caplog.records if "refusal(s)" in r.message]
    assert len(summaries) == 10
    assert sum(r.levelno == logging.WARNING for r in summaries) == 1


def test_summary_mentions_only_due_cards(caplog):
    from gateway.kanban_watchers_dispatcher import _log_spawn_results

    with caplog.at_level(logging.WARNING):
        _log_spawn_results([("fleet", kbd.DispatchResult(claim_errors=[("old", _FENCE)]))])
        caplog.clear()
        _log_spawn_results([("fleet", kbd.DispatchResult(claim_errors=[("old", _FENCE), ("new", _FENCE)]))])
    assert len(caplog.records) == 1
    assert "new:" in caplog.text and "old:" not in caplog.text


def test_fence_caches_are_bounded_and_prune_stale(fleet, monkeypatch):
    now = time.time()
    kbd._fence_logged.update({("claim", str(i)): now - 7201 for i in range(4096)})
    assert kbd._fence_log_due("claim", "new", now)
    assert len(kbd._fence_logged) == 1
    kbd._fence_logged.update({("claim", str(i)): now for i in range(4096)})
    kbd._fence_log_due("claim", "fresh", now)
    assert len(kbd._fence_logged) <= 4096

    kbd._claim_fence.update({
        str(i): {"kind": "foreign", "since": now, "last": now} for i in range(4096)
    })
    tid = kb.create_task(fleet, title="bounded", assignee="worker", tenant="sheldon")
    _tick(fleet, tid)
    assert len(kbd._claim_fence) <= 4096
    getattr(kbd, "_claim_fence", {}).clear()
    kbd._claim_fence.update({
        str(i): {"kind": "foreign", "since": now - 7201, "last": now - 7201} for i in range(4096)
    })
    _tick(fleet, tid)
    assert len(kbd._claim_fence) == 1


def test_unverified_recovery_ownership_warns_hourly(fleet, caplog):
    tid = kb.create_task(fleet, title="ownership unknown", assignee="worker")
    fleet.execute(
        "UPDATE fleet_kanban_issue_map SET source_node = NULL WHERE local_task_id = ?", (tid,),
    )
    fleet.commit()
    errors = []
    with caplog.at_level(logging.DEBUG):
        for _ in range(5):
            assert not kbd._recovery_owned_here(fleet, errors, "reconcile", tid, "sheldon")
    records = [r for r in caplog.records if "fleet ownership cannot be verified" in r.message]
    assert len(records) == 5
    assert sum(r.levelno == logging.WARNING for r in records) == 1
    assert len(errors) == 5
