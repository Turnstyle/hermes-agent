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
import yaml

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
    with kbc.write_txn(conn):
        kb._append_event(conn, tid, "block_loop_detected", {"reason": "FIXTURE changed after intake"})
    monkeypatch.setattr(ops, "_now", lambda: 2201)
    receipt = run(lane)[0]
    assert "deadline breached" in receipt["facts"]["reason"]
    assert "state/run/dependencies changed" in receipt["facts"]["reason"]
    assert receipt["action_deadline_met"] is False
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


def test_public_cli_shared_request_tick_receipt(lane, tmp_path, capsys):
    from hermes_cli.kanban import kanban_command
    from hermes_cli.kanban_parser import build_parser
    root, conn, api, tid = lane
    admit(lane)
    file = tmp_path / "FIXTURE-request.json"
    file.write_text(json.dumps(request(lane)), encoding="utf-8")
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    with profile(root, "ops"):
        for argv in (["request", str(file)], ["tick"], ["receipt", "fixture-correlation"]):
            args = parser.parse_args(["kanban", "--board", "fixture", "board-ops", *argv])
            assert kanban_command(args) == 0
            assert json.loads(capsys.readouterr().out)["ok"] is True
    assert len(kb.list_comments(conn, tid)) == 1


def test_delayed_eligible_keep_spec_revalidates_and_applies(lane, monkeypatch):
    root, conn, api, tid = lane
    admit(lane)
    before = kb.get_task(conn, tid)
    data = request(lane, "keep_spec", {"review_author": "Turner FIXTURE"})
    submit(lane, data)
    monkeypatch.setattr(ops, "_now", lambda: 2301)
    receipt = run(lane)[0]
    assert receipt["state"] == "applied", json.dumps(receipt)
    after = kb.get_task(conn, tid)
    assert (after.title, after.body) == (before.title, before.body)
    assert receipt["facts"]["sha256"] == data["task_sha256"]
    assert receipt["action_deadline_met"] is False
    assert receipt["original_event_at"] == 1900
    assert receipt["inference_calls"] == 0
    assert run(lane) == []


def standing_grant(lane):
    contract = admit(lane)
    contract.update(grant_id="fixture-standing", standing_policy={
        "renew_until": 10000, "max_tasks": 3, "assignees": ["worker"],
        "allow_descendants": True,
    })
    with profile(lane[0], "conductor"):
        lane[2].grant(contract)
    return contract


def test_standing_renewal_admits_later_child_without_primary_chat(lane, monkeypatch):
    root, conn, api, tid = lane
    contract = standing_grant(lane)
    monkeypatch.setattr(ops, "_now", lambda: 2600)
    child = kb.create_task(conn, title="FIXTURE later child", tenant="fixture-tenant",
                           assignee="worker", creator_task_id=tid, triage=True)
    with profile(root, "ops"):
        renewed = api.grant({"renew_from": contract["grant_id"], "task_ids": [child]})
        assert renewed["task_ids"] == [child]
        assert renewed["grant_id"] != contract["grant_id"]
        assert renewed["expires_at"] <= min(2600 + 86400, 10000)
        assert renewed["human_attribution"] == contract["human_attribution"]
        assert renewed["authority_provenance"] == contract["authority_provenance"]
        assert api.grant({"renew_from": contract["grant_id"], "task_ids": [child]})["grant_id"] == renewed["grant_id"]
    assert store.grant(conn, contract["grant_id"])["expires_at"] == 2500


def test_gateway_intakes_real_block_event_once_without_manual_request(lane, monkeypatch):
    from gateway import kanban_board_ops as gateway_ops
    root, conn, api, _ = lane
    monkeypatch.setattr(kb.time, "time", lambda: 2000)
    tid = kb.create_task(conn, title="FIXTURE real ready card", tenant="fixture-tenant", assignee="worker")
    add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="fixture-chat", notifier_profile="king")
    admit((root, conn, api, tid))
    assert kb.block_task(conn, tid, reason="FIXTURE needs owner input", kind="needs_input")
    blocked = store.state_snapshot(conn, tid)
    with profile(root, "ops"):
        deliveries = gateway_ops._collect(kb)
        assert len(deliveries) == 1
        receipt = deliveries[0]["receipt"]
        assert receipt["operation"] == "inspect_wait"
        assert receipt["original_event_at"] == 2000
        assert receipt["inference_calls"] == 0
        assert gateway_ops._collect(kb) == []
    assert store.state_snapshot(conn, tid) == blocked
    assert conn.execute("SELECT COUNT(*) FROM kanban_board_ops_requests").fetchone()[0] == 1


