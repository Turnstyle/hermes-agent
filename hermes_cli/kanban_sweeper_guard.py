"""Read-only worker evidence for canonical-node sweeper transitions.

Consumed by the canonical-node Fleet sweeper (out of tree) before it moves a card. An
owner-managed attempt has three handles: the dispatcher's spawned submitter
(``tasks.worker_pid`` / ``task_runs.worker_pid``, a waiter that may already be gone), the
owner's admission receipt (``owner_admitted``, settled in the owner's ``session_admissions``)
and the interpreter the owner bound to the run (``worker_bound``). A dead or absent waiter
is not proof that nothing runs: the receipt and the bound interpreter must also say so.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time


def check_idle(conn, task_id):
    """Return ``{"idle": bool, "evidence": [...]}``; call inside a write transaction before mutations.

    Closed runs retain PID evidence, so task status alone is insufficient. This inspects this
    host only; foreign claims cannot prove remote idleness. Missing or unreadable evidence is
    never proof of idleness.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as dispatch

    task = conn.execute(
        "SELECT worker_pid, worker_started_at, claim_lock, claim_expires, "
        "current_run_id, status FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if task is None:
        return {"idle": False, "evidence": [{"source": "task", "reason": "missing_task"}]}
    rows = [("task", None, dict(task))]
    runs = conn.execute(
        "SELECT id, worker_pid, worker_started_at, claim_lock, claim_expires, "
        "status, ended_at FROM task_runs WHERE task_id = ?", (task_id,),
    ).fetchall()
    rows.extend(("run", r["id"], dict(r)) for r in runs)
    evidence = []
    now = int(time.time())
    prefix = kb._host_prefix()
    for source, run_id, row in rows:
        entry = {"source": source, "run_id": run_id, "worker_pid": row["worker_pid"],
                 "worker_started_at": row["worker_started_at"],
                 "claim_lock": row["claim_lock"], "claim_expires": row["claim_expires"]}
        reason = _process_reason(dispatch, prefix, row["worker_pid"], row["worker_started_at"], row["claim_lock"])
        active = source == "task" or row.get("ended_at") is None
        if reason is None and not row["worker_pid"] and active and row["claim_lock"] and (
                row["claim_expires"] is None or row["claim_expires"] >= now):
            reason = "active_claim_without_pid"
        if reason:
            evidence.append(dict(entry, reason=reason))
    current_run = task["current_run_id"]
    for run in runs:
        evidence.extend(_owner_evidence(conn, dispatch, prefix, task_id, run["id"],
                                        current=run["id"] == current_run and run["ended_at"] is None))
    return {"idle": not evidence, "evidence": evidence}


def _process_reason(dispatch, prefix, pid, started_at, claim):
    """Why one recorded process keeps the task busy, or ``None`` when it is provably gone."""
    if not pid:
        return None
    if claim and not str(claim).startswith(prefix):
        return "foreign_worker_unverified"
    if dispatch._worker_alive(pid, started_at):
        return "worker_running"
    if dispatch._pid_alive(pid) and dispatch._process_fingerprint(int(pid)) is None:
        return "worker_identity_unreadable"
    return None


def _run_event(conn, task_id, run_id, kind):
    """The newest ``kind`` payload of one run; ``None`` absent, ``{}`` unreadable."""
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind=? "
        "ORDER BY id DESC LIMIT 1", (task_id, run_id, kind)).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _receipt_status(binding):
    """The owner's ``session_admissions`` status for an ``owner_admitted`` binding, read-only.

    ``"missing"`` when the owner store has no such admission and ``"unreadable"`` when the
    binding or the store cannot be read: neither says the admission is settled.
    """
    try:
        uri = Path(binding["db"]).as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as owner:
            row = owner.execute(
                "SELECT status FROM session_admissions WHERE target_session_id=? AND request_id=?",
                (binding["session_id"], binding["request_id"])).fetchone()
    except (KeyError, TypeError, ValueError, OSError, sqlite3.Error):
        return "unreadable"
    return "missing" if row is None else str(row[0])


def _owner_evidence(conn, dispatch, prefix, task_id, run_id, *, current):
    """Owner-runtime evidence for one run: its bound interpreter and its admission receipt."""
    from hermes_cli import kanban_owner_recovery as owner_recovery

    evidence = []
    bound = _run_event(conn, task_id, run_id, "worker_bound")
    if bound is not None:
        if not bound.get("pid"):
            reason = "bound_interpreter_unreadable"  # a binding without a pid proves nothing
        else:
            inner = _process_reason(dispatch, prefix, bound["pid"], bound.get("started_at"),
                                    bound.get("claim_lock"))
            reason = inner and f"bound_{inner}"
        if reason:
            evidence.append({"source": "worker_bound", "run_id": run_id, "worker_pid": bound.get("pid"),
                             "claim_lock": bound.get("claim_lock"), "reason": reason})
    binding = _run_event(conn, task_id, run_id, "owner_admitted")
    if binding is None:
        return evidence
    status = _receipt_status(binding)
    if status == "terminal":
        return evidence
    # An unsettled receipt can still start or resume the attempt. It stops mattering only when
    # nothing can still execute it: for the open run, the owner-recovery proof; for a closed run,
    # the admitting owner is gone (its managed interpreters die with it) and no worker lives.
    if current:
        settled = owner_recovery.owner_attempt_provably_over(conn, task_id)
    else:
        settled = owner_recovery._owner_process_gone(binding) and not evidence
    if not settled:
        evidence.append({"source": "owner_admitted", "run_id": run_id, "session_id": binding.get("session_id"),
                         "receipt_status": status, "reason": "owner_receipt_unresolved"})
    return evidence
