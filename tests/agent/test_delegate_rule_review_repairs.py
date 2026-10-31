"""PR725 review regressions: real SQLite/turns, no external transports."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import owner_prompt_policy as policy
from tests.agent.test_owner_prompt_policy_sessions import runtime  # noqa: F401


def test_f01_memory_sync_and_prefetch_are_unruled(runtime):
    agent = runtime.make()
    manager = Mock()
    agent._memory_manager = manager
    raw = "Please remember the blue shipping container"
    ruled = policy.append_rule(raw)
    messages = [{"role": "user", "content": ruled}]
    agent._sync_external_memory_for_turn(original_user_message=ruled, final_response="remembered", interrupted=False, messages=messages)
    assert manager.sync_all.call_args.args[0] == raw
    assert manager.queue_prefetch_all.call_args.args[0] == raw
    assert manager.sync_all.call_args.kwargs["messages"][0]["content"] == raw
    assert messages[0]["content"] == ruled


def test_f01_trivial_memory_does_not_prefetch(runtime):
    agent = runtime.make()
    agent._memory_manager = Mock()
    agent._sync_external_memory_for_turn(original_user_message=policy.append_rule("hi"), final_response="hello", interrupted=False)
    agent._memory_manager.queue_prefetch_all.assert_not_called()
    assert agent._memory_manager.sync_all.call_args.args[0] == "hi"


def test_f02_image_opener_queued_text_before_build(runtime, monkeypatch):
    from tests.tui_gateway.test_submit_time_user_row import _desktop_session
    from tests.agent.test_turn_context import _build
    from tui_gateway import server
    monkeypatch.setattr(server, "_hermes_home", runtime.home)
    sid, _ = _desktop_session(monkeypatch, runtime.db)
    session = server._sessions[sid]
    agent = runtime.make(source="desktop")
    agent._ensure_db_session()
    session.update(agent=None, session_key=agent.session_id, history=[], running=True)
    monkeypatch.setattr(server, "_submit_policy_runtime", lambda s: ("anthropic", "opaque", None))
    envelope = {"text": "queued second turn"}
    image = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]
    try:
        server._persist_submit_user_row(session, "", None)
        server._persist_queued_user_row(session, envelope, None)
        ctx = _build(agent, user_message=image, summarize_user_message_for_log=lambda value: "" if isinstance(value, list) else value)
        agent._persist_session(ctx.messages, [])
        assert policy.has_rule(ctx.user_message)
        session.update(agent=agent, history=ctx.messages, running=False)
        server._replace_queued_user_row_for_turn(session, envelope, is_dispatching=True)
        server._adopt_submit_user_row(session, agent, envelope["text"], envelope["text"])
        ctx2 = _build(agent, user_message=envelope["text"], conversation_history=ctx.messages)
        agent._persist_session(ctx2.messages, [])
        rows = runtime.db.get_messages_as_conversation(agent.session_id)
        assert [policy.has_rule(r["content"]) for r in rows if r["role"] == "user"] == [True, False]
        policy.check_wire_messages(agent, ctx2.messages)
    finally:
        server._sessions.pop(sid, None)


def test_f03_moa_virtual_preset_is_not_a_model(monkeypatch):
    monkeypatch.setattr(policy, "default_pin", lambda: ("anthropic", "claude-pin"))
    assert not policy.route_requires_rule("moa", "claude-council")
    assert not policy.route_requires_rule("moa", "")


def test_f03_moa_references_receive_clean_copies(monkeypatch):
    from agent.turn_request_assembly import _append_moa_context
    captured = {}
    def aggregate(**kw):
        captured.update(kw)
        return "reference answer"
    monkeypatch.setattr("agent.moa_loop.aggregate_moa_context", aggregate)
    msgs = [{"role": "user", "content": policy.append_rule("task")}]
    _append_moa_context(SimpleNamespace(), msgs, {}, policy.append_rule("task"))
    assert captured["user_prompt"] == "task"
    assert captured["api_messages"][0]["content"] == "task"
    assert policy.has_rule(msgs[0]["content"])


def test_f04_speculative_submit_rule_removed_on_resolved_nonclaude(runtime):
    from agent.context_compressor import _DB_PERSISTED_MARKER
    agent = runtime.make(provider="openrouter", model="gpt-5.4")
    agent._ensure_db_session()
    ruled = policy.append_rule("task")
    row_id = runtime.db.append_message(agent.session_id, "user", content=ruled)
    agent._pending_cli_user_message = {"role": "user", "content": ruled, "_row_id": row_id, _DB_PERSISTED_MARKER: True, "_delegate_rule_speculative": True}
    assert policy.apply_turn_start_policy(agent, "task", None, []) == ("task", None)
    assert agent._pending_cli_user_message["content"] == "task"
    assert runtime.db.get_messages(agent.session_id)[0]["content"] == "task"
    agent.run_conversation("task")
    assert agent._pending_cli_user_message is None
    assert agent._pending_cli_user_message is None


def test_f04_desktop_adoption_resolved_fallback(runtime, monkeypatch):
    from tui_gateway import server
    from tests.tui_gateway.test_submit_time_user_row import _desktop_session
    monkeypatch.setattr(server, "_hermes_home", runtime.home)
    sid, _ = _desktop_session(monkeypatch, runtime.db)
    agent = runtime.make(provider="openrouter", model="gpt-5.4", source="desktop")
    agent._ensure_db_session()
    session = server._sessions[sid]
    session.update(agent=None, session_key=agent.session_id, history=[])
    monkeypatch.setattr(server, "_submit_policy_runtime", lambda s: ("anthropic", "opaque", None))
    try:
        server._persist_submit_user_row(session, "task", None)
        assert policy.has_rule(session["_submit_user_row"]["content"])
        session["agent"] = agent
        server._adopt_submit_user_row(session, agent, "task", "task")
        assert agent._pending_cli_user_message["content"] == "task"
        agent.run_conversation("task")
        assert len(runtime.db.get_messages(agent.session_id)) == 2
    finally:
        server._sessions.pop(sid, None)


def test_f05_boundary_uses_exported_anchor():
    from gateway.platforms.api_server_turn_boundary import response_turn_start_index
    msgs = [{"role": "user", "content": policy.append_rule("ask")}, {"role": "assistant", "content": "thinking"}, {"role": "tool", "content": "result"}, {"role": "user", "content": "steer", "display_kind": "steer"}]
    assert response_turn_start_index([], "ask", {"messages": msgs, "current_turn_user_idx": 0}) == 1


@pytest.mark.parametrize("invalid", [True, -1, 90, "0", None])
def test_f05_invalid_boundary_falls_back(invalid):
    from gateway.platforms.api_server_turn_boundary import response_turn_start_index
    assert response_turn_start_index([], "ask", {"messages": [{"role": "user", "content": "ask"}], "current_turn_user_idx": invalid}) == 1


def test_f06_uppercase_auto_opaque_pin(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"model": {"provider": "AUTO", "default": {"model": "opaque", "provider": "anthropic"}}})
    assert policy.default_pin() == ("anthropic", "opaque")


def test_f06_desktop_dict_pin_and_env_model(monkeypatch):
    from tui_gateway import server
    monkeypatch.setattr(server, "_load_cfg", lambda: {"model": {"provider": "AUTO", "default": {"model": "opaque", "provider": "anthropic"}}})
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
    assert server._resolve_model() == "opaque"
    assert server._config_model_target() == ("opaque", "anthropic")
    monkeypatch.setenv("HERMES_MODEL", "opaque-custom")
    assert server._submit_unbuilt_route({}) == ("anthropic", "opaque-custom")


def test_f07_audit_native_parts_and_inactive_rows(runtime):
    from scripts.audit_delegate_rule import audit_database
    agent = runtime.make()
    agent._ensure_db_session()
    old = runtime.db.append_message(agent.session_id, "user", content=policy.append_rule("obsolete"))
    runtime.db._conn.execute("UPDATE messages SET active=0 WHERE id=?", (old,))
    runtime.db._conn.commit()
    runtime.db.append_message(agent.session_id, "user", content=[{"type": "image_url", "image_url": {"url": policy.DELEGATE_RULE}}])
    result = audit_database(runtime.home / "state.db")
    assert result["errors"] == []
    assert (result["present"], result["missing"]) == (0, 1)


def test_f07_root_provider_pin(tmp_path):
    from scripts.audit_delegate_rule import config_pin
    path = tmp_path / "config.yaml"
    path.write_text("provider: anthropic\nmodel: opaque\n")
    assert config_pin(path) == ("anthropic", "opaque")


def test_f08_title_and_turn_start_memory_inputs(runtime, monkeypatch):
    from tests.agent.test_turn_context import _build
    agent = runtime.make()
    agent._memory_manager = Mock()
    seen = {}
    agent._memory_manager.prefetch_all.return_value = ""
    agent._memory_manager.describe_recall.return_value = ""
    monkeypatch.setattr("agent.title_generator.apply_subagent_title", lambda *a, **k: seen.update(subagent=(a, k)))
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *a, **k: seen.update(title=(a, k)))
    _build(agent, user_message="task title")
    assert policy.DELEGATE_RULE not in repr(seen)
    assert policy.DELEGATE_RULE not in repr(agent._memory_manager.mock_calls)
    assert "task title" in repr(seen)
