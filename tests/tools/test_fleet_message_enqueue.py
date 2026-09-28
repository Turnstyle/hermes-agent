"""Create-if-absent enqueue for relay envelope ids (fleet_messages_v1 doc keys)."""

from tools.fleet_message_enqueue import enqueue_busy_dm


def test_enqueue_busy_dm_reuses_existing_doc_without_resetting_status():
    envelope_id = "a" * 32
    store: dict[str, dict] = {}

    def writer(paths, collection, set_by):
        assert collection == "fleet_messages_v1"
        import json
        from pathlib import Path

        for path in paths:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
            store[doc["message_id"]] = dict(doc)
        return 0

    def reader(message_id):
        return store.get(message_id)

    first = enqueue_busy_dm(
        sender="scout",
        recipient="target",
        body="first body",
        message_id=envelope_id,
        writer=writer,
        reader=reader,
    )
    assert first == envelope_id
    assert len(store) == 1
    assert store[envelope_id]["status"] == "queued"

    store[envelope_id]["status"] = "delivered"

    second = enqueue_busy_dm(
        sender="other",
        recipient="other",
        body="second body",
        message_id=envelope_id,
        writer=writer,
        reader=reader,
    )
    assert second == envelope_id
    assert len(store) == 1
    assert store[envelope_id]["status"] == "delivered"
    assert store[envelope_id]["body"] == "first body"
