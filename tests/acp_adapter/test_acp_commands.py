import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from acp.schema import TextContentBlock

from acp_adapter.server import HermesACPAgent
from acp_adapter.session import SessionManager
from hermes_state import SessionDB


class FakeAgent:
    def __init__(self):
        self.model = "fake-model"
        self.provider = "fake-provider"
        self.enabled_toolsets = ["hermes-acp"]
        self.disabled_toolsets = []
        self.tools = []
        self.valid_tool_names = set()
        self._supports_active_turn_redirect = True
        self.steers = []
        self.redirects = []
        self.runs = []

    def steer(self, text):
        self.steers.append(text)
        return True

    def redirect(self, text):
        self.redirects.append(text)
        return True

    def run_conversation(self, *, user_message, conversation_history, task_id, **kwargs):
        self.runs.append(user_message)
        messages = list(conversation_history or [])
        messages.append({"role": "user", "content": user_message})
        final = f"ran: {user_message}"
        messages.append({"role": "assistant", "content": final})
        return {"final_response": final, "messages": messages}


class CaptureConn:
    def __init__(self):
        self.updates = []

    async def session_update(self, *args, **kwargs):
        if kwargs:
            self.updates.append((kwargs.get("session_id"), kwargs.get("update")))
        else:
            self.updates.append((args[0], args[1]))

    async def request_permission(self, *args, **kwargs):
        return SimpleNamespace(outcome="allow")


class NoopDb:
    def get_meta(self, *_args, **_kwargs):
        return None

    def list_meta_prefix(self, *_args, **_kwargs):
        return []

    def get_session(self, *_args, **_kwargs):
        return None

    def create_session(self, *_args, **_kwargs):
        return None

    def update_session(self, *_args, **_kwargs):
        return None


def make_agent_and_state():
    fake = FakeAgent()
    manager = SessionManager(agent_factory=lambda **kwargs: fake, db=NoopDb())
    acp_agent = HermesACPAgent(session_manager=manager)
    state = manager.create_session(cwd=".")
    conn = CaptureConn()
    acp_agent.on_connect(conn)
    return acp_agent, state, fake, conn


def test_acp_real_agent_gets_session_db_for_recall(monkeypatch):
    """ACP sessions persist to SessionDB; recall must receive the same DB handle."""
    captured = {}
    sentinel_db = NoopDb()

    class CapturingAgent(FakeAgent):
        def __init__(self, **kwargs):
            super().__init__()
            captured.update(kwargs)

    def mod(name, **attrs):
        module = ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        return module

    monkeypatch.setitem(sys.modules, "run_agent", mod("run_agent", AIAgent=CapturingAgent))
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.config",
        mod("hermes_cli.config", load_config=lambda: {"model": {"default": "m", "provider": "p"}}),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.runtime_provider",
        mod(
            "hermes_cli.runtime_provider",
            resolve_runtime_provider=lambda **_kwargs: {
                "provider": "p",
                "api_mode": "chat_completions",
                "base_url": "u",
                "api_key": "k",
                "command": None,
                "args": [],
            },
        ),
    )

    manager = SessionManager(db=sentinel_db)
    agent = manager._make_agent(session_id="acp-session", cwd=".")

    assert isinstance(agent, CapturingAgent)
    assert captured["session_db"] is sentinel_db
    assert captured["platform"] == "acp"
    assert captured["session_id"] == "acp-session"


@pytest.mark.asyncio
async def test_acp_steer_slash_command_injects_into_running_agent():
    acp_agent, state, fake, _conn = make_agent_and_state()
    state.is_running = True

    response = await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text="/steer prefer the simpler fix")],
    )

    assert response.stop_reason == "end_turn"
    assert fake.steers == ["prefer the simpler fix"]
    assert fake.runs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect_supported", [True, False])
