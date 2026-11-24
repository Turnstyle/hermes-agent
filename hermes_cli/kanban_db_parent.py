"""Commit-time native parent assignment validation shared by board mutations."""
import json
import os
from pathlib import Path


def parent_assignment_is_live(conn, caller_assignment: dict) -> bool:
    """Call only under the mutation's IMMEDIATE transaction."""
    import time
    now = int(time.time())
    caller = caller_assignment
    # Read the caller and target under this mutation's IMMEDIATE transaction.
    # No preflight proof or separate read connection can authorize the commit.
    actual = next((r[2] for r in conn.execute('PRAGMA database_list') if r[1] == 'main'), '')
    if not actual or Path(actual).resolve() != Path(caller['db']).resolve():
        return False
    row = conn.execute(
        "SELECT 1 FROM tasks t JOIN task_runs r ON r.id=t.current_run_id "
        "WHERE t.id=? AND t.status='running' AND t.current_run_id=? "
        "AND t.claim_lock=? AND t.claim_expires>=? AND t.tenant IS ? "
        "AND t.assignee IS ? AND r.task_id=t.id AND r.status='running' "
        "AND r.ended_at IS NULL AND r.claim_lock=t.claim_lock AND r.claim_expires>=?",
        (caller['task_id'], caller['run_id'], caller['claim_lock'], now,
         caller.get('tenant'), caller['profile'], now),
    ).fetchone()
    bound = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
        "AND kind='worker_bound' ORDER BY id DESC LIMIT 1",
        (caller['task_id'], caller['run_id']),
    ).fetchone()
    identity = json.loads(bound[0]) if bound else {}
    if (not row or identity.get('pid') != os.getpid()
            or identity.get('claim_lock') != caller['claim_lock']):
        return False
    return True
