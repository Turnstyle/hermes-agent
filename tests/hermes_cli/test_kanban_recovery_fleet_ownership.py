"""Recovery paths skip rows this Fleet node does not own (t_23c40d89).

``reconcile_orphaned_running`` selected every ``running`` row with NULL claim
bookkeeping. On a synced Fleet board that includes other nodes' mirrored cards:
no local claim, worker or lease. It then probed the row's PID on THIS host and
attempted a ``running -> ready`` write that the lifecycle fence refused every
tick. Ownership is now decided BEFORE the PID probe and before any write, from
the canonical home (``COALESCE(current_node, source_node)``) cross-checked with
``tasks.tenant``. Unknown or contradictory homes stay unchanged AND visible.

The sibling ``release_stale_claims`` has the same gap for a claim this host did
not take; it gets the same guard. Boards without a Fleet adapter are unchanged.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

NODE = "turnerbook"
_FENCE = "verified execution lease required before running"

_MAP_DDL = """CREATE TABLE IF NOT EXISTS fleet_kanban_issue_map (
    local_task_id TEXT PRIMARY KEY,
    issue_id TEXT,
    raw_title TEXT,
    canonical_body TEXT,
    source_node TEXT,
    current_node TEXT,
    source_profile TEXT
)"""

# Shape of the live adapter trigger: the node id is the 5th VALUES literal.
_TRIGGER_DDL = f"""CREATE TRIGGER fleet_kanban_task_insert
    AFTER INSERT ON tasks
    BEGIN
        INSERT INTO fleet_kanban_issue_map(
            local_task_id,issue_id,raw_title,canonical_body,source_node,current_node,source_profile
        ) VALUES(NEW.id,'fk_' || lower(hex(randomblob(4))),NEW.title,NEW.body,'{NODE}',NULL,'p');
    END"""


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


@pytest.fixture
def fleet(conn):
    """Install the Fleet adapter (map table + insert trigger) on this board."""
    conn.execute(_MAP_DDL)
    conn.execute(_TRIGGER_DDL)
    conn.commit()
    return conn


def _task(conn, *, source=None, current=None, tenant=None, assignee="w", map_row=True):
    """A task mapped to a canonical home. ``source``/``current`` override the map row."""
    tid = kb.create_task(conn, title="t", assignee=assignee, tenant=tenant)
    if map_row:
        if source is not None or current is not None:
            conn.execute(
                "UPDATE fleet_kanban_issue_map SET source_node = ?, current_node = ? "
                "WHERE local_task_id = ?", (source, current, tid),
            )
    else:
        conn.execute("DELETE FROM fleet_kanban_issue_map WHERE local_task_id = ?", (tid,))
    conn.commit()
    return tid


def _orphan(conn, tid, *, claim_lock=None, claim_expires=None, worker_pid=None):
    conn.execute(
        "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, worker_pid=? "
        "WHERE id=?", (claim_lock, claim_expires, worker_pid, tid),
    )
    conn.commit()


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]


def _fence(conn, tid):
    """Lifecycle-guard trigger refusing every UPDATE of ``tid`` (what the lease fence does)."""
    conn.execute(
        f"CREATE TRIGGER fence_{tid} BEFORE UPDATE ON tasks WHEN OLD.id = '{tid}' "
        f"BEGIN SELECT RAISE(ABORT, '{_FENCE}'); END"
    )
    conn.commit()


# ---------------------------------------------------------------- local orphan


def test_local_orphan_is_requeued(fleet):
    tid = _task(fleet, tenant=NODE)  # source_node=NODE, current_node NULL
    _orphan(fleet, tid)
    errs: list = []
    assert kbd.reconcile_orphaned_running(fleet, errors_out=errs) == [tid]
    assert _status(fleet, tid) == "ready"
    assert errs == []


def test_local_orphan_moved_here_by_current_node(fleet):
    """current_node (where the card was handed to) wins over source_node."""
    tid = _task(fleet, source="max", current=NODE, tenant=NODE)
    _orphan(fleet, tid)
    assert kbd.reconcile_orphaned_running(fleet) == [tid]


def test_unmapped_card_predating_adapter_is_local(fleet):
    tid = _task(fleet, map_row=False)
    _orphan(fleet, tid)
    assert kbd.reconcile_orphaned_running(fleet) == [tid]


def test_board_without_adapter_is_unchanged(conn):
    tid = kb.create_task(conn, title="legacy", assignee="w", tenant="whatever")
    _orphan(conn, tid)
    errs: list = []
    assert kbd.reconcile_orphaned_running(conn, errors_out=errs) == [tid]
    assert errs == []


# -------------------------------------------------------------- foreign mirror


def test_foreign_mirror_is_skipped_silently(fleet, caplog):
    tid = _task(fleet, source="max", current=None, tenant="max")
    _orphan(fleet, tid)
    errs: list = []
    with caplog.at_level(logging.WARNING, logger=kb._log.name):
        assert kbd.reconcile_orphaned_running(fleet, errors_out=errs) == []
    assert _status(fleet, tid) == "running"
    assert errs == []  # expected state on a synced board: not a refusal
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_foreign_current_node_overrides_local_source(fleet):
    """Handed to another node: canonical home is current_node, not source_node."""
    tid = _task(fleet, source=NODE, current="sheldon", tenant="sheldon")
    _orphan(fleet, tid)
    assert kbd.reconcile_orphaned_running(fleet) == []
    assert _status(fleet, tid) == "running"


def test_foreign_mirror_never_attempts_the_write(fleet):
    """No UPDATE reaches the row, so no fence has to refuse it."""
    tid = _task(fleet, source="sheldon", tenant="sheldon")
    _orphan(fleet, tid)
    seen: list[str] = []
    fleet.set_trace_callback(lambda sql: seen.append(sql))
    try:
        kbd.reconcile_orphaned_running(fleet)
    finally:
        fleet.set_trace_callback(None)
    assert not [s for s in seen if s.lstrip().upper().startswith("UPDATE TASKS")]


# ------------------------------------------------------------------ unknown home


@pytest.mark.parametrize("source,current", [("", None), ("  ", None), (None, None)])
def test_unknown_home_stays_protected_and_visible(fleet, caplog, source, current):
    tid = _task(fleet, tenant=NODE)
    fleet.execute(
        "UPDATE fleet_kanban_issue_map SET source_node = ?, current_node = ? "
        "WHERE local_task_id = ?", (source, current, tid),
    )
    fleet.commit()
    _orphan(fleet, tid)
    errs: list = []
    with caplog.at_level(logging.WARNING, logger=kb._log.name):
        assert kbd.reconcile_orphaned_running(fleet, errors_out=errs) == []
    assert _status(fleet, tid) == "running"
    assert len(errs) == 1 and errs[0][0] == tid
    assert "ownership unverified" in errs[0][1]
    assert any("cannot be verified" in r.getMessage() for r in caplog.records)


def test_blank_current_node_overrides_local_source_and_is_unknown(fleet):
    """COALESCE contract: only NULL falls through to source_node; '' is a bad home."""
    tid = _task(fleet, tenant=NODE)
    fleet.execute(
        "UPDATE fleet_kanban_issue_map SET source_node = ?, current_node = '' "
        "WHERE local_task_id = ?", (NODE, tid),
    )
    fleet.commit()
    _orphan(fleet, tid)
    errs: list = []
    assert kbd.reconcile_orphaned_running(fleet, errors_out=errs) == []
    assert _status(fleet, tid) == "running"
    assert len(errs) == 1 and "not a node id" in errs[0][1]


def test_non_text_canonical_home_is_unknown(fleet):
    """A BLOB home must not be read as a node id (TEXT affinity would turn an
    integer into the string '12345', i.e. a plain foreign node, so use a BLOB)."""
    tid = _task(fleet, tenant=NODE)
    fleet.execute(
        "UPDATE fleet_kanban_issue_map SET source_node = X'6d6178', current_node = NULL "
        "WHERE local_task_id = ?", (tid,),
    )
    fleet.commit()
    assert fleet.execute(
        "SELECT typeof(source_node) FROM fleet_kanban_issue_map WHERE local_task_id = ?", (tid,),
    ).fetchone()[0] == "blob"
    _orphan(fleet, tid)
    errs: list = []
    assert kbd.reconcile_orphaned_running(fleet, errors_out=errs) == []
    assert _status(fleet, tid) == "running"
    assert len(errs) == 1 and "not a node id" in errs[0][1]


def test_contradictory_home_stays_protected_and_visible(fleet):
    """Map says this node, but the card's home (tenant) says another: do not guess."""
    tid = _task(fleet, source=NODE, current=NODE, tenant="max")
    _orphan(fleet, tid)
    errs: list = []
    assert kbd.reconcile_orphaned_running(fleet, errors_out=errs) == []
    assert _status(fleet, tid) == "running"
    assert len(errs) == 1 and "contradicts" in errs[0][1]


