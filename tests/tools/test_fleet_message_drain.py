"""tools/fleet_message_drain.py: claim/transition logic for fleet_messages_v1 (native-door port).

The carried idle-tick / API-turn / CLI-child / linger cases exercised executors v0.22.5 removed;
their surviving properties live in tests/tools/test_fleet_message_admission.py and
tests/gateway/test_fleet_message_bridge.py against the native Bot Chat door.

Every behaviour test runs against an in-memory store with Firestore's updateTime-precondition
semantics, and again against a real Firestore emulator when FLEET_MESSAGES_EMULATOR=host:port is set
(the emulator variant is the integration proof of the atomic claim).
"""
from __future__ import annotations

import datetime
import contextlib
import itertools
import json
import os
import sys
import threading
import urllib.error
import time
import urllib.request
import uuid
from types import SimpleNamespace

import pytest

from tools import fleet_message_drain as fmd

NOW = datetime.datetime(2026, 9, 28, 6, 0, tzinfo=datetime.timezone.utc)
ME = "tb-king"
EMU = os.environ.get("FLEET_MESSAGES_EMULATOR", "")
PROJECT = "mission-control-444444"



@pytest.mark.parametrize("override, expected", [
    ("/fixture/selected.json", "/fixture/selected.json"),
    (None, "/fixture/os-user/.config/conductive-admin/conductive-admin-sa-key.json"),
])
def test_live_source_uses_selected_account_and_caches_it(monkeypatch, override, expected):
    """Keep the A-L3 source choice when the fleet queue moves to a plugin."""
    pytest.importorskip("google.oauth2")
    import pwd
    import google.auth
    from google.oauth2 import service_account

    monkeypatch.setattr(google.auth, "default", lambda **kw: pytest.fail("ADC is forbidden"))
    monkeypatch.setenv("HOME", "/fixture/profile-home")
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir="/fixture/os-user"))
    if override is None:
        monkeypatch.delenv("FLEET_MESSAGES_SA_KEY_FILE", raising=False)
    else:
        monkeypatch.setenv("FLEET_MESSAGES_SA_KEY_FILE", override)
    calls = []
    account = SimpleNamespace(valid=True, token="fixture-result")

    def load(path, *, scopes):
        calls.append((path, scopes))
        return account

    monkeypatch.setattr(service_account.Credentials, "from_service_account_file", load)
    monkeypatch.setattr(fmd, "_live_credentials", None)
    monkeypatch.setattr(fmd, "_credentials_warned", False)
    monkeypatch.setattr(fmd.subprocess, "run", lambda *a, **kw: pytest.fail("unexpected fallback"))
    assert fmd._live_token() == "fixture-result"
    assert fmd._live_token() == "fixture-result"
    assert calls == [(expected, ["https://www.googleapis.com/auth/datastore"])]


def test_live_token_falls_back_to_gcloud_and_warns_once_per_failed_streak(monkeypatch, caplog, tmp_path):
    pytest.importorskip("google.auth")
    import google.auth
    from google.auth.exceptions import RefreshError

    class Expired:
        valid = False

        def refresh(self, request):
            raise RefreshError("expired")

    monkeypatch.setattr(google.auth, "default", lambda **kw: pytest.fail("ADC must never be used"))
    monkeypatch.setattr(fmd, "_named_key_credentials", lambda: Expired())
    monkeypatch.setattr(fmd, "_live_credentials", None)
    monkeypatch.setattr(fmd, "_gcloud_token_cached", "")
    monkeypatch.setattr(fmd, "_gcloud_token_until", 0)
    monkeypatch.setattr(fmd, "_credentials_warned", False)
    from hermes_platform.resolver import known_dirs
    gcloud_exe = tmp_path / "gcloud"
    gcloud_exe.write_text("#!/bin/sh\nexit 0\n")
    gcloud_exe.chmod(0o755)
    monkeypatch.setattr(known_dirs, "snap_bin_dirs", lambda: (str(tmp_path),), raising=False)
    monkeypatch.setenv("PATH", "")
    calls = []
    commands = []

    def gcloud(*args, **kwargs):
        calls.append(kwargs)
        commands.append(args[0])
        return SimpleNamespace(stdout="gcloud-token\n")

    monkeypatch.setattr(fmd.subprocess, "run", gcloud)
    monkeypatch.setenv("PYTHONPATH", "/hermes/bootstrap/site-packages")
    monkeypatch.setenv("PYTHONHOME", "/hermes/python")
    assert fmd._live_token() == "gcloud-token"
    assert fmd._live_token() == "gcloud-token"
    assert commands == [[str(gcloud_exe), "auth", "print-access-token", "--account=conductive-admin-sa@fifth-flame-482113-v8.iam.gserviceaccount.com"]]
    assert len(calls) == 1 and calls[0]["timeout"] <= 3
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in calls[0]["env"]
    # gcloud runs its own Python: the Hermes bootstrap's PYTHONPATH/PYTHONHOME must not leak into it.
    assert "PYTHONPATH" not in calls[0]["env"] and "PYTHONHOME" not in calls[0]["env"]

    monkeypatch.setattr(fmd, "_gcloud_token_cached", "")
    monkeypatch.setattr(fmd.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(OSError("offline")))
    for _ in range(2):
        with pytest.raises(fmd.NoGoogleCredentials, match="named service account and gcloud token failed"):
            fmd._live_token()
    assert [r.message for r in caplog.records if "no Google credentials" in r.message] == [
        "fleet message drain: no Google credentials (named service account and gcloud token failed); "
        "drain skipped, turns unaffected"]


