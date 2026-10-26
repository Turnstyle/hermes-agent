"""Finite chat must consume parallel delegated results before the CLI exits."""

import asyncio
import http.server
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("mode", ["quiet", "oneshot", "redirected", "resumed"])
def test_finite_chat_joins_parallel_children_before_final_response(tmp_path, mode):
    """Real parser, CLI, agent loops and delegate_task. Only inference is synthetic.

    Both child HTTP requests must reach the barrier before either can finish.
    The provider synthesizes a final answer only from returned tool results,
    not from child requests or transcripts that the parent has not consumed.
    """
    home = tmp_path / "profile"
    home.mkdir()
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    children, joined_results, errors = [], [], []
    workers = ("WORKER_ALPHA", "WORKER_BETA")

    class Provider(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_error(404)

        def do_POST(self):
            try:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                messages = request.get("messages", [])
                if not messages:
                    self.send_error(404)
                    return
                users = [str(m.get("content", "")) for m in messages if m["role"] == "user"]
                worker = next((w for w in workers if w in users[-1]), None)
                results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
                message: dict[str, Any]
                if worker:
                    with lock:
                        children.append(worker)
                    barrier.wait(timeout=20)
                    message = {"role": "assistant", "content": worker + "_COMPLETE"}
                elif results:
                    with lock:
                        joined_results.extend(results)
                    summaries = json.dumps(results)
                    joined = all(w + "_COMPLETE" in summaries for w in workers)
                    message = {"role": "assistant", "content": (
                        "FANOUT_JOINED_AND_SYNTHESIZED" if joined else "PARENT_ENDED_BEFORE_JOIN"
                    )}
                else:
                    message = {"role": "assistant", "content": None, "tool_calls": [{
                        "id": "call_fanout", "type": "function", "function": {
                            "name": "delegate_task", "arguments": json.dumps({
                                "tasks": [{"goal": f"Complete {w} and return its completion token."}
                                          for w in workers],
                            }),
                        },
                    }]}
                response = {
                    "id": "chatcmpl-local", "object": "chat.completion", "created": 1,
                    "model": "test-model", "choices": [{
                        "index": 0, "message": message,
                        "finish_reason": "tool_calls" if "tool_calls" in message else "stop",
                    }], "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
                }
                content_type = "application/json"
                if request.get("stream"):
                    response["object"] = "chat.completion.chunk"
                    response["choices"][0]["delta"] = response["choices"][0].pop("message")
                    for index, tool in enumerate(message.get("tool_calls", [])):
                        tool["index"] = index
                    raw = ("data: " + json.dumps(response) + "\n\ndata: [DONE]\n\n").encode()
                    content_type = "text/event-stream"
                else:
                    raw = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except Exception as exc:
                with lock:
                    errors.append(repr(exc))
                self.send_error(500)

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/v1"
    (home / "config.yaml").write_text(
        f"model:\n  provider: custom\n  base_url: {url}\n  api_mode: chat_completions\n"
        "gateway:\n  multiplex_profiles: false\n"
        "auxiliary:\n  title_generation:\n    enabled: false\n"
        "memory:\n  memory_enabled: false\n  user_profile_enabled: false\n"
        "terminal:\n  env: local\n  oneshot_completion_wait_seconds: 1\n"
        f"delegation:\n  max_concurrent_children: 2\n  base_url: {url}\n"
        "  api_key: local-test-only\n  model: test-model\n",
        encoding="utf-8",
    )
    query = tmp_path / "query.txt"
    query.write_text("Delegate two independent tasks, then synthesize their results.", encoding="utf-8")
    # Retain native Windows location variables, but never inherited credentials
    # or another session's finite/approval/runtime markers.
    env = {key: os.environ[key] for key in (
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "LOCALAPPDATA", "APPDATA",
    ) if key in os.environ}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), HERMES_HOME=str(home),
               HERMES_MANAGED_DIR=str(tmp_path / "managed"), TERMINAL_CWD=str(tmp_path),
               OPENAI_BASE_URL=url, OPENAI_API_KEY="local-test-only", PYTHONPATH=str(REPO_ROOT),
               PYTHONDONTWRITEBYTECODE="1", LANG="C.UTF-8")
    mode_flags = {"quiet": ["-Q"], "oneshot": ["--oneshot"], "redirected": [],
                  "resumed": ["-Q"]}
    command = [
        sys.executable, "-c", "from hermes_cli.main import main; main()", "chat",
        *mode_flags[mode], "--provider", "custom", "--model", "test-model", "--base-url", url,
        "--toolsets", "delegation", "--ignore-rules", "--query-file", str(query),
        "--reasoning", "high", "--max-turns", "10",
    ]
    from tests.gateway.fixtures.local_recovery_probe import daemon, rpc, websocket

    async def create_resume_target(desc):
        async with websocket(home, desc) as ws:
            created = await rpc(ws, "session.create", request_id="interactive-origin", source="cli",
                                model="test-model", provider="custom", base_url=url,
                                toolsets=["delegation"], cwd=str(tmp_path), ignore_rules=True)
            return created["result"]["stored_session_id"]

    async def check_receipt(desc, row):
        sid, request_id, payload = row
        payload = json.loads(payload)
        assert payload["finite"] is True
        async with websocket(home, desc) as ws:
            assert "result" in await rpc(ws, "session.resume", session_id=sid)
            # The row stores the committed admission; its ``cli_turn_v1`` block holds the per-turn CLI
            # fields under their wire names, so the retry re-sends exactly what the CLI submitted.
            wire = {key: value for key, value in payload.items() if key != "cli_turn_v1"}
            retry = dict(session_id=sid, input_id=request_id, **wire, **payload.get("cli_turn_v1", {}))
            assert (await rpc(ws, "prompt.submit", **retry))["result"]["status"] == "terminal"
            conflict = await rpc(ws, "prompt.submit", **{**retry, "finite": False})
            assert conflict["error"]["message"] == "admission_conflict"
            invalid = await rpc(ws, "prompt.submit", **{**retry, "finite": "true"})
            assert invalid["error"]["message"] == "invalid_params"
    try:
        # Own the ordinary authority explicitly: a finite viewer must not own its lifetime.
        with daemon(REPO_ROOT, home, env, barrier=False) as (owner, desc):
            if mode == "resumed":
                sid = asyncio.run(create_resume_target(desc))
                command = command[:5] + ["-Q", "--resume", sid, "--query-file", str(query)]
            result = subprocess.run(command, cwd=tmp_path, env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, encoding="utf-8", timeout=75, check=False)
            assert owner.poll() is None
            with closing(sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True)) as db:
                admissions = db.execute("SELECT status FROM session_admissions").fetchall()
                saved = db.execute("SELECT target_session_id,request_id,payload_json FROM session_admissions").fetchone()
                # Inline children never leave a detached-result receipt for a later viewer.
                assert db.execute("SELECT count(*) FROM async_delegations").fetchone() == (0,)
            assert admissions == [("terminal",)], admissions
            asyncio.run(check_receipt(desc, saved))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    diagnostic = (result.stdout, result.stderr, joined_results, children, errors)
    assert result.returncode == 0, diagnostic
    assert "FANOUT_JOINED_AND_SYNTHESIZED" in result.stdout, diagnostic
    assert "PARENT_ENDED_BEFORE_JOIN" not in result.stdout, diagnostic
    assert sorted(children) == sorted(workers), diagnostic
    assert not errors, diagnostic
    assert len(joined_results) == 1, diagnostic
    assert [r["status"] for r in joined_results[0]["results"]] == ["completed", "completed"]
    assert [r["summary"] for r in joined_results[0]["results"]] == [w + "_COMPLETE" for w in workers]


