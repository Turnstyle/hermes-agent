"""An expired queued fleet message records a sender-visible notice and is not delivered."""

from __future__ import annotations

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
