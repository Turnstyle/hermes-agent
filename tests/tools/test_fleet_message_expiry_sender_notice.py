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
    notices = [fields for fields in store.docs.values() if fields.get("kind") == "notify_wake"]
    assert len(notices) == 1
    assert notices[0]["to"] == sender
    assert notices[0]["body"] == status["notice"]


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


def test_expiry_queues_one_sender_wake_and_expired_wake_does_not_loop():
    store = MemoryStore()
    doc_id = uid("timed-out")
    store.seed(doc_id, msg(31, sender="tb-cndr"))

    assert fmd.claim_next(store, "tb-king", now=NOW, queued_timeout_seconds=1800) is None
    original = store.get(doc_id).fields
    assert original["status"] == "expired"
    wakes = [(key, fields) for key, fields in store.docs.items() if fields.get("kind") == "notify_wake"]
    assert len(wakes) == 1
    wake_id, wake = wakes[0]
    assert wake["status"] == "queued"
    assert wake["to"] == "tb-cndr"
    assert wake["from"] == "tb-king"
    assert wake["body"] == original["sender_notice"]
    assert doc_id in wake["body"] and "tb-king" in wake["body"]
    assert wake["message_id"] == wake_id

    assert fmd.claim_next(store, "tb-king", now=NOW, queued_timeout_seconds=1800) is None
    assert len(store.docs) == 2

    later = NOW + datetime.timedelta(hours=25)
    assert fmd.claim_next(store, "tb-cndr", now=later) is None
    assert store.get(wake_id).fields["status"] == "expired"
    assert len(store.docs) == 2
