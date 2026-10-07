"""Gateway-draining session chat spools must replay as turns, not transcript rows."""

import asyncio
import contextlib
import logging

import pytest
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from gateway.config import Platform
from gateway.platforms.api_server import APIServerAdapter
from gateway.shutdown_flush import (
    GATEWAY_DRAINING_REASON,
    recover_gateway_draining_session_chats,
    recover_pending_to_db,
)


def _write_draining_spool(flush_dir, *, session_id: str, text: str) -> None:
    payload = {
        "session_key": f"agent:api:{session_id}",
        "reason": GATEWAY_DRAINING_REASON,
        "ts": int(time.time()),
        "data": {"text": text, "session_id": session_id},
    }
    path = flush_dir / "drain-spool.json"
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("old", [False, True])
def test_gateway_draining_spool_replays_via_session_chat_dispatch(tmp_path, monkeypatch, old):
    flush_dir = tmp_path / "pending_messages"
    flush_dir.mkdir()
    monkeypatch.setattr("gateway.shutdown_flush._get_flush_dir", lambda: flush_dir)

    session_id = "bot-chat"
    text = "deliver me after restart"
    _write_draining_spool(flush_dir, session_id=session_id, text=text)
    expected_text = text
    if old:
        path = flush_dir / "drain-spool.json"
        payload = json.loads(path.read_text())
        payload["ts"] -= 90000
        path.write_text(json.dumps(payload))
        sent_at = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(payload["ts"]))
        expected_text = (f"Late delivery: this message was sent at {sent_at}. "
                         "The gateway was restarting, so it can only be delivered now.\n\n" + text)

    mock_db = MagicMock()
    assert recover_pending_to_db(mock_db) == 0
    mock_db.append_message.assert_not_called()
    assert flush_dir.joinpath("drain-spool.json").exists()

    async def _exercise_replay() -> None:
        runner = SimpleNamespace(adapters={})
        assert await recover_gateway_draining_session_chats(runner) == 0
        assert flush_dir.joinpath("drain-spool.json").exists()

        api = APIServerAdapter.__new__(APIServerAdapter)
        dispatch = AsyncMock(return_value=False)
        api.dispatch_session_chat_turn = dispatch
        runner.adapters = {Platform.API_SERVER: api}

        assert await recover_gateway_draining_session_chats(runner) == 0
        dispatch.assert_awaited_once_with(session_id=session_id, message=expected_text)
        assert flush_dir.joinpath("drain-spool.json").exists()

        dispatch.reset_mock()
        dispatch.return_value = True
        assert await recover_gateway_draining_session_chats(runner) == 1
        dispatch.assert_awaited_once_with(session_id=session_id, message=expected_text)
        assert not flush_dir.joinpath("drain-spool.json").exists()

    asyncio.run(_exercise_replay())


def test_failed_replay_turn_keeps_the_spool_for_a_later_replay(tmp_path, monkeypatch):
    """A replayed turn that fails (after its retry) is not a success: the sole spool survives."""
    flush_dir = tmp_path / "pending_messages"
    flush_dir.mkdir()
    monkeypatch.setattr("gateway.shutdown_flush._get_flush_dir", lambda: flush_dir)
    _write_draining_spool(flush_dir, session_id="bot-chat", text="deliver me after restart")

    api = APIServerAdapter.__new__(APIServerAdapter)
    api._concurrency_limited_response = lambda: None
    api._build_session_chat_ctx = AsyncMock(return_value=({"session_id": "bot-chat", "run_kwargs": {}}, None))
    api._answer_through_live_bot_chat = AsyncMock(return_value=None)
    api._conversation_history_for_session = AsyncMock(return_value=[])
    failed = {"failed": True, "error": "Error code: 503 - upstream overloaded"}
    api._run_agent = AsyncMock(return_value=(failed, None))
    runner = SimpleNamespace(adapters={Platform.API_SERVER: api})

    async def _exercise() -> None:
        assert await recover_gateway_draining_session_chats(runner) == 0
        assert flush_dir.joinpath("drain-spool.json").exists()

        api._run_agent = AsyncMock(return_value=({"final_response": "done"}, None))
        assert await recover_gateway_draining_session_chats(runner) == 1
        assert not flush_dir.joinpath("drain-spool.json").exists()

    asyncio.run(_exercise())


