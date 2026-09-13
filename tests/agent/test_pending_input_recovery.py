from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.conversation_compression import mark_context_compression_timed_out
from agent.conversation_loop import _preflight_timeout_result
from agent.pending_input_recovery import (
    acknowledge_pending_input_recovery_loss,
    PendingInputDispatchUncertainError,
    PendingInputRecoveryError,
    claim_pending_input_recovery,
    consume_pending_input_recovery,
    get_pending_input_recovery,
    get_surface_pending_input_recovery,
    list_pending_input_recoveries,
    mark_pending_input_recovery_dispatch_started,
    mark_pending_input_recovery_provider_returned,
    pending_input_admission,
    reconcile_pending_input_recovery_rows,
    resolve_pending_input_recovery_dispatch,
    stage_pending_input_recovery,
    stamp_pending_input_admission,
)
from agent.turn_context import PreflightCompressionTimedOut
from agent.turn_context_compaction import (
    CompactionOutcome, _idle_compaction, _preflight_compression,
    _run_preflight_passes,
)
from hermes_state import SessionDB


def _accepted(text: str = "finish the deployment") -> dict:
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


def _user(
    admission_id: str, *, surface: str = "cli:surface",
    text: str = "finish the deployment", timestamp: float = 123.0,
    display_metadata: dict | None = None,
) -> dict:
    message = {
        "role": "user", "content": text, "timestamp": timestamp,
        "display_metadata": dict(display_metadata or {}),
    }
    stamp_pending_input_admission(
        message, admission_id=admission_id, surface_key=surface,
    )
    return message