async def test_acp_busy_recovery_resend_is_neither_redirected_nor_queued(
    monkeypatch, redirect_supported,
):
    """An explicit recovery can claim only after it owns turn admission."""
    acp_agent, state, fake, conn = make_agent_and_state()
    state.is_running = True
    fake._supports_active_turn_redirect = redirect_supported
    raw = "recover this exact input"
    recovery = {
        "record_version": 1,
        "admission_id": "acp-busy-recovery",
        "surface_key": f"acp:{state.session_id}",
        "status": "pending",
        "text": raw,
        "safe_to_replay": True,
        "dispatch_state": None,
    }
    claims = []
    monkeypatch.setattr(
        "acp_adapter.server.get_surface_pending_input_recovery",
        lambda _db, _surface: recovery,
    )
    monkeypatch.setattr(
        "acp_adapter.server.claim_pending_input_recovery",
        lambda *_args, **_kwargs: claims.append(True),
    )

    response = await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text=raw)],
    )

    assert response.stop_reason == "end_turn"
    assert claims == []
    assert fake.redirects == []
    assert state.queued_prompts == []
    assert fake.runs == []
    assert "cannot be redirected or queued" in str(conn.updates[-1][1])








@pytest.mark.asyncio
async def test_acp_cancel_publishes_hard_stop_while_holding_runtime_lock():
    acp_agent, state, fake, _conn = make_agent_and_state()
    state.is_running = True
    state.current_prompt_text = "original request"
    observed = {}

    def interrupt():
        acquired = state.runtime_lock.acquire(blocking=False)
        observed["lock_held"] = not acquired
        if acquired:
            state.runtime_lock.release()

    fake.interrupt = interrupt

    await acp_agent.cancel(state.session_id)

    assert observed["lock_held"] is True
    assert state.cancel_event.is_set()
    assert state.interrupted_prompt_text == "original request"


@pytest.mark.asyncio
async def test_retained_timeout_drains_prior_queue_without_claiming_failed_generation(
    monkeypatch,
):
    """Accepted busy-queue work keeps order, but is never treated as a resend."""
    acp_agent, state, _fake, conn = make_agent_and_state()
    state.is_running = True
    state.current_prompt_text = "failed generation"
    state.current_prompt_content = "failed generation"
    state.current_prompt_admission_id = "failed-admission"
    state.queued_prompts[:] = ["already queued work"]
    monkeypatch.setattr(
        "acp_adapter.server.get_pending_input_recovery",
        lambda _db, admission_id: {
            "record_version": 1,
            "admission_id": admission_id,
            "surface_key": f"acp:{state.session_id}",
            "status": "pending",
            "safe_to_replay": True,
        },
    )
    drain = AsyncMock(return_value=SimpleNamespace(stop_reason="end_turn"))
    acp_agent.prompt = drain

    response = await acp_agent._finish_turn(
        state,
        state.session_id,
        conn,
        {
            "turn_exit_reason": "context_compression_timeout",
            "recovery_persisted": True,
            "messages": [],
            "final_response": "compression timed out",
        },
        state.session_id,
        False,
        "failed-admission",
    )

    assert response.stop_reason == "end_turn"
    assert state.queued_prompts == []
    assert state.current_prompt_text == ""
    drain.assert_awaited_once_with(
        prompt=[TextContentBlock(type="text", text="already queued work")],
        session_id=state.session_id,
        _allow_recovery_claim=False,
    )


@pytest.mark.asyncio
async def test_failed_recovery_preservation_keeps_prompt_and_queue_inert(monkeypatch):
    acp_agent, state, _fake, conn = make_agent_and_state()
    state.is_running = True
    state.current_prompt_text = "accepted but not preserved"
    state.current_prompt_content = "accepted but not preserved"
    state.current_prompt_admission_id = "missing-recovery"
    state.queued_prompts[:] = ["must remain behind it"]
    monkeypatch.setattr(
        "acp_adapter.server.get_pending_input_recovery",
        lambda *_args: None,
    )
    drain = AsyncMock(return_value=SimpleNamespace(stop_reason="end_turn"))
    acp_agent.prompt = drain

    await acp_agent._finish_turn(
        state,
        state.session_id,
        conn,
        {
            "turn_exit_reason": "context_compression_timeout",
            "recovery_persisted": False,
            "messages": [],
            "final_response": "compression timed out",
        },
        state.session_id,
        False,
        "missing-recovery",
    )

    assert state.current_prompt_text == "accepted but not preserved"
    assert state.current_prompt_content == "accepted but not preserved"
    assert state.queued_prompts == ["must remain behind it"]
    drain.assert_not_awaited()