def test_live_token_fails_closed_when_gcloud_cannot_be_resolved(monkeypatch):
    pytest.importorskip("google.auth")
    import google.auth
    from hermes_platform.resolver import known_dirs

    def no_named_source():
        raise FileNotFoundError("fixture source unavailable")

    monkeypatch.setattr(fmd, "_named_key_credentials", no_named_source)
    monkeypatch.setattr(fmd, "_live_credentials", None)
    monkeypatch.setattr(fmd, "_gcloud_token_cached", "")
    monkeypatch.setattr(fmd, "_credentials_warned", False)
    monkeypatch.setattr(known_dirs, "snap_bin_dirs", lambda: ())
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(fmd.subprocess, "run", lambda *args, **kwargs: pytest.fail("unexpected token command"))

    with pytest.raises(fmd.NoGoogleCredentials, match="named service account and gcloud token failed"):
        fmd._live_token()


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




























def _fake_reported_bot_chat(monkeypatch, tmp_path, *, mode: str, linger: float = 0) -> tuple:
    """A real child reports its turn independently of its later process exit."""
    from tools import bot_relay

    home = tmp_path / "profiles" / ME
    home.mkdir(parents=True)
    started = tmp_path / "child-pid"
    finished = tmp_path / "child-finished"
    report_name = tmp_path / "report-name"
    child = tmp_path / "fake-hermes"
    child.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys, time\n"
        "pathlib.Path(os.environ['FAKE_STARTED']).write_text(str(os.getpid()))\n"
        "report = os.environ.get('HERMES_QUIET_TURN_REPORT_FILE')\n"
        "if report:\n"
        "    pathlib.Path(os.environ['FAKE_REPORT_NAME']).write_text(report)\n"
        "if os.environ['FAKE_MODE'] != 'never' and report:\n"
        "    pathlib.Path(report).write_text(json.dumps({'pid': os.getpid(), 'exit_code': "
        "7 if os.environ['FAKE_MODE'] == 'failed' else 0, 'error': '', 'reply': ''}))\n"
        "release = os.environ.get('FAKE_RELEASE')\n"
        "deadline = time.monotonic() + 15\n"
        "while release and not pathlib.Path(release).exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.02)\n"
        "time.sleep(float(os.environ['FAKE_LINGER']))\n"
        "pathlib.Path(os.environ['FAKE_FINISHED']).write_text('finished')\n"
        "print('final output', flush=True)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    child.chmod(0o700)
    monkeypatch.setattr(bot_relay, "_hermes_cli", lambda: str(child))
    monkeypatch.setattr(bot_relay, "TURN_ATTEMPT_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(bot_relay, "delivery_env", lambda author, profile_home: {
        "FAKE_STARTED": str(started), "FAKE_FINISHED": str(finished),
        "FAKE_REPORT_NAME": str(report_name), "FAKE_MODE": mode, "FAKE_LINGER": str(linger),
    })
    return home, started, finished, report_name










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
def test_failed_turn_requeue_never_runs_the_handed_over_message_again(store):
    """Native-door port: a message that reached execution keeps its one execution claim, so a
    requeue (an error path, a stale reclaim) is held on its next claim, never handed over twice."""
    doc = uid("retry")
    store.seed(doc, msg(10))
    claimed = fmd.claim_next(store, ME, now=NOW)
    assert fmd.mark_read(store, claimed, now=NOW)
    fmd.finish(store, claimed, {"status": "failed", "error": "provider 529"}, max_attempts=5, now=NOW)
    row = store.get(doc)
    assert row.fields["status"] == "queued"
    assert row.fields["attempts"] == 2 and "provider 529" in row.fields["last_error"]
    again = fmd.claim_next(store, ME, now=NOW)
    assert again is not None and again.doc_id == doc
    assert fmd.mark_read(store, again, now=NOW) is False   # the execution claim already exists
    held = store.get(doc).fields
    assert held["status"] == "read" and held["last_error"] == fmd.EXECUTION_UNVERIFIED


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


