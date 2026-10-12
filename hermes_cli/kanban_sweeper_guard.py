"""Read-only worker evidence for canonical-node sweeper transitions."""
import time


def check_idle(conn, task_id):
    """Return idle/evidence; call inside a write transaction before mutations.

    Closed runs retain PID evidence, so task status alone is insufficient.
    This inspects this host only; foreign claims cannot prove remote idleness.
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
        pid = row["worker_pid"]
        claim = row["claim_lock"]
        active = source == "task" or row.get("ended_at") is None
        entry = {"source": source, "run_id": run_id, "worker_pid": pid,
                 "worker_started_at": row["worker_started_at"],
                 "claim_lock": claim, "claim_expires": row["claim_expires"]}
        if pid:
            if claim and not str(claim).startswith(prefix):
                entry["reason"] = "foreign_worker_unverified"
                evidence.append(entry)
            elif dispatch._worker_alive(pid, row["worker_started_at"]):
                entry["reason"] = "worker_running"
                evidence.append(entry)
            elif dispatch._pid_alive(pid) and dispatch._process_fingerprint(int(pid)) is None:
                entry["reason"] = "worker_identity_unreadable"
                evidence.append(entry)
        elif active and claim and (row["claim_expires"] is None or row["claim_expires"] >= now):
            entry["reason"] = "active_claim_without_pid"
            evidence.append(entry)
    return {"idle": not evidence, "evidence": evidence}