def test_immediate_timeout_stages_inert_record_without_transcript_row(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("head", source="cli")
    message = _user("admission-immediate")

    record, live = stage_pending_input_recovery(
        db,
        accepted_input=_accepted(),
        live_messages=[message],
        session_ids=["head"],
        admission_id="admission-immediate",
        surface_key="cli:surface",
        accepted_input_durably_committed=False,
    )

    assert live == []
    assert record["status"] == "pending"
    assert record["executable"] is False
    assert record["accepted_input_durably_committed"] is False
    assert db.get_messages_as_conversation("head") == []


def test_in_place_timeout_atomically_withdraws_committed_user_row(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("head", source="cli")
    db.append_message("head", "assistant", "ready")
    message = _user("admission-in-place")
    db.append_message(
        "head", "user", message["content"],
        timestamp=message["timestamp"],
        display_metadata=message["display_metadata"],
    )

    record, live = stage_pending_input_recovery(
        db,
        accepted_input=_accepted(),
        live_messages=[{"role": "assistant", "content": "ready"}, message],
        session_ids=["head"],
        admission_id="admission-in-place",
        surface_key="cli:surface",
        accepted_input_durably_committed=True,
    )

    assert [m["content"] for m in live] == ["ready"]
    assert [m["content"] for m in db.get_messages_as_conversation("head")] == ["ready"]
    assert record["accepted_input_durably_committed"] is True
    assert db.get_session("head")["message_count"] == 1


def test_rotation_uses_exact_identity_and_preserves_same_timestamp_rows(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("parent", source="cli")
    message = _user(
        "admission-rotation", timestamp=777.0, display_metadata={"attempt": 2},
    )
    unrelated = _user(
        "admission-unrelated", text="unrelated same-timestamp input", timestamp=777.0,
    )
    db.append_message(
        "parent", "user", unrelated["content"], timestamp=unrelated["timestamp"],
        display_metadata=unrelated["display_metadata"],
    )
    db.append_message(
        "parent", "user", message["content"], timestamp=message["timestamp"],
        display_metadata=message["display_metadata"],
    )
    db.end_session("parent", "compression")
    db.create_session("child", source="cli", parent_session_id="parent")
    db.append_message("child", "assistant", "compacted context")
    db.append_message(
        "child", "user", unrelated["content"], timestamp=unrelated["timestamp"],
        display_metadata=unrelated["display_metadata"],
    )
    db.append_message(
        "child", "user", message["content"], timestamp=message["timestamp"],
        display_metadata=message["display_metadata"],
    )

    record, live = stage_pending_input_recovery(
        db,
        accepted_input=_accepted(),
        live_messages=[
            {"role": "assistant", "content": "compacted context"}, unrelated, message,
        ],
        session_ids=["parent", "child"],
        admission_id="admission-rotation",
        surface_key="cli:surface",
        accepted_input_durably_committed=True,
    )

    assert [m["content"] for m in live] == [
        "compacted context", "unrelated same-timestamp input",
    ]
    assert [m["content"] for m in db.get_messages_as_conversation("parent")] == [
        "unrelated same-timestamp input",
    ]
    assert [m["content"] for m in db.get_messages_as_conversation("child")] == [
        "compacted context", "unrelated same-timestamp input",
    ]
    assert message["display_metadata"]["attempt"] == 2
    assert record["origin_session_id"] == "parent"
    assert record["head_session_id"] == "child"


def test_explicit_consume_is_idempotent_and_rejects_second_resend(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("head", source="cli")
    message = _user("admission-consume")
    stage_pending_input_recovery(
        db,
        accepted_input=_accepted(),
        live_messages=[message],
        session_ids=["head"],
        admission_id="admission-consume",
        surface_key="cli:surface",
        accepted_input_durably_committed=False,
    )

    record, claimed = claim_pending_input_recovery(
        db, "admission-consume", resend_id="resend-once",
    )
    assert claimed is True
    assert record["status"] == "claimed"
    assert list_pending_input_recoveries(db)[0]["admission_id"] == "admission-consume"
    with pytest.raises(PendingInputRecoveryError, match="no provider or recovery successor"):
        consume_pending_input_recovery(
            db, "admission-consume", resend_id="resend-once",
        )

    successor = _user("resend-once", text="finish the deployment")
    db.append_message(
        "head", "user", successor["content"], timestamp=successor["timestamp"],
        display_metadata=successor["display_metadata"],
    )
    # Transcript durability alone is not provider-entry evidence.
    assert list_pending_input_recoveries(db)[0]["status"] == "claimed"
    mark_pending_input_recovery_dispatch_started(
        db, "admission-consume", resend_id="resend-once",
    )
    mark_pending_input_recovery_provider_returned(
        db, "admission-consume", resend_id="resend-once",
    )
    db.append_message("head", "assistant", "done")
    record, consumed = consume_pending_input_recovery(
        db, "admission-consume", resend_id="resend-once",
        provider_response_received=True,
    )
    assert consumed is True
    assert record["status"] == "consumed"
    same, claimed_again = consume_pending_input_recovery(
        db, "admission-consume", resend_id="resend-once",
    )
    assert claimed_again is False
    assert same["resend_id"] == "resend-once"
    with pytest.raises(PendingInputRecoveryError, match="already consumed"):
        consume_pending_input_recovery(
            db, "admission-consume", resend_id="different-resend",
        )
    assert get_pending_input_recovery(db, "admission-consume")["status"] == "consumed"


def test_claim_is_fenced_then_can_be_reclaimed_without_successor(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("head", source="cli")
    message = _user("admission-crash")
    stage_pending_input_recovery(
        db, accepted_input=_accepted(), live_messages=[message], session_ids=["head"],
        admission_id="admission-crash", surface_key="cli:surface",
        accepted_input_durably_committed=False,
    )
    claimed, _ = claim_pending_input_recovery(
        db, "admission-crash", resend_id="lost-before-flush",
    )

    outstanding = list_pending_input_recoveries(db, surface_prefix="cli:")
    assert [(value["admission_id"], value["status"]) for value in outstanding] == [
        ("admission-crash", "claimed")
    ]
    with pytest.raises(PendingInputRecoveryError, match="active resend claim"):
        claim_pending_input_recovery(
            db, "admission-crash", resend_id="racing-live-process",
        )
    monkeypatch.setattr(
        "agent.pending_input_recovery.time.time",
        lambda: float(claimed["claimed_at"]) + 31.0,
    )
    with pytest.raises(PendingInputRecoveryError, match="active resend claim"):
        claim_pending_input_recovery(
            db, "admission-crash", resend_id="live-owner-after-lease",
        )
    monkeypatch.setattr(
        "agent.pending_input_recovery._claim_owner_is_alive", lambda *_args: False,
    )
    record, reclaimed = claim_pending_input_recovery(
        db, "admission-crash", resend_id="retry-after-restart",
    )
    assert reclaimed is True
    assert record["resend_id"] == "retry-after-restart"
    assert record["claim_generation"] == 2


def test_stale_claim_pid_reuse_requires_matching_process_incarnation(
    tmp_path, monkeypatch,
):
    """A reused live PID is not proof that the original claimant survived."""
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("head", source="cli")
    message = _user("admission-pid-reuse")
    stage_pending_input_recovery(
        db, accepted_input=_accepted(), live_messages=[message],
        session_ids=["head"], admission_id="admission-pid-reuse",
        surface_key="cli:surface", accepted_input_durably_committed=False,
    )
    claimed, _ = claim_pending_input_recovery(
        db, "admission-pid-reuse", resend_id="old-incarnation",
    )
    recorded_incarnation = float(claimed["claim_owner_incarnation"])
    monkeypatch.setattr(
        "agent.pending_input_recovery.time.time",
        lambda: float(claimed["claimed_at"]) + 31.0,
    )
    monkeypatch.setattr(
        "agent.pending_input_recovery._process_incarnation",
        lambda _pid: recorded_incarnation,
    )
    with pytest.raises(PendingInputRecoveryError, match="active resend claim"):
        claim_pending_input_recovery(
            db, "admission-pid-reuse", resend_id="matching-incarnation",
        )

    monkeypatch.setattr(
        "agent.pending_input_recovery._process_incarnation",
        lambda _pid: recorded_incarnation + 100.0,
    )
    record, reclaimed = claim_pending_input_recovery(
        db, "admission-pid-reuse", resend_id="reused-pid-new-incarnation",
    )
    assert reclaimed is True
    assert record["resend_id"] == "reused-pid-new-incarnation"
    assert record["claim_generation"] == 2


def test_stale_claim_takeover_respects_live_turn_owner_and_reclaims_dead_owner(
    tmp_path, monkeypatch,
):
    import os

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("head", source="cli")
    message = _user("admission-fenced")
    stage_pending_input_recovery(
        db, accepted_input=_accepted(), live_messages=[message], session_ids=["head"],
        admission_id="admission-fenced", surface_key="cli:surface",
        accepted_input_durably_committed=False,
    )
    claimed, _ = claim_pending_input_recovery(
        db, "admission-fenced", resend_id="first-owner",
    )
    monkeypatch.setattr(
        "agent.pending_input_recovery.time.time",
        lambda: float(claimed["claimed_at"]) + 31.0,
    )
    monkeypatch.setattr(
        "agent.pending_input_recovery._claim_owner_is_alive", lambda *_args: False,
    )

    live_holder = f"pid={os.getpid()}:turn=live:platform=cli"
    assert db.try_acquire_session_turn_lease("head", live_holder)
    with pytest.raises(PendingInputRecoveryError, match="active in another process"):
        claim_pending_input_recovery(
            db, "admission-fenced", resend_id="blocked-takeover",
        )
    db.release_session_turn_lease("head", live_holder)

    dead_holder = "pid=999999999:turn=dead:platform=cli"
    assert db.try_acquire_session_turn_lease("head", dead_holder)
    record, reclaimed = claim_pending_input_recovery(
        db, "admission-fenced", resend_id="dead-owner-takeover",
    )
    assert reclaimed is True
    assert record["resend_id"] == "dead-owner-takeover"


def test_claim_fence_follows_resend_into_new_session(tmp_path, monkeypatch):
    import os

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("old-head", source="cli")
    db.create_session("new-head", source="cli")
    message = _user("admission-new-session")
    stage_pending_input_recovery(
        db, accepted_input=_accepted(), live_messages=[message], session_ids=["old-head"],
        admission_id="admission-new-session", surface_key="cli:surface",
        accepted_input_durably_committed=False,
    )
    claimed, _ = claim_pending_input_recovery(
        db, "admission-new-session", resend_id="new-session-resend",
        claim_session_id="new-head",
    )
    live_holder = f"pid={os.getpid()}:turn=new-session-live:platform=cli"
    assert db.try_acquire_session_turn_lease("new-head", live_holder)
    monkeypatch.setattr(
        "agent.pending_input_recovery.time.time",
        lambda: float(claimed["claimed_at"]) + 31.0,
    )
    monkeypatch.setattr(
        "agent.pending_input_recovery._claim_owner_is_alive", lambda *_args: False,
    )

    with pytest.raises(PendingInputRecoveryError, match="active in another process"):
        claim_pending_input_recovery(
            db, "admission-new-session", resend_id="steal-after-claim-ttl",
            claim_session_id="old-head",
        )

    assert get_pending_input_recovery(db, "admission-new-session")["resend_id"] == (
        "new-session-resend"
    )
    db.release_session_turn_lease("new-head", live_holder)


def test_superseded_claimant_row_is_withdrawn_if_loser_resumes(
    tmp_path, monkeypatch,
):
    """A paused claimant cannot append an executable row after B takes over."""
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("head", source="cli")
    original = _user("admission-takeover")
    stage_pending_input_recovery(
        db,
        accepted_input=_accepted(),
        live_messages=[original],
        session_ids=["head"],
        admission_id="admission-takeover",
        surface_key="cli:surface",
        accepted_input_durably_committed=False,
    )
    claim_a, _ = claim_pending_input_recovery(
        db, "admission-takeover", resend_id="resend-a",
    )
    monkeypatch.setattr(
        "agent.pending_input_recovery.time.time",
        lambda: float(claim_a["claimed_at"]) + 31.0,
    )
    monkeypatch.setattr(
        "agent.pending_input_recovery._claim_owner_is_alive", lambda *_args: False,
    )
    claim_b, taken = claim_pending_input_recovery(
        db, "admission-takeover", resend_id="resend-b",
    )
    assert taken is True
    assert claim_b["resend_id"] == "resend-b"
    assert claim_b["superseded_resend_ids"] == ["resend-a"]

    loser = _user("resend-a")
    db.append_message(
        "head", "user", loser["content"], timestamp=loser["timestamp"],
        display_metadata=loser["display_metadata"],
    )
    with pytest.raises(PendingInputRecoveryError, match="was superseded"):
        mark_pending_input_recovery_dispatch_started(
            db, "admission-takeover", resend_id="resend-a",
        )
    assert db.get_messages_as_conversation("head") == []

    # A crash before the losing mark call is also caught on the next turn.
    db.append_message(
        "head", "user", loser["content"], timestamp=loser["timestamp"],
        display_metadata=loser["display_metadata"],
    )
    cleaned, removed = reconcile_pending_input_recovery_rows(
        db, db.get_messages_as_conversation("head"),
    )
    assert removed == 1
    assert cleaned == []
    assert db.get_messages_as_conversation("head") == []


def test_superseded_claimant_timeout_cannot_stage_duplicate_recovery(
    tmp_path, monkeypatch,
):
    """A's own timeout must atomically fence R before minting successor A."""
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("head", source="cli")
    original = _user("admission-takeover-timeout")
    stage_pending_input_recovery(
        db, accepted_input=_accepted(), live_messages=[original],
        session_ids=["head"], admission_id="admission-takeover-timeout",
        surface_key="cli:surface", accepted_input_durably_committed=False,
    )
    claim_a, _ = claim_pending_input_recovery(
        db, "admission-takeover-timeout", resend_id="resend-timeout-a",
    )
    monkeypatch.setattr(
        "agent.pending_input_recovery.time.time",
        lambda: float(claim_a["claimed_at"]) + 31.0,
    )
    monkeypatch.setattr(
        "agent.pending_input_recovery._claim_owner_is_alive", lambda *_args: False,
    )
    claim_b, _ = claim_pending_input_recovery(
        db, "admission-takeover-timeout", resend_id="resend-timeout-b",
    )
    loser = _user("resend-timeout-a")
    db.append_message(
        "head", "user", loser["content"], timestamp=loser["timestamp"],
        display_metadata=loser["display_metadata"],
    )
    agent = SimpleNamespace(
        session_id="head", _session_db=db,
        _session_persist_lock=threading.RLock(),
        _active_session_turn_lease_holder=None,
        _pending_input_recovery_claim_id="admission-takeover-timeout",
        _pending_input_recovery_resend_id="resend-timeout-a",
        _pending_cli_user_message=loser,
        _persist_user_message_idx=0,
        _persist_user_message_override=None,
        _session_messages=[loser],
        _persist_disabled=True,
        _inflight_turn_id="turn-a", _inflight_turn_session_id="head",
        _last_compression_attempt_recorded=True, platform="cli",
    )
    exc = PreflightCompressionTimedOut(
        "A timed out before provider entry",
        conversation_history=[loser], live_messages=[loser],
        accepted_input_durably_committed=True,
        accepted_input_session_ids=["head"],
    )

    result = _preflight_timeout_result(agent, exc, _accepted())

    assert result["recovery_persisted"] is True
    assert result["api_calls"] == 0
    assert result["messages"] == []
    assert db.get_messages_as_conversation("head") == []
    assert get_pending_input_recovery(db, "resend-timeout-a") is None
    retained = get_pending_input_recovery(db, "admission-takeover-timeout")
    assert retained["status"] == "claimed"
    assert retained["resend_id"] == claim_b["resend_id"] == "resend-timeout-b"
    assert [
        record["admission_id"] for record in list_pending_input_recoveries(db)
    ] == ["admission-takeover-timeout"]


def test_replayed_input_timeout_atomically_replaces_original_recovery(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("head", source="cli")
    original = _user("admission-replay-timeout")
    stage_pending_input_recovery(
        db, accepted_input=_accepted(), live_messages=[original],
        session_ids=["head"], admission_id="admission-replay-timeout",
        surface_key="cli:surface", accepted_input_durably_committed=False,
    )
    claim_pending_input_recovery(
        db, "admission-replay-timeout", resend_id="resend-replay-timeout",
    )
    replay = _user("resend-replay-timeout")
    db.append_message(
        "head", "user", replay["content"], timestamp=replay["timestamp"],
        display_metadata=replay["display_metadata"],
    )
    agent = SimpleNamespace(
        session_id="head", _session_db=db,
        _session_persist_lock=threading.RLock(),
        _active_session_turn_lease_holder=None,
        _pending_input_recovery_claim_id="admission-replay-timeout",
        _pending_input_recovery_resend_id="resend-replay-timeout",
        _pending_cli_user_message=replay,
        _persist_user_message_idx=0, _persist_user_message_override=None,
        _session_messages=[replay], _persist_disabled=True,
        _inflight_turn_id="turn", _inflight_turn_session_id="head",
        _last_compression_attempt_recorded=True, platform="cli",
    )
    exc = PreflightCompressionTimedOut(
        "replay timed out before provider entry",
        conversation_history=[replay], live_messages=[replay],
        accepted_input_durably_committed=True,
        accepted_input_session_ids=["head"],
    )

    result = _preflight_timeout_result(agent, exc, _accepted())

    assert result["recovery_persisted"] is True
    assert result["messages"] == []
    old = get_pending_input_recovery(db, "admission-replay-timeout")
    successor = get_pending_input_recovery(db, "resend-replay-timeout")
    assert old["status"] == "consumed"
    assert old["resolution"] == "durable_recovery_successor"
    assert successor["status"] == "pending"
    assert [
        record["admission_id"] for record in list_pending_input_recoveries(db)
    ] == ["resend-replay-timeout"]


def test_acp_surface_head_selects_exact_latest_id_for_duplicate_text(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("head", source="acp")
    surface = "acp:stable-session"
    for admission_id in ("acp-older", "acp-latest"):
        message = _user(admission_id, surface=surface, text="same ACP prompt")
        stage_pending_input_recovery(
            db, accepted_input=_accepted("same ACP prompt"), live_messages=[message],
            session_ids=["head"], admission_id=admission_id, surface_key=surface,
            accepted_input_durably_committed=False,
        )

    selected = get_surface_pending_input_recovery(db, surface)
    assert selected["admission_id"] == "acp-latest"
    claim_pending_input_recovery(
        db, "acp-latest", resend_id="acp-retry-generation",
    )
    assert get_surface_pending_input_recovery(db, surface)["admission_id"] == "acp-latest"

    successor = _user(
        "acp-retry-generation", surface=surface, text="same ACP prompt",
    )
    db.append_message(
        "head", "user", successor["content"], timestamp=successor["timestamp"],
        display_metadata=successor["display_metadata"],
    )
    assert get_surface_pending_input_recovery(db, surface)["admission_id"] == "acp-latest"
    mark_pending_input_recovery_dispatch_started(
        db, "acp-latest", resend_id="acp-retry-generation",
    )
    consume_pending_input_recovery(
        db, "acp-latest", resend_id="acp-retry-generation",
        provider_response_received=True,
    )
    assert get_surface_pending_input_recovery(db, surface)["admission_id"] == "acp-older"


def _provider_test_agent(db, *, transport):
    return SimpleNamespace(
        session_id="head", _session_db=db,
        _pending_input_recovery_claim_id="admission-provider",
        _pending_input_recovery_resend_id="resend-provider",
        _disable_streaming=False, _has_stream_consumers=lambda: True,
        base_url="", provider="openai", model="test-model", platform="cli",
        api_mode="chat_completions", _model_request_active=None,
        _pending_redirect_lock=None, _pending_redirect=False,
        _has_pending_redirect=lambda: False,
        _interruptible_streaming_api_call=transport,
        thinking_callback=None,
    )


def _stage_provider_recovery(db):
    db.create_session("head", source="cli")
    message = _user("admission-provider")
    stage_pending_input_recovery(
        db, accepted_input=_accepted(), live_messages=[message], session_ids=["head"],
        admission_id="admission-provider", surface_key="cli:surface",
        accepted_input_durably_committed=False,
    )
    claim_pending_input_recovery(
        db, "admission-provider", resend_id="resend-provider",
    )


def _perform_provider_call(agent, *, api_kwargs=None):
    from agent.turn_api_call import perform_api_call

    api_kwargs = api_kwargs or {"messages": []}
    return perform_api_call(
        agent, api_kwargs=api_kwargs, _original_api_kwargs=api_kwargs,
        _llm_middleware_trace=[], _moa_prepared_request=None,
        _retry=SimpleNamespace(restart_with_redirected_messages=False),
        thinking_spinner=None, retry_count=0, api_call_count=1,
        api_request_id="request", effective_task_id="task", turn_id="turn",
        interrupted=False,
    )


def test_middleware_short_circuit_does_not_ack_or_mark_provider_dispatch(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    calls = []
    agent = _provider_test_agent(db, transport=lambda *_a, **_k: calls.append(True))
    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware",
        lambda request, next_call, **_context: {"synthetic": True},
    )

    verdict = _perform_provider_call(agent)

    record = get_pending_input_recovery(db, "admission-provider")
    assert verdict.provider_call_completed is False
    assert verdict.recovery_returned_without_dispatch is True
    assert verdict.recovery_resend_id == "resend-provider"
    assert calls == []
    assert record["status"] == "pending"
    assert record["previous_resend_id"] == "resend-provider"
    assert "dispatch_started_at" not in record
    assert agent._pending_input_recovery_claim_id is None
    assert agent._pending_input_recovery_resend_id is None


def test_relay_managed_short_circuit_is_not_provider_entry(tmp_path, monkeypatch):
    """Relay may satisfy execution without invoking the physical callback."""
    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    calls = []
    agent = _provider_test_agent(
        db, transport=lambda *_a, **_k: calls.append(True),
    )
    agent._disable_streaming = True
    agent._interruptible_api_call = lambda *_a, **_k: calls.append(True)
    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware",
        lambda request, next_call, **_context: next_call(request),
    )
    monkeypatch.setattr(
        "agent.relay_llm.execute",
        lambda request, callback, **_context: {"managed": "synthetic"},
    )

    verdict = _perform_provider_call(agent)

    record = get_pending_input_recovery(db, "admission-provider")
    assert verdict.provider_call_completed is False
    assert verdict.recovery_returned_without_dispatch is True
    assert verdict.response == {"managed": "synthetic"}
    assert calls == []
    assert record["status"] == "pending"
    assert "dispatch_started_at" not in record


def test_retry_loop_discards_synthetic_response_and_live_resend_after_short_circuit(
    tmp_path, monkeypatch,
):
    from dataclasses import dataclass

    from agent import conversation_loop as loop_module
    from agent.pending_input_recovery import return_pending_input_recovery_without_dispatch
    from agent.turn_api_call import ApiCallVerdict

    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    resend = _user("resend-provider", timestamp=456.0)
    db.append_message(
        "head", "user", resend["content"], timestamp=resend["timestamp"],
        display_metadata=resend["display_metadata"],
    )
    return_pending_input_recovery_without_dispatch(
        db, "admission-provider", resend_id="resend-provider",
    )

    @dataclass
    class _Continue:
        action: str = "continue"

    monkeypatch.setattr(loop_module, "nous_rate_limit_guard", lambda agent: _Continue())
    monkeypatch.setattr(loop_module, "build_api_request", lambda agent: _Continue())
    monkeypatch.setattr(
        loop_module,
        "perform_api_call",
        lambda agent: ApiCallVerdict(
            action="fallthrough", response={"synthetic": True},
            thinking_spinner=None, interrupted=False,
            provider_call_completed=False,
            recovery_returned_without_dispatch=True,
            recovery_resend_id="resend-provider",
        ),
    )
    agent = SimpleNamespace(
        _session_messages=[resend], _pending_cli_user_message=resend,
    )
    state = SimpleNamespace(
        retry_count=0, max_retries=1, messages=[resend],
        conversation_history=[resend], api_call_count=1,
    )

    result = loop_module._run_api_retry_loop(agent, state)

    assert result["turn_exit_reason"] == "pending_input_not_dispatched"
    assert result["provider_call_sent"] is False
    assert result["messages"] == []
    assert state.conversation_history == []
    assert agent._session_messages == []
    assert agent._pending_cli_user_message is None
    assert db.get_messages_as_conversation("head") == []


def test_restart_does_not_treat_synthetic_assistant_as_provider_return(tmp_path):
    """A no-callback response pair is quarantined, never auto-acknowledged."""
    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    resend = _user(
        "resend-provider", text="finish the deployment", timestamp=456.0,
    )
    db.append_message(
        "head", "user", resend["content"], timestamp=resend["timestamp"],
        display_metadata=resend["display_metadata"],
    )
    db.append_message("head", "assistant", "synthetic middleware response")
    history = db.get_messages_as_conversation("head")

    reconciled, removed = reconcile_pending_input_recovery_rows(db, history)

    assert removed == 1
    assert reconciled == []
    assert db.get_messages_as_conversation("head") == []
    record = get_pending_input_recovery(db, "admission-provider")
    assert record["status"] == "claimed"
    assert record.get("provider_response_received") is not True
    assert record.get("dispatch_state") not in {"started", "uncertain"}


def test_failed_transport_and_quarantine_plus_synthetic_middleware_never_auto_consumes(
    tmp_path, monkeypatch,
):
    """Started dispatch plus assistant adjacency is still not return proof."""
    from agent import pending_input_recovery as recovery_module

    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    resend = _user("resend-provider", timestamp=456.0)
    db.append_message(
        "head", "user", resend["content"], timestamp=resend["timestamp"],
        display_metadata=resend["display_metadata"],
    )

    def _transport(*_args, **_kwargs):
        raise OSError("socket failed")

    agent = _provider_test_agent(db, transport=_transport)
    monkeypatch.setattr(
        recovery_module, "quarantine_pending_input_recovery_dispatch",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("sqlite busy")),
    )

    def _synthetic_on_error(request, next_call, **_context):
        try:
            return next_call(request)
        except PendingInputDispatchUncertainError:
            return {"synthetic": True}

    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware", _synthetic_on_error,
    )

    with pytest.raises(PendingInputDispatchUncertainError):
        _perform_provider_call(agent)

    reconciled, removed = reconcile_pending_input_recovery_rows(
        db, db.get_messages_as_conversation("head"),
    )

    assert removed == 1
    assert reconciled == []
    assert db.get_messages_as_conversation("head") == []
    record = get_pending_input_recovery(db, "admission-provider")
    assert record["status"] == "claimed"
    assert record["dispatch_state"] == "uncertain"
    assert "provider_returned_at" not in record


def test_relay_retry_after_uncertain_dispatch_never_enters_transport_twice(
    tmp_path, monkeypatch,
):
    """A Relay intercept cannot turn one recovered claim into two sends."""
    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    transport_calls = []

    def _transport(*_args, **_kwargs):
        transport_calls.append(True)
        if len(transport_calls) == 1:
            raise OSError("first socket outcome unknown")
        return {"would": "duplicate"}

    agent = _provider_test_agent(db, transport=lambda *_a, **_k: None)
    agent._disable_streaming = True
    agent._interruptible_api_call = _transport
    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware",
        lambda request, next_call, **_context: next_call(request),
    )

    def _retrying_relay(request, callback, **_context):
        first_error = None
        try:
            callback(request)
        except PendingInputDispatchUncertainError as exc:
            first_error = exc
        try:
            return callback(request)
        except PendingInputDispatchUncertainError as exc:
            assert exc is first_error
            return {"managed": "synthetic-after-retry"}

    monkeypatch.setattr("agent.relay_llm.execute", _retrying_relay)

    with pytest.raises(PendingInputDispatchUncertainError):
        _perform_provider_call(agent)

    assert transport_calls == [True]
    record = get_pending_input_recovery(db, "admission-provider")
    assert record["status"] == "claimed"
    assert record["dispatch_state"] == "uncertain"
    assert "provider_returned_at" not in record


@pytest.mark.parametrize("swallow_marker_error", [False, True])
def test_dispatch_marker_failure_never_enters_transport_or_accepts_wrapper_result(
    tmp_path, monkeypatch, swallow_marker_error,
):
    """A failed durable start marker is a definitive no-dispatch terminal."""
    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    transport_calls = []
    agent = _provider_test_agent(db, transport=lambda *_a, **_k: None)
    agent._disable_streaming = True
    agent._interruptible_api_call = (
        lambda *_a, **_k: transport_calls.append(True)
    )
    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware",
        lambda request, next_call, **_context: next_call(request),
    )
    monkeypatch.setattr(
        "agent.turn_api_call._mark_pending_input_recovery_dispatch_started",
        lambda _agent: (_ for _ in ()).throw(OSError("dispatch marker unavailable")),
    )

    def _relay(request, callback, **_context):
        try:
            return callback(request)
        except OSError:
            if swallow_marker_error:
                return {"managed": "must-be-discarded"}
            raise

    monkeypatch.setattr("agent.relay_llm.execute", _relay)

    verdict = _perform_provider_call(agent)

    assert transport_calls == []
    assert verdict.provider_call_completed is False
    assert verdict.recovery_returned_without_dispatch is True
    assert verdict.recovery_resend_id == "resend-provider"
    assert verdict.response == (
        {"managed": "must-be-discarded"} if swallow_marker_error else None
    )
    record = get_pending_input_recovery(db, "admission-provider")
    assert record["status"] == "pending"
    assert record["previous_resend_id"] == "resend-provider"
    assert "dispatch_started_at" not in record
    assert agent._pending_input_recovery_claim_id is None
    assert agent._pending_input_recovery_resend_id is None


def test_relay_repeated_callback_after_success_reuses_one_physical_result(
    tmp_path, monkeypatch,
):
    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    transport_calls = []
    response = {"ok": True}

    def _transport(*_args, **_kwargs):
        transport_calls.append(True)
        return response

    agent = _provider_test_agent(db, transport=lambda *_a, **_k: None)
    agent._disable_streaming = True
    agent._interruptible_api_call = _transport
    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware",
        lambda request, next_call, **_context: next_call(request),
    )

    def _relay(request, callback, **_context):
        first = callback(request)
        second = callback(request)
        assert second is first
        return second

    monkeypatch.setattr("agent.relay_llm.execute", _relay)

    verdict = _perform_provider_call(agent)

    assert transport_calls == [True]
    assert verdict.response is response
    assert verdict.provider_call_completed is True
    assert verdict.recovery_returned_without_dispatch is False
    record = get_pending_input_recovery(db, "admission-provider")
    assert record["status"] == "claimed"
    assert record["provider_return_resend_id"] == "resend-provider"


def test_relay_concurrent_callbacks_share_one_recovered_physical_result(
    tmp_path, monkeypatch,
):
    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    transport_calls = []
    transport_entered = threading.Event()
    release_transport = threading.Event()
    response = {"ok": "one physical call"}

    def _transport(*_args, **_kwargs):
        transport_calls.append(True)
        transport_entered.set()
        assert release_transport.wait(timeout=2)
        return response

    agent = _provider_test_agent(db, transport=lambda *_a, **_k: None)
    agent._disable_streaming = True
    agent._interruptible_api_call = _transport
    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware",
        lambda request, next_call, **_context: next_call(request),
    )

    def _relay(request, callback, **_context):
        results = []
        errors = []

        def _invoke():
            try:
                results.append(callback(request))
            except BaseException as exc:  # assertion evidence, re-raised below
                errors.append(exc)

        first = threading.Thread(target=_invoke)
        second = threading.Thread(target=_invoke)
        first.start()
        assert transport_entered.wait(timeout=2)
        second.start()
        assert transport_calls == [True]
        release_transport.set()
        first.join(timeout=2)
        second.join(timeout=2)
        assert not first.is_alive() and not second.is_alive()
        assert errors == []
        assert results == [response, response]
        return results[-1]

    monkeypatch.setattr("agent.relay_llm.execute", _relay)

    verdict = _perform_provider_call(agent)

    assert transport_calls == [True]
    assert verdict.response is response
    assert verdict.provider_call_completed is True
    record = get_pending_input_recovery(db, "admission-provider")
    assert record["status"] == "claimed"
    assert record["provider_return_resend_id"] == "resend-provider"


