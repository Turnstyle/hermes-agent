"""Real SessionDB and full turns through actual source entry functions, offline HTTP only."""
import asyncio
import copy
import json
import socket
import sqlite3
import threading
from types import SimpleNamespace

import httpx
from openai import OpenAI
import pytest

from hermes_state import SessionDB
from run_agent import AIAgent

RULE = "Use 1 to 10 delegate_task sub-agents and don't do the work inside your main model, and pass this same instruction on to anyone you hand work to."


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model: {provider: openrouter, default: test/model, supports_vision: true}\nagent: {max_turns: 2, image_input_mode: native}\ncompression: {enabled: false}\n")
    def denied(*args, **kwargs):
        raise AssertionError("network is forbidden")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    calls = []
    def respond(request):
        body = json.loads(request.content)
        calls.append(copy.deepcopy(body))
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"id": "offline-native", "type": "message", "role": "assistant",
                "model": body["model"], "content": [{"type": "text", "text": "offline reply"}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 2}})
        return httpx.Response(200, json={"id": "offline-completion", "object": "chat.completion", "created": 1,
            "model": body["model"] or "claude-profile-transport", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "offline reply"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}})
    clients = []
    def make_client():
        client = OpenAI(api_key="offline", base_url="https://offline.invalid/v1",
                        http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        clients.append(client)
        return client
    from anthropic import Anthropic
    def make_anthropic_client():
        client = Anthropic(api_key="offline", base_url="https://offline.invalid",
                           http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        clients.append(client)
        return client
    monkeypatch.setattr("agent.anthropic_adapter.build_anthropic_client", lambda *a, **k: make_anthropic_client())
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda *a, **k: [])
    monkeypatch.setattr("model_tools.check_toolset_requirements", lambda *a, **k: {})
    monkeypatch.setattr("agent.process_bootstrap.OpenAI", lambda *a, **k: make_client())
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *a, **k: None)
    monkeypatch.setattr("hermes_cli.mcp_startup.ensure_mcp_discovery_before_agent_build", lambda **k: None)
    db = SessionDB(db_path=tmp_path / "state.db")
    agents = []
    def make(*, provider="openrouter", model="claude-test", source="cli", sid="session", api_mode="chat_completions"):
        agent = AIAgent(api_key="offline", base_url="https://offline.invalid/v1",
                        model=model, provider=provider, api_mode=api_mode, quiet_mode=True,
                        session_db=db, session_id=sid, platform=source, skip_context_files=True,
                        skip_memory=True, enabled_toolsets=[], max_iterations=2)
        agent._cached_system_prompt = "SYSTEM"
        agent._use_prompt_caching = False
        agent.compression_enabled = False
        agent.save_trajectories = False
        agent._disable_streaming = True
        agent._api_max_retries = 1
        monkeypatch.setattr(agent, "_try_recover_primary_transport", lambda *a, **k: False)
        monkeypatch.setattr(agent, "_create_request_openai_client", lambda *a, **k: make_client())
        monkeypatch.setattr(agent, "_create_request_anthropic_client", lambda *a, **k: make_anthropic_client())
        agents.append(agent)
        return agent
    try:
        yield SimpleNamespace(db=db, make=make, calls=calls, home=tmp_path)
    finally:
        for agent in agents:
            agent.close()
        db.close()
        for client in clients:
            client.close()


@pytest.fixture(params=["provider", "model", "profile_default_unset", "nonclaude"])
def source_route(request, runtime):
    case = request.param
    provider, model = {
        "provider": ("anthropic", "opaque"),
        "model": ("openrouter", "anthropic/claude-sonnet"),
        "profile_default_unset": ("openrouter", None),
        "nonclaude": ("openai-codex", "gpt-6.1-sol"),
    }[case]
    if case in ("profile_default_unset", "nonclaude"):
        (runtime.home / "config.yaml").write_text("model: {provider: anthropic, default: claude-sonnet, supports_vision: true}\ncompression: {enabled: false}\n")
    def make(**kwargs):
        agent = runtime.make(provider=provider, model=model or "claude-sonnet", **kwargs)
        if case == "profile_default_unset":
            agent.model = None
            agent._primary_runtime = None
            agent._ensure_db_session()
            with sqlite3.connect(f"file:{runtime.home / 'state.db'}?mode=ro", uri=True) as conn:
                assert conn.execute("SELECT model FROM sessions WHERE id='session'").fetchone()[0] is None
        return agent
    def expected(text):
        return text if case == "nonclaude" else text + "\n\n" + RULE
    route = SimpleNamespace(make=make, expected=expected, provider=provider, model=model, stored_model=None)
    yield route
    if case == "profile_default_unset":
        with sqlite3.connect(f"file:{runtime.home / 'state.db'}?mode=ro", uri=True) as conn:
            assert conn.execute("SELECT model FROM sessions WHERE id='session'").fetchone()[0] == route.stored_model


