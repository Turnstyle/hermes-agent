"""Quiet -Q must not ack async-delegation delivery before the follow-up turn is admitted."""

from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace

import cli
from cli import HermesCLI
from hermes_constants import get_hermes_home
from tools import async_delegation as ad
from tools.process_registry import process_registry


def _delegation_row(delegation_id: str) -> dict:
    conn = sqlite3.connect(get_hermes_home() / "state.db")
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT delivery_state, delivery_claim, delivery_attempts FROM async_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _quiet_cli_with_real_session_lease(session_id: str = "s-1"):
    the_cli = object.__new__(HermesCLI)
    the_cli.session_id = session_id
    the_cli.config = {}
    the_cli._active_session_lease = None
    the_cli._console_print = lambda *_a, **_k: None
    the_cli.conversation_history = []
    return the_cli


def test_held_lease_keeps_completion_pending_until_follow_up_accepted(monkeypatch):
    """A notify follow-up that cannot reclaim the session lease leaves the ledger pending."""
    from hermes_cli import quiet_single_query as qsq
    from hermes_cli.active_sessions import release_active_session, try_acquire_active_session

    ad._reset_for_tests()
    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(cli.atexit, "register", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "_handed_off_session_ids", set())
    monkeypatch.setattr(qsq, "quiet_notify_linger_seconds", lambda: 0.0)

    session_id = "quiet-notify-delivery"
    the_cli = _quiet_cli_with_real_session_lease(session_id)
    assert the_cli._claim_active_session("cli") is True

    delegation_id = {"value": ""}
    other_hold: dict = {}
    turn = {"n": 0}

    real_release = HermesCLI._release_active_session

    def release_then_block_other_writer(self):
        real_release(self)
        lease, refusal = try_acquire_active_session(
            session_id=session_id,
            surface="desktop",
            config={},
            metadata={"live_session_id": "other-writer"},
        )
        assert refusal is None and lease is not None
        other_hold["lease"] = lease

    the_cli._release_active_session = lambda: release_then_block_other_writer(the_cli)

    def run_conversation(**kwargs):
        turn["n"] += 1
        if turn["n"] == 1:

            def child():
                return {"summary": "delegated answer", "status": "completed"}

            handle = ad.dispatch_async_delegation(
                goal="quiet notify",
                context=None,
                toolsets=None,
                role="leaf",
                model=None,
                session_key=session_id,
                parent_session_id=None,
                runner=child,
            )
            assert handle["status"] == "dispatched"
            delegation_id["value"] = handle["delegation_id"]
            deadline = time.monotonic() + 5.0
            while ad.active_count() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not ad.active_count()
            return {"final_response": "main answer"}
        raise AssertionError("follow-up turn must not run while another writer holds the lease")

    the_cli.agent = SimpleNamespace(run_conversation=run_conversation, session_id=session_id)

    try:
        try:
            cli._run_quiet_single_query(the_cli, "hello")
        except SystemExit as exc:
            assert exc.code == 0
    finally:
        HermesCLI._release_active_session(the_cli)
        if other_hold.get("lease") is not None:
            release_active_session(other_hold["lease"])
        while not process_registry.completion_queue.empty():
            process_registry.completion_queue.get_nowait()

    assert turn["n"] == 1
    row = _delegation_row(delegation_id["value"])
    assert row["delivery_state"] == "pending"
    assert row["delivery_claim"] is None
    assert row["delivery_attempts"] == 0

    ad.restore_undelivered_completions(process_registry.completion_queue)
    accepted = []

    def accepting_run_turn(text):
        accepted.append(text)
        return {"final_response": text}

    try:
        qsq.continue_quiet_notify_completions(
            session_id,
            accepting_run_turn,
            owns_event=lambda evt: evt.get("session_key") == session_id,
            linger_budget=0.0,
        )
    finally:
        while not process_registry.completion_queue.empty():
            process_registry.completion_queue.get_nowait()

    assert accepted, "restored completion must run as a follow-up"
    row = _delegation_row(delegation_id["value"])
    assert row["delivery_state"] == "delivered"
    ad._reset_for_tests()


