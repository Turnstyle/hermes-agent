"""Failure-atomic routing and multiplex pending-turn cleanup contracts."""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionEntry, SessionStore


def _bare_store(*, config=None) -> tuple[SessionStore, SessionEntry]:
    store = object.__new__(SessionStore)
    store._lock = threading.Lock()
    store._loaded = True
    store._routing_fallback_baseline = None
    store.config = config or GatewayConfig()
    stamp = datetime(2026, 9, 12, 12, 0, 0)
    entry = SessionEntry(
        session_key="agent:main:signal:dm:chat",
        session_id="session-before-reset",
        created_at=stamp,
        updated_at=stamp,
        platform=Platform.SIGNAL,
        metadata={"durable": "old"},
    )
    store._entries = {entry.session_key: entry}
    return store, entry


def test_failed_reset_save_never_publishes_the_candidate_route(monkeypatch) -> None:
    store, old_entry = _bare_store()
    attempted = []

    def fail_save(data, generation):
        assert store._lock.locked()
        assert store._entries[old_entry.session_key] is old_entry
        assert generation > 0
        attempted.append(data[old_entry.session_key])
        raise OSError("routing store unavailable")

    store._routing_generation = 0
    monkeypatch.setattr(store, "_persist_routing_data", fail_save)

    with pytest.raises(OSError, match="routing store unavailable"):
        store.reset_session(old_entry.session_key)

    current = store.lookup_by_session_key(old_entry.session_key)
    assert current is old_entry
    assert current.session_id == "session-before-reset"
    assert current.metadata == {"durable": "old"}
    assert attempted[0]["session_id"] != current.session_id
    assert attempted[0]["metadata"] == {}


def test_multiplex_gc_includes_served_profile_without_a_routing_entry(
    monkeypatch, tmp_path
) -> None:
    config = GatewayConfig(
        multiplex_profiles=True,
        multiplex_profile_allowlist=["orphan"],
    )
    store, _entry = _bare_store(config=config)
    root = tmp_path / "hermes"
    orphan_home = root / "profiles" / "orphan"
    default_db = SimpleNamespace(db_path=root / "state.db")
    orphan_db = SimpleNamespace(db_path=orphan_home / "state.db")
    store._db = default_db

    enumeration_calls = []

    def served_profiles(multiplex, profile_allowlist=None):
        enumeration_calls.append((multiplex, profile_allowlist))
        return [("default", root), ("orphan", orphan_home)]

    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        served_profiles,
    )

    def open_profile_db(*, db_path=None):
        path = Path(db_path)
        if path == root / "state.db":
            return default_db
        if path == orphan_home / "state.db":
            return orphan_db
        raise AssertionError(f"unexpected SessionDB path: {path}")

    monkeypatch.setattr(store, "_open_session_db_for_active_scope", open_profile_db)
    calls = []
    monkeypatch.setattr(
        "gateway.pending_turn_draft.gc_pending_turn_drafts",
        lambda db, *, now=None: calls.append((db, now)) or 1,
    )
    cutoff = datetime(2026, 9, 13, 0, 0, 0)

    assert store.gc_pending_turn_drafts(now=cutoff) == 2
    assert enumeration_calls == [(True, ["orphan"])]
    assert calls == [(default_db, cutoff), (orphan_db, cutoff)]