def test_wrapper_error_after_physical_success_uses_result_without_retry(
    tmp_path, monkeypatch,
):
    """Post-processing failure cannot turn a returned recovery into send two."""
    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    transport_calls = []
    physical = {"ok": "physical"}
    agent = _provider_test_agent(
        db,
        transport=lambda *_a, **_k: transport_calls.append(True) or physical,
    )

    def _raise_after_callback(request, next_call, **_context):
        next_call(request)
        raise RuntimeError("middleware post-processing failed")

    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware",
        _raise_after_callback,
    )

    verdict = _perform_provider_call(agent)

    assert transport_calls == [True]
    assert verdict.provider_call_completed is True
    assert verdict.response is physical
    record = get_pending_input_recovery(db, "admission-provider")
    assert record["status"] == "claimed"
    assert record["provider_return_resend_id"] == "resend-provider"


def test_wrapper_cancellation_after_physical_success_settles_gate_then_propagates(
    tmp_path, monkeypatch,
):
    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    transport_calls = []
    agent = _provider_test_agent(
        db,
        transport=lambda *_a, **_k: transport_calls.append(True) or {"ok": True},
    )

    def _cancel_after_callback(request, next_call, **_context):
        next_call(request)
        raise KeyboardInterrupt()

    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware",
        _cancel_after_callback,
    )

    with pytest.raises(KeyboardInterrupt):
        _perform_provider_call(agent)

    assert transport_calls == [True]
    record = get_pending_input_recovery(db, "admission-provider")
    assert record["provider_return_resend_id"] == "resend-provider"
    assert agent._pending_input_recovery_claim_id is None
    assert agent._pending_input_recovery_resend_id is None
    assert agent._pending_input_recovery_provider_completed == (
        "admission-provider", "resend-provider",
    )