def test_acp_recover_delivered_withdraws_restart_tail_and_advances_head(tmp_path):
    from agent.pending_input_recovery import (
        claim_pending_input_recovery,
        get_pending_input_recovery,
        get_surface_pending_input_recovery,
        mark_pending_input_recovery_dispatch_started,
        stage_pending_input_recovery,
        stamp_pending_input_admission,
    )

    db = SessionDB(db_path=tmp_path / "state.db")
    fake = FakeAgent()
    manager = SessionManager(agent_factory=lambda **_kwargs: fake, db=db)
    acp_agent = HermesACPAgent(session_manager=manager)
    state = manager.create_session(cwd=".")
    surface = f"acp:{state.session_id}"

    def accepted(text):
        return {
            "version": 1,
            "kind": "accepted_user_input",
            "text": text,
            "input_kind": "plain_text",
            "exact": True,
            "lossy": False,
            "loss_reasons": [],
            "safe_to_replay": True,
            "provider_call_sent": False,
            "executable": False,
        }

    def tagged(admission_id, text):
        message = {"role": "user", "content": text, "timestamp": 123.0}
        stamp_pending_input_admission(
            message, admission_id=admission_id, surface_key=surface,
        )
        return message

    older = tagged("acp-older", "older pending work")
    stage_pending_input_recovery(
        db,
        accepted_input=accepted(older["content"]),
        live_messages=[older],
        session_ids=[state.session_id],
        admission_id="acp-older",
        surface_key=surface,
        accepted_input_durably_committed=False,
    )
    current = tagged("acp-current", "possibly delivered work")
    stage_pending_input_recovery(
        db,
        accepted_input=accepted(current["content"]),
        live_messages=[current],
        session_ids=[state.session_id],
        admission_id="acp-current",
        surface_key=surface,
        accepted_input_durably_committed=False,
    )
    claim_pending_input_recovery(
        db,
        "acp-current",
        resend_id="acp-current-resend",
        claim_session_id=state.session_id,
    )
    resend = tagged("acp-current-resend", current["content"])
    db.append_message(
        state.session_id,
        "user",
        resend["content"],
        timestamp=resend["timestamp"],
        display_metadata=resend["display_metadata"],
    )
    mark_pending_input_recovery_dispatch_started(
        db, "acp-current", resend_id="acp-current-resend",
    )
    state.history = [resend]
    fake._session_messages = [resend]

    response = acp_agent._handle_slash_command(
        "/recover acp-current delivered", state,
    )

    assert response == "Recovery marked delivered; Hermes will not resend it."
    assert get_pending_input_recovery(db, "acp-current")["status"] == "consumed"
    assert db.get_messages_as_conversation(state.session_id) == []
    assert state.history == []
    assert fake._session_messages == []
    assert get_surface_pending_input_recovery(db, surface)["admission_id"] == "acp-older"