def first_stored(rt, sid="session"):
    # Read the actual SQLite row, independently of SessionDB's replay projection.
    with sqlite3.connect(f"file:{rt.home / 'state.db'}?mode=ro", uri=True) as conn:
        content = conn.execute("SELECT content FROM messages WHERE session_id=? AND role='user' ORDER BY id LIMIT 1", (sid,)).fetchone()[0]
    try:
        return json.loads(content)
    except ValueError:
        return content


@pytest.mark.parametrize("provider,model,positive", [
    ("anthropic", "opaque", True), ("openrouter", "anthropic/claude-sonnet", True),
    ("openai-codex", "gpt-6.1-sol", False),
])
def test_full_turn_stores_and_sends_same_rule(runtime, provider, model, positive):
    agent = runtime.make(provider=provider, model=model)
    result = agent.run_conversation("task")
    assert result["final_response"] == "offline reply", result
    expected = "task\n\n" + RULE if positive else "task"
    assert first_stored(runtime) == expected
    assert next(m["content"] for m in runtime.calls[-1]["messages"] if m["role"] == "user") == expected


def test_null_model_profile_pin(runtime):
    (runtime.home / "config.yaml").write_text("model: {provider: anthropic, default: claude-sonnet}\n")
    agent = runtime.make()
    # Real null-session-model case: the central resolver must use the active profile pin.
    agent.model = None
    agent._primary_runtime = None
    from tests.agent.test_turn_context import _build
    ctx = _build(agent, user_message="task")
    agent._persist_session(ctx.messages, [])
    assert first_stored(runtime) == "task\n\n" + RULE


def test_resume_replay_does_not_rewrite_first_row(runtime):
    agent = runtime.make()
    result = agent.run_conversation("task")
    assert result["final_response"] == "offline reply"
    before = first_stored(runtime)
    resumed = agent.run_conversation("follow up", conversation_history=result["messages"])
    assert resumed["final_response"] == "offline reply"
    assert first_stored(runtime) == before == "task\n\n" + RULE
    rows = runtime.db.get_messages("session")
    assert [r["content"] for r in rows if r["role"] == "user"] == [before, "follow up"]
    users = [m["content"] for m in runtime.calls[-1]["messages"] if m["role"] == "user"]
    assert users == [before, "follow up"]


def test_preexisting_unruled_session_stays_unchanged(runtime):
    agent = runtime.make()
    agent._ensure_db_session()
    runtime.db.append_message("session", "user", "historical")
    runtime.db.append_message("session", "assistant", "old reply")
    history = runtime.db.get_messages_as_conversation("session")
    result = agent.run_conversation("follow up", conversation_history=history)
    assert result["final_response"] == "offline reply"
    assert first_stored(runtime) == "historical"
    assert [r["content"] for r in runtime.db.get_messages("session") if r["role"] == "user"] == ["historical", "follow up"]


