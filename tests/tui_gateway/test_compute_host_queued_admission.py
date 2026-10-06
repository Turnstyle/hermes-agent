"""A queued compute-host send cancelled after the first generation gate must not reach the model (t_c220db5c).

``_run_real_turn`` checks the queued generation under ``history_lock``, releases it for DB setup, then
dispatches. Stop (or any generation bump) landing in that window used to leave the dispatch unguarded:
the receipt helper wrote no row, but the agent still ran the cancelled send. The first test is the
Codex checker probe (NB1, t_edb5b1dd REVIEW4.json) kept verbatim. Harness from
test_compute_host_turn_protocol.py.
"""

from __future__ import annotations

import io
import json
import threading
import time
import types

import pytest

from tui_gateway import server
from tui_gateway.compute_host import ComputeHost


def _frames(out: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def _wait(out: io.StringIO, predicate, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for frame in _frames(out):
            if predicate(frame):
                return frame
        time.sleep(0.01)
    raise AssertionError(f"timed out; saw={_frames(out)}")


@pytest.fixture()
def turn_env(monkeypatch, tmp_path):
    """Neutralize the turn pipeline's environment-heavy side paths (same set as the
    prompt.submit tests) so the frame protocol is what's under test. Threads stay REAL:
    ``_run_real_turn`` joins ``session["_run_thread"]`` itself before emitting ``turn.end``."""
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda sid, session: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})


def _agent(deltas: list[str], *, delay_s: float = 0.0, interrupt: threading.Event | None = None):
    def run_conversation(prompt, *, conversation_history=None, stream_callback=None, **_kw):
        chunks = []
        for chunk in deltas:
            if interrupt is not None and interrupt.is_set():
                break
            chunks.append(chunk)
            if stream_callback is not None:
                stream_callback(chunk)
            if delay_s:
                time.sleep(delay_s)
        final = "".join(chunks)
        messages = [*(conversation_history or []), {"role": "user", "content": prompt},
                    {"role": "assistant", "content": final}]
        return {"final_response": final, "messages": messages}

    return types.SimpleNamespace(
        session_id="s1-key", run_conversation=run_conversation, clear_interrupt=lambda: None,
        hard_interrupt=lambda *a, **k: interrupt is not None and interrupt.set())


def _session(agent) -> dict:
    return {
        "agent": agent, "session_key": "s1-key", "history": [], "history_lock": threading.Lock(),
        "history_version": 0, "running": False, "attached_images": [], "image_counter": 0,
        "cols": 80, "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all",
        "inflight_turn": None, "active_session_lease": object(),
    }



def test_compute_cancel_after_initial_generation_gate(turn_env, monkeypatch, tmp_path):
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / "cancel.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    agent = _agent(["should not run"])
    invoked = []
    original_run = agent.run_conversation
    def run(prompt, **kwargs):
        invoked.append(prompt)
        return original_run(prompt, **kwargs)
    agent.run_conversation = run
    session = _session(agent)
    server._sessions["s1"] = session
    original_ensure = server._ensure_session_db_row
    cancelled = False
    def cancel_then_ensure(s):
        nonlocal cancelled
        if not cancelled:
            with s["history_lock"]:
                s["_queued_prompt_generation"] = 1
                s["_turn_cancel_requested"] = True
            cancelled = True
        return original_ensure(s)
    monkeypatch.setattr(server, "_ensure_session_db_row", cancel_then_ensure)
    try:
        host.handle_frame({"type": "turn.start", "sid": "s1", "request_id": "cancel-race",
                           "text": "cancelled send", "queued_prompt_generation": 0,
                           "display_metadata": {"client_message_ids": ["user-cancelled"]}})
        _wait(out, lambda f: f["type"] == "turn.end")
        assert invoked == [], {"invoked": invoked, "frames": _frames(out)}
        assert db.get_messages_as_conversation("s1-key") == []
    finally:
        host.close()
        server._sessions.pop("s1", None)
        db.close()


def test_generation_bump_without_cancel_flag_ends_interrupted(turn_env, monkeypatch, tmp_path):
    """A bare generation bump (compress re-anchor, not Stop) after the first gate: no model call, and the
    host still answers ``turn.end interrupted=True`` so the parent settles the claim as cancelled."""
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / "bump.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    invoked = []
    agent = _agent(["should not run"])
    original_run = agent.run_conversation
    agent.run_conversation = lambda prompt, **kw: (invoked.append(prompt), original_run(prompt, **kw))[1]
    session = _session(agent)
    session["_queued_prompt_generation"] = 4
    server._sessions["s1"] = session
    original_seed = server._persist_branch_seed
    def bump_then_seed(s):
        with s["history_lock"]:
            s["_queued_prompt_generation"] = 5
        return original_seed(s)
    monkeypatch.setattr(server, "_persist_branch_seed", bump_then_seed)
    try:
        host.handle_frame({"type": "turn.start", "sid": "s1", "request_id": "bump-race",
                           "text": "superseded send", "queued_prompt_generation": 4})
        end = _wait(out, lambda f: f["type"] == "turn.end")
        assert invoked == []
        assert end["request_id"] == "bump-race" and end["interrupted"] is True
        assert not any(f["type"] == "turn.error" for f in _frames(out))
        with session["history_lock"]:
            assert session["running"] is False
            assert session["inflight_turn"] is None
        assert db.get_messages_as_conversation("s1-key") == []
    finally:
        host.close()
        server._sessions.pop("s1", None)
        db.close()


def test_current_generation_queued_send_still_runs(turn_env, monkeypatch, tmp_path):
    """Control: an unchanged generation passes both gates and reaches the model exactly once."""
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / "ok.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    invoked = []
    agent = _agent(["done"])
    original_run = agent.run_conversation
    agent.run_conversation = lambda prompt, **kw: (invoked.append(prompt), original_run(prompt, **kw))[1]
    session = _session(agent)
    session["_queued_prompt_generation"] = 2
    server._sessions["s1"] = session
    try:
        host.handle_frame({"type": "turn.start", "sid": "s1", "request_id": "ok",
                           "text": "live send", "queued_prompt_generation": 2})
        end = _wait(out, lambda f: f["type"] == "turn.end")
        assert invoked == ["live send"]
        assert end["interrupted"] is False
    finally:
        host.close()
        server._sessions.pop("s1", None)
        db.close()
