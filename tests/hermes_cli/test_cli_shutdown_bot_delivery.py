"""Bot Chat delivery exit must not close a session another delivery has leased."""

from types import SimpleNamespace

import pytest

from hermes_cli.cli_shutdown import _flush_one_shot_session_store


@pytest.mark.parametrize("held", (None, "flock", "lease"))
def test_released_delivery_ends_session_only_with_both_locks(tmp_path, monkeypatch, held):
    import cli
    from hermes_cli.active_sessions import try_acquire_active_session
    from tools.bot_relay import acquire_turn_lock

    home = tmp_path / "profiles" / "ops"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))

    monkeypatch.setattr(cli, "_handed_off_session_ids", set())
    persisted = []
    flushed = []
    ended = []
    db = SimpleNamespace(flush_token_counts=lambda: flushed.append(True),
                         end_session=lambda *args: ended.append(args))
    agent = SimpleNamespace(_session_db=db, _session_messages=[{"role": "user", "content": "hi"}],
                            _persist_session=lambda *args: persisted.append(args), _persist_disabled=False)
    owner = SimpleNamespace(agent=agent, session_id="s-1", conversation_history=[],
                            _bot_chat_delivery_lease_released=True, _active_session_lease=None, config={})
    lease = None
    if held == "lease":
        lease, refusal = try_acquire_active_session(session_id="s-1", surface="desktop", config={})
        assert refusal is None
    try:
        if held == "flock":
            with acquire_turn_lock(tmp_path, "ops", 0):
                _flush_one_shot_session_store(owner)
        else:
            _flush_one_shot_session_store(owner)
    finally:
        if lease is not None:
            lease.release()
    assert persisted and flushed == [True]
    assert ended == ([("s-1", "cli_close")] if held is None else [])
    with acquire_turn_lock(tmp_path, "ops", 0):
        probe, refusal = try_acquire_active_session(session_id="s-1", surface="cli", config={})
        assert refusal is None
        probe.release()


def test_failed_delivery_still_closes_session(monkeypatch):
    import cli

    monkeypatch.setattr(cli, "_handed_off_session_ids", set())
    ended = []
    db = SimpleNamespace(flush_token_counts=lambda: None,
                         end_session=lambda *args: ended.append(args))
    owner = SimpleNamespace(agent=SimpleNamespace(_session_db=db, _persist_disabled=False), session_id="s-1")
    _flush_one_shot_session_store(owner)
    assert ended == [("s-1", "cli_close")]
