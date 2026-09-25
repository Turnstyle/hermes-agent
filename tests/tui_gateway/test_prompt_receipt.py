"""prompt.submit ``client_message_id`` -> durable ``display_metadata.client_message_ids`` + ``prompt.receipt``.

A send that sat in the busy queue used to reach the transcript with nothing tying it to the client's
optimistic bubble, so desktop kept the bubble pending and stacked it below every later reply (t_edb5b1dd).
"""

from hermes_state import SessionDB
from tui_gateway import server


def _desktop_session(monkeypatch, db):
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda _sid: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda _session: None)
    resp = server.handle_request({"id": "c", "method": "session.create", "params": {"cols": 96, "source": "desktop"}})
    assert "result" in resp, resp
    return resp["result"]["session_id"], resp["result"]["stored_session_id"]


def _capture_events(monkeypatch):
    events = []
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None: events.append((event, sid, payload)))
    return events


def _receipts(events):
    return [payload for event, _sid, payload in events if event == "prompt.receipt"]


def test_live_submit_stores_client_id_on_the_row_and_emits_receipt(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key = _desktop_session(monkeypatch, db)
    session = server._sessions[sid]
    events = _capture_events(monkeypatch)
    monkeypatch.setattr(server, "_start_agent_build", lambda *args: None)
    monkeypatch.setattr(server, "_restart_completed_failed_agent_build", lambda *args: False)

    class NoThread:
        def __init__(self, target, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(server.threading, "Thread", NoThread)
    try:
        reply = server.handle_request({"id": "p", "method": "prompt.submit", "params": {
            "session_id": sid, "text": "hello there", "client_message_id": "user-abc"}})["result"]
        rows = db.get_messages_as_conversation(key, include_row_ids=True)
        assert len(rows) == 1
        assert rows[0]["display_metadata"]["client_message_ids"] == ["user-abc"]
        assert _receipts(events) == [
            {"client_message_ids": ["user-abc"], "status": "persisted", "user_row_id": reply["user_row_id"]}]
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_malformed_client_id_is_ignored(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key = _desktop_session(monkeypatch, db)
    session = server._sessions[sid]
    events = _capture_events(monkeypatch)
    monkeypatch.setattr(server, "_start_agent_build", lambda *args: None)
    monkeypatch.setattr(server, "_restart_completed_failed_agent_build", lambda *args: False)

    class NoThread:
        def __init__(self, target, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(server.threading, "Thread", NoThread)
    try:
        server.handle_request({"id": "p", "method": "prompt.submit", "params": {
            "session_id": sid, "text": "hello", "client_message_id": "x" * 500}})
        rows = db.get_messages_as_conversation(key, include_row_ids=True)
        assert not (rows[0].get("display_metadata") or {}).get("client_message_ids")
        assert _receipts(events) == []
        session["running"] = False
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_merged_queued_sends_carry_every_client_id():
    session = {"history_lock": server.threading.RLock()}
    server._enqueue_prompt(session, "first", None, client_message_id="user-1")
    server._enqueue_prompt(session, "second", None, client_message_id="user-2")
    assert session["queued_prompt"]["text"] == "first\n\nsecond"
    assert session["queued_prompt"]["client_message_ids"] == ["user-1", "user-2"]


def test_queued_self_duplicate_of_live_prompt_is_receipted_as_dropped(monkeypatch):
    events = _capture_events(monkeypatch)
    session = {"history_lock": server.threading.RLock()}
    server._start_inflight_turn(session, "run the tests")
    server._enqueue_prompt(session, "run the tests", None, client_message_id="user-dup", sid="s1")
    assert not session.get("queued_prompt")
    assert _receipts(events) == [{"client_message_ids": ["user-dup"], "status": "dropped"}]


def test_drained_queued_prompt_writes_row_with_ids_and_receipts(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key = _desktop_session(monkeypatch, db)
    session = server._sessions[sid]
    events = _capture_events(monkeypatch)
    ran = []
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _s: False)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda rid, sid, session, text, **kw: ran.append((text, kw)))
    try:
        server._enqueue_prompt(session, "one", None, client_message_id="user-1", sid=sid)
        server._enqueue_prompt(session, "two", None, client_message_id="user-2", sid=sid)
        session["running"] = False
        assert server._drain_queued_prompt("r", sid, session) is True
        rows = db.get_messages_as_conversation(key, include_row_ids=True)
        assert [r["content"] for r in rows] == ["one\n\ntwo"]
        assert rows[0]["display_metadata"]["client_message_ids"] == ["user-1", "user-2"]
        assert _receipts(events) == [{
            "client_message_ids": ["user-1", "user-2"], "status": "persisted", "user_row_id": rows[0]["_row_id"]}]
        # The turn adopts the staged row (content match) and carries the ids on its user dict too.
        assert session["_submit_user_row"]["_row_id"] == rows[0]["_row_id"]
        assert ran[0][1]["display_metadata"] == {"client_message_ids": ["user-1", "user-2"]}
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_cancelled_drain_claim_writes_no_row_and_no_receipt(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key = _desktop_session(monkeypatch, db)
    session = server._sessions[sid]
    events = _capture_events(monkeypatch)
    try:
        server._enqueue_prompt(session, "one", None, client_message_id="user-1", sid=sid)
        server._persist_queued_user_row(
            sid, session, "one", ["user-1"], int(session.get("_queued_prompt_generation", 0)) + 1)
        assert db.get_messages_as_conversation(key) == []
        assert _receipts(events) == []
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_cancel_between_claim_and_row_write_leaves_no_row_and_no_receipt(monkeypatch, tmp_path):
    """Checker B6: Stop bumps the generation after the drain claimed the envelope but before the row write."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key = _desktop_session(monkeypatch, db)
    session = server._sessions[sid]
    events = _capture_events(monkeypatch)
    ensure = server._ensure_session_db_row

    def cancel_then_ensure(s):
        with s["history_lock"]:
            s["_queued_prompt_generation"] = int(s.get("_queued_prompt_generation", 0)) + 1
        return ensure(s)

    monkeypatch.setattr(server, "_ensure_session_db_row", cancel_then_ensure)
    try:
        server._persist_queued_user_row(sid, session, "cancelled", ["user-cancelled"], 0)
        assert db.get_messages_as_conversation(key) == []
        assert _receipts(events) == []
        assert "_submit_user_row" not in session
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_new_client_param_is_part_of_the_strict_contract(monkeypatch, tmp_path):
    """Checker B4 (gateway side): the param passes strict dispatch validation on THIS gateway; an
    old strict gateway refuses it with 4000, which the desktop resends without (submit-compat.test.ts)."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, _key = _desktop_session(monkeypatch, db)
    monkeypatch.setattr(server, "_start_agent_build", lambda *args: None)
    monkeypatch.setattr(server, "_restart_completed_failed_agent_build", lambda *args: False)

    class NoThread:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            pass

    monkeypatch.setattr(server.threading, "Thread", NoThread)
    try:
        reply = server.handle_request({"id": "p", "method": "prompt.submit", "params": {
            "session_id": sid, "text": "hi", "client_message_id": "user-1"}})
        assert "result" in reply, reply
        bad = server.handle_request({"id": "q", "method": "prompt.submit", "params": {
            "session_id": sid, "text": "hi", "client_message_idx": "user-1"}})
        assert bad["error"]["code"] == 4000
    finally:
        server._sessions.pop(sid, None)
        db.close()