def test_blank_tenant_is_no_evidence(fleet):
    tid = _task(fleet, source=NODE, current=NODE, tenant=None)
    _orphan(fleet, tid)
    assert kbd.reconcile_orphaned_running(fleet) == [tid]


def test_unparseable_adapter_trigger_fails_closed(conn):
    """A trigger whose node id cannot be read: every mapped row is unverifiable."""
    conn.execute(_MAP_DDL)
    conn.execute(
        "CREATE TRIGGER fleet_kanban_task_insert AFTER INSERT ON tasks BEGIN "
        "INSERT INTO fleet_kanban_issue_map(local_task_id) VALUES(NEW.id); END"
    )
    tid = kb.create_task(conn, title="t", assignee="w")
    conn.execute(
        "UPDATE fleet_kanban_issue_map SET source_node = ? WHERE local_task_id = ?",
        (NODE, tid),
    )
    _orphan(conn, tid)
    errs: list = []
    assert kbd.reconcile_orphaned_running(conn, errors_out=errs) == []
    assert _status(conn, tid) == "running"
    assert len(errs) == 1 and "unreadable" in errs[0][1]


def test_unreadable_map_fails_closed(fleet):
    tid = _task(fleet, tenant=NODE)
    _orphan(fleet, tid)
    fleet.execute("ALTER TABLE fleet_kanban_issue_map RENAME COLUMN current_node TO cn")
    errs: list = []
    assert kbd.reconcile_orphaned_running(fleet, errors_out=errs) == []
    assert _status(fleet, tid) == "running"
    assert len(errs) == 1 and "ownership" in errs[0][1]


