"""A claimed completion whose follow-up turn was never admitted goes back without spending an attempt.

``defer_event_delivery`` is the event-level twin of ``release_event_delivery``: both hand the row back to
pending and return the in-memory offer to the orphan sweep, but a deferral refunds the claim's attempt
(the turn never started), and in a managed worker it routes through the owner ledger, never local SQL.
"""

import os
import queue
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent import runtime_session_store
from tools import async_delegation as ad


@pytest.fixture(autouse=True)
def _clean_state():
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


def _pending(delegation_id: str) -> None:
    event = {"type": "async_delegation", "delegation_id": delegation_id, "session_key": "bot-chat",
             "parent_session_id": "parent", "dispatched_at": 1.0, "status": "completed", "summary": "done"}
    ad._persist_dispatch(event)
    ad._persist_completion(event, {"status": "completed", "summary": "done"})


def _delivery(delegation_id: str) -> tuple:
    with ad._DB_LOCK, ad._transaction() as conn:
        return conn.execute("""SELECT delivery_state, delivery_claim, delivery_attempts, updated_at
               FROM async_delegations WHERE delegation_id=?""", (delegation_id,)).fetchone()


def test_admission_denied_attempt_is_refunded_and_the_row_is_offered_again(monkeypatch):
    monkeypatch.setattr(ad, "_owner_liveness", lambda: (lambda pid, started: False))  # owner process gone
    _pending("deferred")
    offered: queue.Queue = queue.Queue()
    later = _delivery("deferred")[3] + ad._ORPHAN_STALE_S + 1

    assert ad.sweep_orphaned_completions(offered, now=later) == 1
    evt = offered.get_nowait()
    claim = ad.claim_event_delivery(evt, "cli-quiet")
    assert claim and _delivery("deferred")[:3] == ("pending", claim, 1)

    ad.defer_event_delivery(evt, claim)  # the follow-up turn was refused before it ran

    assert _delivery("deferred")[:3] == ("pending", None, 0)  # attempt refunded, claim cleared
    redo = _delivery("deferred")[3] + ad._ORPHAN_STALE_S + 1
    assert ad.sweep_orphaned_completions(offered, now=redo) == 1  # the offer went back to the sweep
    assert offered.get_nowait()["delegation_id"] == "deferred"


def test_release_still_spends_the_attempt_the_deferral_refunds():
    _pending("released")
    evt = {"type": "async_delegation", "delegation_id": "released"}
    for _ in range(2):
        claim = ad.claim_event_delivery(evt, "cli-quiet")
        ad.defer_event_delivery(evt, claim)
    assert _delivery("released")[2] == 0
    claim = ad.claim_event_delivery(evt, "cli-quiet")
    ad.release_event_delivery(evt, claim)  # a turn that started and failed did use its attempt
    assert _delivery("released")[:3] == ("pending", None, 1)


@pytest.mark.parametrize("evt, claim", [
    ({"type": "async_delegation", "delegation_id": "unclaimed"}, ""),  # non-durable copy: no token
    ({"type": "process_complete", "delegation_id": "unclaimed"}, "tok"),  # not a delegation event
])
def test_deferral_ignores_events_without_a_durable_claim(evt, claim):
    _pending("unclaimed")
    ad.defer_event_delivery(evt, claim)
    assert _delivery("unclaimed")[:3] == ("pending", None, 0)


class _OwnerLedger:
    def __init__(self):
        self.scope = {"pid": os.getpid(), "session_id": "parent"}
        self.calls = []

    def _apply(self, operation, payload):
        self.calls.append((operation, payload))
        return {"value": True}


def test_managed_worker_deferral_routes_through_the_owner_ledger(monkeypatch):
    from tools import async_delegation_worker as worker

    monkeypatch.setattr(runtime_session_store, "_worker_process", True)
    monkeypatch.setattr(worker, "_store", None)
    store = _OwnerLedger()
    worker.bind_worker_delegation_store(store)
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("worker opened canonical SQLite"))
    evt = {"type": "async_delegation", "delegation_id": "deleg_owned", "completed_at": time.time()}
    home_key = ("some-home", "deleg_owned")
    ad._offered.add(home_key)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(ad.defer_event_delivery, evt, "tok").result()

    assert [op for op, _ in store.calls] == ["delegation.defer"]
    assert store.calls[0][1]["delegation_id"] == "deleg_owned"
    assert home_key not in ad._offered  # orphan offer semantics are kept in the worker too