@pytest.mark.parametrize("accepted, old", [(True, False), (True, True), (False, True)])
def test_served_profile_replay_scope_age_and_bounded_retries(tmp_path, monkeypatch, caplog, accepted, old):
    from gateway import shutdown_flush as flush
    from gateway.platforms.api_server import _api_request_profile

    launch = tmp_path / "pending_messages"
    served_home = tmp_path / "profiles" / "alpha"
    served = served_home / "pending_messages"
    launch.mkdir()
    served.mkdir(parents=True)
    _write_draining_spool(launch, session_id="launch", text="launch message")
    _write_draining_spool(served, session_id="alpha-chat", text="served message")
    path = served / "drain-spool.json"
    (served / "transcript.json").write_text(json.dumps({"reason": "transcript_cap_drop"}))
    if old:
        payload = json.loads(path.read_text())
        payload["ts"] -= 90000
        path.write_text(json.dumps(payload))
    monkeypatch.setattr(flush, "_get_flush_dir", lambda: launch)
    monkeypatch.setattr(flush, "SERVED_PROFILE_REPLAY_DELAYS", (0, 0, 0, 0))
    monkeypatch.setattr("gateway.run._multiplex_profile_homes", lambda config: [
        ("default", tmp_path), ("alpha", served_home), ("missing", tmp_path / "absent"),
    ])
    seen = []
    scopes = []

    @contextlib.contextmanager
    def scope(name):
        scopes.append(name)
        yield
        scopes.append("exit")

    async def dispatch(*, session_id, message):
        assert scopes[-1] == "alpha"
        seen.append((_api_request_profile.get(), session_id, message))
        return accepted

    api = SimpleNamespace(_profile_scope=scope, dispatch_session_chat_turn=dispatch)
    runner = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=True),
                             adapters={Platform.API_SERVER: api})
    caplog.set_level(logging.WARNING, logger=flush.logger.name)
    token = _api_request_profile.set("caller")
    try:
        assert asyncio.run(flush.recover_served_profile_draining_chats(runner)) == int(accepted)
        assert _api_request_profile.get() == "caller"
    finally:
        _api_request_profile.reset(token)
    assert len(seen) == (1 if accepted else 5)
    assert all(profile == "alpha" and session == "alpha-chat" for profile, session, text in seen)
    if old:
        assert all(text.startswith("Late delivery: this message was sent at ") for _, _, text in seen)
        assert all("The gateway was restarting, so it can only be delivered now.\n\nserved message" in text
                   for _, _, text in seen)
    else:
        assert seen[0][2] == "served message"
    assert path.exists() is not accepted
    assert (served / "transcript.json").exists()
    assert (launch / "drain-spool.json").exists()  # Launch-home replay remains owned by the original path.
    final = [record for record in caplog.records if "The text is still in that file" in record.message]
    assert len(final) == (0 if accepted else 1)
    if final:
        assert str(served) in final[0].message


def test_served_replay_isolates_profile_errors_and_retries_then_stops(tmp_path, monkeypatch):
    from gateway import shutdown_flush as flush
    from gateway.platforms.api_server import _api_request_profile

    launch = tmp_path / "pending_messages"
    launch.mkdir()
    homes = [(name, tmp_path / "profiles" / name) for name in ("broken", "alpha", "beta")]
    for name, home in homes:
        folder = home / "pending_messages"
        folder.mkdir(parents=True)
        _write_draining_spool(folder, session_id=name, text=name)
    monkeypatch.setattr(flush, "_get_flush_dir", lambda: launch)
    monkeypatch.setattr(flush, "SERVED_PROFILE_REPLAY_DELAYS", (0, 0, 0, 0))
    monkeypatch.setattr("gateway.run._multiplex_profile_homes", lambda config: homes)
    calls = []
    attempts = {}

    def scope(name):
        if name == "broken":
            raise RuntimeError("bad profile")
        return contextlib.nullcontext()

    async def dispatch(*, session_id, message):
        calls.append((_api_request_profile.get(), session_id))
        attempts[session_id] = attempts.get(session_id, 0) + 1
        if session_id == "alpha" and attempts[session_id] == 1:
            raise RuntimeError("temporary turn failure")
        return True

    api = SimpleNamespace(_profile_scope=scope, dispatch_session_chat_turn=dispatch)
    runner = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=True),
                             adapters={Platform.API_SERVER: api})
    assert asyncio.run(flush.recover_served_profile_draining_chats(runner)) == 2
    assert calls == [("alpha", "alpha"), ("beta", "beta"), ("alpha", "alpha")]
    assert _api_request_profile.get() is None
    assert (homes[0][1] / "pending_messages" / "drain-spool.json").exists()
    assert not (homes[1][1] / "pending_messages" / "drain-spool.json").exists()
    assert not (homes[2][1] / "pending_messages" / "drain-spool.json").exists()


@pytest.mark.parametrize("config", [None, SimpleNamespace(multiplex_profiles=False)])
def test_non_multiplex_runner_does_no_served_replay(monkeypatch, config):
    from gateway import shutdown_flush as flush

    def forbidden():
        raise AssertionError("multiplex-off recovery must not scan folders")
    monkeypatch.setattr("gateway.shutdown_flush._get_flush_dir", forbidden)
    runner = SimpleNamespace(adapters={})
    if config is not None:
        runner.config = config
    assert asyncio.run(flush.recover_served_profile_draining_chats(runner)) == 0
