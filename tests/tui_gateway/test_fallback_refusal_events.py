"""Desktop worker emits a failed receipt and a retained RPC error for pre-send refusal."""
from unittest.mock import MagicMock
import pytest
from tui_gateway import server
from tests.tui_gateway.test_prompt_accept_logging import turn_env as base_turn_env, turn_stubs as base_turn_stubs, _session
from tests.agent.test_independent_cooldown_review import active_agent

@pytest.fixture
def active():
    # Build before the synchronous turn-thread fixture: globally replacing Thread
    # during logging QueueListener initialization would run its loop inline.
    return active_agent({'provider':'openrouter','model':'active'})

@pytest.fixture
def turn_env(active, monkeypatch, tmp_path):
    base_turn_stubs.__wrapped__(monkeypatch, tmp_path)
    base_turn_env.__wrapped__(None, monkeypatch)

@pytest.mark.parametrize('once', [False, True])
def test_removed_route_emits_unsent_terminal_frame_without_crash(turn_env, active, monkeypatch, tmp_path, once):
    agent = active
    agent.run_conversation = MagicMock(side_effect=AssertionError('provider turn reached'))
    session = _session(agent=agent)
    original_history = [{'role':'user','content':'earlier'}, {'role':'assistant','content':'reply'}]
    session['history'] = original_history[:]
    saved = {'model':'original'}
    if once:
        session['one_turn_model_restore'] = saved
    cfg = tmp_path/'config.yaml'
    cfg.write_text('fallback_providers: []\n')
    monkeypatch.setattr(server, '_active_config_path', lambda: cfg)
    monkeypatch.setattr(server, '_apply_pending_model_switch', lambda *a: None)
    monkeypatch.setattr(server, '_sync_agent_compression_with_config', lambda *a: None)
    monkeypatch.setattr(server, '_ensure_session_db_row', lambda *a: True)
    crash = tmp_path/'crash.log'
    monkeypatch.setattr(server, '_CRASH_LOG', str(crash))
    restore = MagicMock()
    monkeypatch.setattr(server, '_restore_agent_model_runtime', restore)
    monkeypatch.setattr(server, '_restart_slash_worker', lambda *a: None)
    monkeypatch.setattr(server, '_persist_live_session_runtime', lambda *a: None)
    monkeypatch.setattr(server, '_persist_live_session_system_prompt', lambda *a: None)
    events=[]
    monkeypatch.setattr(server, '_emit', lambda name,sid,payload=None,**kw: events.append((name,payload)))
    receipt = MagicMock()
    deadline = agent._rate_limited_until
    assert server._run_prompt_submit('rid','ui-sid',session,'unsent offline input', terminal_callback=receipt)
    frame = [p for n,p in events if n=='message.complete']
    assert len(frame)==1
    assert frame[0]['status']=='error'
    assert 'not sent to the model' in frame[0]['error']
    assert frame[0]['error_surface']=={'layer':'runtime','code':'fallback_route_unavailable','retryable':True}
    assert frame[0]['recoverable'] is True
    receipt.assert_called_once()
    assert receipt.call_args.args[0]['status']=='failed'
    assert 'not sent to the model' in receipt.call_args.args[0]['error']
    assert session['inflight_turn']['status']=='error'
    assert session['inflight_turn']['user']=='unsent offline input'
    assert session['history']==original_history
    assert session['running'] is False
    assert not crash.exists()
    agent.run_conversation.assert_not_called()
    agent.client.chat.completions.create.assert_not_called()
    assert agent._rate_limited_until==deadline
    assert agent._rate_limit_backoff_count==4
    if once:
        restore.assert_called_once_with(agent, saved)
        assert 'one_turn_model_restore' not in session
    else:
        restore.assert_not_called()


def test_unrelated_preparation_error_keeps_crash_and_failed_terminal_path(turn_env, active, monkeypatch, tmp_path):
    agent = active
    agent.run_conversation=MagicMock()
    session=_session(agent=agent)
    monkeypatch.setattr(server, '_ensure_session_db_row', lambda *a: True)
    def fail(*a):
        raise RuntimeError('controlled unrelated preparation error')
    monkeypatch.setattr(server, '_sync_agent_fallback_with_config', fail)
    crash=tmp_path/'crash.log'
    monkeypatch.setattr(server, '_CRASH_LOG', str(crash))
    events=[]
    monkeypatch.setattr(server, '_emit', lambda n,s,p=None,**kw: events.append((n,p)))
    receipt=MagicMock()
    assert server._run_prompt_submit('rid','ui-sid',session,'offline input',terminal_callback=receipt)
    assert 'controlled unrelated preparation error' in crash.read_text()
    frame=[p for n,p in events if n=='message.complete'][0]
    assert frame['status']=='error'
    assert frame.get('error_surface',{}).get('code')!='fallback_route_unavailable'
    assert receipt.call_args.args[0]['status']=='failed'
    agent.run_conversation.assert_not_called()