@pytest.mark.parametrize("query,image", [("Delegate a task", None), (None, "image.png")])
def test_tty_seeded_chat_keeps_background_delegation(monkeypatch, query, image):
    """A TTY -q (or image seed) still owns a later-turn completion consumer."""
    import cli
    import tools.delegate_tool as dt
    from gateway.session_context import reset_session_vars
    from run_agent import AIAgent
    from tests.tools.test_delegate import _make_mock_parent

    reset_session_vars()
    monkeypatch.delenv("HERMES_SINGLE_QUERY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_collect_query_images", lambda q, i: (q, [i] if i else []))
    parent = _make_mock_parent()
    parent.session_id = "interactive-parent"
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: MagicMock())
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **kw: {
        "model": "test-model", "provider": "custom", "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    })
    dispatched = []

    def dispatch(unit, unit_id, slot_key, routing):
        dispatched.append(unit)
        return {"status": "dispatched", "delegation_id": "interactive-delegation"}

    monkeypatch.setattr("tools.delegate_tool_dispatch._dispatch_unit", dispatch)
    seeded = SimpleNamespace(run=lambda: AIAgent._dispatch_delegate_task(
        parent, {"tasks": [{"goal": "independent task"}]},
    ))
    try:
        result = cli._run_single_query_mode(seeded, query, image, False, False)
        assert isinstance(result, str)
        payload = json.loads(result)
        assert payload["status"] == "dispatched"
        assert len(dispatched) == 1
        assert "HERMES_SINGLE_QUERY_SESSION" not in os.environ
    finally:
        reset_session_vars()


def test_shared_inherited_session_markers_preserve_delegated_child_fence(monkeypatch, tmp_path):
    """The shared marker scrub clears per-session/supervisor markers but keeps HERMES_DELEGATED_CHILD_CONTEXT."""
    from agent.delegation_context import is_delegated_child_process_context, kanban_path_is_fenced
    from hermes_cli.process_identity import is_inherited_session_marker, strip_inherited_session_markers

    assert not is_inherited_session_marker("HERMES_DELEGATED_CHILD_CONTEXT")
    env = {
        "HERMES_SINGLE_QUERY_SESSION": "1",
        "HERMES_SESSION_ID": "20261015_120000_oneshot",
        "HERMES_SESSION_SOURCE": "cli_single_query",
        "HERMES_SESSION_PLATFORM": "cli",
        "HERMES_SUPERVISED_CHILD": "1",
        "HERMES_LAUNCHD_LABEL": "com.nous.hermes.gateway",
        "HERMES_CRON_SESSION": "cron-1",
        "HERMES_DELEGATED_CHILD_CONTEXT": "1",
        "KEEP_VAR": "ok",
    }
    assert strip_inherited_session_markers(env) == []
    assert env["HERMES_SUPERVISED_CHILD"] == "1"
    stripped = strip_inherited_session_markers(env, desktop_owned=True)
    assert set(stripped) == {
        "HERMES_SINGLE_QUERY_SESSION", "HERMES_SESSION_ID", "HERMES_SESSION_SOURCE", "HERMES_SESSION_PLATFORM",
        "HERMES_SUPERVISED_CHILD", "HERMES_LAUNCHD_LABEL", "HERMES_CRON_SESSION",
    }
    assert env == {"HERMES_DELEGATED_CHILD_CONTEXT": "1", "KEEP_VAR": "ok"}

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    strip_inherited_session_markers(desktop_owned=True)
    assert is_delegated_child_process_context() is True
    assert kanban_path_is_fenced(tmp_path / "kanban" / "board.json") is True


def test_desktop_launch_and_backend_entries_clear_inherited_markers(monkeypatch, tmp_path):
    """Desktop launch and the Desktop-owned dashboard/serve backend clear inherited one-shot markers,
    so a Desktop session bound afterwards is interactive (no one-shot budget, async wake allowed)."""
    from argparse import Namespace
    from agent.oneshot_footprint import is_single_query_session
    from gateway.session_context import reset_session_vars, set_session_vars
    from hermes_cli import main_desktop
    from hermes_cli.main import _dashboard_sanitize_desktop_env
    from tools.approval_context import _is_single_query_approval_context
    from tools.delegate_tool import _oneshot_spawn_budget
    from tools.delegate_tool_dispatch import _resolve_async_wake_sid

    polluted = {
        "HERMES_SINGLE_QUERY_SESSION": "1",
        "HERMES_SESSION_ID": "20261015_120000_oneshot",
        "HERMES_SESSION_SOURCE": "cli_single_query",
        "HERMES_SESSION_KEY": "cli:oneshot",
        "HERMES_SUPERVISED_CHILD": "1",
        "HERMES_LAUNCHD_LABEL": "com.nous.hermes.gateway",
        "HERMES_CRON_SESSION": "cron-1",
        "HERMES_DELEGATED_CHILD_CONTEXT": "1",
    }
    for k, v in polluted.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(main_desktop, "_detect_linux_password_store", lambda: None)

    launch_env, _ = main_desktop._desktop_launch_env(
        Namespace(fake_boot=False, ignore_existing=False, hermes_root=None, cwd=str(tmp_path)))
    for k in polluted:
        if k != "HERMES_DELEGATED_CHILD_CONTEXT":
            assert k not in launch_env, f"{k} leaked through _desktop_launch_env"
    assert launch_env.get("HERMES_DELEGATED_CHILD_CONTEXT") == "1"

    monkeypatch.setenv("HERMES_DESKTOP", "1")
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "fixture-not-a-credential")
    _dashboard_sanitize_desktop_env(headless_backend=True)
    for k in polluted:
        if k != "HERMES_DELEGATED_CHILD_CONTEXT":
            assert k not in os.environ, f"{k} survived _dashboard_sanitize_desktop_env"
    assert os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT") == "1"

    reset_session_vars()
    try:
        set_session_vars(platform="desktop", source="desktop", session_id="desktop-sess-1",
                         ui_session_id="desktop-sess-1", session_history_delivery="1")
        assert is_single_query_session() is False
        assert _oneshot_spawn_budget(SimpleNamespace(), 3) is None
        assert _resolve_async_wake_sid("desktop-sess-1") == ""
        assert _is_single_query_approval_context() is False
    finally:
        reset_session_vars()