def test_acp_recover_delivered_preserves_durable_answered_pair(tmp_path):
    from agent.pending_input_recovery import (
        claim_pending_input_recovery,
        get_pending_input_recovery,
        mark_pending_input_recovery_dispatch_started,
        mark_pending_input_recovery_provider_returned,
        stage_pending_input_recovery,
        stamp_pending_input_admission,
    )

    db = SessionDB(db_path=tmp_path / "state.db")
    fake = FakeAgent()
    manager = SessionManager(agent_factory=lambda **_kwargs: fake, db=db)
    acp_agent = HermesACPAgent(session_manager=manager)
    state = manager.create_session(cwd=".")
    surface = f"acp:{state.session_id}"
    original = {"role": "user", "content": "deploy once"}
    stamp_pending_input_admission(
        original, admission_id="acp-answered", surface_key=surface,
    )
    stage_pending_input_recovery(
        db,
        accepted_input={
            "version": 1, "kind": "accepted_user_input",
            "text": original["content"], "input_kind": "plain_text",
            "exact": True, "lossy": False, "loss_reasons": [],
            "safe_to_replay": True, "provider_call_sent": False,
            "executable": False,
        },
        live_messages=[original],
        session_ids=[state.session_id],
        admission_id="acp-answered",
        surface_key=surface,
        accepted_input_durably_committed=False,
    )
    claim_pending_input_recovery(
        db, "acp-answered", resend_id="acp-resend-answered",
        claim_session_id=state.session_id,
    )
    resend = {"role": "user", "content": "deploy once"}
    stamp_pending_input_admission(
        resend, admission_id="acp-resend-answered", surface_key=surface,
    )
    answer = {"role": "assistant", "content": "deployed"}
    db.append_message(
        state.session_id, "user", resend["content"],
        display_metadata=resend["display_metadata"],
    )
    mark_pending_input_recovery_dispatch_started(
        db, "acp-answered", resend_id="acp-resend-answered",
    )
    mark_pending_input_recovery_provider_returned(
        db, "acp-answered", resend_id="acp-resend-answered",
    )
    db.append_message(state.session_id, "assistant", answer["content"])
    state.history = [resend, answer]
    fake._session_messages = [resend, answer]

    response = acp_agent._handle_slash_command(
        "/recover acp-answered delivered", state,
    )

    record = get_pending_input_recovery(db, "acp-answered")
    assert response == "Recovery marked delivered; Hermes will not resend it."
    assert record["status"] == "consumed"
    assert record["provider_response_received"] is True
    assert state.history == [resend, answer]
    assert fake._session_messages == [resend, answer]
    assert [
        (message["role"], message["content"])
        for message in db.get_messages_as_conversation(state.session_id)
    ] == [("user", "deploy once"), ("assistant", "deployed")]


def test_acp_recover_refuses_resolution_during_active_provider_turn(tmp_path):
    from agent.pending_input_recovery import (
        claim_pending_input_recovery,
        get_pending_input_recovery,
        mark_pending_input_recovery_dispatch_started,
        stage_pending_input_recovery,
        stamp_pending_input_admission,
    )

    db = SessionDB(db_path=tmp_path / "state.db")
    fake = FakeAgent()
    manager = SessionManager(agent_factory=lambda **_kwargs: fake, db=db)
    acp_agent = HermesACPAgent(session_manager=manager)
    state = manager.create_session(cwd=".")
    surface = f"acp:{state.session_id}"
    original = {"role": "user", "content": "dispatch may be active"}
    stamp_pending_input_admission(
        original, admission_id="acp-active", surface_key=surface,
    )
    stage_pending_input_recovery(
        db,
        accepted_input={
            "version": 1, "kind": "accepted_user_input",
            "text": original["content"], "input_kind": "plain_text",
            "exact": True, "lossy": False, "loss_reasons": [],
            "safe_to_replay": True, "provider_call_sent": False,
            "executable": False,
        },
        live_messages=[original],
        session_ids=[state.session_id],
        admission_id="acp-active",
        surface_key=surface,
        accepted_input_durably_committed=False,
    )
    claim_pending_input_recovery(
        db, "acp-active", resend_id="acp-active-resend",
        claim_session_id=state.session_id,
    )
    # Dispatch marker can precede transcript persistence; there is deliberately
    # no durable resend row for a guard lookup to discover.
    mark_pending_input_recovery_dispatch_started(
        db, "acp-active", resend_id="acp-active-resend",
    )
    state.is_running = True

    response = acp_agent._handle_slash_command(
        "/recover acp-active retry", state,
    )

    record = get_pending_input_recovery(db, "acp-active")
    assert "cannot be resolved" in response
    assert record["status"] == "claimed"
    assert record["dispatch_state"] == "started"
    assert record["resend_id"] == "acp-active-resend"


