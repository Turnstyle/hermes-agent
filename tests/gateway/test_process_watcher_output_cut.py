"""The gateway watcher's completion event says when its output tail is not the whole output.

The registry's own completion already carries ``output_cut`` (``_completion_output``); the gateway
watcher builds its own ~2000-char tail, so without the marker a relayed bot DM reply (or any long
result) reads as whole and the agent is never told ``process(action="log")`` has the rest.
Fake registry and adapter only: no live gateway, no subprocess.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner

_LONG = "".join(f"reply line {i:05d}\n" for i in range(400))  # well past the 2000-char tail


def _runner(monkeypatch, tmp_path) -> GatewayRunner:
    (tmp_path / "config.yaml").write_text("display:\n  background_process_notifications: concise\n",
                                          encoding="utf-8")
    import gateway.run as gateway_run
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner = GatewayRunner(GatewayConfig())
    runner.adapters[Platform.TELEGRAM] = SimpleNamespace(send=AsyncMock(), handle_message=AsyncMock(),
                                                         _active_sessions={})
    runner._enqueue_process_completion_notification = AsyncMock(return_value=True)
    return runner


def _session(output):
    return SimpleNamespace(output_buffer=output, exited=True, exit_code=0, command="runner", started_at=None,
                           completion_reason="exited", owner_task_id="", task_id="")


def test_completion_event_carries_the_bounded_cut(monkeypatch, tmp_path):
    runner = _runner(monkeypatch, tmp_path)
    watcher = {"session_id": "proc_dm", "platform": "telegram", "chat_id": "123"}
    evt = runner._build_process_completion_event(watcher, _session(_LONG), "proc_dm")
    tail = evt["output"].split("\n", 1)[1]  # after the watcher's own truncation line
    assert evt["output_cut"] == len(_LONG) - len(tail) > 0
    assert _LONG.endswith(tail)
    whole = runner._build_process_completion_event(watcher, _session("short reply\n"), "proc_dm")
    assert "output_cut" not in whole and whole["output"] == "short reply\n"


@pytest.mark.asyncio
async def test_injected_completion_points_the_agent_at_the_full_log(monkeypatch, tmp_path):
    import tools.process_registry as pr_module

    runner = _runner(monkeypatch, tmp_path)
    sessions = [_session(_LONG)]
    monkeypatch.setattr(pr_module, "process_registry", SimpleNamespace(
        get=lambda sid: sessions.pop(0) if sessions else None, is_completion_consumed=lambda sid: False))

    async def _instant_sleep(*_a, **_kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    await runner._run_process_watcher({"session_id": "proc_dm", "check_interval": 0, "platform": "telegram",
                                       "chat_id": "123", "session_key": "agent:main:telegram:dm:123",
                                       "notify_on_complete": True})

    runner._enqueue_process_completion_notification.assert_awaited_once()
    text, evt = runner._enqueue_process_completion_notification.await_args.args
    assert evt["output_cut"] > 0
    assert 'process(action="log", session_id="proc_dm") has the full output' in text
    assert "reply line 00399" in text  # the tail itself is still delivered
