"""Delegate rule contracts ported from Rosie's 19 original and 12 adversarial cases.

Use real imported boundaries and SessionDB, rather than AST-extracted functions.
The owner's first-turn, substring-idempotence and warn-only decisions supersede
Rosie's original every-turn, terminal-suffix and fail-closed expectations.
"""
import copy
import logging
from types import SimpleNamespace

import pytest

from agent import owner_prompt_policy as policy
from agent.turn_context import build_api_messages
from hermes_state import SessionDB
from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin
from tests.agent.test_turn_context import _FakeAgent, _build
from tests.tui_gateway.test_submit_time_user_row import _desktop_session, _flush_agent
from tui_gateway import server

RULE = "Use 1 to 10 delegate_task sub-agents and don't do the work inside your main model, and pass this same instruction on to anyone you hand work to."


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    import socket
    def denied(*args, **kwargs):
        raise AssertionError("network is forbidden")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(policy, "_wire_warned", set())
    monkeypatch.setattr(policy, "WIRE_RULE_MISSING_COUNT", 0)
    monkeypatch.delenv(policy.STRICT_ENV, raising=False)


@pytest.mark.parametrize("provider,model,expected", [
    ("anthropic", "", True), (" ANTHROPIC ", "opaque", True),
    ("openrouter", "Anthropic/Claude-Sonnet", True),
    ("bedrock", "us.anthropic.claude-v4", True), ("custom", "CLAUDE-test", True),
    ("openai-codex", "gpt-6.1-sol", False), ("custom", "glm", False),
    ("anthropic_messages", "kimi", False), ("moa", "review", False),
])
def test_selectors(provider, model, expected):
    assert policy.route_requires_rule(provider, model, pin=("", "")) is expected


@pytest.mark.parametrize("text", ["", "task", RULE, RULE + " \n", "quoted " + RULE + " then task", "use delegates", RULE + "\n" + RULE])
def test_exact_idempotence(text):
    expected = text if RULE in text else (text + "\n\n" if text else "") + RULE
    assert policy.append_rule(text) == expected
    assert policy.append_rule(expected) == expected


def test_middle_occurrence_not_terminal():
    assert policy.append_rule(RULE + " more") == RULE + " more"


@pytest.mark.parametrize("value", [[], [{"type": "image_url", "image_url": {"url": "file:test"}}],
    [{"type": "input_audio", "input_audio": {"data": "X"}}, {"type": "text", "text": "task"}],
    [{"type": "unknown", "x": [1]}]])
def test_multimodal(value):
    original = copy.deepcopy(value)
    out = policy.append_rule(value)
    assert out == original + [{"type": "text", "text": RULE}]
    assert policy.append_rule(out) == out
    if value:
        out[0]["new"] = "value"
    assert value == original


@pytest.mark.parametrize("kind", ["text", "input_text"])
def test_multimodal_terminal(kind):
    value = [{"type": kind, "text": RULE + " more"}, {"type": "image_url", "image_url": {}}]
    assert policy.append_rule(value) == value


@pytest.mark.parametrize("value", [None, 1, {}, object()])
def test_invalid(value):
    with pytest.raises(TypeError):
        policy.append_rule(value)


def test_clean_override():
    assert policy.apply_to_turn("SYNTHETIC task", "task") == ("SYNTHETIC task\n\n" + RULE, "task\n\n" + RULE)


def test_none_and_nonclaude():
    agent = _FakeAgent()
    ctx = _build(agent, user_message="task", persist_user_message="clean")
    assert ctx.user_message == "task"
    assert agent._persist_user_message_override == "clean"
    assert policy.apply_to_turn("task", None)[1] is None


def test_list_clean():
    assert policy.apply_to_turn("prefix", [{"type": "text", "text": "task"}])[1] == [
        {"type": "text", "text": "task"}, {"type": "text", "text": RULE}]


@pytest.mark.parametrize("config", ["claude-sonnet", {"default": "claude-sonnet"}, {"model": "claude-sonnet"},
    {"default": {"model": "claude-sonnet", "provider": "anthropic"}}])