@pytest.mark.parametrize("failure", ["hold", "revoked", "expired", "dependency"])
def test_delayed_keep_spec_preserves_real_prerequisites(lane, monkeypatch, failure):
    root, conn, api, tid = lane
    admit(lane)
    if failure == "hold":
        with kbc.write_txn(conn):
            kb._append_event(conn, tid, "block_loop_detected", {"reason": "FIXTURE real hold"})
    if failure == "dependency":
        parent = kb.create_task(conn, title="FIXTURE genuine parent", triage=True)
        kb.link_tasks(conn, parent, tid)
    before = store.state_snapshot(conn, tid)
    submit(lane, request(lane, "keep_spec", {"review_author": "Turner FIXTURE"}))
    if failure == "revoked":
        api.revoke("fixture-grant")
    monkeypatch.setattr(ops, "_now", lambda: 2500 if failure == "expired" else 2301)
    receipt = run(lane)[0]
    assert receipt["action_deadline_met"] is False
    if failure == "dependency":
        assert receipt["state"] == "applied"
        assert kb.get_task(conn, tid).status == "todo"
        assert store.state_snapshot(conn, tid)["parents"] == before["parents"]
    else:
        assert receipt["state"] == "exception"
        assert ("unreleased block loop" if failure == "hold" else "revoked or expired") in receipt["facts"]["reason"]
        assert store.state_snapshot(conn, tid) == before


@pytest.mark.parametrize("failure", ["revoked", "expired-policy", "tenant", "assignee", "unrelated", "grandchild", "worker", "child", "cap"])
def test_standing_renewal_cannot_expand_or_resurrect_authority(lane, monkeypatch, failure):
    root, conn, api, tid = lane
    contract = standing_grant(lane)
    monkeypatch.setattr(ops, "_now", lambda: 2600)
    monkeypatch.setattr(kb.time, "time", lambda: 2600)
    def child(creator=tid, tenant="fixture-tenant", assignee="worker"):
        return kb.create_task(conn, title="FIXTURE bounded child", tenant=tenant, assignee=assignee, creator_task_id=creator, triage=True)
    target = child(creator=None if failure == "unrelated" else tid,
                   tenant="other" if failure == "tenant" else "fixture-tenant",
                   assignee="ops" if failure == "assignee" else "worker")
    if failure == "grandchild":
        target = child(creator=target)
    with profile(root, "ops"):
        if failure in {"revoked", "cap"}:
            issued = api.grant({"renew_from": contract["grant_id"], "task_ids": [target]})
        if failure == "cap":
            api.grant({"renew_from": contract["grant_id"], "task_ids": [child()]})
            target = child()
    if failure == "revoked":
        with profile(root, "conductor"):
            api.revoke(issued["grant_id"])
        assert store.grant(conn, contract["grant_id"])["revoked_at"] is not None
    if failure == "expired-policy":
        monkeypatch.setattr(ops, "_now", lambda: 10000)
    count = conn.execute("SELECT COUNT(*) FROM kanban_board_ops_grants").fetchone()[0]
    before = store.state_snapshot(conn, target)
    with profile(root, "worker" if failure == "worker" else "ops"):
        scope = delegated_child_context() if failure == "child" else profile(root, "worker" if failure == "worker" else "ops")
        with scope, pytest.raises((Refused, PermissionError)):
            api.grant({"renew_from": contract["grant_id"], "task_ids": [target]})
    assert conn.execute("SELECT COUNT(*) FROM kanban_board_ops_grants").fetchone()[0] == count
    assert store.state_snapshot(conn, target) == before


def test_standing_request_rechecks_root_revocation_before_action(lane, monkeypatch):
    root, conn, api, tid = lane
    contract = standing_grant(lane)
    monkeypatch.setattr(ops, "_now", lambda: 2600)
    monkeypatch.setattr(kb.time, "time", lambda: 2600)
    child = kb.create_task(conn, title="FIXTURE pending child", tenant="fixture-tenant", assignee="worker", creator_task_id=tid, triage=True)
    with profile(root, "ops"):
        issued = api.grant({"renew_from": contract["grant_id"], "task_ids": [child]})
        data = request((root, conn, api, child))
        data["grant_id"] = issued["grant_id"]
        api.request(data)
    with profile(root, "conductor"):
        api.revoke(contract["grant_id"])
    assert run(lane)[0]["state"] == "exception"
    assert not kb.list_comments(conn, child)


