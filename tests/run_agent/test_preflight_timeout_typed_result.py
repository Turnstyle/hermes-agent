"""The turn-start preflight timeout surfaces as a typed result, not a traceback.

Companion to the #98424 fail-closed boundary and the #98741/#99710 typed
timeout chain: when ``build_turn_context`` raises
``PreflightCompressionTimedOut``, ``run_conversation`` must convert it into
the same typed recovery dict the in-loop timeout consumers return —
``failed=True`` + ``compression_exhausted=True`` + the actionable message —
because the gateway's generic exception handler deliberately hides raw
exception text from users (it would deliver "Sorry, I encountered an
unexpected error" and skip the clean-session recovery contract).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _timeout_agent():
    from run_agent import AIAgent

    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.save_trajectories = False
    agent.compression_enabled = True
    agent.context_compressor.protect_first_n = 0
    agent.context_compressor.protect_last_n = 0
    agent.context_compressor.threshold_tokens = 1
    agent.context_compressor.should_compress = MagicMock(return_value=True)
    agent.context_compressor.should_defer_preflight_to_real_usage = MagicMock(
        return_value=False
    )
    agent.context_compressor.get_active_compression_failure_cooldown = MagicMock(
        return_value=None
    )

    def _timed_out(messages, _system_message, **_kwargs):
        from agent.conversation_compression import (
            mark_context_compression_timed_out,
            reset_context_compression_timeout_outcome,
        )

        reset_context_compression_timeout_outcome(agent)
        mark_context_compression_timed_out(agent)
        return messages, agent._cached_system_prompt

    agent._compress_context = _timed_out
    return agent


def test_turn_start_preflight_timeout_returns_typed_result_not_exception():
    agent = _timeout_agent()

    history = [
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old response"},
    ]
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        # Must NOT raise PreflightCompressionTimedOut out of run_conversation.
        result = agent.run_conversation(
            "[API-only recovery note]\ncontinue \ud800",
            conversation_history=history,
            persist_user_message="continue \ud800",
        )

    agent.client.chat.completions.create.assert_not_called()
    assert result["failed"] is True
    assert result["completed"] is False
    assert result["compression_exhausted"] is True
    assert result["turn_exit_reason"] == "context_compression_timeout"
    assert result["messages"] == history
    assert result["unprocessed_input"] == {
        "version": 1,
        "kind": "accepted_user_input",
        "text": "continue \ufffd",
        "input_kind": "plain_text",
        "exact": False,
        "lossy": True,
        "loss_reasons": ["surrogate_normalization"],
        "safe_to_replay": False,
        "provider_call_sent": False,
        "executable": False,
        "normalization_changed": True,
    }
    # The actionable preflight guidance survives into both user-facing fields.
    assert "provider call was not sent" in result["final_response"]
    assert "provider call was not sent" in result["error"]


def test_preflight_timeout_returns_history_for_adopted_rotation_child():
    agent = _timeout_agent()
    parent_history = [
        {"role": "user", "content": "parent request"},
        {"role": "assistant", "content": "parent response"},
    ]
    child_history = [
        {"role": "user", "content": "[CONTEXT COMPACTION] child summary"},
        {"role": "assistant", "content": "child continuation"},
    ]

    def _adopt_child(_agent):
        _agent.session_id = "rotated-child-session"
        return child_history

    with (
        patch(
            "agent.turn_context.recover_rotated_compression_session",
            side_effect=_adopt_child,
        ),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(
            "unprocessed child request",
            conversation_history=parent_history,
        )

    agent.client.chat.completions.create.assert_not_called()
    assert agent.session_id == "rotated-child-session"
    assert result["messages"] == child_history
    assert result["messages"] != parent_history
    assert all(
        row.get("content") != "unprocessed child request"
        for row in result["messages"]
    )


@pytest.mark.parametrize(
    ("provider", "moa_config"),
    [
        ("openrouter", {}),
        ("moa", None),
    ],
)
def test_preflight_timeout_preserves_moa_semantics_from_turn_or_virtual_provider(
    provider, moa_config
):
    agent = _timeout_agent()
    agent.provider = provider
    history = [
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old response"},
    ]

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(
            "compare approaches",
            conversation_history=history,
            moa_config=moa_config,
        )

    agent.client.chat.completions.create.assert_not_called()
    recovery = result["unprocessed_input"]
    assert recovery["input_kind"] == "moa"
    assert recovery["loss_reasons"] == ["per_turn_semantics_not_retained"]
    assert recovery["safe_to_replay"] is False


def test_empty_or_invalid_plain_persistence_input_is_not_preservable():
    from agent.conversation_loop import _accepted_input_recovery

    assert _accepted_input_recovery("fallback", "  ") is None
    assert _accepted_input_recovery("fallback", []) is None


def test_structured_native_and_moa_inputs_are_truthfully_lossy_not_executable():
    from agent.conversation_loop import _accepted_input_recovery

    structured = _accepted_input_recovery(
        [{"type": "text", "text": "caption"}], "caption"
    )
    native = _accepted_input_recovery(
        [
            {"type": "text", "text": "caption"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
        "caption",
    )
    moa = _accepted_input_recovery("compare approaches", None, moa_active=True)
    native_moa = _accepted_input_recovery(
        [
            {"type": "text", "text": "caption"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
        "caption",
        moa_active=True,
    )

    assert structured == {
        "version": 1,
        "kind": "accepted_user_input",
        "text": (
            "[Structured input was accepted before reset; structured components were not "
            "retained.]\n\ncaption"
        ),
        "input_kind": "structured",
        "exact": False,
        "lossy": True,
        "loss_reasons": ["structured_components_not_retained"],
        "safe_to_replay": False,
        "provider_call_sent": False,
        "executable": False,
    }
    assert native["input_kind"] == "native_image"
    assert native["loss_reasons"] == ["attachments_not_retained"]
    assert "data:image" not in native["text"]
    assert moa["input_kind"] == "moa"
    assert moa["loss_reasons"] == ["per_turn_semantics_not_retained"]
    assert native_moa["input_kind"] == "native_image"
    assert set(native_moa["loss_reasons"]) == {
        "attachments_not_retained",
        "per_turn_semantics_not_retained",
    }
    assert "Native-image input" in native_moa["text"]
    assert "MoA input" in native_moa["text"]
    assert "data:image" not in native_moa["text"]
    assert all(
        value["executable"] is False for value in (structured, native, moa, native_moa)
    )
    assert all(
        value["safe_to_replay"] is False
        for value in (structured, native, moa, native_moa)
    )
