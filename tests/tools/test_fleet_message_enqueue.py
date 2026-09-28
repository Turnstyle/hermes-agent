"""Create-if-absent enqueue for relay envelope ids (fleet_messages_v1 doc keys)."""

import threading

from tools.fleet_message_enqueue import MessageAlreadyExists, enqueue_busy_dm


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


def test_concurrent_enqueues_of_one_envelope_leave_one_doc():
    envelope_id = "b" * 32
    store: dict[str, dict] = {}
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def writer(paths, collection, set_by):
        assert collection == "fleet_messages_v1"
        import json
        from pathlib import Path

        barrier.wait()
        doc = json.loads(Path(paths[0]).read_text(encoding="utf-8"))
        mid = doc["message_id"]
        with lock:
            if mid in store:
                raise MessageAlreadyExists(mid)
            store[mid] = dict(doc)
            store[mid]["status"] = "delivered"
        return 0

    def reader(message_id):
        return None

    results: list[str | BaseException] = []

    def run():
        try:
            results.append(
                enqueue_busy_dm(
                    sender="scout",
                    recipient="target",
                    body="concurrent body",
                    message_id=envelope_id,
                    writer=writer,
                    reader=reader,
                )
            )
        except BaseException as exc:
            results.append(exc)

    threads = [threading.Thread(target=run), threading.Thread(target=run)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 2
    assert all(r == envelope_id for r in results)
    assert len(store) == 1
    assert store[envelope_id]["status"] == "delivered"
    assert store[envelope_id]["body"] == "concurrent body"

    def writer_exists_only(paths, collection, set_by):
        import json
        from pathlib import Path

        doc = json.loads(Path(paths[0]).read_text(encoding="utf-8"))
        mid = doc["message_id"]
        if mid in store:
            raise MessageAlreadyExists(mid)
        return 0

    third = enqueue_busy_dm(
        sender="other",
        recipient="other",
        body="must not overwrite",
        message_id=envelope_id,
        writer=writer_exists_only,
        reader=reader,
    )
    assert third == envelope_id
    assert store[envelope_id]["status"] == "delivered"
    assert store[envelope_id]["body"] == "concurrent body"


def test_default_writer_keeps_fleet_ops_progress_off_stdout(tmp_path, monkeypatch, capsys):
    """The enqueue CLI's stdout is the JSON line the delivery runner parses; fleet_ops's
    "OK <id> ..." progress print must land on stderr, or every busy DM fails to queue."""
    import tools.fleet_message_enqueue as enq

    fake = tmp_path / "fleet_ops.py"
    fake.write_text(
        "def create_file_if_absent(path, collection, set_by):\n"
        "    print('OK fake-id progress line')\n"
        "    return 0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(enq, "_FLEET_OPS", fake)
    message_id = enqueue_busy_dm(sender="scout", recipient="target", body="hi")
    out, err = capsys.readouterr()
    assert out == ""
    assert "OK fake-id progress line" in err
    assert message_id