# ------------------------------------------------------------------ PID collision


def test_foreign_row_pid_is_never_probed_on_this_host(fleet, monkeypatch):
    """A foreign node's PID number can collide with a live local process."""
    tid = _task(fleet, source="sheldon", tenant="sheldon")
    # The colliding "foreign" PID is this test process: guaranteed live on this
    # host, and nothing to signal or clean up (the suite's kill guard stays idle).
    _orphan(fleet, tid, worker_pid=os.getpid())
    probed: list = []
    real = kbd._worker_alive
    monkeypatch.setattr(
        kbd, "_worker_alive", lambda *a, **k: probed.append(a) or real(*a, **k),
    )
    errs: list = []
    assert kbd.reconcile_orphaned_running(fleet, errors_out=errs) == []
    assert probed == []  # ownership decided BEFORE the host-local PID probe
    assert errs == []
    assert _status(fleet, tid) == "running"


def test_local_row_with_live_pid_still_defers(fleet, monkeypatch):
    tid = _task(fleet, tenant=NODE)
    _orphan(fleet, tid, worker_pid=os.getpid())
    probed: list = []
    real = kbd._worker_alive
    monkeypatch.setattr(
        kbd, "_worker_alive", lambda *a, **k: probed.append(a) or real(*a, **k),
    )
    assert kbd.reconcile_orphaned_running(fleet) == []
    assert len(probed) == 1  # a locally owned row IS probed, and a live PID defers it
    assert _status(fleet, tid) == "running"


