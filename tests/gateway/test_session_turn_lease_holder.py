"""X10: ``SessionDB.session_turn_lease_holder`` is a read-only diagnostic on the stable root key."""
import time

import hermes_state


def _db(tmp_path):
    db = hermes_state.SessionDB(tmp_path / "state.db")
    db.create_session("root", source="test")
    return db


def test_reports_the_live_holder_without_renewing_it(tmp_path):
    db = _db(tmp_path)
    assert db.session_turn_lease_holder("root") is None
    assert db.try_acquire_session_turn_lease("root", "holder-a", ttl_seconds=60)
    before = db.session_turn_lease_expires_at("root", "holder-a")
    time.sleep(0.01)
    assert db.session_turn_lease_holder("root") == "holder-a"
    assert db.session_turn_lease_expires_at("root", "holder-a") == before  # never renewed
    db.release_session_turn_lease("root", "holder-a")
    assert db.session_turn_lease_holder("root") is None


def test_compression_child_reads_its_conversation_root_lease(tmp_path):
    db = _db(tmp_path)
    db.end_session("root", "compression")
    db.create_session("child", source="test", parent_session_id="root")
    assert db.try_acquire_session_turn_lease("root", "holder-root", ttl_seconds=60)
    assert db.session_turn_lease_holder("child") == "holder-root"


def test_expired_holder_reads_as_none_and_is_not_reclaimed(tmp_path, monkeypatch):
    import hermes_state_compression

    db = _db(tmp_path)
    assert db.try_acquire_session_turn_lease("root", "holder-a", ttl_seconds=1)
    later = time.time() + 5
    monkeypatch.setattr(hermes_state_compression.time, "time", lambda: later)
    assert db.session_turn_lease_holder("root") is None
    with db._read_ctx() as conn:  # a diagnostic read never deletes or rewrites the row
        assert conn.execute("SELECT holder FROM session_turn_leases").fetchall()[0][0] == "holder-a"
    assert db.session_turn_lease_holder("") is None