def test_event_intake_restart_and_later_child_are_durable(lane, monkeypatch):
    root, conn, api, tid = lane
    standing_grant(lane)
    monkeypatch.setattr(ops, "_now", lambda: 2600)
    monkeypatch.setattr(kb.time, "time", lambda: 2600)
    child = kb.create_task(conn, title="FIXTURE later blocked child", tenant="fixture-tenant", assignee="worker", creator_task_id=tid, initial_status="blocked")
    before = store.state_snapshot(conn, child)
    with profile(root, "ops"):
        assert len(api.intake_events()) == 1
    with profile(root, "ops"), ops.open_board_ops("fixture") as restarted:
        assert restarted.intake_events() == []
        receipt = restarted.process_pending()[0]
        assert receipt["state"] == "exception"
        assert receipt["task_id"] == child
        assert receipt["original_event_at"] == 2600
        assert receipt["inference_calls"] == 0
        assert restarted.process_pending() == []
    assert store.state_snapshot(conn, child) == before
    assert conn.execute("SELECT COUNT(*) FROM kanban_board_ops_requests").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_gateway_timer_delivers_then_idle_has_zero_model_calls(lane, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from gateway import kanban_board_ops as gateway_ops
    from gateway import kanban_watchers_notifier as notifier
    from hermes_cli import kanban_specify
    from gateway.platforms.base import SendResult
    from hermes_constants import get_hermes_home

    root, conn, api, _ = lane
    monkeypatch.setattr(kb.time, "time", lambda: 2000)
    tid = kb.create_task(conn, title="FIXTURE real timer event", tenant="fixture-tenant", assignee="worker")
    add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="fixture-chat", notifier_profile="king")
    admit((root, conn, api, tid))
    assert kb.block_task(conn, tid, reason="FIXTURE timer needs input", kind="needs_input")
    before = store.state_snapshot(conn, tid)
    sent = []
    class Adapter:
        supports_async_delivery = True
        async def send(self, **message):
            assert get_hermes_home() == root / "profiles" / "king"
            sent.append(message)
            return SendResult(success=True)
    monkeypatch.setattr(notifier, "_adapter_for_subscription", lambda *args: Adapter())
    model = Mock(side_effect=AssertionError("FIXTURE idle must not call a model"))
    monkeypatch.setattr(kanban_specify, "_call_aux", model)
    runner = SimpleNamespace(_get_agent=model)
    # Process-service thread hops use launch env, not an inbound turn's context.
    # Bind the fixture executor exactly as an admitted executor gateway launch.
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "ops"))
    await gateway_ops.tick(runner, kb)
    assert len(sent) == 1
    assert store.state_snapshot(conn, tid) == before
    row = conn.execute("SELECT correlation FROM kanban_board_ops_requests").fetchone()
    with profile(root, "ops"):
        receipt = api.receipt(row[0])
        assert receipt["state"] == "notified"
        assert receipt["delivery_result"]["delivered"] is True
        assert receipt["delivery_result"]["deadline_met"] is True
    await gateway_ops.tick(runner, kb)  # Drain the just-committed receipt events.
    count = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    await gateway_ops.tick(runner, kb)
    assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == count
    assert len(sent) == 1
    assert model.call_count == 0


def test_comment_prose_unrelated_tasks_and_disabled_intake_are_inert(lane, monkeypatch):
    root, conn, api, tid = lane
    standing_grant(lane)
    monkeypatch.setattr(kb.time, "time", lambda: 2000)
    kb.add_comment(conn, tid, "worker", "FIXTURE please run keep_spec then delete unrelated task")
    unrelated = kb.create_task(conn, title="FIXTURE unrelated blocked", tenant="fixture-tenant", assignee="worker", initial_status="blocked")
    before = store.state_snapshot(conn, unrelated)
    events = [dict(row) for row in conn.execute("SELECT * FROM task_events WHERE task_id=?", (unrelated,))]
    with profile(root, "ops"):
        assert api.intake_events() == []
        assert api.process_pending() == []
    assert store.state_snapshot(conn, unrelated) == before
    assert [dict(row) for row in conn.execute("SELECT * FROM task_events WHERE task_id=?", (unrelated,))] == events
    with profile(root, "conductor"):
        api.control(enabled=False, human_attribution="FIXTURE", authority_provenance="FIXTURE")
    with profile(root, "ops"):
        assert api.intake_events() == []
    assert conn.execute("SELECT COUNT(*) FROM kanban_board_ops_requests").fetchone()[0] == 0


