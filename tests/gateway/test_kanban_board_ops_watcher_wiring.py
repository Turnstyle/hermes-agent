"""The production dispatcher timer invokes Board Ops (fleet carry K01 wiring).

Drives one real iteration of ``GatewayKanbanWatchersMixin._kanban_dispatcher_watcher``
with its external edges stubbed (boot, zombie reaper, dispatch tick, sleeps) and
asserts the Board Ops tick runs on that timer with this runner and the board module,
and that a dispatch pause also pauses Board Ops.
"""
from __future__ import annotations

import asyncio
import types

import pytest

import gateway.kanban_board_ops as board_ops_gateway
import gateway.kanban_watchers as watchers
from hermes_cli import kanban_db_dispatch as kbd


class _Runner(watchers.GatewayKanbanWatchersMixin):
    def __init__(self):
        self._running = True
        self._kanban_dispatcher_lock_handle = None

    async def _sleep_between_ticks(self, interval):
        self._running = False


def _drive_one_tick(monkeypatch, *, dispatch_allowed=True):
    kb_module = types.SimpleNamespace(name="fixture-kb")
    calls = []

    async def fake_tick(runner, kb):
        calls.append((runner, kb))

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(board_ops_gateway, "tick", fake_tick)
    monkeypatch.setattr(watchers.GatewayKanbanWatchersMixin, "_kanban_dispatcher_boot",
                        lambda self: (lambda: {}, kb_module, {}))
    monkeypatch.setattr(watchers, "_resolve_dispatcher_settings",
                        lambda cfg, kb: types.SimpleNamespace(interval=1.0))
    monkeypatch.setattr(watchers, "_KanbanDispatcher",
                        lambda kb, settings: types.SimpleNamespace(tick_once=lambda: [], auto_decompose_tick=lambda n: None))
    monkeypatch.setattr(watchers, "_kanban_dispatch_allowed", lambda: dispatch_allowed)
    monkeypatch.setattr(watchers, "_resolve_auto_decompose_settings", lambda load: (False, 0))
    monkeypatch.setattr(watchers, "_log_spawn_results", lambda results: False)
    monkeypatch.setattr(watchers, "_eligible_work_stalled", lambda *a: False)
    monkeypatch.setattr(kbd, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(watchers.asyncio, "sleep", no_sleep)
    runner = _Runner()
    asyncio.run(runner._kanban_dispatcher_watcher())
    return runner, kb_module, calls


def test_dispatcher_timer_runs_board_ops_tick(monkeypatch):
    runner, kb_module, calls = _drive_one_tick(monkeypatch)
    assert calls == [(runner, kb_module)]


def test_dispatch_pause_also_pauses_board_ops(monkeypatch):
    _runner, _kb, calls = _drive_one_tick(monkeypatch, dispatch_allowed=False)
    assert calls == []
