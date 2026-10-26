"""Fleet queue -> native Bot Chat door: one execution per message, fenced to the native receipt.

Every case drives ``tools.fleet_message_admission.drain_once`` through the real
``tools.bot_live_delivery.deliver_to_live_owner`` door code; only the authority RPC
(``authority_delivery``), its discovery and the Firestore registry (``MemoryStore``) are fakes.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from tests.tools.test_fleet_message_drain import NOW, MemoryStore, msg, uid
from tools import bot_live_delivery as live
from tools import fleet_message_admission as fma
from tools import fleet_message_drain as fmd
from tools import fleet_message_spool as spool

ME = "tb-king"
CONFIG = fmd.DrainConfig(target="emulator", emulator_host="127.0.0.1:1")


class FakeDoor:
    """The recipient authority's ``bot_relay.deliver``: one admission per id, receipts on disk."""

    def __init__(self):
        self.calls: list[tuple[Path, dict]] = []
        self.records: dict[str, dict] = {}
        self.fail: BaseException | None = None
        self.status = "queued"

    def __call__(self, home, params):
        self.calls.append((Path(home).resolve(), dict(params)))
        if self.fail is not None:
            raise self.fail
        record = self.records.get(params["id"])
        if record is not None and (record["message"], record.get("author")) != (params["message"], params.get("author")):
            from hermes_cli.gateway_client import GatewayRPCError

            raise GatewayRPCError("admission_conflict")  # session_bot._require_same_delivery
        if record is None:
            record = self.records[params["id"]] = dict(
                status=self.status, delivery_id=params["id"], profile_home=str(Path(home).resolve()),
                session_id="bot-chat", message=params["message"], admission_id="adm-" + params["id"][:8], reply="",
                **({"author": params["author"]} if params.get("author") else {}))
            self._publish(record)
        return {k: record[k] for k in ("status", "delivery_id", "profile_home", "session_id", "reply")}

    def _publish(self, record):
        with live._locked(record["profile_home"]) as root:
            live._write(root / f"{record['delivery_id']}.json", record)

    def settle(self, delivery_id, **outcome):
        record = self.records[delivery_id]
        record.update(outcome)
        self._publish(record)


@pytest.fixture
def home(tmp_path):
    path = tmp_path / ".hermes" / "profiles" / ME
    path.mkdir(parents=True)
    (path / "state.db").write_text("")
    return path


@pytest.fixture
def door(monkeypatch, home):
    fake = FakeDoor()
    monkeypatch.setattr(live, "find_canonical_live_owner", lambda h: dict(
        profile_home=str(Path(h).resolve()), session_id="bot-chat", canonical=True,
        lease_id="authority-1", live_session_id="bot-chat"))
    monkeypatch.setattr(live, "authority_delivery", fake)
    return fake


def drain(home, store, **kw):
    return fma.drain_once(home, config=CONFIG, store=store, now=kw.pop("now", NOW), **kw)


def test_admitted_doc_is_done_only_from_its_settled_native_receipt(home, door):
    store = MemoryStore()
    first, second = uid("m1"), uid("m2")
    store.seed(first, msg(10))
    store.seed(second, msg(5))
    assert drain(home, store) == "admitted"
    (called_home, params), = door.calls
    did = spool.native_delivery_id(first)
    assert called_home == home.resolve() and params["id"] == did and params["profile"] == ME
    assert params["session_id"] == "bot-chat" and params["author"]["id"] == "bot:tb-cndr"
    assert store.get(first).fields["status"] == "read"
    # Only a settled receipt advances: the second message waits while the first is pending.
    assert drain(home, store) == "pending"
    assert len(door.calls) == 1 and store.get(second).fields["status"] == "queued"
    door.settle(did, status="settled", reply="done")
    assert drain(home, store) == "admitted"
    assert store.get(first).fields["status"] == "done"
    assert [p["id"] for _, p in door.calls] == [did, spool.native_delivery_id(second)]
    assert spool.execution(home, first)["status"] == "settled"


def test_failed_native_receipt_fails_the_doc_and_never_requeues(home, door):
    store = MemoryStore()
    doc = uid("fail")
    store.seed(doc, msg(10))
    drain(home, store)
    door.settle(spool.native_delivery_id(doc), status="failed", reason="provider_server_error")
    assert drain(home, store) == "idle"
    fields = store.get(doc).fields
    assert fields["status"] == "failed" and "provider_server_error" in fields["last_error"]
    assert len(door.calls) == 1


def test_refused_before_dial_requeues_without_an_attempt_and_reuses_the_delivery_id(home, door):
    store = MemoryStore()
    doc = uid("predial")
    store.seed(doc, msg(10))
    door.fail = live.PreDialError("authority socket refused")
    assert drain(home, store) == "refused"
    fields = store.get(doc).fields
    assert fields["status"] == "queued" and fields["attempts"] == 1  # no attempt counted
    assert spool.execution(home, doc) is None  # claim released: nothing ran
    door.fail = None
    assert drain(home, store) == "admitted"
    assert {p["id"] for _, p in door.calls} == {spool.native_delivery_id(doc)}


