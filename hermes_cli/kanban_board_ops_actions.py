"""Admitted database-only actions and conservative lifecycle exception evidence."""

from __future__ import annotations

import hashlib
from pathlib import Path

from hermes_cli import kanban_db as kb
# Import the original carry. Do not substitute a second keep-spec implementation.
from hermes_cli.kanban_db import keep_spec_triage_task as specify_triage_task_keep_spec
from hermes_cli.kanban_board_ops_policy import Refused, OPERATIONS


def validate_payload(operation: str, payload: dict) -> None:
    keys = {
        "keep_spec": {"review_author"}, "record_input": {"text"},
        "inspect_wait": set(), "interrupted_cli": {"candidate_path", "candidate_sha256"},
        "escalate": {"reason", "jev_status"},
    }
    if operation not in OPERATIONS or not isinstance(payload, dict) or set(payload) != keys[operation]:
        raise Refused("unsupported operation or payload fields")
    if any(not isinstance(v, str) or not v.strip() for v in payload.values()):
        raise Refused("payload values must be nonblank strings")
    if sum(len(v) for v in payload.values()) > 8192:
        raise Refused("payload exceeds 8192 characters")
    if operation == "escalate" and payload["jev_status"] not in {"not_requested", "not_configured", "unavailable", "timed_out"}:
        raise Refused("Jev status cannot supply execution authorization")


def task_release_checks(conn, task: dict, node_id: str, now: int) -> None:
    if task["current_run_id"] is not None or any(task.get(k) is not None for k in ("claim_lock", "claim_expires", "worker_pid")):
        raise Refused("current run or claim bookkeeping requires owner recovery")
    if task["consecutive_failures"] or task["block_recurrences"]:
        raise Refused("failure or block-loop counter requires explicit owner action")
    assert_local_ownership(conn, task, node_id, now)


def assert_local_ownership(conn, task: dict, node_id: str, now: int) -> None:
    """Shared node/lease fence for every admitted operation, including comments."""
    installed = kb._fleet_adapter_installed_node_id(conn)
    if installed is not None and installed != node_id:
        raise Refused("installed Fleet node differs from admitted node")
    if kb._is_foreign_fleet_mirror(conn, task["id"], installed):
        raise Refused("foreign Fleet ownership fence")
    # Existing Fleet triggers remain the final write fence. Unrecognized or
    # partially installed adapter schemas cannot be treated as local authority.
    mapping_table = conn.execute("SELECT 1 FROM sqlite_master WHERE name='fleet_kanban_issue_map'").fetchone()
    if mapping_table:
        mapping = conn.execute("SELECT * FROM fleet_kanban_issue_map WHERE local_task_id=?", (task["id"],)).fetchone()
        if mapping:
            if installed is None:
                raise Refused("Fleet mapping has no installed node identity")
            tables = conn.execute("SELECT 1 FROM sqlite_master WHERE name='fleet_kanban_verified_leases'").fetchone()
            if tables is None or "issue_id" not in mapping.keys():
                raise Refused("Fleet lease proof unavailable")
            lease = conn.execute("SELECT * FROM fleet_kanban_verified_leases WHERE issue_id=?", (mapping["issue_id"],)).fetchone()
            if lease is None or type(lease["expires_at"]) not in (int, float) or lease["expires_at"] <= now:
                raise Refused("Fleet verified lease missing or expired")


def apply_action(conn, request: dict, grant: dict, task: dict, now: int) -> tuple[str, dict]:
    operation, payload = request["operation"], request["payload"]
    if operation == "keep_spec":
        task_release_checks(conn, task, request["node_id"], now)
        if payload["review_author"] not in grant["review_authors"]:
            raise Refused("review author is outside owner-admitted reviewers")
        ok, reason, digest, status, committed, deferred = specify_triage_task_keep_spec(
            conn, task["id"], expected_sha256=request["task_sha256"], author=payload["review_author"],
        )
        if not ok or not committed:
            raise Refused(reason)
        if deferred:
            # A broad or failed promotion must not partially commit the action.
            raise Refused("keep-spec readiness failed: " + deferred)
        return "applied", {"action": operation, "status_after": status, "sha256": digest, "author": payload["review_author"]}
    if operation == "record_input":
        comment_id = kb.add_comment(conn, task["id"], grant["operator_profile"], payload["text"])
        return "applied", {"action": operation, "comment_id": comment_id, "holds_released": False}
    if operation == "inspect_wait":
        if task["status"] == "running" and task["current_run_id"] is not None:
            from hermes_cli.kanban_db_dispatch import _worker_alive
            row = conn.execute("SELECT worker_pid,worker_started_at FROM task_runs WHERE id=?", (task["current_run_id"],)).fetchone()
            if row and row["worker_pid"] and row["worker_started_at"] is not None and _worker_alive(row["worker_pid"], row["worker_started_at"]):
                return "observed", {"action": operation, "healthy_current_run": task["current_run_id"], "redispatched": False}
        return "exception", {"reason": "no verified healthy current run; stale wait needs owner decision", "redispatched": False}
    if operation == "interrupted_cli":
        return "exception", interrupted_evidence(task, payload)
    return "exception", {"reason": payload["reason"], "jev_status": payload["jev_status"], "execution_authorized": False, "inference_calls": 0}


def interrupted_evidence(task: dict, payload: dict) -> dict:
    """Read a bounded saved candidate, never kill/resume/clean a session.

    Even dead-PID proof plus a saved candidate cannot prove a successful CLI
    exit, or authorize a new maker. Recovery remains with the owning Conductor.
    """
    result = {"reason": "interrupted CLI requires owning Conductor recovery", "candidate_verified": False,
              "session_dead_verified": False, "redispatched": False, "files_modified": False}
    try:
        workspace = Path(task["workspace_path"]).resolve(strict=True)
        candidate = Path(payload["candidate_path"]).resolve(strict=True)
        candidate.relative_to(workspace)
        if candidate.is_file() and candidate.stat().st_size <= 1024 * 1024:
            result["candidate_verified"] = hashlib.sha256(candidate.read_bytes()).hexdigest() == payload["candidate_sha256"]
    except (OSError, ValueError, TypeError):
        pass
    if task.get("worker_pid") and task.get("worker_started_at") is not None:
        import psutil
        try:
            psutil.Process(task["worker_pid"])
        except psutil.NoSuchProcess:
            result["session_dead_verified"] = True
        except psutil.AccessDenied:
            pass
    return result
