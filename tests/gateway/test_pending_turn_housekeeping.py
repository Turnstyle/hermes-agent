"""Session-routing and housekeeping contracts for pending-turn recovery."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.run_watchers import GatewaySessionWatchersMixin
from gateway.session import SessionEntry, SessionStore


def _bare_store(metadata: dict | None = None) -> tuple[SessionStore, SessionEntry, list[dict]]:
    store = object.__new__(SessionStore)
    store._lock = threading.Lock()
    store._loaded = True
    store._routing_fallback_baseline = None
    stamp = datetime(2026, 9, 12, 12, 0, 0)
    entry = SessionEntry(
        session_key="agent:main:signal:dm:chat",
        session_id="session-1",
        created_at=stamp,
        updated_at=stamp,
        platform=Platform.SIGNAL,
        metadata=dict(metadata or {}),
    )
    store._entries = {entry.session_key: entry}
    saves: list[dict] = []

    def _save() -> None:
        assert store._lock.locked()
        saves.append(entry.to_dict())

    store._save = _save
    return store, entry, saves


def test_pending_turn_gc_deduplicates_profile_databases_and_isolates_failures(
    monkeypatch, caplog,
) -> None:
    store, _entry, _saves = _bare_store()
    store._entries = {
        "agent:main:signal:dm:root": SimpleNamespace(),
        "agent:alpha:signal:dm:a": SimpleNamespace(),
        "agent:alpha:signal:dm:b": SimpleNamespace(),
        "agent:beta:signal:dm:c": SimpleNamespace(),
    }
    default_db = SimpleNamespace(db_path=Path("/tmp/hermes-default-state.db"))
    alpha_db = SimpleNamespace(db_path=Path("/tmp/hermes-alpha-state.db"))
    beta_db = SimpleNamespace(db_path=Path("/tmp/hermes-beta-state.db"))
    store._db = default_db
    store._named_profile_for_key = lambda key: (
        "alpha" if ":alpha:" in key else "beta" if ":beta:" in key else None
    )
    store._db_for_key = lambda key: alpha_db if ":alpha:" in key else beta_db

    calls = []

    def _gc(db, *, now=None):
        calls.append((db, now))
        if db is beta_db:
            raise RuntimeError("beta unavailable")
        return 2

    monkeypatch.setattr("gateway.pending_turn_draft.gc_pending_turn_drafts", _gc)
    cutoff = datetime(2026, 9, 13, 0, 0, 0)
    with caplog.at_level(logging.WARNING, logger="gateway.session"):
        deleted = store.gc_pending_turn_drafts(now=cutoff)

    assert deleted == 4
    assert calls == [(default_db, cutoff), (alpha_db, cutoff), (beta_db, cutoff)]
    assert sum(db is alpha_db for db, _ in calls) == 1
    assert any("beta unavailable" in record.message for record in caplog.records)


def _housekeeping_runner(*, max_age_days: int, gc_side_effect=None):
    async_store = SimpleNamespace(
        gc_pending_turn_drafts=AsyncMock(side_effect=gc_side_effect, return_value=0),
        prune_old_entries=AsyncMock(return_value=1),
    )
    runner = SimpleNamespace(
        _sweep_idle_cached_agents=lambda: 0,
        _sweep_agent_cache_under_pressure=lambda: None,
        async_session_store=async_store,
        config=SimpleNamespace(session_store_max_age_days=max_age_days),
        _last_session_store_prune_ts=0.0,
    )
    return runner, async_store


@pytest.mark.asyncio
async def test_hourly_pending_turn_gc_runs_when_stale_session_pruning_is_disabled(
    monkeypatch,
) -> None:
    runner, async_store = _housekeeping_runner(max_age_days=0)
    monkeypatch.setattr("gateway.run_watchers.time.time", lambda: 7200.0)

    await GatewaySessionWatchersMixin._session_housekeeping(runner)

    async_store.gc_pending_turn_drafts.assert_awaited_once_with()
    async_store.prune_old_entries.assert_not_awaited()
    assert runner._last_session_store_prune_ts == 7200.0


@pytest.mark.asyncio
async def test_hourly_pending_turn_gc_failure_does_not_block_stale_route_pruning(
    monkeypatch, caplog,
) -> None:
    runner, async_store = _housekeeping_runner(
        max_age_days=90, gc_side_effect=RuntimeError("pending cleanup failed")
    )
    monkeypatch.setattr("gateway.run_watchers.time.time", lambda: 7200.0)

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        await GatewaySessionWatchersMixin._session_housekeeping(runner)

    async_store.gc_pending_turn_drafts.assert_awaited_once_with()
    async_store.prune_old_entries.assert_awaited_once_with(90)
    assert any("pending cleanup failed" in record.message for record in caplog.records)