def test_owner_busy_refusal_is_a_quiet_requeue(home, door):
    from hermes_cli.gateway_client import GatewayRPCError

    store = MemoryStore()
    doc = uid("busy")
    store.seed(doc, msg(10))
    door.fail = GatewayRPCError("session_busy")
    assert drain(home, store) == "refused"
    assert store.get(doc).fields["status"] == "queued"
    assert store.get(doc).fields["last_error"] == "session_busy"
    assert not [d for d in store.docs if d.endswith(fmd.EXPIRY_NOTICE_SUFFIX)]  # nothing told the sender


def test_final_door_refusal_fails_the_doc(home, door):
    from hermes_cli.gateway_client import GatewayRPCError

    store = MemoryStore()
    doc = uid("denied")
    store.seed(doc, msg(10))
    door.fail = GatewayRPCError("permission_denied")
    assert drain(home, store) == "failed"
    assert store.get(doc).fields["status"] == "failed"


@pytest.mark.parametrize("failure", ["connection_lost", "unknown_execution"])
def test_uncertain_execution_is_held_and_never_asked_again(home, door, failure):
    from hermes_cli.gateway_client import GatewayRPCError

    store = MemoryStore()
    doc = uid("uncertain")
    store.seed(doc, msg(10))
    door.fail = ConnectionError("socket closed after the RPC went out") if failure == "connection_lost" \
        else GatewayRPCError("unknown_execution")
    assert drain(home, store) == "uncertain"
    fields = store.get(doc).fields
    assert fields["status"] == "read" and fields["last_error"].startswith(fmd.EXECUTION_UNVERIFIED)
    assert spool.execution(home, doc)["status"] == "uncertain"
    door.fail = None
    # A stale reclaim never turns the read claim into permission to run again.
    assert fmd.reclaim_stale(store, older_than_seconds=0, now=NOW + datetime.timedelta(minutes=1))["held"] == 1
    assert drain(home, store, now=NOW + datetime.timedelta(minutes=2)) == "idle"
    # Even a doc an operator (or an older node's reclaim) requeued meets the execution record.
    row = store.get(doc)
    store.outside_write(doc, {"status": "queued"})
    assert drain(home, store, now=NOW + datetime.timedelta(minutes=3)) == "held"
    assert len(door.calls) == 1  # the door was never asked a second time
    assert store.get(doc).fields["status"] == "read" and row.fields["status"] == "read"


def test_uncertain_claim_settles_from_a_receipt_that_appears_later(home, door):
    store = MemoryStore()
    doc = uid("late")
    store.seed(doc, msg(10))
    door.fail = ConnectionError("lost")
    drain(home, store)
    did = spool.native_delivery_id(doc)
    # The first ask had in fact been admitted; the owner publishes its settled receipt.
    door.fail = None
    door.records[did] = dict(status="settled", delivery_id=did, profile_home=str(home.resolve()),
                             session_id="bot-chat", message="m", admission_id="adm", reply="ok")
    door._publish(door.records[did])
    assert drain(home, store) == "idle"
    assert store.get(doc).fields["status"] == "done"
    assert len(door.calls) == 1


def test_local_spool_and_cloud_claim_share_one_execution(home, door):
    store = MemoryStore()
    mid = spool.spool_busy_dm(home, sender="tb-cndr", recipient=ME, body="Message from 🤖 c (@tb-cndr): hi",
                              writer=lambda *a, **k: (_ for _ in ()).throw(OSError("registry down")),
                              reader=lambda _m: None)["message_id"]
    assert drain(home, store) == "admitted"  # local-first: no registry needed
    door.settle(spool.native_delivery_id(mid), status="settled")
    # The registry doc of the same message lands later (a cloud commit whose ACK was lost).
    envelope = spool.read_envelope(home, mid)
    store.seed(mid, msg(1, body=envelope["body"], sender="tb-cndr"))
    assert drain(home, store) == "held"
    assert store.get(mid).fields["status"] == "done"  # reconciled from the local execution record
    assert len(door.calls) == 1
    assert spool.read_envelope(home, mid) is None  # settled payload retired


def test_local_first_drain_survives_a_registry_outage(home, door):
    class Down(MemoryStore):
        def query_queued(self, to, limit):
            raise OSError("firestore unreachable")

    mid = spool.spool_busy_dm(home, sender="tb-cndr", recipient=ME, body="hello",
                              writer=lambda *a, **k: 1, reader=lambda _m: None)["message_id"]
    assert drain(home, Down()) == "admitted"
    assert door.calls[0][1]["id"] == spool.native_delivery_id(mid)


