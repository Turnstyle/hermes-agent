"""tools/fleet_message_drain.py: turn-end claim/transition logic for fleet_messages_v1.

Every behaviour test runs against an in-memory store with Firestore's updateTime-precondition
semantics, and again against a real Firestore emulator when FLEET_MESSAGES_EMULATOR=host:port is set
(the emulator variant is the integration proof of the atomic claim).
"""
from __future__ import annotations

import datetime
import itertools
import json
import os
import threading
import urllib.request
import uuid

import pytest

from tools import fleet_message_drain as fmd

NOW = datetime.datetime(2026, 9, 28, 6, 0, tzinfo=datetime.timezone.utc)
ME = "tb-king"
EMU = os.environ.get("FLEET_MESSAGES_EMULATOR", "")
PROJECT = "mission-control-444444"


class MemoryStore:
    """In-memory fleet_messages_v1 with Firestore's query + updateTime-precondition semantics."""

    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.ut: dict[str, str] = {}
        self._tick = itertools.count(1)
        self.reads = 0
        self.commits = 0
        self.lock = threading.Lock()
        self.queries: list[tuple] = []

    def _stamp(self, doc_id):
        self.ut[doc_id] = f"2026-09-28T06:00:00.{next(self._tick):06d}Z"
        return self.ut[doc_id]

    def seed(self, doc_id, fields):
        with self.lock:
            self.docs[doc_id] = dict(fields)
            self._stamp(doc_id)

    def query_queued(self, to, limit):
        with self.lock:
            self.reads += 1
            self.queries.append((to, limit))
            rows = [fmd.Row(k, dict(v), self.ut[k]) for k, v in self.docs.items()
                    if v.get("to") == to and v.get("status") == "queued"]
        rows.sort(key=lambda r: fmd.parse_ts(r.fields["created_at"]))
        return rows[:limit]

    def get(self, doc_id):
        with self.lock:
            return fmd.Row(doc_id, dict(self.docs[doc_id]), self.ut[doc_id]) if doc_id in self.docs else None

    def update(self, doc_id, fields, update_time):
        fmd._check_write(doc_id, fields, update_time)
        with self.lock:
            self.commits += 1
            if self.ut.get(doc_id) != update_time:
                raise fmd.PreconditionFailed("FAILED_PRECONDITION")
            self.docs[doc_id].update(fields)
            return self._stamp(doc_id)

    # test helper: an outside writer (e.g. a late sender success) touches the doc
    def outside_write(self, doc_id, fields):
        with self.lock:
            self.docs[doc_id].update(fields)
            self._stamp(doc_id)