def test_provider_return_marker_cancellation_publishes_terminal_state_before_raise(
    tmp_path, monkeypatch,
):
    """A BaseException in durable marker I/O cannot strand latch waiters."""
    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    transport_calls = []
    agent = _provider_test_agent(
        db,
        transport=lambda *_a, **_k: transport_calls.append(True) or {"ok": True},
    )
    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware",
        lambda request, next_call, **_context: next_call(request),
    )
    monkeypatch.setattr(
        "agent.pending_input_recovery.mark_pending_input_recovery_provider_returned",
        lambda *_a, **_k: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        _perform_provider_call(agent)

    assert transport_calls == [True]
    record = get_pending_input_recovery(db, "admission-provider")
    assert record["status"] == "claimed"
    assert record["dispatch_state"] == "started"
    assert "provider_returned_at" not in record
    assert agent._pending_input_recovery_claim_id is None
    assert agent._pending_input_recovery_resend_id is None
    assert agent._pending_input_recovery_provider_completed == (
        "admission-provider", "resend-provider",
    )


def test_detached_callback_completion_wins_over_premature_synthetic(
    tmp_path, monkeypatch,
):
    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    transport_entered = threading.Event()
    release_transport = threading.Event()
    worker_done = threading.Event()
    transport_calls = []
    callback_results = []
    physical = {"ok": "detached-physical"}

    def _transport(*_args, **_kwargs):
        transport_calls.append(True)
        transport_entered.set()
        assert release_transport.wait(timeout=2)
        return physical

    agent = _provider_test_agent(db, transport=_transport)

    def _detaching_wrapper(request, next_call, **_context):
        def _run():
            try:
                callback_results.append(next_call(request))
            finally:
                worker_done.set()

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        assert transport_entered.wait(timeout=2)
        threading.Timer(0.05, release_transport.set).start()
        return {"synthetic": "premature"}

    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware", _detaching_wrapper,
    )

    verdict = _perform_provider_call(agent)

    assert worker_done.wait(timeout=2)
    assert transport_calls == [True]
    assert callback_results == [physical]
    assert verdict.provider_call_completed is True
    assert verdict.response is physical
    record = get_pending_input_recovery(db, "admission-provider")
    assert record["provider_return_resend_id"] == "resend-provider"
    assert record["provider_return_generation"] == record["claim_generation"]
    assert agent._pending_input_recovery_claim_id is None
    assert agent._pending_input_recovery_resend_id is None
    assert agent._pending_input_recovery_provider_completed == (
        "admission-provider", "resend-provider",
    )


def test_callback_arriving_after_wrapper_return_never_enters_transport(
    tmp_path, monkeypatch,
):
    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    allow_callback = threading.Event()
    worker_done = threading.Event()
    callback_errors = []
    transport_calls = []
    agent = _provider_test_agent(
        db, transport=lambda *_a, **_k: transport_calls.append(True),
    )

    def _late_wrapper(request, next_call, **_context):
        def _run():
            try:
                assert allow_callback.wait(timeout=2)
                next_call(request)
            except BaseException as exc:
                callback_errors.append(exc)
            finally:
                worker_done.set()

        threading.Thread(target=_run, daemon=True).start()
        return {"synthetic": "too-early"}

    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware", _late_wrapper,
    )

    verdict = _perform_provider_call(agent)
    allow_callback.set()

    assert worker_done.wait(timeout=2)
    assert transport_calls == []
    assert callback_errors
    assert verdict.provider_call_completed is False
    assert verdict.recovery_returned_without_dispatch is True
    record = get_pending_input_recovery(db, "admission-provider")
    assert record["status"] == "pending"
    assert "dispatch_started_at" not in record


def test_synthetic_mock_recovery_attributes_never_activate_dispatch_boundary():
    from agent.turn_api_call import _pending_recovery_dispatch_ids

    agent = SimpleNamespace(
        _pending_input_recovery_claim_id=MagicMock(),
        _pending_input_recovery_resend_id=MagicMock(),
    )

    assert _pending_recovery_dispatch_ids(agent) == (None, None)


def test_terminal_provider_return_marks_and_defers_consume_until_durable_pair(
    tmp_path, monkeypatch,
):
    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    agent = _provider_test_agent(db, transport=lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware",
        lambda request, next_call, **_context: next_call(request),
    )

    verdict = _perform_provider_call(agent)

    record = get_pending_input_recovery(db, "admission-provider")
    assert verdict.provider_call_completed is True
    assert record["status"] == "claimed"
    assert record["dispatch_state"] == "started"
    assert record["provider_return_resend_id"] == "resend-provider"
    assert record["provider_return_generation"] == record["claim_generation"]
    assert agent._pending_input_recovery_claim_id is None
    assert agent._pending_input_recovery_provider_completed == (
        "admission-provider", "resend-provider",
    )


def test_invalid_recovered_physical_return_is_fenced_from_retry(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    calls = []
    agent = _provider_test_agent(
        db, transport=lambda *_a, **_k: calls.append(True) or None,
    )
    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware",
        lambda request, next_call, **_context: next_call(request),
    )

    verdict = _perform_provider_call(agent)

    assert calls == [True]
    assert verdict.provider_call_completed is True
    assert verdict.recovered_provider_call_completed is True
    record = get_pending_input_recovery(db, "admission-provider")
    assert record["provider_return_resend_id"] == "resend-provider"


def test_retry_loop_terminates_invalid_recovered_response_without_fallback(
    tmp_path, monkeypatch,
):
    from dataclasses import dataclass

    from agent import conversation_loop as loop_module
    from agent.turn_api_call import ApiCallVerdict

    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    resend = _user("resend-provider", timestamp=456.0)
    db.append_message(
        "head", "user", resend["content"], timestamp=resend["timestamp"],
        display_metadata=resend["display_metadata"],
    )
    mark_pending_input_recovery_dispatch_started(
        db, "admission-provider", resend_id="resend-provider",
    )
    mark_pending_input_recovery_provider_returned(
        db, "admission-provider", resend_id="resend-provider",
    )
    calls = []

    @dataclass
    class _Continue:
        action: str = "continue"

    monkeypatch.setattr(loop_module, "nous_rate_limit_guard", lambda agent: _Continue())
    monkeypatch.setattr(loop_module, "build_api_request", lambda agent: _Continue())

    def _physical_once(agent):
        calls.append(True)
        return ApiCallVerdict(
            action="fallthrough", response=None, thinking_spinner=None,
            interrupted=False, provider_call_completed=True,
            recovered_provider_call_completed=True,
        )

    monkeypatch.setattr(loop_module, "perform_api_call", _physical_once)
    monkeypatch.setattr(
        "agent.turn_recovery.validate_response_shape",
        lambda _agent, _response: (True, ["empty response"]),
    )
    monkeypatch.setattr(
        loop_module, "check_api_response",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("invalid recovery entered ordinary retry/fallback")
        ),
    )
    agent = SimpleNamespace(
        session_id="head", _session_db=db,
        _active_session_turn_lease_holder=None,
        _session_messages=[resend], _pending_cli_user_message=resend,
        _pending_input_recovery_claim_id=None,
        _pending_input_recovery_resend_id=None,
        _pending_input_recovery_provider_completed=(
            "admission-provider", "resend-provider",
        ),
    )
    state = SimpleNamespace(
        retry_count=0, max_retries=3, messages=[resend],
        conversation_history=[resend], api_call_count=1, response=None,
    )

    result = loop_module._run_api_retry_loop(agent, state)

    assert calls == [True]
    assert result["turn_exit_reason"] == "recovered_provider_response_invalid"
    assert result["provider_call_sent"] is True
    assert result["messages"] == []
    assert db.get_messages_as_conversation("head") == []
    assert get_pending_input_recovery(db, "admission-provider")["status"] == "consumed"
    assert agent._pending_input_recovery_provider_completed is None


def test_classic_provider_return_crash_quarantines_orphan_before_unrelated_turn(
    tmp_path, monkeypatch,
):
    """Provider return without durable assistant never lets B ride with later C."""
    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    resend = _user(
        "resend-provider", text="B ALREADY ACCEPTED", timestamp=456.0,
    )
    db.append_message(
        "head", "user", resend["content"], timestamp=resend["timestamp"],
        display_metadata=resend["display_metadata"],
    )
    first_transport_calls = []
    agent = _provider_test_agent(
        db,
        transport=lambda *_a, **_k: first_transport_calls.append("B") or {"ok": True},
    )
    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware",
        lambda request, next_call, **_context: next_call(request),
    )

    verdict = _perform_provider_call(
        agent, api_kwargs={"messages": [{"role": "user", "content": resend["content"]}]},
    )
    assert verdict.provider_call_completed is True
    assert first_transport_calls == ["B"]
    before_restart = get_pending_input_recovery(db, "admission-provider")
    assert before_restart["status"] == "claimed"
    assert before_restart["provider_return_resend_id"] == "resend-provider"

    restarted, removed = reconcile_pending_input_recovery_rows(
        db, db.get_messages_as_conversation("head"),
    )
    assert removed == 1
    assert restarted == []
    assert db.get_messages_as_conversation("head") == []
    after_restart = get_pending_input_recovery(db, "admission-provider")
    assert after_restart["status"] == "claimed"
    assert after_restart["dispatch_state"] == "uncertain"
    assert after_restart["provider_return_resend_id"] == "resend-provider"

    wire_requests = []
    ordinary = _provider_test_agent(
        db,
        transport=lambda request, **_kwargs: wire_requests.append(request) or {"ok": True},
    )
    ordinary._pending_input_recovery_claim_id = None
    ordinary._pending_input_recovery_resend_id = None
    new_request = {"messages": [{"role": "user", "content": "C NEW REQUEST"}]}
    _perform_provider_call(ordinary, api_kwargs=new_request)
    assert wire_requests == [new_request]
    assert "B ALREADY ACCEPTED" not in str(wire_requests)


@pytest.mark.parametrize("error", [RuntimeError("network lost"), InterruptedError()])
def test_terminal_provider_raise_retains_uncertain_non_replayable_claim(
    tmp_path, monkeypatch, error,
):
    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)

    def _raise(*_args, **_kwargs):
        raise error

    agent = _provider_test_agent(db, transport=_raise)
    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware",
        lambda request, next_call, **_context: next_call(request),
    )

    with pytest.raises(PendingInputDispatchUncertainError):
        _perform_provider_call(agent)

    record = get_pending_input_recovery(db, "admission-provider")
    assert record["status"] == "claimed"
    assert record["dispatch_state"] == "uncertain"
    assert agent._pending_input_recovery_claim_id is None
    assert agent._pending_input_recovery_resend_id is None
    monkeypatch.setattr(
        "agent.pending_input_recovery.time.time",
        lambda: float(record["claimed_at"]) + 31.0,
    )
    with pytest.raises(PendingInputRecoveryError, match="uncertain provider dispatch"):
        claim_pending_input_recovery(
            db, "admission-provider", resend_id="unsafe-replay",
        )


