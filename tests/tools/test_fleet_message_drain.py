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
import urllib.error
import urllib.request
import uuid
from types import SimpleNamespace

import pytest

from tools import fleet_message_drain as fmd

NOW = datetime.datetime(2026, 9, 28, 6, 0, tzinfo=datetime.timezone.utc)
ME = "tb-king"
EMU = os.environ.get("FLEET_MESSAGES_EMULATOR", "")
PROJECT = "mission-control-444444"


def test_live_token_falls_back_to_gcloud_and_warns_once_per_failed_streak(monkeypatch, caplog):
    import google.auth
    from google.auth.exceptions import RefreshError

    class Expired:
        valid = False

        def refresh(self, request):
            raise RefreshError("expired")

    monkeypatch.setattr(google.auth, "default", lambda **kwargs: (Expired(), None))
    monkeypatch.setattr(fmd, "_live_credentials", None)
    monkeypatch.setattr(fmd, "_gcloud_token_cached", "")
    monkeypatch.setattr(fmd, "_gcloud_token_until", 0)
    monkeypatch.setattr(fmd, "_credentials_warned", False)
    calls = []

    def gcloud(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(stdout="gcloud-token\n")

    monkeypatch.setattr(fmd.subprocess, "run", gcloud)
    monkeypatch.setenv("PYTHONPATH", "/hermes/bootstrap/site-packages")
    monkeypatch.setenv("PYTHONHOME", "/hermes/python")
    assert fmd._live_token() == "gcloud-token"
    assert fmd._live_token() == "gcloud-token"
    assert len(calls) == 1 and calls[0]["timeout"] <= 3
    # gcloud runs its own Python: the Hermes bootstrap's PYTHONPATH/PYTHONHOME must not leak into it.
    assert "PYTHONPATH" not in calls[0]["env"] and "PYTHONHOME" not in calls[0]["env"]

    monkeypatch.setattr(fmd, "_gcloud_token_cached", "")
    monkeypatch.setattr(fmd.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(OSError("offline")))
    for _ in range(2):
        with pytest.raises(fmd.NoGoogleCredentials, match="ADC expired and gcloud token failed"):
            fmd._live_token()
    assert [r.message for r in caplog.records if "no Google credentials" in r.message] == [
        "fleet message drain: no Google credentials (ADC expired and gcloud token failed); "
        "drain skipped, turns unaffected"]


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
        rows.sort(key=lambda r: (fmd.parse_ts(r.fields.get("created_at")) is None,
                                 fmd.parse_ts(r.fields.get("created_at")) or
                                 datetime.datetime.max.replace(tzinfo=datetime.timezone.utc), r.doc_id))
        return rows[:limit]

    def query_status(self, status, limit):
        with self.lock:
            self.reads += 1
            return [fmd.Row(k, dict(v), self.ut[k]) for k, v in self.docs.items()
                    if v.get("status") == status][:limit]

    def get(self, doc_id):
        with self.lock:
            return fmd.Row(doc_id, dict(self.docs[doc_id]), self.ut[doc_id]) if doc_id in self.docs else None

    def create(self, doc_id, fields):
        with self.lock:
            self.commits += 1
            if doc_id in self.docs:
                raise urllib.error.HTTPError("memory://", 409, "ALREADY_EXISTS", None, None)
            self.docs[doc_id] = dict(fields)
            self._stamp(doc_id)

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


def test_idle_tick_delivers_one_queued_doc_and_keeps_order(tmp_path):
    store = MemoryStore()
    store.seed("older", msg(5))
    store.seed("newer", msg(2))
    received = []

    assert fmd.idle_tick(tmp_path / "profiles" / ME, store=store,
                         config=fmd.DrainConfig(target="emulator", emulator_host="fake"),
                         busy=lambda: False,
                         run_turn=lambda text, author, metadata: received.append(text) or {"status": "settled"},
                         now=NOW) is True
    assert "older" in received[0]
    assert store.get("older").fields["status"] == "done"
    assert store.get("newer").fields["status"] == "queued"


@pytest.mark.platforms("posix")
def test_idle_cli_turn_runs_with_parent_turn_lock_and_records_receipt(tmp_path, monkeypatch):
    from tools.bot_relay import TurnBusyError, acquire_turn_lock

    home = tmp_path / "profiles" / ME
    home.mkdir(parents=True)
    store = MemoryStore()
    store.seed("idle-cli", msg(5))
    def child_boundary(argv, **kwargs):
        assert "-Q" in argv and argv[argv.index("-p") + 1] == ME
        with pytest.raises(TurnBusyError):
            with acquire_turn_lock(tmp_path, ME, timeout_seconds=0):
                pass
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(fmd.subprocess, "run", child_boundary)
    monkeypatch.setattr("tools.bot_relay._hermes_cli", lambda: "hermes")
    monkeypatch.setattr("tools.bot_relay.delivery_env", lambda author, home: {})
    with acquire_turn_lock(tmp_path, ME, timeout_seconds=0):
        assert fmd.idle_tick(home, store=store,
                             config=fmd.DrainConfig(target="emulator", emulator_host="fake"),
                             busy=lambda: False, now=NOW)
    row = store.get("idle-cli").fields
    assert row["status"] == "done" and row["delivered_at"] and row["read_at"] and row["done_at"]


def test_stale_owner_does_not_block_idle_drain(tmp_path, monkeypatch):
    from tools import bot_live_delivery

    home = tmp_path / "profiles" / ME
    store = MemoryStore()
    store.seed("stale", msg(5))
    monkeypatch.setattr(bot_live_delivery, "find_canonical_owner", lambda _: {"lease_id": "stale"})
    monkeypatch.setattr(bot_live_delivery, "find_canonical_live_owner", lambda _: None)
    assert fmd.idle_tick(home, store=store,
                         config=fmd.DrainConfig(target="emulator", emulator_host="fake"),
                         run_turn=lambda *_: {"status": "settled"}, now=NOW)
    assert store.get("stale").fields["status"] == "done"


def test_idle_tick_busy_recipient_does_not_claim(tmp_path):
    store = MemoryStore()
    store.seed("waiting", msg(5))
    assert fmd.idle_tick(tmp_path / "profiles" / ME, store=store,
                         config=fmd.DrainConfig(target="emulator", emulator_host="fake"),
                         busy=lambda: True, run_turn=lambda *_: pytest.fail("ran while busy"),
                         now=NOW) is False
    assert store.get("waiting").fields["status"] == "queued"
    assert store.commits == 0


def test_idle_tick_releases_claim_if_recipient_becomes_busy(tmp_path):
    store = MemoryStore()
    store.seed("racing", msg(5))
    checks = iter((False, True))
    assert fmd.idle_tick(tmp_path / "profiles" / ME, store=store,
                         config=fmd.DrainConfig(target="emulator", emulator_host="fake"),
                         busy=lambda: next(checks), run_turn=lambda *_: pytest.fail("ran while busy"),
                         now=NOW) is False
    row = store.get("racing")
    assert row.fields["status"] == "queued" and row.fields["attempts"] == 1
    assert "read_at" not in row.fields


def test_api_agent_turn_uses_same_claim_and_records_settled_receipt(monkeypatch, tmp_path):
    from tools import bot_mode_dm

    store = MemoryStore()
    store.seed("api-message", msg(5))
    config = fmd.DrainConfig(target="emulator", emulator_host="fake")
    monkeypatch.setattr(fmd, "drain_config", lambda cfg=None: config)
    monkeypatch.setattr(fmd, "store_for", lambda cfg: store)
    monkeypatch.setattr(fmd, "utcnow", lambda: NOW)
    monkeypatch.setattr(bot_mode_dm, "is_canonical_bot_chat", lambda agent: True)
    calls = []
    monkeypatch.setattr(fmd, "_idle_cli_turn", lambda home, text, author: (
        calls.append((home, text, author)) or {"status": "settled"}))

    assert fmd.drain_agent_turn(SimpleNamespace(session_id="chat"), tmp_path / "profiles" / ME) is True
    assert store.get("api-message").fields["status"] == "done"
    assert calls[0][0] == tmp_path / "profiles" / ME
    assert calls[0][2]["name"] == "tb-cndr"


def test_api_drain_dispatch_returns_before_turn_and_serializes_next_turn(monkeypatch, tmp_path):
    from tools import bot_mode_dm

    home = tmp_path / "profiles" / ME
    entered = threading.Event()
    release = threading.Event()
    done = threading.Event()
    follower_started = threading.Event()
    next_turn = threading.Event()
    monkeypatch.setattr(bot_mode_dm, "is_canonical_bot_chat", lambda _: True)
    monkeypatch.setattr(fmd, "drain_config", lambda: fmd.DrainConfig(target="emulator", emulator_host="fake"))

    def drained(*_):
        entered.set()
        assert release.wait(2)
        done.set()
        raise RuntimeError("drained turn failed")

    monkeypatch.setattr(fmd, "drain_agent_turn", drained)
    agent = SimpleNamespace(session_id="chat")
    assert fmd.schedule_drain_agent_turn(agent, home, []) is True
    assert entered.wait(2)
    assert not done.is_set()
    def _next_api_turn():
        follower_started.set()
        with fmd.api_turn_lock(home, "chat", agent=agent):
            next_turn.set()

    follower = threading.Thread(target=_next_api_turn, daemon=True)
    follower.start()
    assert follower_started.wait(2)
    assert not next_turn.is_set()
    release.set()
    assert done.wait(2)
    assert next_turn.wait(2)
    follower.join(timeout=2)


@pytest.mark.platforms("posix")
def test_api_turn_does_not_wait_for_profile_flock(monkeypatch, tmp_path):
    from tools import bot_mode_dm
    from tools.bot_relay import TurnBusyError, acquire_turn_lock

    home = tmp_path / "profiles" / ME
    agent = SimpleNamespace(session_id="chat")
    monkeypatch.setattr(bot_mode_dm, "is_canonical_bot_chat", lambda _: True)
    monkeypatch.setattr(fmd, "drain_config", lambda: fmd.DrainConfig(target="emulator", emulator_host="fake"))
    with acquire_turn_lock(tmp_path, ME, timeout_seconds=0):
        start = fmd.time.monotonic()
        with fmd.api_turn_lock(home, "chat", agent=agent):
            pass
        assert fmd.time.monotonic() - start < 0.5


def test_api_drain_burst_has_one_worker_per_profile(monkeypatch, tmp_path):
    from tools import bot_mode_dm

    home = tmp_path / "profiles" / ME
    entered = threading.Event()
    release = threading.Event()
    done = threading.Event()
    monkeypatch.setattr(bot_mode_dm, "is_canonical_bot_chat", lambda _: True)
    monkeypatch.setattr(fmd, "drain_config", lambda: fmd.DrainConfig(target="emulator", emulator_host="fake"))

    def drained(*_):
        entered.set()
        assert release.wait(2)
        done.set()

    monkeypatch.setattr(fmd, "drain_agent_turn", drained)
    agent = SimpleNamespace(session_id="chat")
    try:
        assert fmd.schedule_drain_agent_turn(agent, home)
        assert entered.wait(2)
        assert sum(fmd.schedule_drain_agent_turn(agent, home) for _ in range(50)) == 0
    finally:
        release.set()
    assert done.wait(2)


@pytest.mark.platforms("posix")
def test_api_drain_retries_after_profile_flock_is_busy(monkeypatch, tmp_path):
    from tools import bot_mode_dm
    from tools.bot_relay import acquire_turn_lock

    home = tmp_path / "profiles" / ME
    store = MemoryStore()
    store.seed("flock-busy-message", msg(5))
    done = threading.Event()
    monkeypatch.setattr(bot_mode_dm, "is_canonical_bot_chat", lambda _: True)
    monkeypatch.setattr(fmd, "drain_config", lambda cfg=None: fmd.DrainConfig(target="emulator", emulator_host="fake"))
    monkeypatch.setattr(fmd, "store_for", lambda cfg: store)
    monkeypatch.setattr(fmd, "utcnow", lambda: NOW)
    monkeypatch.setattr(fmd, "_idle_cli_turn", lambda *_: {"status": "settled"})
    original = fmd.drain_agent_turn

    def drained(*_):
        try:
            original(*_)
        finally:
            done.set()

    monkeypatch.setattr(fmd, "drain_agent_turn", drained)
    agent = SimpleNamespace(session_id="chat")
    with acquire_turn_lock(tmp_path, ME, timeout_seconds=0):
        assert fmd.schedule_drain_agent_turn(agent, home)
        deadline = fmd.time.monotonic() + 2
        while str(home.resolve()) in fmd._api_drain_flights and fmd.time.monotonic() < deadline:
            done.wait(0.01)
        assert str(home.resolve()) not in fmd._api_drain_flights
        assert store.get("flock-busy-message").fields["status"] == "queued"
    done.clear()
    assert fmd.schedule_drain_agent_turn(agent, home)
    assert done.wait(2)
    assert store.get("flock-busy-message").fields["status"] == "done"


def test_api_request_does_not_wait_for_drained_turn(monkeypatch, tmp_path):
    from tools import bot_mode_dm
    from tools.bot_relay import TurnBusyError

    home = tmp_path / "profiles" / ME
    entered = threading.Event()
    release = threading.Event()
    done = threading.Event()
    monkeypatch.setattr(bot_mode_dm, "is_canonical_bot_chat", lambda _: True)
    monkeypatch.setattr(fmd, "drain_config", lambda: fmd.DrainConfig(target="emulator", emulator_host="fake"))

    def drained(*_):
        entered.set()
        try:
            assert release.wait(2)
        finally:
            done.set()

    monkeypatch.setattr(fmd, "drain_agent_turn", drained)
    try:
        assert fmd.schedule_drain_agent_turn(SimpleNamespace(session_id="chat"), home)
        assert entered.wait(2)
        start = fmd.time.monotonic()
        with pytest.raises(TurnBusyError):
            with fmd.api_turn_lock(home, "chat", agent=SimpleNamespace(session_id="chat")):
                pass
        assert fmd.time.monotonic() - start < 1.5
    finally:
        release.set()
    assert done.wait(2)


@pytest.mark.platforms("posix")
def test_api_drain_timeout_requeues_and_releases_profile_lock(monkeypatch, tmp_path):
    from tools import bot_mode_dm
    from tools.bot_relay import acquire_turn_lock

    store = MemoryStore()
    store.seed("timeout-message", msg(5))
    home = tmp_path / "profiles" / ME
    config = fmd.DrainConfig(target="emulator", emulator_host="fake")
    monkeypatch.setattr(fmd, "drain_config", lambda cfg=None: config)
    monkeypatch.setattr(fmd, "store_for", lambda cfg: store)
    monkeypatch.setattr(fmd, "utcnow", lambda: NOW)
    monkeypatch.setattr(bot_mode_dm, "is_canonical_bot_chat", lambda _: True)
    monkeypatch.setattr(fmd, "_idle_cli_turn", lambda *_: (_ for _ in ()).throw(
        fmd.subprocess.TimeoutExpired("fake-hermes", 1)))
    done = threading.Event()
    original = fmd.drain_agent_turn

    def drained(*args):
        try:
            original(*args)
        finally:
            done.set()

    monkeypatch.setattr(fmd, "drain_agent_turn", drained)
    assert fmd.schedule_drain_agent_turn(SimpleNamespace(session_id="chat"), home)
    assert done.wait(2)
    assert store.get("timeout-message").fields["status"] == "queued"
    assert "timed out" in store.get("timeout-message").fields["last_error"]
    # done.set() fires inside the worker's `with acquire_turn_lock`, a moment before the thread
    # leaves it and drops the flock; wait briefly for the release instead of probing once.
    with acquire_turn_lock(tmp_path, ME, timeout_seconds=2):
        pass


def test_api_drain_cli_child_has_turn_deadline(monkeypatch, tmp_path):
    from tools import bot_relay

    home = tmp_path / "profiles" / ME
    seen = []
    monkeypatch.setattr(bot_relay, "_hermes_cli", lambda: "fake-hermes")
    monkeypatch.setattr(bot_relay, "delivery_env", lambda author, profile_home: {})

    def fake_run(argv, **kwargs):
        payload = fmd.Path(argv[-1])
        seen.append((payload.read_text(), kwargs["timeout"], payload, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(fmd.subprocess, "run", fake_run)
    assert fmd._idle_cli_turn(home, "queued message", None) == {"status": "settled", "error": ""}
    assert seen[0][0] == "queued message"
    assert seen[0][1] == bot_relay.TURN_ATTEMPT_TIMEOUT_SECONDS
    assert seen[0][3]["stdout"] == fmd.subprocess.DEVNULL
    assert seen[0][3]["stderr"] == fmd.subprocess.DEVNULL
    assert not seen[0][2].exists()


def test_api_drain_worker_keeps_each_profile_scope(monkeypatch, tmp_path):
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
    from tools import bot_mode_dm

    seen = []
    done = threading.Event()
    monkeypatch.setattr(bot_mode_dm, "is_canonical_bot_chat", lambda _: True)
    monkeypatch.setattr(fmd, "drain_config", lambda: fmd.DrainConfig(target="emulator", emulator_host="fake"))

    def drained(agent, home, history):
        seen.append((str(home), str(get_hermes_home())))
        done.set()

    monkeypatch.setattr(fmd, "drain_agent_turn", drained)
    for name in ("a", "b", "a"):
        home = tmp_path / "profiles" / name
        token = set_hermes_home_override(home)
        try:
            done.clear()
            assert fmd.schedule_drain_agent_turn(SimpleNamespace(session_id="chat"), home)
            assert done.wait(2)
        finally:
            reset_hermes_home_override(token)
    assert seen == [(str(tmp_path / "profiles" / name), str(tmp_path / "profiles" / name))
                    for name in ("a", "b", "a")]


# ---------- ordering + exactly once ----------
def test_turn_ends_pick_up_each_queued_doc_once_in_created_at_order(store):
    a, b, c = uid("a"), uid("b"), uid("c")
    store.seed(b, msg(20))
    store.seed(c, msg(5))
    store.seed(a, msg(25))            # oldest, seeded out of order on purpose
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


def test_rest_query_queued_sorts_full_page_without_server_order(monkeypatch):
    transport = fmd.FirestoreStore(base_url="http://unused", project="p", database="d",
                                   token_fn=lambda: "unused")
    request = []
    docs = [("a-newest", 1), ("b-oldest", 30), ("c-middle", 10)]
    response = [{"document": {"name": transport.prefix + doc_id,
                              "fields": {"created_at": fmd.enc(msg(minutes)["created_at"], "created_at")},
                              "updateTime": "2026-09-28T06:00:00Z"}}
                for doc_id, minutes in docs]
    monkeypatch.setattr(transport, "_req", lambda url, method, body: request.append(body) or response)

    rows = transport.query_queued(ME, 2)

    assert len(request) == transport.reads == 1
    structured = request[0]["structuredQuery"]
    assert "orderBy" not in structured and structured["limit"] == fmd.MAX_LIMIT
    assert [row.doc_id for row in rows] == ["b-oldest", "c-middle"]


def test_foreign_or_non_queued_rows_are_never_written():
    # Defense in depth: even a store that returned someone else's doc must not get a write.
    s = MemoryStore()
    s.seed("foreign", msg(10, to="someone-else"))
    s.seed("stale", msg(9, status="delivered"))
    s.query_queued = lambda to, limit: [s.get("foreign"), s.get("stale")]
    assert fmd.claim_next(s, ME, now=NOW) is None
    assert s.commits == 0


def test_expired_docs_are_moved_out_of_the_queued_page(store):
    old, fresh = uid("old"), uid("fresh")
    store.seed(old, msg(25 * 60))     # expires_at is 1h in the past
    store.seed(fresh, msg(3))
    assert drain_all(store) == [fresh]
    assert store.get(old).fields["status"] == "expired"


def test_full_expired_page_cannot_starve_a_fresh_message(store):
    limit = 10
    for index in range(limit):
        store.seed(uid(f"expired-{index}"), msg(26 * 60 + index))
    fresh = uid("fresh")
    store.seed(fresh, msg(3))
    assert fmd.claim_next(store, ME, limit=limit, now=NOW) is None
    claimed = fmd.claim_next(store, ME, limit=limit, now=NOW)
    assert claimed is not None and claimed.doc_id == fresh
    assert store.reads == 2


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


def test_reclaimer_requeues_stale_claims_and_fails_at_attempt_limit():
    store = MemoryStore()
    store.seed("stale-read", msg(40, status="read", attempts=1))
    store.seed("stale-delivered", msg(40, status="delivered", attempts=2))
    store.seed("fresh-delivered", msg(2, status="delivered", attempts=1))
    store.seed("last-attempt", msg(40, status="read", attempts=4))
    store.seed("expired", msg(40, status="delivered", attempts=1,
                              expires_at=fmd.rfc3339(NOW - datetime.timedelta(seconds=1))))

    counts = fmd.reclaim_stale(store, now=NOW)

    assert counts == {"scanned": 5, "fresh": 1, "requeued": 2, "failed": 1,
                      "expired": 1, "conflicts": 0, "dry_run": False}
    for doc_id, attempts in (("stale-read", 2), ("stale-delivered", 3)):
        fields = store.get(doc_id).fields
        assert fields["status"] == "queued" and fields["attempts"] == attempts
        assert fields["updated_at"] == fmd.rfc3339(NOW)
        assert "claimer died or store failed" in fields["last_error"]
        assert "may already have run" in fields["last_error"]
    assert store.get("fresh-delivered").fields["status"] == "delivered"
    assert store.get("last-attempt").fields["status"] == "failed"
    assert store.get("last-attempt").fields["failed_at"] == fmd.rfc3339(NOW)
    assert store.get("expired").fields["status"] == "expired"
    assert store.get("expired").fields["expired_at"] == fmd.rfc3339(NOW)


def test_reclaimer_skips_conflicts_and_cli_dry_run_never_writes(monkeypatch, capsys):
    store = MemoryStore()
    store.seed("conflict", msg(40, status="read"))
    store.seed("dry", msg(40, status="delivered"))
    query = store.query_status

    def racing_query(status, limit):
        rows = query(status, limit)
        if status == "read":
            store.outside_write("conflict", {"status": "done"})
        return rows

    store.query_status = racing_query
    counts = fmd.reclaim_stale(store, now=NOW)
    assert counts["conflicts"] == 1
    assert store.get("conflict").fields["status"] == "done"

    store.seed("dry", msg(40, status="delivered"))
    before = store.get("dry")
    commits = store.commits
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"fleet_messages": {"target": "live"}})
    monkeypatch.setattr(fmd, "store_for", lambda config: store)
    monkeypatch.setattr(fmd, "utcnow", lambda: NOW)
    assert fmd.main(["reclaim-stale", "--dry-run"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["dry_run"] is True and printed["requeued"] == 1
    assert store.commits == commits and store.get("dry") == before

    transport = fmd.FirestoreStore(base_url="http://unused", project="p", database="d",
                                   token_fn=lambda: "unused")
    request = []
    monkeypatch.setattr(transport, "_req", lambda url, method, body: request.append(body) or [])
    assert transport.query_status("read", 200) == []
    structured = request[0]["structuredQuery"]
    assert structured["where"] == {"fieldFilter": {"field": {"fieldPath": "status"},
                                                  "op": "EQUAL", "value": fmd.enc("read")}}
    assert "orderBy" not in structured and structured["limit"] == 200

    def unavailable(config):
        raise fmd.NoGoogleCredentials("ADC expired and gcloud token failed")

    monkeypatch.setattr(fmd, "store_for", unavailable)
    assert fmd.main(["reclaim-stale"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "fleet message reclaim-stale: ADC expired and gcloud token failed\n"


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
    bad, missing, invalid, invalid_created, good = (uid(label) for label in
                                                    ("bad", "missing", "invalid", "invalid-created", "good"))
    store.seed(bad, msg(20, kind="carrier_pigeon"))
    without_expiry = msg(19)
    without_expiry.pop("expires_at")
    store.seed(missing, without_expiry)
    store.seed(invalid, msg(18, expires_at="not-a-timestamp"))
    store.seed(invalid_created, msg(17, created_at="not-a-timestamp"))
    store.seed(good, msg(10))
    assert drain_all(store) == [good]
    row = store.get(bad)
    assert row.fields["status"] == "rejected" and "unknown kind" in row.fields["last_error"]
    for doc_id in (missing, invalid):
        row = store.get(doc_id)
        assert row.fields["status"] == "rejected" and "expires_at" in row.fields["last_error"]
    row = store.get(invalid_created)
    assert row.fields["status"] == "rejected" and "created_at" in row.fields["last_error"]


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
    assert cfg.queued_timeout_seconds == 1800
    assert fmd.drain_config({"fleet_messages": {"drain_on_turn_end": True, "target": "live",
                                                 "queued_timeout_seconds": 900}}).queued_timeout_seconds == 900
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
