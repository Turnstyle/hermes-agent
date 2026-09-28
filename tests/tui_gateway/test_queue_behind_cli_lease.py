"""A human Desktop send into a chat a bot CLI one-shot holds is queued, not refused.

The bot process keeps its turn. A Desktop-surface holder still gets SESSION_NOT_OWNED.
"""

import threading

import pytest

from hermes_cli.active_sessions import SESSION_NOT_OWNED, ActiveSessionRefusal
from tui_gateway import server


def _session():
    return {
        "session_key": "20260910_013348_685aa0",
        "history_lock": threading.Lock(),
        "running": False,
        "transport": None,
        "attached_images": [],
    }


def _plant():
    session = _session()
    server._sessions["sid"] = session
    return session


def test_human_send_queues_behind_a_cli_lease_and_does_not_interrupt(monkeypatch):
    session = _plant()
    refused = ActiveSessionRefusal("opened by cli", SESSION_NOT_OWNED)
    held = {"now": True}
    drained = []

    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda sid, sess: refused)
    monkeypatch.setattr(server, "_foreign_cli_holds_session", lambda sess: held["now"])
    monkeypatch.setattr(
        server, "_interrupt_session_turn", lambda *args, **kwargs: pytest.fail("must not interrupt"))
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda rid, sid, sess: drained.append(rid))

    def sleep(_seconds):
        held["now"] = False

    monkeypatch.setattr(server.time, "sleep", sleep)
    monkeypatch.setattr(server, "_start_session_work", lambda target, *, name, session=None: target() or True)

    try:
        result = server._methods["prompt.submit"]("r", {"session_id": "sid", "text": "my turn next"})
    finally:
        server._sessions.pop("sid", None)

    assert result["result"]["status"] == "queued"
    assert result["result"]["behind"] == "bot"
    assert session["queued_prompt"]["text"] == "my turn next"
    assert drained == ["cli-lease-sid"]


def test_a_desktop_holder_is_still_refused(monkeypatch):
    session = _plant()
    refused = ActiveSessionRefusal("opened by desktop", SESSION_NOT_OWNED)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda sid, sess: refused)
    monkeypatch.setattr(server, "_foreign_cli_holds_session", lambda sess: False)
    monkeypatch.setattr(
        server, "_interrupt_session_turn", lambda *args, **kwargs: pytest.fail("must not interrupt"))

    try:
        result = server._methods["prompt.submit"]("r", {"session_id": "sid", "text": "hello"})
    finally:
        server._sessions.pop("sid", None)

    assert result["error"]["code"] == 4090
    assert result["error"]["data"]["reason"] == SESSION_NOT_OWNED
    assert session.get("queued_prompt") is None
