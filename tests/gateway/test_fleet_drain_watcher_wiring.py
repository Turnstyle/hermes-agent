"""Gateway startup schedules the fleet DM drain watcher (fleet carry G18/G19 wiring).

Drives the real ``GatewayStartupMixin._start_spawn_background_watchers`` with the other
watchers stubbed and asserts the supervised spawn list carries the fleet drain watcher,
and that the spawned factory runs ``fleet_drain_watcher`` against this runner.
"""
from __future__ import annotations

import asyncio

import gateway.fleet_message_bridge as bridge_mod
from gateway.run_startup import GatewayStartupMixin


class _Runner(GatewayStartupMixin):
    _PRE_RECONNECT_WATCHERS = ()
    _POST_RECONNECT_WATCHERS = ()

    def __init__(self):
        self._failed_platforms = []
        self.spawned = {}

    def _spawn_supervised(self, factory, name, **_kwargs):
        self.spawned[name] = factory

    def _spawn_reconnect_watcher(self):
        return None

    def _scale_to_zero_should_arm(self):
        return False

    def _log_scale_to_zero_not_armed_reason(self):
        return None

    async def _drain_control_watcher(self):
        return None


def test_startup_spawns_fleet_drain_watcher_bound_to_runner(monkeypatch):
    seen = []

    async def fake_watcher(runner, **_kwargs):
        seen.append(runner)

    monkeypatch.setattr(bridge_mod, "fleet_drain_watcher", fake_watcher)
    runner = _Runner()
    runner._start_spawn_background_watchers()

    assert "fleet_message_drain_watcher" in runner.spawned
    asyncio.run(runner.spawned["fleet_message_drain_watcher"]())
    assert seen == [runner]
