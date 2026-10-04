"""An expired queued fleet message records a sender-visible notice and is not delivered."""

from __future__ import annotations

import datetime

from tests.tools.test_fleet_message_drain import NOW, MemoryStore, msg, uid
from tools import fleet_message_drain as fmd


def test_expired_queued_doc_records_sender_notice_without_delivery():
    store = MemoryStore()
    doc_id = uid("stale")
    sender = "tb-cndr"
    store.seed(doc_id, msg(25 * 60, sender=sender))

    claimed = fmd.claim_next(store, "tb-king", now=NOW)
    assert claimed is None
    row = store.get(doc_id)
    assert row.fields["status"] == "expired"
    assert row.fields.get("sender_notice")

    status = fmd.sender_delivery_status(store, sender, doc_id)
    assert status is not None
    assert status["status"] == "expired"
    assert status["notice"]
    assert fmd.sender_delivery_status(store, "other-sender", doc_id) is None


def test_queued_timeout_records_loud_sender_notice_before_24_hour_expiry():
    store = MemoryStore()
    doc_id = uid("timed-out")
    store.seed(doc_id, msg(31, sender="tb-cndr"))

    assert fmd.claim_next(store, "tb-king", now=NOW, queued_timeout_seconds=1800) is None
    status = fmd.sender_delivery_status(store, "tb-cndr", doc_id)
    assert status["status"] == "expired"
    assert "NOT delivered" in status["notice"]
    assert "30 minutes" in status["notice"]
    assert "delivered_at" not in store.get(doc_id).fields


def _notice(store, doc_id):
    return store.get(doc_id + fmd.EXPIRY_NOTICE_SUFFIX)


def test_expiry_also_queues_one_notice_to_the_original_sender():
    store = MemoryStore()
    doc_id = uid("stale")
    store.seed(doc_id, msg(25 * 60, sender="tb-cndr"))

    assert fmd.claim_next(store, "tb-king", now=NOW) is None
    notice = _notice(store, doc_id).fields
    assert (notice["to"], notice["from"], notice["status"]) == ("tb-cndr", fmd.SYSTEM_SENDER, "queued")
    assert notice["kind"] in fmd.KINDS and not fmd._malformed(notice)
    assert f"to @tb-king, message {doc_id} was NOT delivered" in notice["body"]
    assert "queued 1500 minutes" in notice["body"]


def test_queued_timeout_notice_states_the_wait_in_minutes():
    store = MemoryStore()
    doc_id = uid("timed-out")
    store.seed(doc_id, msg(31, sender="tb-cndr"))
    fmd.claim_next(store, "tb-king", now=NOW, queued_timeout_seconds=1800)
    assert "queued 31 minutes" in _notice(store, doc_id).fields["body"]


def test_repeated_sweeps_never_duplicate_the_notice():
    store = MemoryStore()
    doc_id = uid("stale")
    store.seed(doc_id, msg(25 * 60))
    fmd.claim_next(store, "tb-king", now=NOW)
    store.outside_write(doc_id, {"status": "queued"})  # a racing reader re-presents the same doc
    fmd.claim_next(store, "tb-king", now=NOW)
    assert [d for d in store.docs if d.endswith(fmd.EXPIRY_NOTICE_SUFFIX)] == [doc_id + fmd.EXPIRY_NOTICE_SUFFIX]


def test_an_expiring_notice_does_not_create_another_notice():
    store = MemoryStore()
    doc_id = uid("stale")
    store.seed(doc_id, msg(25 * 60, sender="tb-cndr"))
    fmd.claim_next(store, "tb-king", now=NOW)
    notice_id = doc_id + fmd.EXPIRY_NOTICE_SUFFIX
    later = NOW + datetime.timedelta(hours=25)

    assert fmd.claim_next(store, "tb-cndr", now=later) is None
    assert store.get(notice_id).fields["status"] == "expired"
    assert sorted(store.docs) == sorted([doc_id, notice_id])


def test_a_failing_notice_write_never_blocks_the_expiry():
    class Broken(MemoryStore):
        def create(self, doc_id, fields):
            raise RuntimeError("firestore down")

    store = Broken()
    doc_id = uid("stale")
    store.seed(doc_id, msg(25 * 60))
    assert fmd.claim_next(store, "tb-king", now=NOW) is None
    assert store.get(doc_id).fields["status"] == "expired"


def test_reclaim_stale_expiry_also_notifies_the_sender():
    store = MemoryStore()
    doc_id = uid("stuck")
    created = NOW - datetime.timedelta(hours=30)
    store.seed(doc_id, msg(30 * 60, status="delivered", updated_at=fmd.rfc3339(created), sender="tb-cndr"))
    counts = fmd.reclaim_stale(store, older_than_seconds=60, now=NOW)
    assert counts["expired"] == 1
    assert _notice(store, doc_id).fields["to"] == "tb-cndr"