# ------------------------------------------------------------------- fenced local


def test_fenced_local_row_is_isolated_not_bypassed(fleet):
    """A locally owned row the lifecycle fence still refuses: recorded, tick survives."""
    fenced = _task(fleet, tenant=NODE)
    ok = _task(fleet, tenant=NODE)
    _orphan(fleet, fenced)
    _orphan(fleet, ok)
    _fence(fleet, fenced)
    errs: list = []
    assert kbd.reconcile_orphaned_running(fleet, errors_out=errs) == [ok]
    assert _status(fleet, fenced) == "running"
    assert _status(fleet, ok) == "ready"
    assert len(errs) == 1 and errs[0][0] == fenced and _FENCE in errs[0][1]


def test_mixed_board_recovers_only_local(fleet):
    local = _task(fleet, tenant=NODE)
    foreign = _task(fleet, source="max", tenant="max")
    unknown = _task(fleet, source="", tenant=NODE)
    for t in (local, foreign, unknown):
        _orphan(fleet, t)
    errs: list = []
    assert kbd.reconcile_orphaned_running(fleet, errors_out=errs) == [local]
    assert _status(fleet, foreign) == "running"
    assert _status(fleet, unknown) == "running"
    assert [e[0] for e in errs] == [unknown]


def test_dispatch_once_surfaces_unknown_but_not_foreign(fleet):
    foreign = _task(fleet, source="max", tenant="max")
    unknown = _task(fleet, source="", tenant=NODE)
    for t in (foreign, unknown):
        _orphan(fleet, t)
    res = kbd.dispatch_once(fleet, spawn_fn=lambda *a, **k: (True, ""), max_spawn=0)
    assert res.reconciled_orphans == []
    refused = [str(e) for e in getattr(res, "reclaim_errors", [])]
    assert any(unknown in e for e in refused)
    assert not any(foreign in e for e in refused)
    assert _status(fleet, foreign) == "running" and _status(fleet, unknown) == "running"


# ----------------------------------------------------- sibling: release_stale_claims

_PAST = 1  # claim_expires long ago


def test_stale_claim_on_foreign_mirror_is_skipped(fleet):
    tid = _task(fleet, source="max", tenant="max")
    _orphan(fleet, tid, claim_lock="max-box:123", claim_expires=_PAST)
    errs: list = []
    assert kb.release_stale_claims(fleet, errors_out=errs) == 0
    assert _status(fleet, tid) == "running"
    assert errs == []


def test_stale_claim_not_taken_here_but_owned_here_is_released(fleet):
    """A local-home card whose claim came from elsewhere still recovers."""
    tid = _task(fleet, tenant=NODE)
    _orphan(fleet, tid, claim_lock="other-box:123", claim_expires=_PAST)
    assert kb.release_stale_claims(fleet) == 1
    assert _status(fleet, tid) != "running"


def test_stale_claim_unknown_home_is_visible(fleet):
    tid = _task(fleet, source="", tenant=NODE)
    _orphan(fleet, tid, claim_lock="other-box:123", claim_expires=_PAST)
    errs: list = []
    assert kb.release_stale_claims(fleet, errors_out=errs) == 0
    assert _status(fleet, tid) == "running"
    assert len(errs) == 1 and errs[0][0] == tid


def test_stale_host_local_claim_is_not_rejudged(fleet):
    """Our own claim_lock is ownership evidence; behavior is unchanged."""
    host = kb._claimer_id().split(":", 1)[0]
    tid = _task(fleet, source="max", tenant="max")  # even with a foreign map row
    _orphan(fleet, tid, claim_lock=f"{host}:dead", claim_expires=_PAST)
    assert kb.release_stale_claims(fleet) == 1