def test_real_chat_single_query_still_sets_and_honors_oneshot_markers_under_desktop_source(monkeypatch):
    """A real chat -Q sets and honors HERMES_SINGLE_QUERY_SESSION=1 even if HERMES_SESSION_SOURCE=desktop."""
    import cli
    from agent.oneshot_footprint import is_single_query_session
    from gateway.session_context import reset_session_vars
    from tools.approval_context import _is_single_query_approval_context
    from tools.delegate_tool import _oneshot_spawn_budget
    from tools.delegate_tool_dispatch import _resolve_async_wake_sid

    reset_session_vars()
    monkeypatch.delenv("HERMES_SINGLE_QUERY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "desktop")
    monkeypatch.setattr(cli, "_should_seed_interactive", lambda *a, **k: False)

    def exit_single_query(code):
        raise SystemExit(code)

    monkeypatch.setattr("hermes_cli.quiet_single_query.exit_single_query", exit_single_query)
    observed = {}

    def fake_claim(surface="cli", *, stderr=False, silent=False):
        observed["marker"] = os.environ.get("HERMES_SINGLE_QUERY_SESSION")
        observed["is_oneshot"] = is_single_query_session()
        observed["budget_err"] = _oneshot_spawn_budget(SimpleNamespace(), 3)
        observed["wake_sid"] = _resolve_async_wake_sid("oneshot-sess")
        observed["approval_oneshot"] = _is_single_query_approval_context()
        return False

    stub = SimpleNamespace(_claim_active_session=fake_claim, _release_active_session=lambda: None,
                           _active_session_refusal_reason="", agent=None)
    try:
        with pytest.raises(SystemExit):
            cli._run_single_query_mode(stub, "run one-shot query", None, True, False)
        assert observed["marker"] == "1"
        assert observed["is_oneshot"] is True
        assert observed["budget_err"] is not None and "0/2 subagents used" in observed["budget_err"]
        assert observed["wake_sid"] is None
        assert observed["approval_oneshot"] is True
    finally:
        monkeypatch.delenv("HERMES_SINGLE_QUERY_SESSION", raising=False)
        reset_session_vars()


def _force_fresh_tui_gateway_server_import():
    """Drop tui_gateway.server so the next lazy import executes its module top level again."""
    sys.modules.pop("tui_gateway.server", None)
    pkg = sys.modules.get("tui_gateway")
    if pkg is not None and hasattr(pkg, "server"):
        delattr(pkg, "server")


@pytest.mark.parametrize("cron", [False, True])
def test_lazy_tui_gateway_server_import_preserves_oneshot_and_cron_markers(monkeypatch, cron):
    """The lazy tui_gateway.server import behind _capture_origin() never strips a real one-shot/cron marker."""
    from agent.oneshot_footprint import is_single_query_session
    from gateway.session_context import reset_session_vars
    from tools.approval_context import _is_cron_approval_context, _is_single_query_approval_context, _no_user_can_answer
    from tools.delegate_tool_dispatch import _capture_origin, _resolve_async_wake_sid

    reset_session_vars()
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "desktop")
    monkeypatch.setenv("HERMES_UI_SESSION_ID", "desktop-win-1")
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    if cron:
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    saved_server = sys.modules.get("tui_gateway.server")
    _force_fresh_tui_gateway_server_import()
    try:
        origin = _capture_origin()
        assert "tui_gateway.server" in sys.modules
        assert os.environ.get("HERMES_SINGLE_QUERY_SESSION") == "1"
        assert _resolve_async_wake_sid(origin[0], origin[4]) is None
        assert is_single_query_session() is True
        if cron:
            assert os.environ.get("HERMES_CRON_SESSION") == "1"
            assert _is_cron_approval_context() is True
            assert _no_user_can_answer() is True
        else:
            assert _is_single_query_approval_context() is True
    finally:
        reset_session_vars()
        if saved_server is not None:  # later tests patch the module object they imported
            sys.modules["tui_gateway.server"] = saved_server
            sys.modules["tui_gateway"].server = saved_server