def test_busy_recipient_still_expires_with_notice_but_never_admits(home, door):
    store = MemoryStore()
    stale, fresh = uid("stale"), uid("fresh")
    store.seed(stale, msg(25 * 60))
    store.seed(fresh, msg(5))
    assert drain(home, store, busy=lambda: True) == "busy"
    assert store.get(stale).fields["status"] == "expired"
    assert store.get(stale + fmd.EXPIRY_NOTICE_SUFFIX).fields["to"] == "tb-cndr"
    assert store.get(fresh).fields["status"] == "queued"
    assert door.calls == []


def test_conflicting_local_envelope_is_never_executed(home, door):
    store = MemoryStore()
    mid = "fm" + "d" * 32
    spool.persist_envelope(home, message_id=mid, sender="tb-cndr", recipient=ME, body="the local copy")
    store.seed(mid, msg(5, body="a different body"))
    # The local envelope is unpublished, so the local-first drain takes it; the cloud doc then conflicts.
    assert drain(home, store) == "admitted"
    door.settle(spool.native_delivery_id(mid), status="settled")
    assert drain(home, store) == "held"
    assert store.get(mid).fields["last_error"] == "local_spool_conflict"
    assert len(door.calls) == 1


def test_notify_wake_is_admitted_as_an_unauthored_result(home, door):
    store = MemoryStore()
    doc = uid("wake")
    store.seed(doc, msg(5, kind="notify_wake", sender=fmd.SYSTEM_SENDER))
    assert drain(home, store) == "admitted"
    params = door.calls[0][1]
    assert "author" not in params and "notification_category" not in params


def test_unready_authority_claims_nothing(home, monkeypatch):
    def not_ready(h):
        raise ValueError("profile authority is not ready")

    monkeypatch.setattr(live, "find_canonical_live_owner", not_ready)
    monkeypatch.setattr(live, "authority_delivery", lambda *a: pytest.fail("no dial without an authority"))
    store = MemoryStore()
    doc = uid("down")
    store.seed(doc, msg(5))
    assert drain(home, store) == "idle"
    assert store.get(doc).fields["status"] == "queued"


def test_execution_claim_is_no_clobber(home):
    assert spool.claim_execution(home, "fm" + "e" * 32, transport="local", fields={})
    assert not spool.claim_execution(home, "fm" + "e" * 32, transport="cloud", fields={})
    assert spool.execution(home, "fm" + "e" * 32)["transport"] == "local"


def test_corrupt_execution_record_is_never_treated_as_absent(home, door):
    store = MemoryStore()
    doc = uid("corrupt")
    store.seed(doc, msg(5))
    path = spool.execution_path(home, doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError):
        drain(home, store)
    assert door.calls == []


def test_envelope_conflict_keeps_both_payloads(home):
    mid = "fm" + "f" * 32
    spool.persist_envelope(home, message_id=mid, sender="a", recipient=ME, body="original")
    assert spool.persist_envelope(home, message_id=mid, sender="a", recipient=ME, body="original")["body"] == "original"
    with pytest.raises(spool.SpoolConflictError):
        spool.persist_envelope(home, message_id=mid, sender="a", recipient=ME, body="changed")
    assert spool.read_envelope(home, mid)["body"] == "original"
    kept = list((spool.spool_dir(home) / spool.STATE_SUBDIR).glob(f"{mid}.conflict-*.json"))
    assert len(kept) == 1 and json.loads(kept[0].read_text())["body"] == "changed"


def test_envelope_write_is_atomic(home, monkeypatch):
    import os

    mid = "fm" + "0" * 32
    monkeypatch.setattr(os, "link", lambda *a: (_ for _ in ()).throw(OSError("crash before publish")))
    with pytest.raises(OSError):
        spool.persist_envelope(home, message_id=mid, sender="a", recipient=ME, body="x")
    assert not spool.envelope_path(home, mid).exists()
    assert not [p for p in spool.spool_dir(home).iterdir() if p.is_file()]  # no partial or temp file left


def test_one_stable_identity_and_a_changed_payload_under_it_is_refused(home, door):
    """Pressure assertion 1: local spool and registry copies ask the door under ONE id derived from
    the fleet message id; a changed payload under that id is refused, never a second turn."""
    mid = "fm" + "1" * 32
    did = spool.native_delivery_id(mid)
    assert did == spool.native_delivery_id(mid) and len(did) == 64 and did != mid
    assert fma.admit_native(home, mid, "original", None).kind == "pending"
    again = fma.admit_native(home, mid, "original", None)
    assert again.kind == "pending" and again.delivery_id == did  # the same admission answers
    changed = fma.admit_native(home, mid, "changed body", None)
    assert changed.kind == "failed" and changed.reason == "admission_conflict"
    assert list(door.records) == [did] and door.records[did]["message"] == "original"