def test_reclaimer_requeues_only_unhandled_claims_and_never_a_read_one():
    """Pressure assertion 2: a stale ``read`` claim reached the native door and may have run, so
    it is held, never requeued; only never-handed-over ``delivered`` claims go back to the queue."""
    store = MemoryStore()
    store.seed("stale-read", msg(40, status="read", attempts=1))
    store.seed("stale-delivered", msg(40, status="delivered", attempts=2))
    store.seed("fresh-delivered", msg(2, status="delivered", attempts=1))
    store.seed("last-attempt", msg(40, status="delivered", attempts=4))
    store.seed("expired", msg(40, status="delivered", attempts=1,
                              expires_at=fmd.rfc3339(NOW - datetime.timedelta(seconds=1))))
    stale_read = store.get("stale-read")

    counts = fmd.reclaim_stale(store, now=NOW)

    assert counts == {"scanned": 5, "fresh": 1, "requeued": 1, "failed": 1,
                      "expired": 1, "held": 1, "conflicts": 0, "dry_run": False}
    assert store.get("stale-read") == stale_read  # untouched: never permission to run it again
    fields = store.get("stale-delivered").fields
    assert fields["status"] == "queued" and fields["attempts"] == 3
    assert fields["updated_at"] == fmd.rfc3339(NOW) and "claimer died or store failed" in fields["last_error"]
    assert store.get("fresh-delivered").fields["status"] == "delivered"
    assert store.get("last-attempt").fields["status"] == "failed"
    assert store.get("last-attempt").fields["failed_at"] == fmd.rfc3339(NOW)
    assert store.get("expired").fields["status"] == "expired"
    assert store.get("expired").fields["expired_at"] == fmd.rfc3339(NOW)


