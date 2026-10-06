"""Queued compute-host admission (t_c220db5c): stale-claim cleanup at every setup boundary, generation
value compatibility, unchanged non-stale refusals, and parent supervisor settlement + drain.
Written by the independent Codex checker; provider execution is stubbed."""
import io
import types

import pytest

from tests.tui_gateway.test_compute_host_queued_admission import _agent, _session, _frames, turn_env
from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tui_gateway.host_supervisor import HostSupervisor


@pytest.mark.parametrize("boundary", ["first_ensure", "receipt_ensure", "submit_ensure", "ownership", "after_receipt"])
def test_stale_boundaries_cleanup(turn_env, monkeypatch, tmp_path, boundary):
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / "probe.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    session = _session(_agent(["forbidden"]))
    called = []
    session["agent"].run_conversation = lambda *a, **k: called.append(a)
    server._sessions["s1"] = session
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    def bump():
        with session["history_lock"]:
            session["_queued_prompt_generation"] = 1
            session["_turn_cancel_requested"] = True
    ensure = server._ensure_session_db_row
    count = 0
    def checked_ensure(s):
        nonlocal count
        count += 1
        if count == {"first_ensure": 1, "receipt_ensure": 2, "submit_ensure": 3}.get(boundary):
            bump()
        return ensure(s)
    monkeypatch.setattr(server, "_ensure_session_db_row", checked_ensure)
    if boundary == "ownership":
        ownership = server._ensure_active_session_slot
        def checked_ownership(*a):
            bump()
            return ownership(*a)
        monkeypatch.setattr(server, "_ensure_active_session_slot", checked_ownership)
    if boundary == "after_receipt":
        persist = server._persist_queued_user_row
        def checked_persist(*a, **kw):
            persist(*a, **kw)
            assert session.get("_submit_user_row")
            bump()
        monkeypatch.setattr(server, "_persist_queued_user_row", checked_persist)
    try:
        host._run_real_turn({"sid": "s1", "request_id": "race", "text": "cancel me",
            "queued_prompt_generation": 0, "display_metadata": {"client_message_ids": ["send-1"]}})
        terminal = [f for f in _frames(out) if f["type"] in ("turn.end", "turn.error")]
        assert len(terminal) == 1 and terminal[0]["type"] == "turn.end"
        assert terminal[0]["interrupted"] is True
        assert called == []
        assert session["running"] is False and session["inflight_turn"] is None
        assert "_submit_user_row" not in session
        assert "_run_thread" not in session
        rows = db.get_messages_as_conversation("s1-key")
        assert len(rows) == (1 if boundary in ("submit_ensure", "ownership", "after_receipt") else 0)
    finally:
        host.close()
        server._sessions.pop("s1", None)
        db.close()


@pytest.mark.parametrize("value", ["absent", None, "0", 0.0, False, "bogus", [], {}])
def test_generation_compatibility(turn_env, monkeypatch, value):
    session = _session(_agent(["ok"]))
    server._sessions["s1"] = session
    called = []
    original = session["agent"].run_conversation
    session["agent"].run_conversation = lambda *a, **kw: (called.append(a), original(*a, **kw))[1]
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    frame = {"sid": "s1", "request_id": "compat", "text": "hello"}
    if value != "absent":
        frame["queued_prompt_generation"] = value
    try:
        host._run_real_turn(frame)
        terminal = [f for f in _frames(out) if f["type"] in ("turn.end", "turn.error")]
        invalid = value == "bogus" or isinstance(value, (dict, list))
        assert len(terminal) == 1
        assert terminal[0]["type"] == ("turn.error" if invalid else "turn.end")
        assert len(called) == (0 if invalid else 1)
        assert session["running"] is False and session["inflight_turn"] is None
    finally:
        host.close()
        server._sessions.pop("s1", None)


@pytest.mark.parametrize("refusal", ["ownership", "closing", "missing_agent"])
def test_nonstale_refusal_preserves_completion(turn_env, monkeypatch, refusal):
    session = _session(_agent(["forbidden"]))
    server._sessions["s1"] = session
    if refusal == "ownership":
        monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a: "ownership refused")
    elif refusal == "closing":
        session["_closing"] = True
    else:
        session["agent"] = None
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    try:
        host._run_real_turn({"sid": "s1", "request_id": "refused", "text": "hello", "queued_prompt_generation": 0})
        terminal = [f for f in _frames(out) if f["type"] in ("turn.end", "turn.error")]
        assert len(terminal) == 1 and terminal[0]["type"] == "turn.end"
        assert terminal[0]["interrupted"] is False
        assert "session_info" in terminal[0]
        assert session["running"] is False
    finally:
        host.close()
        server._sessions.pop("s1", None)


@pytest.mark.parametrize("closing", [False, True])
def test_supervisor_settlement_and_drain(turn_env, monkeypatch, closing):
    from tests.tui_gateway.test_compute_host_borrowed_lease import _seed_parent_lease, _registry
    lease = _seed_parent_lease("s1-key")
    parent = _session(None)
    parent.update(running=True, _closing=closing, _queued_prompt_generation=1,
                  active_session_lease=lease, _compute_host_open_request={"id": "pending"})
    parent["inflight_turn"] = {"user": "stale", "streaming": True}
    parent["_submit_user_row"] = {"content": "stale"}
    if closing:
        parent.pop("active_session_lease")
        parent["_deferred_active_session_lease"] = lease
        server._deferred_active_session_leases[lease.lease_id] = lease
    parent["queued_prompt"] = {"text": "next send", "image_paths": [], "transport": None}
    dispatched = []
    events = []
    monkeypatch.setattr(server, "_emit", lambda *a, **kw: events.append(a))
    monkeypatch.setattr(server, "_compute_host_session_info", lambda s: {})
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda s: False)
    def next_turn(*a, **kw):
        assert parent["inflight_turn"] is None and "_submit_user_row" not in parent
        dispatched.append((a[3], kw["queued_prompt_generation"]))
        return True
    monkeypatch.setattr(server, "_run_prompt_submit", next_turn)
    supervisor = HostSupervisor(autostart=False, expected_build_sha="probe")
    monkeypatch.setattr(supervisor, "start", lambda: None)
    sent = []
    monkeypatch.setattr(supervisor, "_send_frame", sent.append)
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: supervisor)
    try:
        result = server._submit_prompt_to_compute_host("rid", "s1", parent, "cancel me", queued_prompt_generation=0)
        assert "error" not in result and len(sent) == 1
        done = {"type": "turn.end", "sid": "s1", "request_id": sent[0]["request_id"], "interrupted": True}
        supervisor._handle_host_frame(done)
        supervisor._handle_host_frame(done)
        assert supervisor._pending_turns == {}
        assert "_compute_host_turn_id" not in parent and "_compute_host_open_request" not in parent
        assert dispatched == ([] if closing else [("next send", 1)])
        assert len([e for e in events if e[0] == "session.info"]) == 1
        assert (lease.lease_id not in {x["lease_id"] for x in _registry()}) is closing
        if closing:
            assert parent["running"] is False and "_deferred_active_session_lease" not in parent
    finally:
        lease.release()