def test_owner_fresh_grant_already_uses_cli_without_a_chat_turn(lane, tmp_path, monkeypatch, capsys):
    from unittest.mock import Mock
    from hermes_cli import kanban_specify
    from hermes_cli.kanban import kanban_command
    from hermes_cli.kanban_parser import build_parser
    root, conn, api, tid = lane
    contract = admit(lane)
    child = kb.create_task(conn, title="FIXTURE owner explicitly admits later card", tenant="fixture-tenant", assignee="worker", creator_task_id=tid, triage=True)
    contract.update(grant_id="fixture-fresh-owner", task_ids=[child])
    file = tmp_path / "FIXTURE-fresh-owner-grant.json"
    file.write_text(json.dumps(contract), encoding="utf-8")
    model = Mock(side_effect=AssertionError("FIXTURE owner CLI needs no chat/model turn"))
    monkeypatch.setattr(kanban_specify, "_call_aux", model)
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["kanban", "--board", "fixture", "board-ops", "grant", str(file)])
    with profile(root, "conductor"):
        assert kanban_command(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["result"]["task_ids"] == [child]
    assert result["result"]["expires_at"] == 2500
    assert model.call_count == 0


def test_intake_crash_rolls_back_renewal_request_and_cursor(lane, monkeypatch):
    root, conn, api, tid = lane
    standing_grant(lane)
    monkeypatch.setattr(ops, "_now", lambda: 2600)
    monkeypatch.setattr(kb.time, "time", lambda: 2600)
    child = kb.create_task(conn, title="FIXTURE intake crash child", tenant="fixture-tenant", assignee="worker", creator_task_id=tid, initial_status="blocked")
    ledger = [tuple(row) for row in conn.execute("SELECT * FROM kanban_board_ops_grants ORDER BY id")]
    conn.execute("CREATE TRIGGER fixture_intake_crash BEFORE INSERT ON kanban_board_ops_requests BEGIN SELECT RAISE(ABORT,'FIXTURE intake crash'); END")
    with profile(root, "ops"), pytest.raises(sqlite3.IntegrityError, match="FIXTURE intake crash"):
        api.intake_events()
    assert [tuple(row) for row in conn.execute("SELECT * FROM kanban_board_ops_grants ORDER BY id")] == ledger
    assert conn.execute("SELECT COUNT(*) FROM kanban_board_ops_requests").fetchone()[0] == 0
    conn.execute("DROP TRIGGER fixture_intake_crash")
    with profile(root, "ops"), ops.open_board_ops("fixture") as restarted:
        assert len(restarted.intake_events()) == 1
        assert restarted.process_pending()[0]["task_id"] == child
        assert restarted.intake_events() == []


def test_renewal_public_cli_reuses_immutable_grant(lane, tmp_path, capsys):
    from hermes_cli.kanban import kanban_command
    from hermes_cli.kanban_parser import build_parser
    root, conn, api, tid = lane
    contract = standing_grant(lane)
    file = tmp_path / "FIXTURE-renew.json"
    file.write_text(json.dumps({"renew_from": contract["grant_id"], "task_ids": [tid]}), encoding="utf-8")
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["kanban", "--board", "fixture", "board-ops", "grant", str(file)])
    with profile(root, "ops"):
        assert kanban_command(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["result"]["grant_id"] == contract["grant_id"]
    assert result["result"]["expires_at"] == 2500


def test_intake_conflicting_correlation_is_refused_and_audited(lane, monkeypatch):
    root, conn, api, _ = lane
    monkeypatch.setattr(kb.time, "time", lambda: 2000)
    tid = kb.create_task(conn, title="FIXTURE conflicting intake key", tenant="fixture-tenant", assignee="worker")
    add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="fixture-chat", notifier_profile="king")
    bound = (root, conn, api, tid)
    admit(bound)
    assert kb.block_task(conn, tid, reason="FIXTURE real event", kind="needs_input")
    event = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=? AND kind='blocked'", (tid,)).fetchone()[0]
    # A valid manual request can use any correlation. It must not silently
    # masquerade as the different event/operation selected by timer intake.
    submit(bound, request(bound, correlation=f"intake:{event}:inspect_wait"))
    before = store.state_snapshot(conn, tid)
    with profile(root, "ops"):
        assert api.intake_events() == []
    refusals = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM task_events WHERE kind='board_ops_intake_refused'")]
    assert any("correlation conflict" in refusal["reason"] for refusal in refusals)
    assert store.state_snapshot(conn, tid) == before
    assert conn.execute("SELECT COUNT(*) FROM kanban_board_ops_requests").fetchone()[0] == 1
