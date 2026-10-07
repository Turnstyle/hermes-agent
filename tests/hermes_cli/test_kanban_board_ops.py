"""Board Ops run2768 FIXTURES, never installed/live-node acceptance.

Contracts cover authenticated scope, atomic action receipts, exact text/state
binding, finite exceptions and durable recovery. Existing keep-spec regression
owns the carry's algorithm; an absent upstream guard is reported, never patched.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
import hermes_yaml as yaml

from agent.delegation_context import delegated_child_context
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_board_ops as ops
from hermes_cli import kanban_board_ops_store as store
from hermes_cli.kanban_board_ops_policy import Refused
from hermes_cli.kanban_board_ops_transactions import compose_task_transition
from hermes_cli.kanban_db_notify import add_notify_sub


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
    for name in ("conductor", "ops", "king", "worker"):
        home = root / "profiles" / name
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    config = {"kanban": {"board_ops": {"enabled": True, "node_id": "fixture-node",
              "boards": ["fixture"], "owner_profile": "conductor", "executor_profile": "ops"}}}
    (root / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "conductor"))
    monkeypatch.setattr(ops, "_now", lambda: 2000)
    kb.create_board("fixture")
    conn = kbc.connect(board="fixture")
    api = ops.BoardOps(conn, "fixture")
    tid = kb.create_task(conn, title="  FIXTURE reviewed Ω\n", body="exact\n\n", tenant="fixture-tenant", triage=True,
                         assignee="worker", model_override="fixture-model", provider_override="fixture-provider")
    conn.execute("UPDATE task_events SET created_at=1900 WHERE task_id=?", (tid,))
    add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="fixture-chat", notifier_profile="king")
    yield root, conn, api, tid
    conn.close()


def admit(lane, operations=None, task_ids=None):
    root, conn, api, tid = lane
    with profile(root, "conductor"):
        api.control(enabled=True, human_attribution="Turner FIXTURE", authority_provenance="run2768 FIXTURE")
        data = {
            "grant_id": "fixture-grant", "node_id": "fixture-node", "board": "fixture",
            "task_ids": task_ids or [tid], "tenant": "fixture-tenant", "operator_profile": "ops",
            "owner_chain": ["Turner FIXTURE", "CoS FIXTURE", "conductor"],
            "human_attribution": "Turner FIXTURE", "authority_provenance": "run2768 FIXTURE",
            "allowed_operations": operations or ["record_input", "inspect_wait", "interrupted_cli", "escalate", "keep_spec"],
            "expires_at": 2500, "escalation_recipient": {"profile": "king", "platform": "telegram", "chat_id": "fixture-chat", "thread_id": ""},
            "review_authors": ["Turner FIXTURE"],
        }
        api.grant(data)
    return data


def request(lane, operation="record_input", payload=None, correlation="fixture-correlation"):
    root, conn, api, tid = lane
    task = kb.get_task(conn, tid)
    event_id = conn.execute("SELECT MIN(id) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
    return {"correlation": correlation, "grant_id": "fixture-grant", "node_id": "fixture-node", "board": "fixture",
            "task_id": tid, "tenant": "fixture-tenant", "operation": operation,
            "task_sha256": kb.compute_task_sha256(task.title, task.body), "event_id": event_id,
            "payload": {"text": "FIXTURE owner input"} if payload is None else payload}


def submit(lane, data):
    with profile(lane[0], "ops"):
        return lane[2].request(data)


def run(lane):
    with profile(lane[0], "ops"):
        return lane[2].process_pending()


def test_default_off_and_owner_only_control(lane):
    root, conn, api, tid = lane
    before = store.state_snapshot(conn, tid)
    with pytest.raises(Refused, match="no owner admission"):
        api.request(request(lane))
    assert not store.installed(conn)
    with profile(root, "ops"), pytest.raises(Refused, match="configured node Conductor"):
        api.control(enabled=True, human_attribution="FIXTURE", authority_provenance="FIXTURE")
    assert not store.installed(conn)
    assert store.state_snapshot(conn, tid) == before


def test_config_default_off_and_no_authority_from_profile_config(lane):
    root, conn, api, _tid = lane
    (root / "config.yaml").write_text("{}\n", encoding="utf-8")
    (root / "profiles" / "conductor" / "config.yaml").write_text(
        yaml.safe_dump({"kanban": {"board_ops": {"enabled": True}}}), encoding="utf-8",
    )
    with pytest.raises(Refused, match="authority unavailable"):
        api.control(enabled=True, human_attribution="FIXTURE", authority_provenance="FIXTURE")
    assert not store.installed(conn)


def test_authorized_input_action_preserves_card_and_atomically_receipts(lane):
    root, conn, api, tid = lane
    admit(lane)
    before = store.state_snapshot(conn, tid)
    data = request(lane)
    assert submit(lane, data)["state"] == "pending"
    receipt = run(lane)[0]
    assert receipt["state"] == "applied"
    assert receipt["facts"]["holds_released"] is False
    assert receipt["inference_calls"] == 0
    assert store.state_snapshot(conn, tid) == before
    assert [(c.author, c.body) for c in kb.list_comments(conn, tid)] == [("ops", "FIXTURE owner input")]
    with profile(root, "ops"):
        assert api.receipt(data["correlation"])["receipt"] == receipt
    assert run(lane) == []


@pytest.mark.parametrize("identity", ["worker", "child", "process-child", "worker-pin"])
def test_worker_and_child_deny_all_management_and_requests(lane, monkeypatch, identity):
    root, conn, api, tid = lane
    contract = admit(lane)
    before = store.state_snapshot(conn, tid)
    with profile(root, "ops"):
        if identity == "process-child":
            monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(root))
        if identity == "worker-pin":
            monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        scope = delegated_child_context() if identity == "child" else profile(root, "worker" if identity == "worker" else "ops")
        with scope:
            for call in (lambda: api.request(request(lane)), lambda: api.grant(contract),
                         lambda: api.revoke("fixture-grant"), lambda: api.control(enabled=False, human_attribution="FIXTURE", authority_provenance="FIXTURE")):
                with pytest.raises((Refused, PermissionError)):
                    call()
    assert store.state_snapshot(conn, tid) == before
    assert conn.execute("SELECT COUNT(*) FROM kanban_board_ops_requests").fetchone()[0] == 0


@pytest.mark.parametrize("field,value", [
    ("node_id", "other-node"), ("board", "other-board"), ("task_id", "t_other"),
    ("tenant", "other-tenant"), ("grant_id", "other-grant"),
    ("operation", "archive"), ("actor_home", "forged"),
])
def test_scope_and_permission_escalation_refused(lane, field, value):
    admit(lane)
    data = request(lane)
    data[field] = value
    with pytest.raises(Refused):
        submit(lane, data)
    assert lane[1].execute("SELECT COUNT(*) FROM kanban_board_ops_requests").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["grant", "extend", "revoke", "model", "config", "persona", "restart", "install", "kill", "archive", "delete", "unlink", "edit_locks", "release_hold", "dispatch"])
def test_non_delegable_grant_operations(lane, operation):
    contract = admit(lane)
    contract.update(grant_id="other", allowed_operations=[operation])
    with pytest.raises(Refused, match="nondelegable"):
        lane[2].grant(contract)


@pytest.mark.parametrize("change", ["expired", "revoked", "disabled", "grant-digest"])
def test_revocation_expiry_and_content_revalidated_at_action(lane, monkeypatch, change):
    root, conn, api, tid = lane
    admit(lane)
    submit(lane, request(lane))
    if change == "expired":
        monkeypatch.setattr(ops, "_now", lambda: 2500)
    elif change == "revoked":
        api.revoke("fixture-grant")
    elif change == "disabled":
        api.control(enabled=False, human_attribution="FIXTURE", authority_provenance="FIXTURE")
    else:
        conn.execute("UPDATE kanban_board_ops_grants SET digest='tampered'")
    receipt = run(lane)[0]
    assert receipt["state"] == "exception"
    assert not kb.list_comments(conn, tid)
    assert run(lane) == []


def test_correlation_duplicate_and_conflict_do_not_duplicate_action(lane):
    admit(lane)
    data = request(lane)
    submit(lane, data)
    assert submit(lane, data)["state"] == "pending"
    changed = copy.deepcopy(data)
    changed["payload"]["text"] = "other FIXTURE input"
    with pytest.raises(Refused, match="correlation conflict"):
        submit(lane, changed)
    run(lane)
    assert submit(lane, data)["state"] == "applied"
    assert len(kb.list_comments(lane[1], lane[3])) == 1


@pytest.mark.parametrize("change", ["text", "tenant", "status", "run", "dependency", "hold"])
def test_fresh_state_binding_refuses_edits_dependencies_holds_and_runs(lane, change):
    root, conn, api, tid = lane
    admit(lane)
    submit(lane, request(lane))
    if change == "dependency":
        parent = kb.create_task(conn, title="FIXTURE dependency", tenant="fixture-tenant")
        kb.link_tasks(conn, parent, tid)
    elif change == "text":
        conn.execute("UPDATE tasks SET body='edited' WHERE id=?", (tid,))
    elif change == "tenant":
        conn.execute("UPDATE tasks SET tenant='other' WHERE id=?", (tid,))
    elif change == "status":
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    elif change == "run":
        conn.execute("UPDATE tasks SET current_run_id=99,claim_lock='FIXTURE' WHERE id=?", (tid,))
    else:
        with kbc.write_txn(conn):
            kb._append_event(conn, tid, "block_loop_detected", {"reason": "FIXTURE"})
    assert run(lane)[0]["state"] == "exception"
    assert not kb.list_comments(conn, tid)


def test_transaction_failure_rolls_back_action_with_no_partial_receipt(lane):
    root, conn, api, tid = lane
    admit(lane)
    submit(lane, request(lane))
    conn.execute("CREATE TRIGGER fixture_receipt_failure BEFORE INSERT ON task_events WHEN NEW.kind='board_ops_receipt' BEGIN SELECT RAISE(ABORT,'FIXTURE crash before commit'); END")
    with pytest.raises(sqlite3.IntegrityError, match="FIXTURE crash"):
        run(lane)
    assert not kb.list_comments(conn, tid)
    assert store.receipt(conn, "fixture-correlation")["state"] == "pending"
    conn.execute("DROP TRIGGER fixture_receipt_failure")
    assert run(lane)[0]["state"] == "applied"
    assert len(kb.list_comments(conn, tid)) == 1


def test_process_restart_recovers_pending_and_does_not_replay_applied(lane):
    root, conn, api, tid = lane
    admit(lane)
    submit(lane, request(lane))
    with profile(root, "ops"), ops.open_board_ops("fixture") as restarted:
        assert restarted.process_pending()[0]["state"] == "applied"
    with profile(root, "ops"), ops.open_board_ops("fixture") as restarted:
        assert restarted.process_pending() == []
    assert len(kb.list_comments(conn, tid)) == 1


def test_original_event_deadline_and_single_escalation_claim(lane, monkeypatch):
    root, conn, api, tid = lane
    admit(lane)
    submit(lane, request(lane))
    monkeypatch.setattr(ops, "_now", lambda: 2200)
    receipt = run(lane)[0]
    assert "deadline elapsed" in receipt["facts"]["reason"]
    assert receipt["original_event_at"] == 1900
    with profile(root, "ops"):
        deliveries = api.claim_escalations()
        assert len(deliveries) == 1
        assert api.claim_escalations() == []
        result = api.finish_escalation("fixture-correlation", delivered=True, detail="FIXTURE transport success")
        assert result["state"] == "notified"
        assert result["delivery_result"]["deadline_met"] is True
        assert api.claim_escalations() == []
    assert not kb.list_comments(conn, tid)


def test_interrupted_notice_after_crash_is_unknown_and_never_retried(lane, monkeypatch):
    root, conn, api, tid = lane
    admit(lane)
    submit(lane, request(lane, "inspect_wait", {}))
    run(lane)
    with profile(root, "ops"):
        assert len(api.claim_escalations()) == 1
    monkeypatch.setattr(ops, "_now", lambda: 2031)
    with profile(root, "ops"), ops.open_board_ops("fixture") as restarted:
        assert restarted.claim_escalations() == []
        assert restarted.receipt("fixture-correlation")["state"] == "delivery_unknown"
        assert restarted.claim_escalations() == []


@pytest.mark.parametrize("status", ["not_configured", "unavailable", "timed_out"])
def test_jev_refusal_and_timeout_are_finite_no_inference(lane, status):
    root, conn, api, tid = lane
    admit(lane)
    submit(lane, request(lane, "escalate", {"reason": "FIXTURE judgment required", "jev_status": status}))
    result = run(lane)[0]
    assert result["facts"]["execution_authorized"] is False
    assert result["facts"]["jev_status"] == status
    assert result["inference_calls"] == 0
    for _ in range(3):
        assert run(lane) == []


def test_healthy_ongoing_maker_is_observed_never_redispatched(lane):
    root, conn, api, tid = lane
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid)
    from gateway.status import get_process_start_time
    pid, started = os.getpid(), get_process_start_time(os.getpid())
    conn.execute("UPDATE tasks SET worker_pid=?,worker_started_at=? WHERE id=?", (pid, started, tid))
    conn.execute("UPDATE task_runs SET worker_pid=?,worker_started_at=? WHERE id=?", (pid, started, claimed.current_run_id))
    admit(lane)
    before = store.state_snapshot(conn, tid)
    submit(lane, request(lane, "inspect_wait", {}))
    assert run(lane)[0]["state"] == "observed"
    assert store.state_snapshot(conn, tid) == before


def test_interrupted_cli_preserves_dead_session_candidate_and_checkpoints(lane, tmp_path):
    root, conn, api, tid = lane
    workspace = tmp_path / "FIXTURE-saved-maker"
    workspace.mkdir()
    candidate = workspace / "candidate.py"
    candidate.write_text("saved FIXTURE source\n", encoding="utf-8")
    checkpoint = workspace / "checkpoint.json"
    checkpoint.write_text('{"saved":true}', encoding="utf-8")
    # A real short-lived local FIXTURE child proves a dead PID without killing.
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    conn.execute("UPDATE tasks SET workspace_path=?,worker_pid=?,worker_started_at=1 WHERE id=?", (str(workspace), child.pid, tid))
    admit(lane)
    before = store.state_snapshot(conn, tid)
    payload = {"candidate_path": str(candidate), "candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest()}
    submit(lane, request(lane, "interrupted_cli", payload))
    result = run(lane)[0]
    assert result["state"] == "exception"
    assert result["facts"]["candidate_verified"] is True
    assert result["facts"]["session_dead_verified"] is True
    assert candidate.read_text(encoding="utf-8") == "saved FIXTURE source\n"
    assert checkpoint.read_text(encoding="utf-8") == '{"saved":true}'
    assert store.state_snapshot(conn, tid) == before


def test_recipient_must_be_locally_existing_and_task_bound(lane):
    root, conn, api, tid = lane
    contract = admit(lane)
    for target in ("missing-profile", "worker"):
        modified = copy.deepcopy(contract)
        modified["grant_id"] = target
        modified["escalation_recipient"]["profile"] = target
        with pytest.raises(Refused, match="identity|subscription"):
            api.grant(modified)
    submit(lane, request(lane, "inspect_wait", {}))
    conn.execute("DELETE FROM kanban_notify_subs WHERE task_id=?", (tid,))
    run(lane)
    with profile(root, "ops"):
        assert api.claim_escalations() == []
        assert api.receipt("fixture-correlation")["state"] == "recipient_unavailable"


def test_estop_preserved_and_action_refused(lane):
    root, conn, api, tid = lane
    admit(lane)
    submit(lane, request(lane))
    sentinel = root / "ESTOP"
    sentinel.write_text("FIXTURE pause", encoding="utf-8")
    assert "ESTOP" in run(lane)[0]["facts"]["reason"]
    assert sentinel.read_text(encoding="utf-8") == "FIXTURE pause"
    assert not kb.list_comments(conn, tid)


def test_connection_composition_and_target_only_readiness(lane):
    root, conn, api, tid = lane
    unrelated = kb.create_task(conn, title="FIXTURE unrelated", triage=True)
    conn.execute("UPDATE tasks SET status='todo' WHERE id IN (?,?)", (tid, unrelated))
    with kbc.write_txn(conn):
        with compose_task_transition(conn, tid):
            assert kb.recompute_ready(conn) == 1
            assert kb.get_task(conn, tid).status == "ready"
            assert kb.get_task(conn, unrelated).status == "todo"
    assert kb.recompute_ready(conn) == 1


def test_keep_spec_dependency_refuses_without_substitution(lane):
    if hasattr(kb, "_has_unreleased_block_loop"):
        pytest.skip("original carry prerequisite is now supplied; acceptance test owns this path")
    admit(lane)
    before = store.state_snapshot(lane[1], lane[3])
    submit(lane, request(lane, "keep_spec", {"review_author": "Turner FIXTURE"}))
    result = run(lane)[0]
    assert result["state"] == "exception"
    assert "UPSTREAM_PREREQUISITE" in result["facts"]["reason"]
    assert store.state_snapshot(lane[1], lane[3]) == before


def test_retired_public_cli_keeps_ordinary_kanban_parser(lane, capsys):
    from hermes_cli.kanban_parser import build_parser

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    with pytest.raises(SystemExit) as refusal:
        parser.parse_args(["kanban", "--board", "fixture", "board-ops", "tick"])
    assert refusal.value.code == 2
    assert "board-ops" in capsys.readouterr().err
    args = parser.parse_args(["kanban", "--board", "fixture", "unblock", lane[3]])
    assert args.kanban_action == "unblock"
    # The retired CLI's receipt/storage assertions remain at the retained API.
    admit(lane)
    submit(lane, request(lane))
    receipt = run(lane)[0]
    assert receipt["state"] == "applied"
    assert len(kb.list_comments(lane[1], lane[3])) == 1
