"""Compute-host sends receipt their client ids (t_edb5b1dd checker B5). Harness from test_compute_host_turn_protocol.py.
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



def test_compute_host_emits_occurrence_receipt(turn_env,monkeypatch,tmp_path):
    from hermes_state import SessionDB
    db=SessionDB(db_path=tmp_path/'compute.db')
    monkeypatch.setattr(server,'_get_db',lambda:db)
    out=io.StringIO()
    host=ComputeHost(stdout=out,heartbeat_secs=0)
    sid='s1'
    agent=_agent(['answer'])
    original_run=agent.run_conversation
    def persist_and_run(prompt, *, conversation_history=None, stream_callback=None,
                        persist_user_message=None, persist_user_display_metadata=None, **kwargs):
        if db.get_session('s1-key') is None:
            db.create_session('s1-key',source='desktop',model='test')
        # Mirror AIAgent._stage_turn_user_message: a row staged at submit time is adopted, not rewritten.
        staged=getattr(agent,'_pending_cli_user_message',None)
        row_id=(staged['_row_id'] if isinstance(staged,dict) and staged.get('_row_id') else
                db.append_message('s1-key','user',content=persist_user_message or prompt,
                                  display_metadata=persist_user_display_metadata))
        result=original_run(prompt,conversation_history=conversation_history,stream_callback=stream_callback)
        result['messages'][-2].update(_row_id=row_id,_db_persisted=True,
                                     display_metadata=persist_user_display_metadata)
        return result
    agent.run_conversation=persist_and_run
    server._sessions[sid]=_session(agent)
    try:
        host.handle_frame({'type':'turn.start','sid':sid,'request_id':'turn','text':'one\n\ntwo','display_metadata':{'client_message_ids':['user-A','user-B']}})
        _wait(out,lambda f:f['type']=='turn.end')
        rows=db.get_messages_as_conversation('s1-key',include_row_ids=True)
        assert rows, json.dumps(_frames(out))
        users=[r for r in rows if r['role']=='user']
        assert len(users)==1, users  # the submit-time row is the turn's row, never a duplicate
        user=users[0]
        assert user['display_metadata']['client_message_ids']==['user-A','user-B']
        receipts=[f['message']['params']['payload'] for f in _frames(out) if f['type']=='rpc' and f['message'].get('params',{}).get('type')=='prompt.receipt']
        assert receipts==[{'client_message_ids':['user-A','user-B'],'status':'persisted','user_row_id':user['_row_id']}]
    finally:
        server._sessions.pop(sid,None)
        host.close()
        db.close()