@pytest.mark.parametrize("content", ["quoted " + RULE + " then task", [
    {"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]])
def test_caller_rule_and_multimodal_are_durable(runtime, content):
    agent = runtime.make()
    result = agent.run_conversation(content)
    assert result["final_response"] == "offline reply", result
    expected = content if isinstance(content, str) else content + [{"type": "text", "text": RULE}]
    stored = expected if isinstance(content, str) else "look\n[screenshot]\n" + RULE
    assert first_stored(runtime) == stored
    assert next(m["content"] for m in runtime.calls[-1]["messages"] if m["role"] == "user") == expected


def test_cli_early_writer_then_real_turn(runtime, source_route):
    from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin
    agent = source_route.make()
    ui = SimpleNamespace(conversation_history=[])
    CLIChatTurnMixin._chat_stage_user_message(ui, agent, "task")
    agent._persist_session(ui.conversation_history, [])
    assert first_stored(runtime) == source_route.expected("task")
    result = agent.run_conversation("task")
    assert result["final_response"] == "offline reply", result
    assert len([r for r in runtime.db.get_messages("session") if r["role"] == "user"]) == 1


def test_oneshot_actual_entry(runtime, source_route, monkeypatch):
    from hermes_cli import oneshot
    agent = source_route.make()
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_with_fallback", lambda *a, **k: (
        {"provider": source_route.provider, "api_key": "offline", "base_url": "https://offline.invalid/v1"}, None))
    monkeypatch.setattr(oneshot, "_create_session_db_for_oneshot", lambda: runtime.db)
    class PinnedAgent(AIAgent):
        def __new__(cls, **kwargs):
            return agent
    monkeypatch.setattr("run_agent.AIAgent", PinnedAgent)
    response, result = oneshot._run_agent("task", model=source_route.model, toolsets=[])
    assert response == "offline reply", result
    assert first_stored(runtime) == source_route.expected("task")


def test_cron_watchdog_actual_entry(runtime, source_route):
    from cron.scheduler import _run_agent_with_watchdog
    agent = source_route.make(source="cron")
    result = _run_agent_with_watchdog(agent, "task", {}, "job", "test", "task-id", threading.Event())
    assert result["final_response"] == "offline reply", result
    assert first_stored(runtime) == source_route.expected("task")


def test_delegate_child_actual_runner(runtime, source_route):
    from tools.delegate_tool_child_run import _ChildRun
    agent = source_route.make(source="subagent")
    run = _ChildRun(child=agent, parent_agent=None, task_index=0, goal="task", subagent_id="child", child_progress_cb=None)
    result, error, deferred = run.await_child()
    assert error is None and not deferred, error
    assert result["final_response"] == "offline reply", result
    assert first_stored(runtime) == source_route.expected("task")


def test_acp_actual_turn_entry(runtime, source_route):
    from acp_adapter.server import HermesACPAgent
    from acp_adapter.session import SessionState
    agent = source_route.make(source="acp")
    server = HermesACPAgent(session_manager=SimpleNamespace())
    state = SessionState(session_id="session", agent=agent, history=[], cwd=str(runtime.home))
    loop = asyncio.new_event_loop()
    try:
        result = server._run_agent_turn(state=state, session_id="session", user_text="task", user_content="task",
            conn=None, loop=loop, approval_cb=None, edit_approval_requester=None)
        assert result["final_response"] == "offline reply", result
        assert first_stored(runtime) == source_route.expected("task")
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_api_server_actual_run_agent_peer_dm(runtime, source_route, monkeypatch):
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter.gateway_runner = SimpleNamespace()
    agent = source_route.make(source="api_server")
    monkeypatch.setattr(adapter, "_create_agent", lambda **kwargs: agent)
    try:
        result, _usage = await adapter._run_agent("[verified caller: peer-bot]\ntask", [], session_id="session")
        assert result["final_response"] == "offline reply", result
        assert first_stored(runtime) == source_route.expected("[verified caller: peer-bot]\ntask")
    finally:
        adapter._response_store.close()
        adapter._run_idempotency_store.close()


def test_kanban_worker_actual_quiet_entry(runtime, source_route, monkeypatch):
    from hermes_cli.cli_single_query import _run_quiet_single_query
    from hermes_cli import quiet_single_query
    agent = source_route.make(source="kanban")
    ui = SimpleNamespace(agent=agent, conversation_history=[], session_id="session")
    monkeypatch.setattr(quiet_single_query, "continue_quiet_notify_completions", lambda *a, **k: None)
    monkeypatch.setattr(quiet_single_query, "exit_single_query", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit) as exc:
        _run_quiet_single_query(ui, "work kanban task t_offline")
    assert exc.value.code == 0
    assert first_stored(runtime) == source_route.expected("work kanban task t_offline")


def test_desktop_actual_invoke_and_absorb(runtime, source_route, monkeypatch):
    from tests.tui_gateway.test_submit_time_user_row import _desktop_session
    from tui_gateway import server
    from tui_gateway.prompt_turn import _TurnRun
    monkeypatch.setattr(server, "_hermes_home", runtime.home)
    agent = source_route.make(source="desktop")
    sid, _key = _desktop_session(monkeypatch, runtime.db)
    session = server._sessions[sid]
    session["agent"], session["session_key"] = agent, agent.session_id
    agent._ensure_db_session()
    session["history"] = []
    stop = threading.Event()
    ticker = SimpleNamespace(join=lambda: None)
    monkeypatch.setattr(server, "_start_usage_ticker", lambda *a: (stop, ticker))
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    st = _TurnRun(agent, None, None, False, history=[])
    try:
        server._persist_submit_user_row(session, "task", None)
        assert first_stored(runtime) == source_route.expected("task")
        server._invoke_agent(sid, session, st, "task", "task", None, [], None, None, None, "task")
        assert st.result["final_response"] == "offline reply", st.result
        server._absorb_turn_result(sid, session, st, "task", "notification", None)
        assert first_stored(runtime) == source_route.expected("task")
        assert len([r for r in runtime.db.get_messages("session") if r["role"] == "user"]) == 1
    finally:
        server._sessions.pop(sid, None)


def test_moa_claude_slots_remain_unmodified(runtime, monkeypatch):
    (runtime.home / "config.yaml").write_text("""moa:
  default_preset: review
  presets:
    review:
      reference_models:
        - provider: openrouter
          model: anthropic/claude-sonnet
      aggregator:
        provider: openrouter
        model: anthropic/claude-opus
""")
    calls = []
    def reply(**kwargs):
        calls.append(kwargs)
        message = SimpleNamespace(content="offline reply", tool_calls=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None, model="fake")
    monkeypatch.setattr("agent.moa_loop.call_llm", reply)
    agent = runtime.make(provider="moa", model="review")
    result = agent.run_conversation("task")
    assert result["final_response"] == "offline reply", result
    assert first_stored(runtime) == "task"
    assert {c["task"] for c in calls} == {"moa_reference", "moa_aggregator"}
    assert all(RULE not in json.dumps(c["messages"]) for c in calls)


def test_native_kanban_session_worker_turn(runtime, source_route):
    from gateway.session_kanban import _run_task_turns
    agent = source_route.make(source="kanban")
    context = {"skills": [], "context": "card context", "task_id": "t_offline", "goal_mode": False}
    result = _run_task_turns(agent, {"text": "task"}, [], context)
    assert result["final_response"] == "offline reply", result
    assert first_stored(runtime) == source_route.expected("card context\n\ntask")


def test_messaging_gateway_approval_turn(runtime, source_route, monkeypatch):
    from gateway.run_turn_runner import TurnRunner
    agent = source_route.make(source="telegram")
    ctx = SimpleNamespace(session_key="key", session_id="session", message="task",
        source=SimpleNamespace(user_id="peer", user_name="bot", is_bot=True),
        title_user_message=None, persist_user_display_kind=None, persist_user_display_metadata=None,
        moa_config=None, inbound_message_id="inbound-1", mute_notification_reply=False)
    runner = SimpleNamespace(_ctx=ctx, _approval_notify_sync=None, _approval_owner=None,
                             _native_image_run_message=lambda: "task")
    monkeypatch.setattr("gateway.session_onboarding.arm_onboarding_turn", lambda *a: None)
    result = TurnRunner._run_conversation_with_approval(runner, agent, [], None, None, None)
    assert result["final_response"] == "offline reply", result
    assert first_stored(runtime) == source_route.expected("task")


def test_native_anthropic_http_transport(runtime):
    agent = runtime.make(provider="anthropic", model="claude-sonnet-4-5", api_mode="anthropic_messages")
    agent.max_tokens = 128
    agent.reasoning_config = {"enabled": False}
    result = agent.run_conversation("task")
    assert result["final_response"] == "offline reply", result
    assert first_stored(runtime) == "task\n\n" + RULE
    assert RULE in json.dumps(runtime.calls[-1]["messages"])


def test_carried_unadmitted_input_is_first_stored_row(runtime):
    from agent.session_persistence import _PERSIST_AFTER_ADMISSION_INTERRUPT
    from tests.agent.test_turn_context import _build
    agent = runtime.make()
    carried = {"role": "user", "content": "earlier accepted", _PERSIST_AFTER_ADMISSION_INTERRUPT: True}
    ctx = _build(agent, user_message="current", conversation_history=[carried])
    assert first_stored(runtime) == "earlier accepted\n\n" + RULE
    assert ctx.user_message == "current"


def test_compaction_retains_first_stored_rule_and_does_not_reinject(runtime):
    agent = runtime.make()
    assert agent.run_conversation("task")["final_response"] == "offline reply"
    runtime.db.archive_and_compact("session", [{"role": "assistant", "content": "summary of task"}])
    history = runtime.db.get_messages_as_conversation("session")
    result = agent.run_conversation("continue", conversation_history=history)
    assert result["final_response"] == "offline reply"
    assert first_stored(runtime) == "task\n\n" + RULE
    with sqlite3.connect(f"file:{runtime.home / 'state.db'}?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT content, active, compacted FROM messages WHERE session_id=? AND role='user' ORDER BY id", ("session",)).fetchall()
    assert rows == [("task\n\n" + RULE, 0, 1), ("continue", 1, 0)]


def test_central_actual_entry_route_matrix(runtime, source_route):
    agent = source_route.make()
    result = agent.run_conversation("task")
    assert result["final_response"] == "offline reply", result
    assert first_stored(runtime) == source_route.expected("task")
    assert next(m["content"] for m in runtime.calls[-1]["messages"] if m["role"] == "user") == source_route.expected("task")


def test_api_runs_actual_executor_route_matrix(runtime, source_route):
    from gateway.config import PlatformConfig
    from gateway.platforms import api_server, api_server_runs
    adapter = api_server.APIServerAdapter(PlatformConfig(enabled=True))
    agent = source_route.make(source="api_server")
    run = SimpleNamespace(session_id="session", run_id="offline-run", request_profile=None,
        approval_session_key="offline-approval", browser_control_principal=None,
        browser_control_transport_family=None, session_history_delivery=True,
        agent_kwargs={"room_dispatch": None}, turn_author=None, user_message="task",
        conversation_history=[], declared_selected=False)
    try:
        result, _usage, _served = api_server_runs._run_agent_sync(
            adapter, run, agent, lambda *a: None, _api_server=api_server)
        assert result["final_response"] == "offline reply", result
        assert first_stored(runtime) == source_route.expected("task")
    finally:
        adapter._response_store.close()
        adapter._run_idempotency_store.close()


def test_desktop_queue_accept_and_merge_route_matrix(runtime, source_route, monkeypatch):
    from tests.tui_gateway.test_submit_time_user_row import _desktop_session
    from tui_gateway import server
    monkeypatch.setattr(server, "_hermes_home", runtime.home)
    agent = source_route.make(source="desktop")
    sid, _key = _desktop_session(monkeypatch, runtime.db)
    session = server._sessions[sid]
    session["agent"], session["session_key"], session["history"] = agent, agent.session_id, []
    agent._ensure_db_session()
    envelope = {"text": "task"}
    if source_route.model is None:
        # Queue acceptance ensures the session row using the real profile resolver.
        source_route.stored_model = "claude-sonnet"
    try:
        server._persist_queued_user_row(session, envelope, None)
        assert first_stored(runtime) == source_route.expected("task")
        row_id = envelope["_submit_user_row"]["_row_id"]
        envelope["text"] = "task plus"
        server._persist_queued_user_row(session, envelope, None)
        assert first_stored(runtime) == source_route.expected("task plus")
        assert envelope["_submit_user_row"]["_row_id"] == row_id
        assert len([r for r in runtime.db.get_messages("session") if r["role"] == "user"]) == 1
    finally:
        server._sessions.pop(sid, None)
