"""Closing proofs for Board Ops on the v0.22.5 lifecycle seams (fleet carry K01/K04).

One admitted action, the task-state mutation it causes and its receipt commit in ONE
checked transaction: ``BoardOps.process_pending`` opens ``write_txn``; the action's
own ``write_txn`` calls (``keep_spec_triage_task``, ``recompute_ready``) nest as
savepoints only because ``compose_task_transition`` binds the connection
(``write_txn`` -> ``composes_connection``). A forced failure removes all three.
The canonical Fleet ownership fence refuses a foreign-owned row on the action path.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager

import pytest
import hermes_yaml as yaml

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_board_ops as ops
from hermes_cli import kanban_board_ops_store as store
from hermes_cli.kanban_board_ops_policy import Refused
from hermes_cli.kanban_board_ops_transactions import compose_task_transition
from hermes_cli.kanban_db_notify import add_notify_sub

NODE = "fixture-node"


@contextmanager
def profile(root, name):
    token = set_hermes_home_override(root / "profiles" / name)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


@pytest.fixture
def lane(tmp_path, monkeypatch):
    root = tmp_path / "fixture-node"
    root.mkdir()
    for name in ("conductor", "ops", "king"):
        home = root / "profiles" / name
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    config = {"kanban": {"board_ops": {"enabled": True, "node_id": NODE, "boards": ["fixture"],
                                       "owner_profile": "conductor", "executor_profile": "ops"}}}
    (root / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "conductor"))
    monkeypatch.setattr(ops, "_now", lambda: 2000)
    kb.create_board("fixture")
    conn = kbc.connect(board="fixture")
    tid = kb.create_task(conn, title="FIXTURE reviewed spec", body="exact body\n", tenant=NODE, triage=True,
                         assignee="worker")
    conn.execute("UPDATE task_events SET created_at=1900 WHERE task_id=?", (tid,))
    add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="fixture-chat", notifier_profile="king")
    yield root, conn, ops.BoardOps(conn, "fixture"), tid
    conn.close()


def admit(lane, operations=("keep_spec", "record_input")):
    root, _conn, api, tid = lane
    with profile(root, "conductor"):
        api.control(enabled=True, human_attribution="Turner FIXTURE", authority_provenance="FIXTURE")
        api.grant({
            "grant_id": "fixture-grant", "node_id": NODE, "board": "fixture", "task_ids": [tid],
            "tenant": NODE, "operator_profile": "ops", "owner_chain": ["Turner FIXTURE", "conductor"],
            "human_attribution": "Turner FIXTURE", "authority_provenance": "FIXTURE",
            "allowed_operations": list(operations), "expires_at": 2500,
            "escalation_recipient": {"profile": "king", "platform": "telegram", "chat_id": "fixture-chat", "thread_id": ""},
            "review_authors": ["Turner FIXTURE"],
        })


def submit(lane, operation="keep_spec", payload=None):
    root, conn, api, tid = lane
    task = kb.get_task(conn, tid)
    event_id = conn.execute("SELECT MIN(id) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
    data = {"correlation": "fixture-correlation", "grant_id": "fixture-grant", "node_id": NODE,
            "board": "fixture", "task_id": tid, "tenant": NODE, "operation": operation,
            "task_sha256": kb.compute_task_sha256(task.title, task.body), "event_id": event_id,
            "payload": {"review_author": "Turner FIXTURE"} if payload is None else payload}
    with profile(root, "ops"):
        return api.request(data)


def run(lane):
    with profile(lane[0], "ops"):
        return lane[2].process_pending()


def committed_view(conn, tid):
    """Read through an independent connection: only committed rows are visible."""
    path = [r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"][0]
    with closing(sqlite3.connect(path)) as other:
        other.row_factory = sqlite3.Row
        kinds = [r["kind"] for r in other.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]
        return {
            "status": other.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"],
            "kinds": kinds,
            "comments": [r["body"] for r in other.execute("SELECT body FROM task_comments WHERE task_id=?", (tid,))],
            "request_state": other.execute("SELECT state FROM kanban_board_ops_requests").fetchone()["state"],
        }


def install_fleet_adapter(conn, tid, *, current_node, lease_expires=9_999_999_999):
    """Minimal Fleet adapter schema: the node-id trigger, issue map and verified lease."""
    conn.execute(
        "CREATE TABLE fleet_kanban_issue_map (local_task_id TEXT PRIMARY KEY, issue_id TEXT, "
        "title TEXT, body TEXT, source_node TEXT, current_node TEXT)"
    )
    conn.execute(
        "CREATE TRIGGER fleet_kanban_task_insert AFTER INSERT ON tasks BEGIN "
        "INSERT INTO fleet_kanban_issue_map (local_task_id, issue_id, title, body, source_node) "
        f"VALUES (NEW.id, 'fk_' || NEW.id, NEW.title, NEW.body, '{NODE}'); END"
    )
    conn.execute("CREATE TABLE fleet_kanban_verified_leases (issue_id TEXT PRIMARY KEY, expires_at INTEGER)")
    conn.execute("INSERT INTO fleet_kanban_issue_map VALUES (?, ?, 't', 'b', ?, ?)", (tid, "fk_" + tid, NODE, current_node))
    conn.execute("INSERT INTO fleet_kanban_verified_leases VALUES (?, ?)", ("fk_" + tid, lease_expires))


def test_action_state_mutation_and_receipt_commit_together(lane):
    _root, conn, _api, tid = lane
    admit(lane)
    submit(lane)
    receipt = run(lane)[0]
    assert receipt["state"] == "applied"
    assert receipt["facts"]["status_after"] == "ready"
    view = committed_view(conn, tid)
    # Action (spec release audit), state mutation (triage -> todo -> ready through the
    # .5 recompute_ready) and the receipt are all durable after the one commit.
    assert view["status"] == "ready"
    assert {"specified", "promoted", "board_ops_receipt"} <= set(view["kinds"])
    assert any("kept original spec" in body for body in view["comments"])
    assert view["request_state"] == "applied"
    assert not conn.in_transaction


def test_forced_receipt_failure_rolls_back_action_state_and_receipt(lane):
    _root, conn, _api, tid = lane
    admit(lane)
    submit(lane)
    before = committed_view(conn, tid)
    conn.execute(
        "CREATE TRIGGER fixture_receipt_failure BEFORE INSERT ON task_events "
        "WHEN NEW.kind='board_ops_receipt' BEGIN SELECT RAISE(ABORT,'FIXTURE crash before commit'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="FIXTURE crash"):
        run(lane)
    after = committed_view(conn, tid)
    assert after == before
    assert after["status"] == "triage"
    assert not {"specified", "promoted", "board_ops_receipt"} & set(after["kinds"])
    assert after["request_state"] == "pending"
    assert not conn.in_transaction
    conn.execute("DROP TRIGGER fixture_receipt_failure")
    assert run(lane)[0]["state"] == "applied"
    assert committed_view(conn, tid)["status"] == "ready"


def test_failed_action_rolls_back_its_mutation_and_records_exception_receipt(lane):
    _root, conn, _api, tid = lane
    admit(lane)
    submit(lane)
    conn.execute(
        "CREATE TRIGGER fixture_action_failure BEFORE INSERT ON task_events "
        "WHEN NEW.kind='specified' BEGIN SELECT RAISE(ABORT,'FIXTURE action failure'); END"
    )
    receipt = run(lane)[0]
    assert receipt["state"] == "exception"
    assert receipt["facts"]["task_action_committed"] is False
    view = committed_view(conn, tid)
    assert view["status"] == "triage"
    assert "specified" not in view["kinds"] and "promoted" not in view["kinds"]
    assert not any("kept original spec" in body for body in view["comments"])
    assert view["request_state"] == "exception"


def test_lifecycle_write_txn_nests_only_inside_the_composed_transition(lane):
    _root, conn, _api, tid = lane
    with kbc.write_txn(conn):
        with pytest.raises(RuntimeError, match="already inside a transaction"):
            kb.recompute_ready(conn)
    with kbc.write_txn(conn):
        with compose_task_transition(conn, tid):
            assert kb.recompute_ready(conn) == 0
    with pytest.raises(RuntimeError, match="outer checked write transaction"):
        with compose_task_transition(conn, tid):
            pass


def test_foreign_owned_row_is_refused_at_admission(lane):
    _root, conn, _api, tid = lane
    admit(lane)
    install_fleet_adapter(conn, tid, current_node="other-node")
    before = store.state_snapshot(conn, tid)
    with pytest.raises(Refused, match="foreign Fleet ownership fence"):
        submit(lane)
    assert conn.execute("SELECT COUNT(*) FROM kanban_board_ops_requests").fetchone()[0] == 0
    assert store.state_snapshot(conn, tid) == before


def test_ownership_moved_after_admission_refuses_the_action(lane):
    _root, conn, _api, tid = lane
    install_fleet_adapter(conn, tid, current_node=NODE)
    admit(lane)
    assert submit(lane)["state"] == "pending"
    conn.execute("UPDATE fleet_kanban_issue_map SET current_node='other-node' WHERE local_task_id=?", (tid,))
    receipt = run(lane)[0]
    assert receipt["state"] == "exception"
    assert "foreign Fleet ownership fence" in receipt["facts"]["reason"]
    view = committed_view(conn, tid)
    assert view["status"] == "triage" and "specified" not in view["kinds"]


def test_unknown_ownership_is_refused_by_the_canonical_fence(lane):
    _root, conn, _api, tid = lane
    install_fleet_adapter(conn, tid, current_node=NODE)
    admit(lane)
    # Home is this node but the card tenant names another home: _fleet_row_ownership
    # answers UNKNOWN, which Board Ops treats as no authority.
    conn.execute("UPDATE tasks SET tenant='other-node' WHERE id=?", (tid,))
    assert kb._fleet_row_ownership(conn, tid, NODE)[0] == kb.FLEET_OWNER_UNKNOWN
    with pytest.raises(Refused, match="Fleet ownership fence: unknown"):
        task = kb.get_task(conn, tid)
        from hermes_cli.kanban_board_ops_actions import assert_local_ownership
        assert_local_ownership(conn, {"id": tid, "tenant": task.tenant}, NODE, 2000)


def test_local_owned_row_with_verified_lease_is_applied(lane):
    _root, conn, _api, tid = lane
    install_fleet_adapter(conn, tid, current_node=NODE)
    admit(lane)
    submit(lane)
    assert run(lane)[0]["state"] == "applied"
    assert committed_view(conn, tid)["status"] == "ready"
