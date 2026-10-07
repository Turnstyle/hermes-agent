"""Real board claims for tests that inspect worker argv and environment."""
from unittest.mock import patch

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc


def claim_for_spawn(task, *, board=None):
    with kbc.connect_closing(board=board) as conn:
        if kb.get_task(conn, task.id) is None:
            with patch.object(kb, "_new_task_id", return_value=task.id):
                kb.create_task(conn, title=task.title, assignee=task.assignee,
                               workspace_kind="scratch", board=board)
        if task.current_run_id is not None:
            conn.execute("DELETE FROM sqlite_sequence WHERE name='task_runs'")
            conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('task_runs', ?)",
                         (task.current_run_id - 1,))
        claimed = kb.claim_task(conn, task.id)
        assert claimed is not None
        task.status = claimed.status
        task.claim_lock = claimed.claim_lock
        task.current_run_id = claimed.current_run_id
        return task
