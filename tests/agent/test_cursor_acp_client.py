"""Cursor's six fail-loud contracts, exercised over real subprocess stdio."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from openai import BadRequestError

PEER = Path(__file__).parent / "fixtures" / "cursor_acp_peer.py"


def client_for(tmp_path, scenario="normal"):
    from agent.cursor_acp_client import CursorACPClient

    journal = tmp_path / f"{scenario}.jsonl"
    client = CursorACPClient(command=sys.executable, args=[str(PEER), scenario, str(journal)], acp_cwd=str(tmp_path))
    return client, journal


def read_journal(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def complete(client, **kwargs):
    return client.chat.completions.create(model="default[]", messages=[{"role": "user", "content": "ping"}], timeout=5, **kwargs)


def test_model_must_be_advertised_and_selection_must_succeed(tmp_path):
    client, journal = client_for(tmp_path)
    for model in ["composer-2.5", "unknown", None, ""]:
        with pytest.raises(ValueError, match="advertised"):
            client.chat.completions.create(model=model, messages=[], timeout=5)
    assert not any(row.get("method") == "session/prompt" for row in read_journal(journal))
    for model in ["default[]", "composer-2.5[fast=true]"]:
        assert client.chat.completions.create(model=model, messages=[], timeout=5).model == model
    client, journal = client_for(tmp_path, "reject-model")
    with pytest.raises(RuntimeError, match="session/set_config_option"):
        complete(client)
    assert not any(row.get("method") == "session/prompt" for row in read_journal(journal))


def test_ask_mode_is_confirmed_before_prompt_and_failure_aborts(tmp_path):
    client, journal = client_for(tmp_path)
    complete(client)
    rows = read_journal(journal)
    prompt_index = next(i for i, row in enumerate(rows) if row.get("method") == "session/prompt")
    assert any(row.get("method") == "session/set_config_option" and row["params"]["value"] == "ask" for row in rows[:prompt_index])
    for scenario in ["reject-mode", "ignore-mode"]:
        client, journal = client_for(tmp_path, scenario)
        with pytest.raises(RuntimeError):
            complete(client)
        assert not any(row.get("method") == "session/prompt" for row in read_journal(journal))


def test_permission_and_cursor_blocking_requests_are_denied(tmp_path):
    client, journal = client_for(tmp_path, "permission")
    assert complete(client).choices[0].message.content == "pong"
    rows = read_journal(journal)
    capabilities = rows[0]["params"]["clientCapabilities"]
    assert not capabilities.get("fs", {}).get("writeTextFile", False)
    assert not capabilities.get("terminal", False)
    answers = {row["id"]: row for row in rows if "method" not in row}
    assert answers[100]["result"]["outcome"]["outcome"] == "cancelled"
    assert all("error" in answers[rid] for rid in [101, 102, 103])


def test_formats_and_hermes_tools_raise_before_spawning(tmp_path):
    from agent.error_classifier import classify_api_error, FailoverReason

    client, journal = client_for(tmp_path)
    for request in [{"response_format": {"type": "json_schema", "json_schema": {}}}, {"json_schema": {}},
                    {"tools": [{"type": "function", "function": {"name": "read_file"}}]},
                    {"functions": [{"name": "read_file"}]}, {"tool_choice": "auto"}]:
        with pytest.raises(BadRequestError) as caught:
            complete(client, **request)
        assert caught.value.status_code == 400
        assert caught.value.code == ("format_unsupported" if "response_format" in request or "json_schema" in request else "tool_use_unsupported")
        verdict = classify_api_error(caught.value)
        assert verdict.reason == FailoverReason.format_error
        if caught.value.code == "tool_use_unsupported":
            assert verdict.should_fallback
        assert not verdict.retryable
    assert not journal.exists()
    assert complete(client, tool_choice="none", tools=[]).choices[0].message.content == "pong"


def test_native_tool_events_are_metadata_never_hermes_calls(tmp_path):
    client, _ = client_for(tmp_path, "tools")
    for stream in [False, True]:
        result = complete(client, stream=stream)
        if stream:
            chunks = list(result)
            assert all(not chunk.choices or not chunk.choices[0].delta.tool_calls for chunk in chunks)
        else:
            assert not result.choices[0].message.tool_calls
            assert "<tool_call>" in result.choices[0].message.content
        assert [event["sessionUpdate"] for event in result.cursor_native_tool_events] == ["tool_call", "tool_call_update"]
        assert result.cursor_native_tool_events[0]["toolCallId"] == result.cursor_native_tool_events[1]["toolCallId"]


def test_stream_is_live_cancel_reaches_peer_and_usage_is_unknown(tmp_path):
    from agent.turn_api_call import _should_stream

    client, journal = client_for(tmp_path, "stream")
    agent = SimpleNamespace(client=client, provider="cursor-acp", base_url="acp://cursor", _has_stream_consumers=lambda: True)
    assert _should_stream(agent)
    stream = complete(client, stream=True)
    try:
        first = next(stream)
        assert first.choices[0].delta.content == "first"
        assert first.usage is None
        stream.cancel()
        rest = list(stream)
        assert rest[-1].cursor_stop_reason == "cancelled"
        assert rest[-1].usage is None
        assert stream.usage is None
        assert any(row.get("method") == "session/cancel" for row in read_journal(journal))
    finally:
        stream.close()
        client.close()
    client, _ = client_for(tmp_path, "normal")
    assert complete(client).usage is None
    client, _ = client_for(tmp_path, "usage")
    usage = complete(client).usage
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (7, 3, 10)


def test_registered_provider_resolves_to_real_client_in_isolated_home(tmp_path, monkeypatch):
    import os
    from agent.agent_runtime_helpers import create_openai_client
    from agent.cursor_acp_client import CursorACPClient
    from hermes_cli.auth import resolve_external_process_provider_credentials
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from providers import get_provider_profile

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    executable = bindir / "cursor-agent"
    journal = tmp_path / "provider.jsonl"
    executable.write_text(
        f"#!{sys.executable}\nimport runpy, sys\nassert sys.argv[1:] == ['acp']\n"
        f"sys.argv = [{str(PEER)!r}, 'normal', {str(journal)!r}]\nrunpy.run_path({str(PEER)!r}, run_name='__main__')\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    profile = get_provider_profile("cursor-acp")
    assert profile is not None
    credentials = resolve_external_process_provider_credentials("cursor-acp")
    assert credentials["command"] == str(executable)
    assert credentials["args"] == ["acp"]
    runtime = resolve_runtime_provider(requested="cursor-acp", target_model="default[]")
    assert runtime["provider"] == "cursor-acp"
    assert runtime["base_url"] == "acp://cursor"
    agent = SimpleNamespace(provider="cursor-acp", _client_log_context=lambda: "")
    client = create_openai_client(agent, {**credentials, "acp_cwd": str(tmp_path)}, reason="test", shared=False)
    assert isinstance(client, CursorACPClient)
    try:
        assert complete(client).choices[0].message.content == "pong"
        assert profile.fetch_models(timeout=5) == client.list_models(timeout_seconds=5)
    finally:
        client.close()
