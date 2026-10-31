"""Auth.json singleton vs pool-entry rotation ordering for :mod:`agent.credential_pool`."""

from __future__ import annotations

from typing import Any

from agent.credential_pool_timestamps import _parse_absolute_timestamp


def _singleton_predates_entry(state: Any, entry: Any) -> bool:
    """True only when the auth.json singleton is PROVABLY older than *entry*.

    Both sides stamp ``last_refresh`` on every successful rotation. When
    either side lacks a parseable stamp this returns False (cannot prove),
    which keeps the historical adopt-on-difference behavior (#70111) intact
    for legacy writers.
    """
    entry_ts = _parse_absolute_timestamp(entry.last_refresh)
    if entry_ts is None:
        return False
    state_ts = _parse_absolute_timestamp(state.get("last_refresh") if isinstance(state, dict) else None)
    if state_ts is None:
        return False
    return state_ts < entry_ts
