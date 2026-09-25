"""Fail-loud OpenAI-shaped client for the official ``cursor-agent acp`` route.

Each completion owns a fresh ask-mode session. Cursor's native tools remain
observable metadata and are never executed a second time as Hermes tool calls.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from openai import BadRequestError

from agent.copilot_acp_client import _effective_timeout, _render_message_content
from agent.cursor_acp_transport import CursorSession


def _unsupported(code, param):
    message = f"Cursor ACP {param} unsupported."
    return BadRequestError(
        message, response=httpx.Response(400, request=httpx.Request("POST", "https://cursor-acp.invalid/completions")),
        body={"message": message, "type": "invalid_request_error", "code": code, "param": param},
    )


def _option(session, category):
    return next((option for option in session.get("configOptions", [])
                 if option.get("category") == category or option.get("id") == category), None)


def _model_ids(session):
    option = _option(session, "model") or {}
    return [entry["value"] for entry in option.get("options", []) if isinstance(entry.get("value"), str)]


def _select(session, category, value, advertised):
    option = _option(advertised, category)
    if not option or value not in [entry.get("value") for entry in option.get("options", [])]:
        raise ValueError(f"Cursor ACP {category} must be an exact advertised value.")
    result = session.request("session/set_config_option", {
        "sessionId": session.session_id, "configId": option["id"], "value": value,
    })
    confirmed = _option(result, category)
    if not confirmed or confirmed.get("currentValue") != value:
        raise RuntimeError(f"Cursor ACP did not confirm selected {category}.")


def _usage(raw):
    if not isinstance(raw, dict) or not any(raw.get(key) is not None for key in ("inputTokens", "outputTokens", "totalTokens")):
        return None
    return SimpleNamespace(prompt_tokens=raw.get("inputTokens"), completion_tokens=raw.get("outputTokens"),
                           total_tokens=raw.get("totalTokens"), prompt_tokens_details=None)


def _chunk(model, *, text=None, reasoning=None, finish=None, usage=None, stop_reason=None):
    return SimpleNamespace(
        model=model, usage=usage, cursor_stop_reason=stop_reason,
        choices=[SimpleNamespace(index=0, finish_reason=finish, delta=SimpleNamespace(
            role="assistant", content=text, reasoning=reasoning, reasoning_content=reasoning, tool_calls=None))],
    )


class CursorStream:
    """Pull real ACP deltas; cancellation is scoped to this stream's session."""
    def __init__(self, client, model, messages, timeout):
        self.cursor_native_tool_events = []
        self.cursor_usage = None
        self.cursor_stop_reason = None
        self.usage = None
        self._session = None
        self._iterator = self._run(client, model, messages, timeout)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._iterator)

    def cancel(self):
        if self._session:
            self._session.cancel()

    def close(self):
        self._iterator.close()

    def _run(self, client, model, messages, timeout):
        session = client._open(timeout)
        self._session = session
        try:
            advertised = client._initialize(session)
            # Never resolve a plain name, absent name or invalid id to the session default.
            if model not in _model_ids(advertised):
                raise ValueError("Cursor ACP model must be an exact advertised value.")
            _select(session, "model", model, advertised)
            _select(session, "mode", "ask", advertised)
            prompt = "\n\n".join(f"{message.get('role', 'context')}:\n{_render_message_content(message.get('content'))}"
                                   for message in messages)
            updates = session.request_updates("session/prompt", {
                "sessionId": session.session_id, "prompt": [{"type": "text", "text": prompt}],
            })
            while True:
                try:
                    update = next(updates)
                except StopIteration as done:
                    result = done.value
                    break
                kind = update.get("sessionUpdate")
                if kind in ("tool_call", "tool_call_update"):
                    self.cursor_native_tool_events.append(update)
                elif kind == "usage_update":
                    self.cursor_usage = update
                elif kind in ("agent_message_chunk", "agent_thought_chunk"):
                    content = update.get("content") or {}
                    if content.get("type") == "text" and isinstance(content.get("text"), str):
                        field = "text" if kind == "agent_message_chunk" else "reasoning"
                        yield _chunk(model, **{field: content["text"]})
            self.cursor_stop_reason = result.get("stopReason")
            self.cursor_usage = result.get("usage", self.cursor_usage)
            self.usage = _usage(self.cursor_usage)
            yield _chunk(model, finish="stop", usage=self.usage, stop_reason=self.cursor_stop_reason)
        finally:
            self._session = None
            client._release(session)