def test_twenty_lease_misses_then_acceptance_delivers_once(monkeypatch):
    """Busy-session lease misses must not consume the delivery-attempt budget."""
    from hermes_cli import quiet_single_query as qsq
    from hermes_cli.active_sessions import release_active_session, try_acquire_active_session

    ad._reset_for_tests()
    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(cli.atexit, "register", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "_handed_off_session_ids", set())
    monkeypatch.setattr(qsq, "quiet_notify_linger_seconds", lambda: 0.0)

    session_id = "quiet-notify-twenty-misses"
    the_cli = _quiet_cli_with_real_session_lease(session_id)
    assert the_cli._claim_active_session("cli") is True

    delegation_id = {"value": ""}
    other_hold: dict = {}
    turn = {"n": 0}

    real_release = HermesCLI._release_active_session

    def release_then_block_other_writer(self):
        real_release(self)
        lease, refusal = try_acquire_active_session(
            session_id=session_id,
            surface="desktop",
            config={},
            metadata={"live_session_id": "other-writer"},
        )
        assert refusal is None and lease is not None
        other_hold["lease"] = lease

    the_cli._release_active_session = lambda: release_then_block_other_writer(the_cli)

    def run_conversation(**kwargs):
        turn["n"] += 1
        if turn["n"] == 1:

            def child():
                return {"summary": "delegated answer", "status": "completed"}

            handle = ad.dispatch_async_delegation(
                goal="quiet notify",
                context=None,
                toolsets=None,
                role="leaf",
                model=None,
                session_key=session_id,
                parent_session_id=None,
                runner=child,
            )
            assert handle["status"] == "dispatched"
            delegation_id["value"] = handle["delegation_id"]
            deadline = time.monotonic() + 5.0
            while ad.active_count() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not ad.active_count()
            return {"final_response": "main answer"}
        raise AssertionError("follow-up turn must not run while another writer holds the lease")

    the_cli.agent = SimpleNamespace(run_conversation=run_conversation, session_id=session_id)

    try:
        try:
            cli._run_quiet_single_query(the_cli, "hello")
        except SystemExit as exc:
            assert exc.code == 0
    finally:
        HermesCLI._release_active_session(the_cli)
        if other_hold.get("lease") is not None:
            release_active_session(other_hold["lease"])
        while not process_registry.completion_queue.empty():
            process_registry.completion_queue.get_nowait()

    assert turn["n"] == 1
    did = delegation_id["value"]
    assert did

    def assert_pending_not_dropped():
        row = _delegation_row(did)
        assert row["delivery_state"] == "pending"
        assert row["delivery_claim"] is None
        assert row["delivery_attempts"] == 0

    assert_pending_not_dropped()

    for _ in range(19):
        ad.restore_undelivered_completions(process_registry.completion_queue)
        qsq.continue_quiet_notify_completions(
            session_id,
            lambda _text: qsq.FOLLOW_UP_NOT_ACCEPTED,
            owns_event=lambda evt: evt.get("session_key") == session_id,
            linger_budget=0.0,
        )
        assert_pending_not_dropped()

    accepting_calls: list[str] = []

    def accepting_run_turn(text):
        accepting_calls.append(text)
        return {"final_response": text}

    ad.restore_undelivered_completions(process_registry.completion_queue)
    try:
        qsq.continue_quiet_notify_completions(
            session_id,
            accepting_run_turn,
            owns_event=lambda evt: evt.get("session_key") == session_id,
            linger_budget=0.0,
        )
    finally:
        while not process_registry.completion_queue.empty():
            process_registry.completion_queue.get_nowait()

    assert len(accepting_calls) == 1
    row = _delegation_row(did)
    assert row["delivery_state"] == "delivered"
    assert row["delivery_attempts"] < ad._MAX_DELIVERY_ATTEMPTS
    ad._reset_for_tests()