@pytest.mark.asyncio
async def test_acp_whitespace_exact_recovery_resend_matches_raw_provider_content(
    monkeypatch,
):
    acp_agent, state, fake, _conn = make_agent_and_state()
    raw = "  deploy exactly\n"
    recovery = {
        "record_version": 1,
        "version": 1,
        "kind": "accepted_user_input",
        "admission_id": "acp-whitespace-recovery",
        "surface_key": f"acp:{state.session_id}",
        "status": "pending",
        "text": raw,
        "input_kind": "plain_text",
        "exact": True,
        "lossy": False,
        "loss_reasons": [],
        "safe_to_replay": True,
        "provider_call_sent": False,
        "executable": False,
    }
    claimed = []
    persist_values = []

    monkeypatch.setattr(
        "acp_adapter.server.get_surface_pending_input_recovery",
        lambda _db, _surface: recovery,
    )
    monkeypatch.setattr(
        "acp_adapter.server.claim_pending_input_recovery",
        lambda _db, recovery_id, **kwargs: (
            claimed.append((recovery_id, kwargs)) or recovery,
            True,
        ),
    )
    original_run = fake.run_conversation

    def capture_run(**kwargs):
        persist_values.append(kwargs.get("persist_user_message"))
        return original_run(**kwargs)

    fake.run_conversation = capture_run

    response = await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text=raw)],
    )

    assert response.stop_reason == "end_turn"
    assert fake.runs == [raw]
    assert persist_values == [raw]
    assert claimed and claimed[0][0] == "acp-whitespace-recovery"


@pytest.mark.asyncio
async def test_acp_exact_recovery_matches_before_cancelled_prompt_rewrite(monkeypatch):
    acp_agent, state, fake, _conn = make_agent_and_state()
    raw = "  recover the older turn exactly\n"
    state.interrupted_prompt_text = "newer cancelled prompt"
    recovery = {
        "record_version": 1,
        "admission_id": "acp-pre-rewrite-recovery",
        "surface_key": f"acp:{state.session_id}",
        "status": "pending",
        "text": raw,
        "safe_to_replay": True,
        "dispatch_state": None,
    }
    claimed = []
    persist_values = []
    monkeypatch.setattr(
        "acp_adapter.server.get_surface_pending_input_recovery",
        lambda _db, _surface: recovery,
    )
    monkeypatch.setattr(
        "acp_adapter.server.claim_pending_input_recovery",
        lambda _db, recovery_id, **kwargs: (
            claimed.append((recovery_id, kwargs)) or recovery,
            True,
        ),
    )
    original_run = fake.run_conversation

    def capture_run(**kwargs):
        persist_values.append(kwargs.get("persist_user_message"))
        return original_run(**kwargs)

    fake.run_conversation = capture_run

    response = await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text=raw)],
    )

    assert response.stop_reason == "end_turn"
    assert fake.runs == [raw]
    assert persist_values == [raw]
    assert claimed and claimed[0][0] == "acp-pre-rewrite-recovery"


def _make_durable_acp_recovery(tmp_path, *, admission_id: str, text: str):
    from agent.pending_input_recovery import (
        stage_pending_input_recovery,
        stamp_pending_input_admission,
    )

    db = SessionDB(db_path=tmp_path / "state.db")
    fake = FakeAgent()
    manager = SessionManager(agent_factory=lambda **_kwargs: fake, db=db)
    acp_agent = HermesACPAgent(session_manager=manager)
    state = manager.create_session(cwd=".")
    fake.session_id = state.session_id
    surface = f"acp:{state.session_id}"
    original = {"role": "user", "content": text}
    stamp_pending_input_admission(
        original, admission_id=admission_id, surface_key=surface,
    )
    stage_pending_input_recovery(
        db,
        accepted_input={
            "version": 1,
            "kind": "accepted_user_input",
            "text": text,
            "input_kind": "plain_text",
            "exact": True,
            "lossy": False,
            "loss_reasons": [],
            "safe_to_replay": True,
            "provider_call_sent": False,
            "executable": False,
        },
        live_messages=[original],
        session_ids=[state.session_id],
        admission_id=admission_id,
        surface_key=surface,
        accepted_input_durably_committed=False,
    )
    acp_agent.on_connect(CaptureConn())
    return acp_agent, state, fake, db