def test_uncertain_dispatch_does_not_attribute_later_ordinary_turn(
    tmp_path, monkeypatch,
):
    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    resend = _user(
        "resend-provider", text="B OUTCOME UNKNOWN", timestamp=456.0,
    )
    db.append_message(
        "head", "user", resend["content"], timestamp=resend["timestamp"],
        display_metadata=resend["display_metadata"],
    )
    agent = _provider_test_agent(
        db,
        transport=lambda *_a, **_k: (_ for _ in ()).throw(
            OSError("socket outcome unknown")
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware",
        lambda request, next_call, **_context: next_call(request),
    )

    with pytest.raises(PendingInputDispatchUncertainError):
        _perform_provider_call(agent)

    assert agent._pending_input_recovery_claim_id is None
    assert agent._pending_input_recovery_resend_id is None
    cleaned, removed = reconcile_pending_input_recovery_rows(
        db, db.get_messages_as_conversation("head"),
    )
    # Immediate quarantine may already have withdrawn B; reconciliation is
    # idempotent and must still expose an empty executable transcript.
    assert removed in {0, 1}
    assert cleaned == []
    wire_requests = []
    agent._interruptible_streaming_api_call = (
        lambda request, **_kwargs: wire_requests.append(request) or {"ok": True}
    )
    request = {"messages": [{"role": "user", "content": "C NEW REQUEST"}]}
    _perform_provider_call(agent, api_kwargs=request)
    assert wire_requests == [request]
    assert "B OUTCOME UNKNOWN" not in str(wire_requests)


def test_provider_return_is_not_retried_when_ack_persistence_fails(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    calls = []

    def _transport(*_args, **_kwargs):
        calls.append(True)
        return {"ok": True}

    agent = _provider_test_agent(db, transport=_transport)
    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware",
        lambda request, next_call, **_context: next_call(request),
    )
    monkeypatch.setattr(
        "agent.pending_input_recovery.consume_pending_input_recovery",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    verdict = _perform_provider_call(agent)

    record = get_pending_input_recovery(db, "admission-provider")
    assert verdict.provider_call_completed is True
    assert calls == [True]
    assert record["status"] == "claimed"
    assert record["dispatch_state"] == "started"


def test_ack_failure_stops_recovery_tracking_before_later_tool_call_error(
    tmp_path, monkeypatch,
):
    """Only the first physical response belongs to the recovered admission."""
    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    agent = _provider_test_agent(db, transport=lambda *_a, **_k: {"tool": True})
    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware",
        lambda request, next_call, **_context: next_call(request),
    )
    monkeypatch.setattr(
        "agent.pending_input_recovery.consume_pending_input_recovery",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    first = _perform_provider_call(agent)

    assert first.provider_call_completed is True
    assert agent._pending_input_recovery_claim_id is None
    assert agent._pending_input_recovery_resend_id is None
    assert agent._pending_input_recovery_provider_completed == (
        "admission-provider", "resend-provider",
    )
    resend = _user("resend-provider", timestamp=456.0)
    db.append_message(
        "head", "user", resend["content"], timestamp=resend["timestamp"],
        display_metadata=resend["display_metadata"],
    )
    db.append_message(
        "head", "assistant", "calling tool",
        tool_calls=[{
            "id": "call-1", "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }],
    )
    db.append_message(
        "head", "tool", "tool output", tool_call_id="call-1",
    )

    def later_error(*_args, **_kwargs):
        raise RuntimeError("later tool-loop provider failure")

    agent._interruptible_streaming_api_call = later_error
    with pytest.raises(RuntimeError, match="later tool-loop"):
        _perform_provider_call(agent)

    record = get_pending_input_recovery(db, "admission-provider")
    assert record["dispatch_state"] == "started"
    assert record["status"] == "claimed"
    history = db.get_messages_as_conversation("head")
    reconciled, removed = reconcile_pending_input_recovery_rows(db, history)
    assert removed == 0
    assert reconciled == history
    assert [message["role"] for message in reconciled] == [
        "user", "assistant", "tool",
    ]
    assert get_pending_input_recovery(db, "admission-provider")["status"] == "consumed"


def test_transient_ack_failure_retries_after_answer_persistence_without_replay(
    tmp_path, monkeypatch,
):
    from agent import pending_input_recovery as recovery_module
    from agent.turn_api_call import _ack_pending_input_recovery_after_provider_response

    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    resend = _user("resend-provider", text="finish the deployment", timestamp=456.0)
    db.append_message(
        "head", "user", resend["content"], timestamp=resend["timestamp"],
        display_metadata=resend["display_metadata"],
    )
    agent = _provider_test_agent(db, transport=lambda *_a, **_k: {"ok": True})
    real_consume = recovery_module.consume_pending_input_recovery
    attempts = {"count": 0}

    def fail_once(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise OSError("temporary sqlite failure")
        return real_consume(*args, **kwargs)

    monkeypatch.setattr(recovery_module, "consume_pending_input_recovery", fail_once)
    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware",
        lambda request, next_call, **_context: next_call(request),
    )

    verdict = _perform_provider_call(agent)
    assert verdict.provider_call_completed is True
    assert get_pending_input_recovery(db, "admission-provider")["status"] == "claimed"
    db.append_message("head", "assistant", "deployment finished")

    assert _ack_pending_input_recovery_after_provider_response(agent) is False
    assert _ack_pending_input_recovery_after_provider_response(agent) is True
    assert get_pending_input_recovery(db, "admission-provider")["status"] == "consumed"
    assert [message["content"] for message in db.get_messages_as_conversation("head")] == [
        "finish the deployment", "deployment finished",
    ]
    assert attempts["count"] == 2


def test_restart_reconciliation_preserves_answered_pair_after_ack_failure(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    resend = _user("resend-provider", text="finish the deployment", timestamp=456.0)
    db.append_message(
        "head", "user", resend["content"], timestamp=resend["timestamp"],
        display_metadata=resend["display_metadata"],
    )
    mark_pending_input_recovery_dispatch_started(
        db, "admission-provider", resend_id="resend-provider",
    )
    mark_pending_input_recovery_provider_returned(
        db, "admission-provider", resend_id="resend-provider",
    )
    db.append_message("head", "assistant", "deployment finished")
    history = db.get_messages_as_conversation("head")

    reconciled, removed = reconcile_pending_input_recovery_rows(db, history)

    assert removed == 0
    assert reconciled == history
    assert [message["content"] for message in reconciled] == [
        "finish the deployment", "deployment finished",
    ]
    record = get_pending_input_recovery(db, "admission-provider")
    assert record["status"] == "consumed"
    assert record["resolution"] == "durable_response_reconciled"


def test_delivered_resolution_preserves_answered_pair_and_retry_refuses_it(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    resend = _user("resend-provider", text="finish the deployment", timestamp=456.0)
    db.append_message(
        "head", "user", resend["content"], timestamp=resend["timestamp"],
        display_metadata=resend["display_metadata"],
    )
    mark_pending_input_recovery_dispatch_started(
        db, "admission-provider", resend_id="resend-provider",
    )
    mark_pending_input_recovery_provider_returned(
        db, "admission-provider", resend_id="resend-provider",
    )
    db.append_message("head", "assistant", "deployment finished")

    with pytest.raises(PendingInputRecoveryError, match="proven provider return"):
        resolve_pending_input_recovery_dispatch(
            db, "admission-provider", resend_id="resend-provider",
            claim_generation=1, resolution="retry",
        )
    record, changed = resolve_pending_input_recovery_dispatch(
        db, "admission-provider", resend_id="resend-provider",
        claim_generation=1, resolution="delivered",
    )

    assert changed is True
    assert record["status"] == "consumed"
    assert [message["content"] for message in db.get_messages_as_conversation("head")] == [
        "finish the deployment", "deployment finished",
    ]


def test_crash_in_provider_dispatch_quarantines_tail_before_unrelated_turn(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    resend = _user(
        "resend-provider", text="possibly delivered intent", timestamp=456.0,
    )
    db.append_message(
        "head", "user", resend["content"], timestamp=resend["timestamp"],
        display_metadata=resend["display_metadata"],
    )
    mark_pending_input_recovery_dispatch_started(
        db, "admission-provider", resend_id="resend-provider",
    )
    crash_history = db.get_messages_as_conversation("head")
    unrelated = _user("new-unrelated", text="unrelated next turn", timestamp=789.0)

    provider_history, removed = reconcile_pending_input_recovery_rows(
        db, [*crash_history, unrelated],
    )

    assert removed == 1
    assert [message["content"] for message in provider_history] == ["unrelated next turn"]
    assert db.get_messages_as_conversation("head") == []
    record = get_pending_input_recovery(db, "admission-provider")
    assert record["status"] == "claimed"
    assert record["dispatch_state"] == "uncertain"
    assert record["text"] == "finish the deployment"


@pytest.mark.parametrize("resolution", ["delivered", "retry"])
def test_uncertain_dispatch_requires_explicit_idempotent_resolution(
    tmp_path, resolution,
):
    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_provider_recovery(db)
    resend = _user(
        "resend-provider", text="possibly delivered intent", timestamp=456.0,
    )
    db.append_message(
        "head", "user", resend["content"], timestamp=resend["timestamp"],
        display_metadata=resend["display_metadata"],
    )
    mark_pending_input_recovery_dispatch_started(
        db, "admission-provider", resend_id="resend-provider",
    )
    with pytest.raises(PendingInputRecoveryError, match="matching uncertain dispatch"):
        resolve_pending_input_recovery_dispatch(
            db, "admission-provider", resend_id="resend-provider",
            claim_generation=2, resolution=resolution,
        )
    assert [message["content"] for message in db.get_messages_as_conversation("head")] == [
        "possibly delivered intent"
    ]

    record, changed = resolve_pending_input_recovery_dispatch(
        db, "admission-provider", resend_id="resend-provider",
        claim_generation=1, resolution=resolution,
    )

    assert changed is True
    assert record["resolution"] == resolution
    assert record["status"] == ("consumed" if resolution == "delivered" else "pending")
    assert db.get_messages_as_conversation("head") == []
    if resolution == "delivered":
        same, changed_again = resolve_pending_input_recovery_dispatch(
            db, "admission-provider", resend_id="resend-provider",
            claim_generation=1, resolution=resolution,
        )
        assert changed_again is False
        assert same["status"] == "consumed"
    else:
        claimed, claimed_now = claim_pending_input_recovery(
            db, "admission-provider", resend_id="operator-approved-retry",
        )
        assert claimed_now is True
        assert claimed["claim_generation"] == 2


def test_total_ceiling_poisoned_owner_cannot_block_atomic_recovery(tmp_path):
    """The host may return while a fence-cancelled provider call retains its lease.

    The exact same holder is safe for the recovery write because the poison fence
    has already made every later compression commit ineligible.  A different
    live compressor remains rejected by the SessionDB guard.
    """
    from agent.compression_facade import _report_compression_timeout
    from agent.conversation_compression import (
        CompressionCommitFence,
        run_compress_context_with_progress_timeout,
    )

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("head", source="cli")
    accepted = _user("admission-total-ceiling")
    db.append_message(
        "head", "user", accepted["content"], timestamp=accepted["timestamp"],
        display_metadata=accepted["display_metadata"],
    )
    agent = SimpleNamespace(
        session_id="head",
        _session_db=db,
        _session_persist_lock=threading.RLock(),
        _active_session_turn_lease_holder=None,
        _active_compression_lock_holder=None,
        _pending_cli_user_message=accepted,
        _persist_user_message_idx=0,
        _persist_user_message_override=None,
        _session_messages=[accepted],
        _persist_disabled=True,
        _inflight_turn_id="turn",
        _inflight_turn_session_id="head",
        platform="cli",
        context_compressor=SimpleNamespace(record_timeout_failure=lambda *_a, **_k: None),
        _emit_warning=lambda *_a, **_k: None,
        _touch_activity=lambda *_a, **_k: None,
        _last_compression_attempt_recorded=True,
    )
    holder = "pid=test:tid=worker:agent=test:nonce=poisoned"
    started = threading.Event()
    release = threading.Event()
    fence = CompressionCommitFence()
    timeout_cause = {"total_exhausted": False, "progress_observed": False}

    def worker(worker_fence):
        assert db.try_acquire_compression_lock("head", holder, ttl_seconds=60)
        agent._active_compression_lock_holder = holder
        worker_fence.bind_durable_lock_identity("head", holder)
        worker_fence.register_cancelled_lock_release(
            lambda: db.release_compression_lock("head", holder)
        )
        started.set()
        try:
            while not release.wait(0.005):
                worker_fence.touch_progress()
            assert worker_fence.begin_commit() is False
            return [accepted], "late"
        finally:
            db.release_compression_lock("head", holder)
            if agent._active_compression_lock_holder == holder:
                agent._active_compression_lock_holder = None

    def on_timeout_cause(total_exhausted, progress_observed, _timeout_fence):
        timeout_cause.update(
            total_exhausted=total_exhausted,
            progress_observed=progress_observed,
        )

    try:
        returned, _ = run_compress_context_with_progress_timeout(
            worker=worker,
            messages=[accepted],
            system_prompt_fallback="SYSTEM",
            idle_timeout_seconds=0.5,
            total_ceiling_seconds=0.6,
            on_timeout_cause=on_timeout_cause,
            on_timeout=lambda idle, waited, since_progress: _report_compression_timeout(
                agent,
                idle=idle,
                waited=waited,
                since_progress=since_progress,
                total_ceiling=0.6,
                poisoned_compression_lock_identity=fence.durable_lock_identity,
                **timeout_cause,
            ),
            fence=fence,
            telemetry_agent=agent,
            stall_fallback=False,
        )
        assert started.is_set()
        assert returned[0]["content"] == accepted["content"]
        assert fence.is_cancelled
        assert db.get_compression_lock_holder("head") == holder
        from agent.conversation_compression import (
            context_compression_timeout_poisoned_lock_identity,
        )
        assert context_compression_timeout_poisoned_lock_identity(agent) == (
            "head", holder,
        )

        from agent.turn_context import _fail_closed_after_preflight_timeout

        with pytest.raises(PreflightCompressionTimedOut) as raised:
            _fail_closed_after_preflight_timeout(
                agent,
                3_000,
                conversation_history=[accepted],
                live_messages=[accepted],
                accepted_input_durably_committed=True,
                accepted_input_session_ids=["head"],
            )
        exc = raised.value
        assert exc.poisoned_compression_lock_identity == ("head", holder)
        result = _preflight_timeout_result(agent, exc, _accepted())

        assert result["recovery_persisted"] is True
        assert result["api_calls"] == 0
        assert result["messages"] == []
        assert db.get_messages_as_conversation("head") == []
        assert get_pending_input_recovery(db, "admission-total-ceiling")["status"] == "pending"
        # The retained lease is not globally bypassed: another holder cannot
        # acquire it while the orphan is still alive.
        assert not db.try_acquire_compression_lock(
            "head", "pid=other:tid=other:agent=other:nonce=other", ttl_seconds=60,
        )
    finally:
        release.set()
        deadline = time.time() + 2
        while db.get_compression_lock_holder("head") is not None and time.time() < deadline:
            time.sleep(0.01)


def test_stale_total_timeout_capability_never_authorizes_replacement_holder(
    tmp_path,
):
    """A's timeout authority cannot be combined with B's later live lease."""
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("head", source="cli")
    accepted = _user("admission-stale-poison")
    db.append_message(
        "head", "user", accepted["content"], timestamp=accepted["timestamp"],
        display_metadata=accepted["display_metadata"],
    )
    holder_a = "pid=a:tid=a:agent=a:nonce=a"
    holder_b = "pid=b:tid=b:agent=b:nonce=b"
    assert db.try_acquire_compression_lock("head", holder_a, ttl_seconds=60)
    db.release_compression_lock("head", holder_a)
    assert db.get_compression_lock_holder("head") is None
    assert db.try_acquire_compression_lock("head", holder_b, ttl_seconds=60)
    agent = SimpleNamespace(
        session_id="head",
        _session_db=db,
        _session_persist_lock=threading.RLock(),
        _active_session_turn_lease_holder=None,
        # These stale/shared values deliberately describe B. They must not be
        # consulted when the typed exception carries A's exact capability.
        _active_compression_lock_holder=holder_b,
        _last_compression_timeout_total_exhausted=True,
        _pending_cli_user_message=accepted,
        _persist_user_message_idx=0,
        _persist_user_message_override=None,
        _session_messages=[accepted],
        _persist_disabled=True,
        _inflight_turn_id="turn",
        _inflight_turn_session_id="head",
        _last_compression_attempt_recorded=True,
        platform="cli",
    )
    exc = PreflightCompressionTimedOut(
        "A timed out before provider entry",
        conversation_history=[accepted],
        live_messages=[accepted],
        accepted_input_durably_committed=True,
        accepted_input_session_ids=["head"],
        poisoned_compression_lock_identity=("head", holder_a),
    )

    result = _preflight_timeout_result(agent, exc, _accepted())

    assert result["recovery_persisted"] is False
    assert result["api_calls"] == 0
    assert db.get_compression_lock_holder("head") == holder_b
    assert [
        message["content"] for message in db.get_messages_as_conversation("head")
    ] == [accepted["content"]]
    assert get_pending_input_recovery(db, "admission-stale-poison") is None


class _CodexRecoverySession:
    def __init__(self, outcome):
        self.outcome = outcome

    def run_turn(self, *, user_input):
        from agent.transports.codex_app_server_session import TurnResult

        if self.outcome == "startup_failed":
            return TurnResult(error="startup failed")
        self._before_turn_start_callback()
        if self.outcome == "turn_start_failed":
            return TurnResult(error="turn/start failed", turn_start_attempted=True)
        self._turn_start_accepted_callback()
        if self.outcome == "accepted_then_driver_raised":
            raise RuntimeError("driver failed after turn/start acceptance")
        return TurnResult(
            final_text="done", turn_id="turn", thread_id="thread",
            turn_start_attempted=True, turn_start_accepted=True,
        )

    def close(self):
        return None


def _codex_recovery_agent(db, outcome):
    return SimpleNamespace(
        session_id="head", _session_db=db,
        _session_messages=db.get_messages_as_conversation("head"),
        _pending_cli_user_message=None,
        _pending_input_recovery_claim_id="admission-provider",
        _pending_input_recovery_resend_id="resend-provider",
        _active_session_turn_lease_holder=None,
        compression_checkpoint_required=False,
        _codex_session=_CodexRecoverySession(outcome),
        _interrupt_requested=False, _interrupt_message=None,
        clear_interrupt=lambda: None,
    )


def _stage_codex_recovery(db):
    _stage_provider_recovery(db)
    resend = _user("resend-provider", text="finish the deployment")
    db.append_message(
        "head", "user", resend["content"], timestamp=resend["timestamp"],
        display_metadata=resend["display_metadata"],
    )


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_dispatch", "expected_active_rows"),
    [
        ("startup_failed", "pending", None, 0),
        ("turn_start_failed", "claimed", "uncertain", 0),
        ("accepted", "consumed", "started", 0),
    ],
)
def test_codex_recovery_settles_only_on_accepted_turn_start(
    tmp_path, monkeypatch, outcome, expected_status, expected_dispatch, expected_active_rows,
):
    from agent import codex_runtime

    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_codex_recovery(db)
    agent = _codex_recovery_agent(db, outcome)
    if outcome == "startup_failed":
        monkeypatch.setattr(
            codex_runtime, "_ensure_codex_session",
            lambda _agent: (_ for _ in ()).throw(RuntimeError("startup failed")),
        )
    else:
        monkeypatch.setattr(codex_runtime, "_ensure_codex_session", lambda _agent: None)
    monkeypatch.setattr(codex_runtime, "_persist_projected_messages", lambda *_a, **_k: None)
    monkeypatch.setattr(codex_runtime, "_finish_codex_turn", lambda *_a, **_k: {})

    result = codex_runtime.run_codex_app_server_turn(
        agent, user_message="finish the deployment",
        original_user_message="finish the deployment",
        messages=db.get_messages_as_conversation("head"),
        effective_task_id="task",
    )

    record = get_pending_input_recovery(db, "admission-provider")
    assert result["turn_start_accepted"] is (outcome == "accepted")
    assert record["status"] == expected_status
    assert record.get("dispatch_state") == expected_dispatch
    if outcome == "accepted":
        assert record["provider_return_resend_id"] == "resend-provider"
        assert record["provider_return_generation"] == record["claim_generation"]
    assert len(db.get_messages_as_conversation("head")) == expected_active_rows


def test_codex_accepted_turn_retries_transient_consume_after_projection(
    tmp_path, monkeypatch,
):
    from agent import codex_runtime
    from agent import pending_input_recovery as recovery_module

    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_codex_recovery(db)
    agent = _codex_recovery_agent(db, "accepted")
    monkeypatch.setattr(codex_runtime, "_ensure_codex_session", lambda _agent: None)
    real_consume = recovery_module.consume_pending_input_recovery
    attempts = {"count": 0}

    def _fail_once(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise OSError("temporary consume failure")
        return real_consume(*args, **kwargs)

    monkeypatch.setattr(recovery_module, "consume_pending_input_recovery", _fail_once)
    def _persist_answer(*_args, **_kwargs):
        db.append_message("head", "assistant", "done")
        return True

    monkeypatch.setattr(codex_runtime, "_persist_projected_messages", _persist_answer)
    monkeypatch.setattr(codex_runtime, "_finish_codex_turn", lambda *_a, **_k: {})

    result = codex_runtime.run_codex_app_server_turn(
        agent, user_message="finish the deployment",
        original_user_message="finish the deployment",
        messages=db.get_messages_as_conversation("head"),
        effective_task_id="task",
    )

    record = get_pending_input_recovery(db, "admission-provider")
    assert result["turn_start_accepted"] is True
    assert attempts["count"] == 2
    assert record["status"] == "consumed"
    assert record["provider_return_resend_id"] == "resend-provider"
    assert agent._pending_input_recovery_provider_completed is None


def test_codex_accepted_turn_is_settled_even_when_projection_flush_reports_failure(
    tmp_path, monkeypatch,
):
    """Definitive turn/start acceptance wins over a local projection gap."""
    from agent import codex_runtime

    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_codex_recovery(db)
    agent = _codex_recovery_agent(db, "accepted")
    monkeypatch.setattr(codex_runtime, "_ensure_codex_session", lambda _agent: None)
    monkeypatch.setattr(
        codex_runtime, "_persist_projected_messages", lambda *_a, **_k: False,
    )
    monkeypatch.setattr(codex_runtime, "_finish_codex_turn", lambda *_a, **_k: {})

    result = codex_runtime.run_codex_app_server_turn(
        agent, user_message="finish the deployment",
        original_user_message="finish the deployment",
        messages=db.get_messages_as_conversation("head"),
        effective_task_id="task",
    )

    record = get_pending_input_recovery(db, "admission-provider")
    assert result["turn_start_accepted"] is True
    assert result["completed"] is True
    assert result["final_response"] == "done"
    assert record["status"] == "consumed"
    assert record["provider_response_received"] is True
    assert record["provider_return_resend_id"] == "resend-provider"
    assert record["provider_return_generation"] == record["claim_generation"]
    assert record["resolution"] == "provider_return_without_durable_projection"
    assert db.get_messages_as_conversation("head") == []

    # A fresh unrelated turn cannot inherit or execute the accepted prompt.
    restarted, removed = reconcile_pending_input_recovery_rows(db, [])
    assert restarted == []
    assert removed == 0
    wire_requests = []
    ordinary = _provider_test_agent(
        db,
        transport=lambda request, **_kwargs: wire_requests.append(request) or {"ok": True},
    )
    ordinary._pending_input_recovery_claim_id = None
    ordinary._pending_input_recovery_resend_id = None
    request = {"messages": [{"role": "user", "content": "C NEW REQUEST"}]}
    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware",
        lambda value, next_call, **_context: next_call(value),
    )
    _perform_provider_call(ordinary, api_kwargs=request)
    assert wire_requests == [request]
    assert "finish the deployment" not in str(wire_requests)


def test_codex_marker_write_failure_does_not_abort_accepted_turn(
    tmp_path, monkeypatch,
):
    from agent import codex_runtime
    from agent import pending_input_recovery as recovery_module

    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_codex_recovery(db)
    agent = _codex_recovery_agent(db, "accepted")
    monkeypatch.setattr(codex_runtime, "_ensure_codex_session", lambda _agent: None)
    real_mark = recovery_module.mark_pending_input_recovery_provider_returned
    marker_attempts = {"count": 0}

    def _fail_marker_once(*args, **kwargs):
        marker_attempts["count"] += 1
        if marker_attempts["count"] == 1:
            raise OSError("marker unavailable")
        return real_mark(*args, **kwargs)

    monkeypatch.setattr(
        recovery_module, "mark_pending_input_recovery_provider_returned",
        _fail_marker_once,
    )

    def _persist_answer(*_args, **_kwargs):
        db.append_message("head", "assistant", "done")
        return True

    monkeypatch.setattr(codex_runtime, "_persist_projected_messages", _persist_answer)
    monkeypatch.setattr(codex_runtime, "_finish_codex_turn", lambda *_a, **_k: {})

    result = codex_runtime.run_codex_app_server_turn(
        agent, user_message="finish the deployment",
        original_user_message="finish the deployment",
        messages=db.get_messages_as_conversation("head"),
        effective_task_id="task",
    )

    record = get_pending_input_recovery(db, "admission-provider")
    assert result["turn_start_accepted"] is True
    assert result["completed"] is True
    assert record["status"] == "consumed"
    assert record["provider_response_received"] is True
    assert marker_attempts["count"] == 2
    assert record["provider_return_resend_id"] == "resend-provider"


def test_codex_driver_raise_after_accepted_start_keeps_inert_uncertainty(
    tmp_path, monkeypatch,
):
    from agent import codex_runtime

    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_codex_recovery(db)
    agent = _codex_recovery_agent(db, "accepted_then_driver_raised")
    monkeypatch.setattr(codex_runtime, "_ensure_codex_session", lambda _agent: None)

    result = codex_runtime.run_codex_app_server_turn(
        agent, user_message="finish the deployment",
        original_user_message="finish the deployment",
        messages=db.get_messages_as_conversation("head"),
        effective_task_id="task",
    )

    record = get_pending_input_recovery(db, "admission-provider")
    assert result["completed"] is False
    assert result["api_calls"] == 0
    assert "driver failed after turn/start acceptance" in result["error"]
    assert record["status"] == "claimed"
    assert record["dispatch_state"] == "uncertain"
    assert record["provider_return_resend_id"] == "resend-provider"
    assert db.get_messages_as_conversation("head") == []
    assert agent._pending_input_recovery_claim_id is None
    assert agent._pending_input_recovery_resend_id is None
    assert agent._pending_input_recovery_provider_completed is None


def test_codex_accepted_turn_consume_failure_restart_preserves_answered_pair(
    tmp_path, monkeypatch,
):
    from agent import codex_runtime
    from agent import pending_input_recovery as recovery_module

    db = SessionDB(db_path=tmp_path / "state.db")
    _stage_codex_recovery(db)
    agent = _codex_recovery_agent(db, "accepted")
    monkeypatch.setattr(codex_runtime, "_ensure_codex_session", lambda _agent: None)
    monkeypatch.setattr(
        recovery_module, "consume_pending_input_recovery",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("consume unavailable")),
    )
    monkeypatch.setattr(
        codex_runtime,
        "_persist_projected_messages",
        lambda *_a, **_k: db.append_message("head", "assistant", "done"),
    )
    monkeypatch.setattr(codex_runtime, "_finish_codex_turn", lambda *_a, **_k: {})

    result = codex_runtime.run_codex_app_server_turn(
        agent, user_message="finish the deployment",
        original_user_message="finish the deployment",
        messages=db.get_messages_as_conversation("head"),
        effective_task_id="task",
    )
    before_restart = get_pending_input_recovery(db, "admission-provider")
    assert result["turn_start_accepted"] is True
    assert before_restart["status"] == "claimed"
    assert before_restart["provider_return_resend_id"] == "resend-provider"

    history = db.get_messages_as_conversation("head")
    reconciled, removed = reconcile_pending_input_recovery_rows(db, history)

    assert removed == 0
    assert [(row["role"], row["content"]) for row in reconciled] == [
        ("user", "finish the deployment"),
        ("assistant", "done"),
    ]
    after_restart = get_pending_input_recovery(db, "admission-provider")
    assert after_restart["status"] == "consumed"
    assert after_restart["resolution"] == "durable_response_reconciled"


def test_sensitive_recovery_never_persists_raw_secret_or_replays(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("head", source="cli")
    secret = "sk-proj-AbCdEf1234567890GhIjKl"
    message = _user("admission-secret", text=secret)

    record, _live = stage_pending_input_recovery(
        db, accepted_input=_accepted(secret), live_messages=[message],
        session_ids=["head"], admission_id="admission-secret",
        surface_key="cli:surface", accepted_input_durably_committed=False,
    )

    assert secret not in db.get_meta("pending_input_recovery:v1:admission-secret")
    assert secret not in record["text"]
    assert record["safe_to_replay"] is False
    assert "sensitive_content_redacted" in record["loss_reasons"]


def test_plain_text_recovery_identity_preserves_leading_and_trailing_whitespace():
    from agent.conversation_loop import _accepted_input_recovery

    raw = "  deploy exactly\n"
    recovery = _accepted_input_recovery(raw, raw, moa_active=False)

    assert recovery["text"] == raw
    assert recovery["exact"] is True
    assert recovery["safe_to_replay"] is True


def test_oversized_timeout_creates_accessible_loss_record_and_zero_executable_copies(
    tmp_path,
):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("head", source="cli")
    raw = "x" * (64 * 1024 + 1)
    accepted = _user("admission-oversized", text=raw)
    db.append_message(
        "head", "user", raw, timestamp=accepted["timestamp"],
        display_metadata=accepted["display_metadata"],
    )
    agent = SimpleNamespace(
        session_id="head",
        _session_db=db,
        _session_persist_lock=threading.RLock(),
        _active_session_turn_lease_holder=None,
        _active_compression_lock_holder=None,
        _last_compression_timeout_total_exhausted=False,
        _pending_cli_user_message=accepted,
        _persist_user_message_idx=0,
        _persist_user_message_override=None,
        _session_messages=[accepted],
        _persist_disabled=True,
        _inflight_turn_id="turn",
        _inflight_turn_session_id="head",
        _last_compression_attempt_recorded=True,
        platform="cli",
    )
    exc = PreflightCompressionTimedOut(
        "timed out before provider entry",
        conversation_history=[accepted],
        live_messages=[accepted],
        accepted_input_durably_committed=True,
        accepted_input_session_ids=["head"],
    )

    result = _preflight_timeout_result(agent, exc, _accepted(raw))

    assert result["recovery_persisted"] is True
    assert result["messages"] == []
    assert db.get_messages_as_conversation("head") == []
    record = get_pending_input_recovery(db, "admission-oversized")
    assert record["status"] == "pending"
    assert record["safe_to_replay"] is False
    assert record["original_size_bytes"] == len(raw.encode("utf-8"))
    assert len(record["original_sha256"]) == 64
    assert "input_exceeds_recovery_size_limit" in record["loss_reasons"]
    assert raw not in db.get_meta(
        "pending_input_recovery:v1:admission-oversized"
    )
    assert get_surface_pending_input_recovery(
        db, "cli:surface"
    )["admission_id"] == "admission-oversized"


def test_structured_recovery_is_never_marked_replayable(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("head", source="acp")
    message = _user("admission-structured", surface="acp:surface")
    structured = _accepted("[Native-image input was accepted; attachment not retained.]")
    structured.update(input_kind="native_image", exact=False, lossy=True, safe_to_replay=True)

    record, _live = stage_pending_input_recovery(
        db,
        accepted_input=structured,
        live_messages=[message],
        session_ids=["head"],
        admission_id="admission-structured",
        surface_key="acp:surface",
        accepted_input_durably_committed=False,
    )

    assert record["safe_to_replay"] is False
    assert record["input_kind"] == "native_image"
    acknowledged, changed = acknowledge_pending_input_recovery_loss(
        db, "admission-structured",
    )
    assert changed is True
    assert acknowledged["status"] == "consumed"
    assert acknowledged["resolution"] == "acknowledged_loss"


def test_acknowledged_loss_advances_surface_head_to_older_safe_recovery(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("head", source="acp")
    surface = "acp:surface"
    older = _user("acp-safe-older", surface=surface, text="older safe input")
    stage_pending_input_recovery(
        db,
        accepted_input=_accepted("older safe input"),
        live_messages=[older],
        session_ids=["head"],
        admission_id="acp-safe-older",
        surface_key=surface,
        accepted_input_durably_committed=False,
    )
    newest = _user(
        "acp-loss-newest", surface=surface,
        text="[Native-image input marker]",
    )
    structured = {
        **_accepted("[Native-image input marker]"),
        "input_kind": "native_image",
        "exact": False,
        "lossy": True,
        "loss_reasons": ["attachments_not_retained"],
        "safe_to_replay": False,
    }
    stage_pending_input_recovery(
        db,
        accepted_input=structured,
        live_messages=[newest],
        session_ids=["head"],
        admission_id="acp-loss-newest",
        surface_key=surface,
        accepted_input_durably_committed=False,
    )

    assert get_surface_pending_input_recovery(db, surface)["admission_id"] == "acp-loss-newest"
    acknowledge_pending_input_recovery_loss(db, "acp-loss-newest")
    assert get_surface_pending_input_recovery(db, surface)["admission_id"] == "acp-safe-older"


@pytest.mark.parametrize("rotate", [False, True], ids=["in-place", "rotation"])
def test_merged_compaction_anchor_keeps_recovery_identity_through_later_timeout(
    tmp_path, rotate,
):
    from agent.conversation_compression import _ensure_compressed_has_user_turn

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("parent", source="cli")
    accepted = _user("admission-merged-anchor")
    original = [
        {"role": "user", "content": "old work"},
        {"role": "assistant", "content": "old result"},
        accepted,
    ]
    for message in original:
        db.append_message(
            "parent", message["role"], message["content"],
            display_metadata=message.get("display_metadata"),
        )
    compacted = [{
        "role": "user",
        "content": "[Your active task list was preserved across context compression]",
        "_todo_snapshot_synthetic": True,
        "display_metadata": {"scaffolding": "kept"},
    }]
    assert _ensure_compressed_has_user_turn(original, compacted) == "merged"
    assert len(compacted) == 1
    assert pending_input_admission(compacted[0]) == (
        "admission-merged-anchor", "cli:surface",
    )
    assert compacted[0]["display_metadata"]["scaffolding"] == "kept"
    assert not compacted[0].get("_todo_snapshot_synthetic")

    if rotate:
        db.end_session("parent", "compression")
        db.create_session("child", source="cli", parent_session_id="parent")
        db.append_message(
            "child", "user", compacted[0]["content"],
            display_metadata=compacted[0]["display_metadata"],
        )
        head = "child"
        baseline = None
    else:
        db.replace_messages("parent", compacted, active_only=True)
        head = "parent"
        baseline = list(compacted)
    agent = SimpleNamespace(
        session_id=head,
        _session_db=db,
        _session_persist_lock=threading.RLock(),
        _active_session_turn_lease_holder=None,
        _pending_cli_user_message=compacted[0],
        _persist_user_message_idx=0,
        _persist_user_message_override=None,
        _session_messages=list(compacted),
        _persist_disabled=True,
        _inflight_turn_id="turn",
        _inflight_turn_session_id=head,
        _last_compression_attempt_recorded=True,
        platform="cli",
    )
    exc = PreflightCompressionTimedOut(
        "second pass timed out",
        conversation_history=baseline,
        live_messages=compacted,
        accepted_input_durably_committed=True,
        accepted_input_session_ids=["parent", head],
    )

    result = _preflight_timeout_result(agent, exc, _accepted())

    assert result["recovery_persisted"] is True
    assert result["api_calls"] == 0
    assert [message["content"] for message in result["messages"]] == [
        "[Your active task list was preserved across context compression]",
        "Continuing from the retained context handoff.",
    ]
    assert [
        message["content"] for message in db.get_messages_as_conversation(head)
    ] == [
        "[Your active task list was preserved across context compression]",
        "Continuing from the retained context handoff.",
    ]
    assert result["messages"][0]["display_metadata"]["scaffolding"] == "kept"
    assert [message.get("role") for message in result["messages"]] == [
        "user", "assistant",
    ]
    from agent.agent_runtime_helpers import repair_message_sequence

    next_request = [
        *result["messages"],
        {"role": "user", "content": "new explicit request"},
    ]
    assert repair_message_sequence(agent, next_request) == 0
    assert [message["role"] for message in next_request] == [
        "user", "assistant", "user",
    ]
    assert all(
        pending_input_admission(message)[0] != "admission-merged-anchor"
        for sid in {"parent", head}
        for message in db.get_messages_as_conversation(sid)
    )
    record = get_pending_input_recovery(db, "admission-merged-anchor")
    assert record["status"] == "pending"
    assert record["text"] == "finish the deployment"


def test_summary_carrier_cleanup_bridges_next_user_without_mutating_handoff(tmp_path):
    from agent.agent_runtime_helpers import repair_message_sequence
    from agent.context_compressor import SUMMARY_PREFIX, _SUMMARY_END_MARKER

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("head", source="cli")
    handoff_content = f"{SUMMARY_PREFIX}\nPrior facts remain.\n{_SUMMARY_END_MARKER}"
    merged_content = f"{handoff_content}\n\nfinish the deployment"
    accepted = _user("admission-summary-carrier", text=merged_content)
    db.append_message("head", "assistant", "prior boundary")
    db.append_message(
        "head", "user", accepted["content"],
        display_metadata=accepted["display_metadata"],
    )

    _record, live = stage_pending_input_recovery(
        db, accepted_input=_accepted(),
        live_messages=[{"role": "assistant", "content": "prior boundary"}, accepted],
        session_ids=["head"], admission_id="admission-summary-carrier",
        surface_key="cli:surface", accepted_input_durably_committed=True,
    )

    assert [message["role"] for message in live] == [
        "assistant", "user", "assistant",
    ]
    assert live[1]["content"] == handoff_content
    assert pending_input_admission(live[1]) == (None, None)
    assert [message["content"] for message in db.get_messages_as_conversation("head")] == [
        "prior boundary", handoff_content,
        "Continuing from the retained context handoff.",
    ]
    next_request = [*live, {"role": "user", "content": "new explicit request"}]
    assert repair_message_sequence(SimpleNamespace(), next_request) == 0
    assert [message["role"] for message in next_request] == [
        "assistant", "user", "assistant", "user",
    ]


@pytest.mark.parametrize("rotate", [False, True], ids=["in-place", "rotation"])
@pytest.mark.parametrize("idle_first", [False, True], ids=["preflight-first", "idle-first"])
@pytest.mark.parametrize("api_only_prefix", [False, True], ids=["clean-row", "api-prefixed-row"])
def test_first_pass_commit_then_second_pass_timeout_keeps_one_inert_intent(
    tmp_path, monkeypatch, rotate, idle_first, api_only_prefix,
):
    db = SessionDB(db_path=tmp_path / "state.db")
    parent = "parent"
    db.create_session(parent, source="cli")
    accepted = _user("admission-two-pass")
    if api_only_prefix:
        accepted["content"] = "[API-only voice instruction]\nfinish the deployment"
    initial = [
        {"role": "user", "content": "old task"},
        {"role": "assistant", "content": "old answer"},
        {"role": "assistant", "content": "more context"},
        accepted,
    ]
    for message in initial:
        db.append_message(
            parent, message["role"], message["content"],
            display_metadata=message.get("display_metadata"),
        )
    compacted = [
        {"role": "user", "content": "[compacted prior context]"},
        {"role": "assistant", "content": "ready"},
        accepted.copy(),
    ]
    compacted[-1]["display_metadata"] = dict(accepted["display_metadata"])
    calls = {"compress": 0, "provider": 0}

    agent = SimpleNamespace(
        session_id=parent,
        _session_db=db,
        _session_persist_lock=threading.RLock(),
        _active_session_turn_lease_holder=None,
        _pending_cli_user_message=accepted,
        _persist_user_message_idx=len(initial) - 1,
        _persist_user_message_override=None,
        _session_messages=list(initial),
        _persist_disabled=True,
        _inflight_turn_id="turn",
        _inflight_turn_session_id=parent,
        platform="cli",
        model="test-model",
        max_compression_attempts=3,
        compression_enabled=True,
        compression_idle_compact_after_seconds=1,
        _last_activity_ts=0,
        _emit_status=lambda *_a, **_k: None,
        _clear_context_overflow_warn=lambda: None,
        _last_compression_attempt_recorded=True,
    )

    compressor = SimpleNamespace(
        threshold_tokens=1_000,
        context_length=4_000,
        summary_target_ratio=0.5,
        protect_first_n=0,
        protect_last_n=0,
        last_compression_rough_tokens=0,
        get_active_compression_failure_cooldown=lambda: None,
        should_compress=lambda _tokens: True,
    )
    agent.context_compressor = compressor

    def _compress(messages, _system, **_kwargs):
        calls["compress"] += 1
        if calls["compress"] == 1:
            if rotate:
                db.end_session(parent, "compression")
                db.create_session("child", source="cli", parent_session_id=parent)
                for message in compacted:
                    db.append_message(
                        "child", message["role"], message["content"],
                        display_metadata=message.get("display_metadata"),
                    )
                agent.session_id = "child"
                agent._last_compression_attempt_in_place = False
            else:
                db.replace_messages(parent, compacted, active_only=True)
                agent._last_compression_attempt_in_place = True
            return list(compacted), "SYSTEM"
        mark_context_compression_timed_out(agent)
        return messages, "SYSTEM"

    agent._compress_context = _compress
    monkeypatch.setattr(
        "agent.turn_context._preflight_request_tokens", lambda *_a, **_k: 3_000,
    )
    outcome = CompactionOutcome(
        messages=list(initial),
        active_system_prompt="SYSTEM",
        conversation_history=list(initial[:-1]),
        current_turn_user_idx=len(initial) - 1,
        accepted_input_session_ids=[parent],
    )

    if idle_first:
        _idle_compaction(agent, outcome, "SYSTEM", "task", "turn")
        assert calls["compress"] == 1

    with pytest.raises(PreflightCompressionTimedOut) as raised:
        _run_preflight_passes(
            agent, outcome, compressor, 3_000 if idle_first else 4_000,
            "SYSTEM", "task",
        )

    exc = raised.value
    assert exc.live_messages[-1]["content"] == accepted["content"]
    if rotate:
        assert exc.conversation_history is None
    else:
        assert [message["content"] for message in exc.conversation_history] == [
            "[compacted prior context]", "ready", accepted["content"],
        ]
    result = _preflight_timeout_result(agent, exc, _accepted())

    head = "child" if rotate else parent
    assert calls == {"compress": 2, "provider": 0}
    assert result["api_calls"] == 0
    assert result["recovery_persisted"] is True
    assert result["accepted_input_durably_committed"] is True
    assert [message["content"] for message in result["messages"]] == [
        "[compacted prior context]", "ready",
    ]
    assert [message["content"] for message in db.get_messages_as_conversation(head)] == [
        "[compacted prior context]", "ready",
    ]
    assert all(
        message["content"] != accepted["content"]
        for sid in [parent, head]
        for message in db.get_messages_as_conversation(sid)
    )
    record = get_pending_input_recovery(db, "admission-two-pass")
    assert record["status"] == "pending"
    assert record["text"] == "finish the deployment"


def test_idle_timeout_cooldown_cannot_fall_through_to_provider(tmp_path, monkeypatch):
    """Idle timeout is terminal when the unchanged request remains over threshold."""
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("head", source="cli")
    accepted = _user("admission-idle-timeout")
    initial = [
        {"role": "user", "content": "old task"},
        {"role": "assistant", "content": "old answer"},
        accepted,
    ]
    for message in initial:
        db.append_message(
            "head", message["role"], message["content"],
            display_metadata=message.get("display_metadata"),
        )
    cooldown = {"active": False}
    calls = {"compress": 0, "provider": 0}
    compressor = SimpleNamespace(
        threshold_tokens=1_000,
        context_length=4_000,
        summary_target_ratio=0.5,
        protect_first_n=0,
        protect_last_n=0,
        last_compression_rough_tokens=0,
        awaiting_real_usage_after_compression=False,
        get_active_compression_failure_cooldown=lambda: (
            {"remaining_seconds": 60} if cooldown["active"] else None
        ),
        should_compress=lambda tokens: tokens >= 1_000,
    )
    agent = SimpleNamespace(
        session_id="head",
        _session_db=db,
        _session_persist_lock=threading.RLock(),
        _active_session_turn_lease_holder=None,
        _active_compression_lock_holder=None,
        _last_compression_timeout_total_exhausted=False,
        _pending_cli_user_message=accepted,
        _persist_user_message_idx=2,
        _persist_user_message_override=None,
        _session_messages=list(initial),
        _persist_disabled=True,
        _inflight_turn_id="turn",
        _inflight_turn_session_id="head",
        _last_compression_attempt_recorded=True,
        platform="cli",
        model="test-model",
        api_mode="chat_completions",
        compression_enabled=True,
        compression_idle_compact_after_seconds=1,
        _last_activity_ts=0,
        context_compressor=compressor,
        _emit_status=lambda *_a, **_k: None,
    )

    def timeout_idle(messages, _system, **_kwargs):
        calls["compress"] += 1
        mark_context_compression_timed_out(agent)
        cooldown["active"] = True
        return messages, "SYSTEM"

    agent._compress_context = timeout_idle
    monkeypatch.setattr(
        "agent.turn_context._preflight_request_tokens", lambda *_a, **_k: 3_000,
    )
    outcome = CompactionOutcome(
        messages=list(initial),
        active_system_prompt="SYSTEM",
        conversation_history=list(initial[:-1]),
        current_turn_user_idx=2,
        accepted_input_session_ids=["head"],
    )

    _idle_compaction(agent, outcome, "SYSTEM", "task", "turn")
    assert calls["compress"] == 1
    assert cooldown["active"] is True
    with pytest.raises(PreflightCompressionTimedOut) as raised:
        _preflight_compression(agent, outcome, "SYSTEM", "task", "turn")

    result = _preflight_timeout_result(agent, raised.value, _accepted())
    assert result["api_calls"] == 0
    assert result["recovery_persisted"] is True
    assert [message["content"] for message in result["messages"]] == [
        "old task", "old answer",
    ]
    assert [
        message["content"] for message in db.get_messages_as_conversation("head")
    ] == ["old task", "old answer"]
    assert get_pending_input_recovery(db, "admission-idle-timeout")["status"] == "pending"
    assert calls == {"compress": 1, "provider": 0}
