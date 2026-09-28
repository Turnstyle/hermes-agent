"""Bot Chat delivery exit must not close a session another delivery has leased."""

from types import SimpleNamespace

from hermes_cli.cli_shutdown import _flush_one_shot_session_store


def test_released_delivery_lease_keeps_shared_session_open(monkeypatch):
    import cli

    monkeypatch.setattr(cli, "_handed_off_session_ids", set())
    persisted = []
    flushed = []
    ended = []
    db = SimpleNamespace(flush_token_counts=lambda: flushed.append(True),
                         end_session=lambda *args: ended.append(args))
    agent = SimpleNamespace(_session_db=db, _session_messages=[{"role": "user", "content": "hi"}],
                            _persist_session=lambda *args: persisted.append(args), _persist_disabled=False)
    owner = SimpleNamespace(agent=agent, session_id="s-1", conversation_history=[],
                            _bot_chat_delivery_lease_released=True, _active_session_lease=None)
    _flush_one_shot_session_store(owner)
    assert persisted and flushed == [True]
    assert ended == []


def test_failed_delivery_still_closes_session(monkeypatch):
    import cli

    monkeypatch.setattr(cli, "_handed_off_session_ids", set())
    ended = []
    db = SimpleNamespace(flush_token_counts=lambda: None,
                         end_session=lambda *args: ended.append(args))
    owner = SimpleNamespace(agent=SimpleNamespace(_session_db=db, _persist_disabled=False), session_id="s-1")
    _flush_one_shot_session_store(owner)
    assert ended == [("s-1", "cli_close")]