def _assert_acp_recovery_attrs_cleared(fake):
    assert getattr(fake, "_pending_input_admission_id", None) is None
    assert getattr(fake, "_pending_input_surface_key", None) is None
    assert getattr(fake, "_pending_input_recovery_claim_id", None) is None
    assert getattr(fake, "_pending_input_recovery_resend_id", None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_site", ["callback_setup", "agent_prologue"])
async def test_acp_preprovider_failure_returns_recovery_for_same_process_retry(
    tmp_path, monkeypatch, failure_site,
):
    from agent.pending_input_recovery import get_pending_input_recovery

    recovery_id = f"acp-preprovider-{failure_site}"
    raw = "retry this accepted input"
    acp_agent, state, fake, db = _make_durable_acp_recovery(
        tmp_path, admission_id=recovery_id, text=raw,
    )
    original_wire = acp_agent._wire_turn_callbacks
    original_run = fake.run_conversation
    run_attempts = []

    if failure_site == "callback_setup":
        def fail_callback_setup(*_args, **_kwargs):
            raise RuntimeError("callback setup failed before provider entry")

        monkeypatch.setattr(
            acp_agent, "_wire_turn_callbacks", fail_callback_setup,
        )
    else:
        def fail_agent_prologue(**kwargs):
            run_attempts.append(fake._pending_input_recovery_resend_id)
            raise RuntimeError("agent prologue failed before provider entry")

        fake.run_conversation = fail_agent_prologue

    first = await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text=raw)],
    )

    first_record = get_pending_input_recovery(db, recovery_id)
    assert first.stop_reason == "end_turn"
    assert first_record["status"] == "pending"
    assert first_record["dispatch_state"] is None
    first_resend_id = first_record["previous_resend_id"]
    assert first_resend_id
    assert state.is_running is False
    _assert_acp_recovery_attrs_cleared(fake)

    monkeypatch.setattr(acp_agent, "_wire_turn_callbacks", original_wire)

    def capture_retry(**kwargs):
        run_attempts.append(fake._pending_input_recovery_resend_id)
        return original_run(**kwargs)

    fake.run_conversation = capture_retry
    second = await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text=raw)],
    )

    second_record = get_pending_input_recovery(db, recovery_id)
    assert second.stop_reason == "end_turn"
    assert fake.runs == [raw]
    assert run_attempts[-1] != first_resend_id
    assert second_record["status"] == "claimed"
    assert second_record["resend_id"] == run_attempts[-1]
    _assert_acp_recovery_attrs_cleared(fake)


@pytest.mark.asyncio
async def test_acp_started_dispatch_failure_remains_quarantined(tmp_path):
    from agent.pending_input_recovery import (
        get_pending_input_recovery,
        mark_pending_input_recovery_dispatch_started,
    )

    recovery_id = "acp-started-provider-failure"
    raw = "do not duplicate this provider call"
    acp_agent, state, fake, db = _make_durable_acp_recovery(
        tmp_path, admission_id=recovery_id, text=raw,
    )
    observed_resend_ids = []

    def fail_after_dispatch_started(**_kwargs):
        resend_id = fake._pending_input_recovery_resend_id
        observed_resend_ids.append(resend_id)
        mark_pending_input_recovery_dispatch_started(
            db, recovery_id, resend_id=resend_id,
        )
        raise RuntimeError("provider dispatch outcome is ambiguous")

    fake.run_conversation = fail_after_dispatch_started

    response = await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text=raw)],
    )

    record = get_pending_input_recovery(db, recovery_id)
    assert response.stop_reason == "end_turn"
    assert record["status"] == "claimed"
    assert record["dispatch_state"] == "uncertain"
    assert record["resend_id"] == observed_resend_ids[0]
    assert state.is_running is False
    _assert_acp_recovery_attrs_cleared(fake)
