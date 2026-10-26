"""G05: a fallback-served or provider-foreign session row model never pins an API turn.

Authority-owned API sessions run through the native turn runner, which persists the SERVED model
onto the row plus ``model_config.gateway_runtime.fallback_active``. The next session-chat turn must
not adopt that fallback model as the session's standing selection, nor replay a model the current
single-vendor provider cannot serve.
"""
import json
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter

_REPLY = ({"final_response": "ok"}, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_state import SessionDB
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._session_db = SessionDB(db_path=tmp_path / "state.db")
    yield adapter
    adapter._session_db.close()
    adapter._response_store.close()
    adapter._run_idempotency_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_active, expected", [(True, None), (False, "gpt-5.4-mini")],
                         ids=["fallback-served-row", "primary-served-row"])
async def test_session_chat_skips_a_fallback_served_row_model(adapter, fallback_active, expected):
    db = adapter._session_db
    db.create_session("s1", "api_server")
    db.update_session_meta("s1", json.dumps({"gateway_runtime": {"provider": "openrouter",
                                                                 "fallback_active": fallback_active}}),
                           model="gpt-5.4-mini")
    app = web.Application()
    app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
    with patch.object(adapter, "_run_agent", AsyncMock(return_value=_REPLY)) as run:
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/api/sessions/s1/chat", json={"message": "hello"})
            assert resp.status == 200, await resp.text()
    assert run.call_args.kwargs["session_model"] == expected


@pytest.mark.parametrize("provider, row_model, pinned", [
    ("openai-codex", "claude-sonnet-4-5", False),   # known vendor mismatch: never replayed
    ("anthropic", "claude-sonnet-4-5", True),       # the provider's own vendor: standing selection
    ("openrouter", "claude-sonnet-4-5", True),      # aggregators are never "foreign"
])
def test_row_model_pins_only_on_a_provider_that_serves_it(adapter, provider, row_model, pinned):
    applied = []
    adapter._apply_provider_runtime = lambda kwargs, prov, target_model=None, required=False: (
        applied.append((prov, target_model)) or True)
    model, *_ = adapter._select_agent_runtime(
        {"provider": provider}, "global-default", requested_model=None, requested_provider=None, route=None,
        session_model=row_model, confirmed_runtime_lock=False, gateway_session_key=None, session_id="s1")
    assert (model == row_model) is pinned
    assert ((provider, row_model) in applied) is pinned