class EmulatorStore(fmd.FirestoreStore):
    """The production REST store against the emulator, plus seed/outside-write helpers."""

    def __init__(self):
        super().__init__(base_url=f"http://{EMU}", project=PROJECT, database=fmd.DEFAULT_DATABASE,
                         token_fn=lambda: "owner")
        self.base = f"http://{EMU}/v1/projects/{PROJECT}/databases/{fmd.DEFAULT_DATABASE}/documents"
        self.queries: list[tuple] = []

    def query_queued(self, to, limit):
        self.queries.append((to, limit))
        return super().query_queued(to, limit)

    def _raw(self, url, method, body=None):
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     method=method, headers={"Authorization": "Bearer owner",
                                                             "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)

    def seed(self, doc_id, fields):
        body = {"fields": {k: fmd.enc(v, k) for k, v in fields.items()}}
        self._raw(f"{self.base}/{fmd.COLLECTION}?documentId={doc_id}", "POST", body)

    def outside_write(self, doc_id, fields):
        mask = "&".join(f"updateMask.fieldPaths={k}" for k in fields)
        self._raw(f"{self.base}/{fmd.COLLECTION}/{doc_id}?{mask}", "PATCH",
                  {"fields": {k: fmd.enc(v, k) for k, v in fields.items()}})


def _wipe_emulator():
    req = urllib.request.Request(
        f"http://{EMU}/emulator/v1/projects/{PROJECT}/databases/{fmd.DEFAULT_DATABASE}/documents", method="DELETE")
    urllib.request.urlopen(req, timeout=10)


@pytest.fixture(params=["memory", "emulator"])
def store(request):
    if request.param == "emulator":
        if not EMU:
            pytest.skip("FLEET_MESSAGES_EMULATOR not set")
        _wipe_emulator()
        return EmulatorStore()
    return MemoryStore()


def msg(minutes_ago, *, to=ME, status="queued", kind="dm", body=None, sender="tb-cndr", **extra):
    created = NOW - datetime.timedelta(minutes=minutes_ago)
    doc = {"from": sender, "to": to, "task_id": "t_demo", "kind": kind,
           "body": body if body is not None else f"hello {minutes_ago}",
           "status": status, "attempts": 1, "last_error": "target_busy",
           "created_at": fmd.rfc3339(created), "updated_at": fmd.rfc3339(created),
           "expires_at": fmd.rfc3339(created + datetime.timedelta(hours=24))}
    doc.update(extra)
    return doc


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def drain_all(store, *, now=NOW, outcome=None):
    """Simulate successive turn ends: one claim per turn end, the turn settles, repeat."""
    handled = []
    while True:
        claimed = fmd.claim_next(store, ME, limit=10, now=now)
        if claimed is None:
            return handled
        assert fmd.mark_read(store, claimed, now=now)
        handled.append(claimed.doc_id)
        fmd.finish(store, claimed, outcome or {"status": "settled", "text": "ok"}, max_attempts=5, now=now)


# ---------- ordering + exactly once ----------
def test_turn_ends_pick_up_each_queued_doc_once_in_created_at_order(store):
    a, b, c = uid("a"), uid("b"), uid("c")
    store.seed(b, msg(20))
    store.seed(c, msg(5))
    store.seed(a, msg(40))            # oldest, seeded out of order on purpose
    assert drain_all(store) == [a, b, c]
    for doc_id in (a, b, c):
        row = store.get(doc_id)
        assert row.fields["status"] == "done"
        assert row.fields["delivered_at"] and row.fields["read_at"] and row.fields["done_at"]
    assert drain_all(store) == []     # nothing left: a later turn end handles nothing twice


def test_one_query_per_turn_end_scoped_to_me(store):
    store.seed(uid("mine"), msg(10))
    store.seed(uid("theirs"), msg(30, to="tb-cndr"))
    store.queries.clear()
    claimed = fmd.claim_next(store, ME, limit=7, now=NOW)
    assert claimed is not None and claimed.fields["to"] == ME
    assert store.queries == [(ME, 7)]   # exactly one query, filtered on to == me


def test_foreign_or_non_queued_rows_are_never_written():
    # Defense in depth: even a store that returned someone else's doc must not get a write.
    s = MemoryStore()
    s.seed("foreign", msg(10, to="someone-else"))
    s.seed("stale", msg(9, status="delivered"))
    s.query_queued = lambda to, limit: [s.get("foreign"), s.get("stale")]
    assert fmd.claim_next(s, ME, now=NOW) is None
    assert s.commits == 0


def test_expired_docs_are_left_for_the_expiry_job(store):
    old, fresh = uid("old"), uid("fresh")
    store.seed(old, msg(25 * 60))     # expires_at is 1h in the past
    store.seed(fresh, msg(3))
    assert drain_all(store) == [fresh]
    assert store.get(old).fields["status"] == "queued"


# ---------- atomic claim / races ----------
class _Snapshot:
    """A store view whose query returns a fixed (possibly stale) snapshot; writes go to the real store."""

    def __init__(self, store, rows):
        self._store, self._rows = store, rows

    def query_queued(self, to, limit):
        return [fmd.Row(r.doc_id, dict(r.fields), r.update_time) for r in self._rows]

    def update(self, doc_id, fields, update_time):
        return self._store.update(doc_id, fields, update_time)


def test_racing_turn_ends_claim_a_doc_once(store):
    doc = uid("race")
    store.seed(doc, msg(10))
    rows = store.query_queued(ME, 10)   # both racers read the same snapshot before either writes
    winners, barrier = [], threading.Barrier(4)

    def racer():
        barrier.wait()
        claimed = fmd.claim_next(_Snapshot(store, rows), ME, now=NOW)
        if claimed:
            winners.append(claimed.doc_id)

    threads = [threading.Thread(target=racer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert winners == [doc]
    assert store.get(doc).fields["status"] == "delivered"


def test_late_sender_success_wins_and_the_drain_skips(store):
    doc = uid("late")
    store.seed(doc, msg(10))
    snapshot = store.query_queued(ME, 10)
    # The sender's direct delivery succeeded after all and wrote delivered first.
    store.outside_write(doc, {"status": "delivered", "attempts": 2})
    assert fmd.claim_next(_Snapshot(store, snapshot), ME, now=NOW) is None
    row = store.get(doc)
    assert row.fields["status"] == "delivered" and row.fields["attempts"] == 2


def test_writer_that_moves_a_claimed_doc_is_not_overwritten(store):
    doc = uid("moved")
    store.seed(doc, msg(10))
    claimed = fmd.claim_next(store, ME, now=NOW)
    store.outside_write(doc, {"status": "failed", "last_error": "operator"})
    assert fmd.mark_read(store, claimed, now=NOW) is False
    assert store.get(doc).fields["status"] == "failed"


# ---------- errors never lose a doc ----------
def test_failed_turn_requeues_with_attempts_and_last_error_then_retries(store):
    doc = uid("retry")
    store.seed(doc, msg(10))
    claimed = fmd.claim_next(store, ME, now=NOW)
    fmd.mark_read(store, claimed, now=NOW)
    fmd.finish(store, claimed, {"status": "failed", "error": "provider 529"}, max_attempts=5, now=NOW)
    row = store.get(doc)
    assert row.fields["status"] == "queued"
    assert row.fields["attempts"] == 2 and "provider 529" in row.fields["last_error"]
    assert drain_all(store) == [doc]                     # next turn end picks it up again
    assert store.get(doc).fields["status"] == "done"


def test_repeated_failures_end_in_failed_not_deleted(store):
    doc = uid("dead")
    store.seed(doc, msg(10, attempts=0, last_error=None))
    for _ in range(3):
        claimed = fmd.claim_next(store, ME, now=NOW)
        assert claimed is not None
        fmd.mark_read(store, claimed, now=NOW)
        fmd.finish(store, claimed, {"status": "cancelled"}, max_attempts=3, now=NOW)
    row = store.get(doc)
    assert row is not None
    assert row.fields["status"] == "failed" and row.fields["attempts"] == 3
    assert row.fields["failed_at"] and "cancelled" in row.fields["last_error"]
    assert fmd.claim_next(store, ME, now=NOW) is None


def test_terminal_outcome_is_recorded_once(store):
    doc = uid("once")
    store.seed(doc, msg(10))
    claimed = fmd.claim_next(store, ME, now=NOW)
    fmd.mark_read(store, claimed, now=NOW)
    assert fmd.finish(store, claimed, {"status": "settled"}, max_attempts=5, now=NOW) is True
    assert fmd.finish(store, claimed, {"status": "failed", "error": "late"}, max_attempts=5, now=NOW) is False
    assert store.get(doc).fields["status"] == "done"


def test_release_returns_an_unstarted_claim_without_an_attempt(store):
    doc = uid("rel")
    store.seed(doc, msg(10))
    claimed = fmd.claim_next(store, ME, now=NOW)
    assert fmd.release(store, claimed, now=NOW)
    row = store.get(doc)
    assert row.fields["status"] == "queued" and row.fields["attempts"] == 1


def test_malformed_doc_is_rejected_not_run(store):
    bad, good = uid("bad"), uid("good")
    store.seed(bad, msg(20, kind="carrier_pigeon"))
    store.seed(good, msg(10))
    assert drain_all(store) == [good]
    row = store.get(bad)
    assert row.fields["status"] == "rejected" and "unknown kind" in row.fields["last_error"]


# ---------- rendering ----------
def test_dm_renders_as_attributed_bot_chat_message():
    c = fmd.Claimed("m1", msg(5, body="please rerun the probe"), "ut", ME)
    text, author, meta = fmd.render_input(c)
    assert "please rerun the probe" in text and "Message from 🤖 tb-cndr (@tb-cndr)" in text
    assert "m1" in text and "message_agent" in text
    assert author == {"id": "bot:tb-cndr", "name": "tb-cndr", "is_bot": True} and meta == {}


def test_notify_wake_renders_as_a_system_wake():
    c = fmd.Claimed("w1", msg(5, kind="notify_wake", body="Kanban t_x completed"), "ut", ME)
    text, author, meta = fmd.render_input(c)
    assert "Kanban t_x completed" in text and "notification" in text
    assert author is None and meta == {"notification_category": "result"}


# ---------- config + write guard ----------
def test_config_is_dark_by_default_and_validated():
    assert fmd.drain_config({}) is None
    assert fmd.drain_config({"fleet_messages": {"drain_on_turn_end": "yes"}}) is None
    assert fmd.drain_config({"fleet_messages": {"drain_on_turn_end": True}}) is None   # emulator needs a host
    cfg = fmd.drain_config({"fleet_messages": {"drain_on_turn_end": True, "emulator_host": "127.0.0.1:1",
                                               "limit": 999}})
    assert cfg.target == "emulator" and cfg.limit == fmd.MAX_LIMIT
    assert fmd.drain_config({"fleet_messages": {"drain_on_turn_end": True, "target": "live"}}).target == "live"


def test_writes_are_limited_to_status_bookkeeping():
    with pytest.raises(fmd.Refused):
        fmd._check_write("d", {"body": "x"}, "ut")
    with pytest.raises(fmd.Refused):
        fmd._check_write("d", {"to": "x"}, "ut")
    with pytest.raises(fmd.Refused):
        fmd._check_write("d", {"status": "done"}, "")
    with pytest.raises(fmd.Refused):
        fmd._check_write("a/b", {"status": "done"}, "ut")


def test_bot_identity_is_the_profile_folder(tmp_path):
    assert fmd.bot_identity(tmp_path / "profiles" / "tb-king") == "tb-king"
    assert fmd.bot_identity(tmp_path / ".hermes") == "default"