class CursorACPClient:
    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True
    HERMES_SUPPORTS_STREAMING = True

    def __init__(self, *, api_key=None, base_url=None, command=None, args=None,
                 acp_command=None, acp_args=None, acp_cwd=None, timeout=None, **_: Any):
        self.api_key = api_key or "cursor-acp"
        self.base_url = base_url or "acp://cursor"
        self._command = acp_command or command or "cursor-agent"
        self._args = list(acp_args if acp_args is not None else args if args is not None else ["acp"])
        self._cwd = str(Path(acp_cwd or os.getcwd()).resolve())
        self._timeout = _effective_timeout(timeout)
        self._sessions = set()
        self._lock = threading.Lock()
        self.is_closed = False
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _open(self, timeout):
        with self._lock:
            session = CursorSession(self._command, self._args, self._cwd, timeout)
            self._sessions.add(session)
            self.is_closed = False
            return session

    def _release(self, session):
        with self._lock:
            if session in self._sessions:
                self._sessions.remove(session)
                session.close()

    def close(self):
        with self._lock:
            for session in self._sessions:
                session.close()
            self._sessions.clear()
            self.is_closed = True

    def cancel(self):
        with self._lock:
            for session in self._sessions:
                session.cancel()

    def _initialize(self, session):
        session.request("initialize", {"protocolVersion": 1, "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False,
        }, "clientInfo": {"name": "hermes-agent", "version": "0.0.0"}})
        advertised = session.request("session/new", {"cwd": self._cwd, "mcpServers": []})
        session.session_id = advertised.get("sessionId")
        if not isinstance(session.session_id, str) or not session.session_id:
            raise RuntimeError("Cursor ACP did not return a sessionId.")
        return advertised

    def list_models(self, *, timeout_seconds=15):
        session = self._open(timeout_seconds)
        try:
            return _model_ids(self._initialize(session))
        finally:
            self._release(session)

    def _create(self, *, model=None, messages=None, timeout=None, tools=None, functions=None,
                tool_choice=None, function_call=None, response_format=None, json_schema=None, stream=False, **_: Any):
        for param, value in (("response_format", response_format), ("json_schema", json_schema)):
            if value is not None:
                raise _unsupported("format_unsupported", param)
        for param, value in (("tools", tools), ("functions", functions)):
            if value:
                raise _unsupported("tool_use_unsupported", param)
        for param, value in (("tool_choice", tool_choice), ("function_call", function_call)):
            if value is not None and value != "none":
                raise _unsupported("tool_use_unsupported", param)
        result = CursorStream(self, model, messages or [], self._timeout if timeout is None else _effective_timeout(timeout))
        if stream:
            return result
        text, thoughts = [], []
        try:
            for chunk in result:
                delta = chunk.choices[0].delta
                if delta.content:
                    text.append(delta.content)
                if delta.reasoning_content:
                    thoughts.append(delta.reasoning_content)
        finally:
            result.close()
        reasoning = "".join(thoughts) or None
        return SimpleNamespace(model=model, usage=result.usage,
            cursor_native_tool_events=result.cursor_native_tool_events, cursor_usage=result.cursor_usage,
            cursor_stop_reason=result.cursor_stop_reason,
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                content="".join(text), tool_calls=[], reasoning=reasoning, reasoning_content=reasoning, reasoning_details=None))])
