"""Real cached-route refusal crosses the worker and delivery boundary without an API send."""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock
import pytest
from tests.agent.test_independent_cooldown_review import active_agent
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from gateway.config import Platform
from gateway.session import SessionSource
from gateway.platforms.event import MessageEvent
from gateway.platforms.base import BasePlatformAdapter
from agent.turn_failure_copy import PARTIAL_FAILED_TURN_NOTICE

@pytest.mark.parametrize('unrelated', [False, True])
def test_cached_refusal_is_delivered_as_unsent_not_unknown_tool_effects(unrelated, caplog):
    async def check():
        agent = active_agent({'provider': 'openrouter', 'model': 'active'})
        runner = object.__new__(GatewayRunner)
        source = SessionSource(platform=Platform.TELEGRAM, chat_id='chat', user_id='user')
        event = MessageEvent(text='offline input', source=source, message_id='m1')
        entry = SimpleNamespace(session_id='offline-sid')
        prepared = runner._PreparedTurn([], '', event.text, event.text, None, None, 'offline-sid', 'owner')
        runner._hmwa_resolve_session = AsyncMock(return_value=(source, entry, 'key'))
        runner._hmwa_prepare_turn = AsyncMock(return_value=(prepared, []))
        runner._clear_session_env = MagicMock()
        runner._reply_anchor_for_event = lambda event: 'm1'
        runner.hooks = SimpleNamespace(emit=AsyncMock())
        runner._hmwa_stop_typing_for_turn = AsyncMock()
        runner.session_store = object()
        runner._async_session_store = SimpleNamespace(_store=runner.session_store, has_input_owner=AsyncMock(return_value=False), append_to_transcript=AsyncMock())
        runner._hmwa_close_failed_turn = AsyncMock()
        runner._hmwa_user_transcript_entry = MagicMock(return_value={'role':'user','content':event.text})
        runner._agent_config_signature = MagicMock(return_value='sig')
        runner._extract_cache_busting_config = MagicMock(return_value={})
        runner._refresh_fallback_model = MagicMock(return_value=[])
        fake = SimpleNamespace(_runner=runner,
            _ctx=SimpleNamespace(enabled_toolsets=[], user_config={}, source=source),
            _skip_context_files=MagicMock(return_value=True),
            _cached_sid_is_dead=MagicMock(return_value=(None, False)),
            _current_message_count=MagicMock(return_value=0),
            _lookup_cached_agent=MagicMock(return_value=SimpleNamespace(agent=agent, reused=True, evicted=None)),
            _build_fresh_agent=MagicMock(), _release_evicted_agent=MagicMock())
        async def run_agent(**kwargs):
            task = asyncio.get_running_loop().create_future()
            try:
                if unrelated:
                    raise RuntimeError('unrelated controlled error')
                TurnRunner._resolve_turn_agent(fake, {'model':'primary-model','runtime':{}}, 'telegram', None, 1, None, {})
            except Exception as exc:
                task.set_exception(exc)
            return await runner._run_agent_await_turn_worker(SimpleNamespace(executor_task=task), SimpleNamespace(agent_holder=[agent]), asyncio.Event(), None)
        runner._run_agent = run_agent
        async def handle(ev):
            return await runner._handle_message_with_agent(ev, source, 'key', 1)
        adapter = SimpleNamespace(_message_handler=handle,
            _unwrap_ephemeral=lambda reply: (reply,0), _send_with_retry=AsyncMock(return_value=SimpleNamespace(success=True,message_id='out')))
        await BasePlatformAdapter._dispatch_inline_reply(adapter, event)
        sent = adapter._send_with_retry.call_args.kwargs
        assert sent['chat_id'] == 'chat'
        assert sent['reply_to'] == 'm1'
        text = sent['content']
        if unrelated:
            assert PARTIAL_FAILED_TURN_NOTICE in text
            assert 'Agent error in session' in caplog.text
            assert 'unrelated controlled error' not in text
        else:
            assert 'not sent to the model' in text
            assert PARTIAL_FAILED_TURN_NOTICE not in text
            assert 'Agent error in session' not in caplog.text
            assert 'not sent to the model' in runner._hmwa_close_failed_turn.call_args.args[1]
        fake._build_fresh_agent.assert_not_called()
        agent.client.chat.completions.create.assert_not_called()
        runner._hmwa_stop_typing_for_turn.assert_awaited_once()
        runner.async_session_store.append_to_transcript.assert_awaited_once()
    asyncio.run(check())