def test_reclaimer_skips_conflicts_and_cli_dry_run_never_writes(monkeypatch, capsys):
    store = MemoryStore()
    store.seed("conflict", msg(40, status="delivered"))
    query = store.query_status

    def racing_query(status, limit):
        rows = query(status, limit)
        if status == "delivered":
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
        raise fmd.NoGoogleCredentials("named service account and gcloud token failed")

    monkeypatch.setattr(fmd, "store_for", unavailable)
    assert fmd.main(["reclaim-stale"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "fleet message reclaim-stale: named service account and gcloud token failed\n"


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


# ---------- an answered message becomes done (no false "NOT delivered" after reclaim) ----------








def test_expiry_of_a_handed_over_doc_does_not_say_not_delivered():
    store = MemoryStore()
    store.seed("handed", msg(60, read_at=fmd.rfc3339(NOW - datetime.timedelta(minutes=55))))
    assert fmd.claim_next(store, ME, limit=10, now=NOW) is None
    fields = store.get("handed").fields
    assert fields["status"] == "expired"
    assert "NOT delivered" not in fields["sender_notice"]
    notice = store.get("handed" + fmd.EXPIRY_NOTICE_SUFFIX).fields["body"]
    assert "NOT delivered" not in notice and "handed to a Bot Chat turn" in notice



# ---------- queued-timeout clock: requeue and restart do not count ----------
TIMEOUT = 7200


def test_requeue_restarts_the_queued_clock(store):
    # created 3 h ago (past the 2 h timeout), but requeued 22 seconds ago: not expired
    doc = uid("requeued")
    store.seed(doc, msg(180, requeued_at=fmd.rfc3339(NOW - datetime.timedelta(seconds=22))))
    claimed = fmd.claim_next(store, ME, now=NOW, queued_timeout_seconds=TIMEOUT)
    assert claimed is not None and claimed.doc_id == doc
    assert store.get(doc).fields["status"] == "delivered"


def test_requeued_doc_still_expires_a_full_timeout_after_the_requeue(store):
    doc = uid("requeued-old")
    store.seed(doc, msg(300, requeued_at=fmd.rfc3339(NOW - datetime.timedelta(seconds=TIMEOUT + 1))))
    assert fmd.claim_next(store, ME, now=NOW, queued_timeout_seconds=TIMEOUT) is None
    assert store.get(doc).fields["status"] == "expired"
    assert store.get(doc).fields["last_error"] == "queued_delivery_timeout"


def test_requeue_paths_store_requeued_at():
    store = MemoryStore()
    store.seed("rel", msg(10))
    claimed = fmd.claim_next(store, ME, now=NOW)
    later = NOW + datetime.timedelta(minutes=5)
    assert fmd.release(store, claimed, now=later)
    assert store.get("rel").fields["requeued_at"] == fmd.rfc3339(later)

    store.seed("err", msg(10))
    claimed = fmd.claim_next(store, ME, now=NOW + datetime.timedelta(minutes=6))
    assert claimed.doc_id == "err"
    later = NOW + datetime.timedelta(minutes=7)
    assert fmd.record_error(store, claimed, "boom", max_attempts=5, now=later)
    assert store.get("err").fields["status"] == "queued"
    assert store.get("err").fields["requeued_at"] == fmd.rfc3339(later)

    store.seed("stale", msg(40, status="delivered", attempts=1))  # a read claim is never requeued
    fmd.reclaim_stale(store, now=NOW)
    assert store.get("stale").fields["requeued_at"] == fmd.rfc3339(NOW)


def test_gateway_restart_does_not_expire_queued_messages(store):
    # created 3 h ago; the receiving gateway came back up 10 minutes ago
    doc = uid("restart")
    store.seed(doc, msg(180))
    up = NOW - datetime.timedelta(minutes=10)
    claimed = fmd.claim_next(store, ME, now=NOW, queued_timeout_seconds=TIMEOUT, receiver_up_since=up)
    assert claimed is not None and claimed.doc_id == doc


def test_message_expires_a_full_timeout_after_the_gateway_came_back(store):
    doc = uid("after-restart")
    store.seed(doc, msg(600))
    up = NOW - datetime.timedelta(seconds=TIMEOUT + 1)
    assert fmd.claim_next(store, ME, now=NOW, queued_timeout_seconds=TIMEOUT, receiver_up_since=up) is None
    assert store.get(doc).fields["status"] == "expired"


def test_24_hour_sender_limit_is_kept_after_requeue_and_restart(store):
    doc = uid("day")
    store.seed(doc, msg(25 * 60, requeued_at=fmd.rfc3339(NOW - datetime.timedelta(seconds=5))))
    assert fmd.claim_next(store, ME, now=NOW, queued_timeout_seconds=TIMEOUT,
                          receiver_up_since=NOW - datetime.timedelta(seconds=5)) is None
    fields = store.get(doc).fields
    assert fields["status"] == "expired" and fields["last_error"] == "expired"
    assert "24 hours" in fields["sender_notice"]


def test_receiver_up_since_reads_gateway_state(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    home = root / "profiles" / ME
    home.mkdir(parents=True)
    assert fmd.receiver_up_since(home) is None  # no state file: unknown
    # draining gateway: counts from its last state write
    (root / "gateway_state.json").write_text(json.dumps(
        {"pid": 1, "start_time": 1, "gateway_state": "draining", "updated_at": "2026-09-28T05:50:00+00:00"}))
    assert fmd.receiver_up_since(home) == NOW - datetime.timedelta(minutes=10)
    # running gateway with a live process: counts from the process start
    import gateway.status as gstatus
    import psutil
    monkeypatch.setattr(gstatus, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(gstatus, "get_process_start_time", lambda pid: 7)
    monkeypatch.setattr(psutil, "Process", lambda pid: SimpleNamespace(create_time=lambda: NOW.timestamp() - 90))
    (root / "gateway_state.json").write_text(json.dumps(
        {"pid": 4242, "start_time": 7, "gateway_state": "running", "updated_at": "2026-09-28T05:59:59+00:00"}))
    assert fmd.receiver_up_since(home) == NOW - datetime.timedelta(seconds=90)
    # recorded start does not match the live process (pid reused): falls back to updated_at
    (root / "gateway_state.json").write_text(json.dumps(
        {"pid": 4242, "start_time": 8, "gateway_state": "running", "updated_at": "2026-09-28T05:59:00+00:00"}))
    assert fmd.receiver_up_since(home) == NOW - datetime.timedelta(minutes=1)