def test_configured_defaults_use_owning_resolver(config, monkeypatch, tmp_path):
    import hermes_yaml as yaml
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"model": config}))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert policy.route_requires_rule(None, None)


def test_primary_selection():
    agent = SimpleNamespace(provider="openai", model="fallback", _primary_runtime={"provider": "anthropic", "model": "opaque"})
    assert policy.admission_route(agent) == ("anthropic", "opaque")


@pytest.fixture
def desktop(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key = _desktop_session(monkeypatch, db)
    session = server._sessions[sid]
    session["agent"] = SimpleNamespace(provider="anthropic", model="opaque", session_id=key)
    server._ensure_session_db_row(session)
    try:
        yield db, session, key
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_first_write_and_adoption(desktop):
    db, session, key = desktop
    server._persist_submit_user_row(session, "task", "notification")
    row = session["_submit_user_row"]
    row_id, uid, timestamp = row["_row_id"], row["message_uid"], row["timestamp"]
    assert db.get_messages(key)[0]["content"] == "task\n\n" + RULE
    agent = _flush_agent(db, key)
    agent.provider, agent.model = "anthropic", "opaque"
    server._adopt_submit_user_row(session, agent, "task", "task")
    from agent.turn_context import _stage_turn_user_message
    msg, _ = _stage_turn_user_message(agent, "task\n\n" + RULE, None, None, None, None, None)
    assert msg is row
    agent._flush_messages_to_session_db([msg], [])
    assert len(db.get_messages(key)) == 1
    assert (row["_row_id"], row["message_uid"], row["timestamp"]) == (row_id, uid, timestamp)


def test_busy_accept(desktop):
    db, session, key = desktop
    row = server._write_submit_user_row(session, "busy", None, {"queued": True})
    assert row["display_metadata"]["queued"] is True
    assert "_submit_user_row" not in session
    assert db.get_messages(key)[0]["content"] == "busy\n\n" + RULE


def test_busy_merge_keeps_rule_and_identity(desktop):
    db, session, key = desktop
    row = server._write_submit_user_row(session, "first", None)
    row_id, uid = row["_row_id"], row["message_uid"]
    envelope = {"text": "first\nsecond", "_submit_user_row": row}
    server._persist_queued_user_row(session, envelope, None)
    assert db.get_messages(key)[0]["content"] == "first\nsecond\n\n" + RULE
    assert envelope["text"] == "first\nsecond"
    assert (row["_row_id"], row["message_uid"]) == (row_id, uid)
    session["_submit_user_row"] = row
    server._adopt_submit_user_row(session, session["agent"], envelope["text"], envelope["text"])
    assert session["agent"]._pending_cli_user_message is row


def test_stale_identity(desktop):
    db, session, key = desktop
    server._persist_submit_user_row(session, "old", None)
    server._adopt_submit_user_row(session, session["agent"], "new", "new")
    assert not hasattr(session["agent"], "_pending_cli_user_message")
    assert db.get_messages(key)[0]["content"] == "old\n\n" + RULE


def test_expansion_preserves_row(desktop):
    db, session, key = desktop
    server._persist_submit_user_row(session, "task", None)
    server._adopt_submit_user_row(session, session["agent"], "expanded task", "task")
    assert [r["content"] for r in db.get_messages(key)] == ["expanded task\n\n" + RULE]


def test_cli_exposed_content(desktop):
    db, _session, key = desktop
    ui = SimpleNamespace(conversation_history=[])
    agent = _flush_agent(db, key)
    agent.provider, agent.model = "anthropic", "opaque"
    CLIChatTurnMixin._chat_stage_user_message(ui, agent, "task")
    agent._flush_messages_to_session_db(ui.conversation_history, [])
    assert db.get_messages(key)[0]["content"] == "task\n\n" + RULE
    assert ui.conversation_history[0] is agent._pending_cli_user_message


def test_central_before_bind():
    agent = _FakeAgent()
    agent.provider, agent.model = "anthropic", "opaque"
    ctx = _build(agent, user_message="wire", persist_user_message="clean")
    assert ctx.user_message == "wire\n\n" + RULE
    assert agent._persist_user_message_override == "clean\n\n" + RULE


@pytest.mark.parametrize("model_cfg,expected", [
    ({"provider": "anthropic", "default": "opaque"}, ("anthropic", "opaque")),
    ("claude-sonnet", (None, "claude-sonnet")),
    ({"provider": "anthropic", "model": "claude-legacy"}, ("anthropic", "claude-legacy")),
])
def test_default_profile_selection(monkeypatch, tmp_path, model_cfg, expected):
    import hermes_yaml as yaml
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text(yaml.safe_dump({"model": model_cfg}))
    monkeypatch.setattr(server, "_load_cfg", lambda: {"model": model_cfg})
    assert server._submit_policy_runtime({"profile_home": str(profile)})[:2] == expected


@pytest.mark.parametrize("name", ["_resolve_model", "_config_model_target"])
def test_desktop_legacy_defaults(monkeypatch, name):
    monkeypatch.setattr(server, "_load_cfg", lambda: {"model": {"model": "claude-legacy"}})
    result = getattr(server, name)()
    assert (result[0] if isinstance(result, tuple) else result) == "claude-legacy"


@pytest.mark.parametrize("override,expected", [
    ("claude-picked", ("openai", "claude-picked")),
    ({"model": "gpt", "provider": "openai-codex"}, ("openai-codex", "gpt")),
])
def test_string_and_dict_resume_overrides(monkeypatch, tmp_path, override, expected):
    monkeypatch.setattr(server, "_load_cfg", lambda: {"model": {"provider": "openai", "default": "gpt"}})
    session = {"profile_home": str(tmp_path), "resume_runtime_overrides": {"model_override": override, "provider_override": "openai"}}
    assert server._submit_policy_runtime(session)[:2] == expected


def _wire(agent, messages, current=None):
    return build_api_messages(agent, messages, current_turn_user_idx=current,
        ext_prefetch_cache=None, plugin_user_context=None, moa_config=None, active_system_prompt="")[0]


def _wire_agent():
    agent = _FakeAgent()
    agent.provider, agent.model = "anthropic", "claude-test"
    agent._current_turn_timestamp = 1
    agent.ephemeral_system_prompt = None
    agent._copy_reasoning_content_for_api = lambda *args: None
    agent._should_sanitize_tool_calls = lambda: False
    return agent


def test_late_route_warns_once_without_rewrite(caplog):
    agent = _wire_agent()
    messages = [{"role": "user", "content": "raw"}]
    with caplog.at_level(logging.WARNING, logger=policy.__name__):
        assert _wire(agent, messages)[0]["content"] == "raw"
        _wire(agent, messages)
    assert len([r for r in caplog.records if "lacks the rule" in r.message]) == 1
    assert policy.WIRE_RULE_MISSING_COUNT == 2
    assert messages == [{"role": "user", "content": "raw"}]


def test_late_route_strict_opt_in(monkeypatch):
    monkeypatch.setenv(policy.STRICT_ENV, "1")
    with pytest.raises(RuntimeError, match="delegate rule missing"):
        _wire(_wire_agent(), [{"role": "user", "content": "raw"}])


def test_stale_sidecar_is_observed(caplog):
    messages = [{"role": "user", "content": "task\n\n" + RULE, "api_content": "stale"}]
    wire = _wire(_wire_agent(), messages)
    assert wire[0]["content"] == "stale"
    assert policy.WIRE_RULE_MISSING_COUNT == 1
    assert messages[0]["content"] == "task\n\n" + RULE


@pytest.mark.parametrize("messages", [[], [{"role": "assistant", "content": "only"}],
    [{"role": "user", "content": None}], [{"role": "user", "content": {"odd": True}}]])
def test_no_anchor_or_odd_content_does_not_crash(messages, caplog):
    policy.check_wire_messages(_wire_agent(), messages)
    assert policy.WIRE_RULE_MISSING_COUNT == 0
    assert "rule presence not checked" in caplog.text


def test_passing_sidecar_and_multimodal_are_unchanged():
    content = [{"type": "text", "text": RULE}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]
    messages = [{"role": "user", "content": content}]
    assert _wire(_wire_agent(), messages)[0]["content"] == content
    assert policy.WIRE_RULE_MISSING_COUNT == 0


def test_raw_admission_metadata_is_not_forwarded(desktop):
    _db, session, _key = desktop
    server._persist_submit_user_row(session, "task", None)
    assert "_raw_submit_content" not in session["_submit_user_row"]
    server._adopt_submit_user_row(session, session["agent"], "task", "task")
    assert "_raw_submit_content" not in session["agent"]._pending_cli_user_message


def test_changed_admission_route_reconciles_unflushed_cli_row():
    agent = _FakeAgent()
    agent._primary_runtime = {"provider": "openai", "model": "gpt"}
    ui = SimpleNamespace(conversation_history=[])
    CLIChatTurnMixin._chat_stage_user_message(ui, agent, "task")
    row = agent._pending_cli_user_message
    agent._restore_primary_runtime = lambda: (setattr(agent, "provider", "anthropic"), setattr(agent, "model", "opaque"))
    ctx = _build(agent, user_message="task")
    assert ctx.messages[-1] is row
    assert row["content"] == "task\n\n" + RULE


def test_profile_pin_does_not_override_explicit_nonclaude():
    assert not policy.route_requires_rule("openai-codex", "gpt-6.1-sol", pin=("anthropic", "claude-sonnet"))


def test_string_resume_without_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_load_cfg", lambda: {"model": {"provider": "openai", "default": "gpt"}})
    session = {"profile_home": str(tmp_path), "model_override": "claude-picked",
               "resume_runtime_overrides": {"model_override": "claude-picked"}}
    assert server._submit_policy_runtime(session)[:2] == ("openai", "claude-picked")


@pytest.mark.parametrize("display_kind", ["notification", "hidden", "cron"])
def test_first_stored_user_counts_even_if_renderer_hides_it(desktop, display_kind):
    db, _session, key = desktop
    db.append_message(key, "user", "already stored", display_kind=display_kind)
    agent = _FakeAgent()
    agent.session_id, agent._session_db = key, db
    agent.provider, agent.model = "anthropic", "opaque"
    assert _build(agent, user_message="next").user_message == "next"


def test_input_text_rule_survives_real_cli_flush(desktop):
    db, _session, key = desktop
    agent = _flush_agent(db, key)
    agent.provider, agent.model = "anthropic", "opaque"
    ui = SimpleNamespace(conversation_history=[])
    content = [{"type": "input_text", "text": "quoted " + RULE + " more"},
               {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]
    CLIChatTurnMixin._chat_stage_user_message(ui, agent, content)
    agent._flush_messages_to_session_db(ui.conversation_history, [])
    assert db.get_messages(key)[0]["content"] == "quoted " + RULE + " more\n[screenshot]"


def test_no_user_anchor_through_actual_api_builder(caplog):
    assert _wire(_wire_agent(), [{"role": "assistant", "content": "only"}])[0]["content"] == "only"
    assert "without a user message" in caplog.text


def test_reconciles_persisted_desktop_admission_route(desktop):
    db, session, key = desktop
    session["agent"].provider, session["agent"].model = "openai", "gpt"
    server._persist_submit_user_row(session, "task", None)
    assert db.get_messages(key)[0]["content"] == "task"
    agent = _FakeAgent()
    agent.session_id, agent._session_db = key, db
    agent.provider, agent.model = "openai", "gpt"
    server._adopt_submit_user_row(session, agent, "task", "task")
    agent._restore_primary_runtime = lambda: (setattr(agent, "provider", "anthropic"), setattr(agent, "model", "opaque"))
    ctx = _build(agent, user_message="task")
    assert ctx.messages[-1]["content"] == "task\n\n" + RULE
    assert [r["content"] for r in db.get_messages(key)] == ["task\n\n" + RULE]


def test_replay_summary_does_not_claim_missing_first_row(caplog):
    from agent.context_compressor import SUMMARY_PREFIX
    messages = [{"role": "user", "content": SUMMARY_PREFIX + "\nolder context"},
                {"role": "assistant", "content": "understood"}, {"role": "user", "content": "next"}]
    policy.check_wire_messages(_wire_agent(), messages)
    assert policy.WIRE_RULE_MISSING_COUNT == 0
    assert "lacks the rule" not in caplog.text
