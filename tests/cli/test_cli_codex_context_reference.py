"""Regression coverage for provider-aware @-context sizing in the CLI."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def test_at_context_resolution_passes_active_provider():
    """The CLI @-reference path must preserve the active Codex provider."""
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli.model = "gpt-5.6-terra"
    cli.base_url = "https://chatgpt.com/backend-api/codex"
    cli.api_key = "token"
    cli.provider = "openai-codex"
    cli.agent = SimpleNamespace(_config_context_length=None)
    cli._active_agent_route_signature = "route"
    cli._secret_capture_callback = lambda *_args, **_kwargs: None
    cli._last_turn_interrupted = False
    cli._ensure_runtime_credentials = lambda: True
    cli._resolve_turn_agent_config = lambda _message: {
        "signature": "route",
        "model": cli.model,
        "runtime": None,
        "request_overrides": None,
    }
    cli._init_agent = lambda **_kwargs: True

    blocked_result = SimpleNamespace(
        expanded=False,
        blocked=True,
        references=[],
        injected_tokens=0,
        warnings=["blocked for test"],
    )
    with patch("agent.context_references.preprocess_context_references", return_value=blocked_result), \
         patch("agent.model_metadata.get_model_context_length", return_value=372_000) as mock_context, \
         patch("cli._cprint"):
        result = cli.chat("inspect @file:example.py")

    assert result == "blocked for test"
    mock_context.assert_called_once_with(
        "gpt-5.6-terra",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="token",
        provider="openai-codex",
        config_context_length=None,
    )


def test_recovery_submission_bypasses_second_at_context_expansion():
    from cli import HermesCLI
    from hermes_cli.prompt_stash import ComposerSubmission

    class Staged(Exception):
        pass

    cli = HermesCLI.__new__(HermesCLI)
    cli._restore_pending_input_recoveries_to_stash = lambda: 0
    cli._secret_capture_callback = lambda *_args, **_kwargs: None
    cli._ensure_runtime_credentials = lambda: True
    cli._resolve_turn_agent_config = lambda _message: {
        "signature": "route", "model": "model", "runtime": None,
        "request_overrides": None,
    }
    cli._active_agent_route_signature = "route"
    cli._init_agent = lambda **_kwargs: True
    cli.agent = SimpleNamespace()
    cli._chat_route_images = lambda message, _images: message
    cli._chat_expand_context_references = MagicMock(
        side_effect=AssertionError("recovery was expanded twice")
    )
    captured = {}

    def stage(_agent, message, **kwargs):
        captured.update(message=message, **kwargs)
        raise Staged

    cli._chat_stage_user_message = stage
    raw = (
        "inspect @file:example.py\n\n"
        "<attached-context source=\"example.py\">already expanded</attached-context>"
    )
    submission = ComposerSubmission(
        raw,
        recovery_admission_id="recovery-expanded",
        recovery_surface_key="cli:session-a",
    )

    # chat() owns the full claim/staging rollback boundary, so a staging fault
    # is rendered as a terminal None rather than escaping past the composer.
    assert cli.chat(submission) is None

    cli._chat_expand_context_references.assert_not_called()
    assert captured["message"] == raw
    assert captured["recovery_admission_id"] == "recovery-expanded"
    assert captured["recovery_surface_key"] == "cli:session-a"
