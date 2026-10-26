"""K02: a spent goal-mode turn budget hands the run back neutrally, through the owner-managed run.

Closing proof: the hand-back happens inside the owner's managed worker (``run_worker_turns``)
while the owner's admission for the run is still active. The run closes as ``budget_handback``
through ``kanban_db._end_run``, the card returns to its source phase with no failure charged and
no block, the owner's admission receipt is left for the owner to settle, its ``worker_result``
still lands on the closed run, and the reclaim fences agree that nothing is left to recover.
"""
import json
import os
import sqlite3

import psutil
import pytest

from agent import kanban_turn_recovery as rec
from gateway import session_kanban
from hermes_cli import goals
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_budget_handback import handback_budget
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.kanban_owner_recovery import owner_reclaim_blocked, owner_reclaim_paused
from hermes_cli.process_identity import process_birth


class _Agent:
    session_id = "s1"

    def __init__(self):
        self.calls = 0
        self._session_db = self

    def get_messages_as_conversation(self, _sid):
        return []

    def run_conversation(self, message, conversation_history=None):
        self.calls += 1
        return {"completed": True, "final_response": f"slice {self.calls} progress"}


def _frame(tmp_path, monkeypatch, *, bound_pid=None, admission="running"):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(rec, "RECOVERY_DELAYS_SECONDS", (0.0,))
    monkeypatch.setattr("agent.skill_commands.build_preloaded_skills_prompt", lambda *a, **k: ("", [], []))
    kb.init_db()
    path = kb.kanban_db_path()
    ledger = tmp_path / "state.db"
    with connect_closing(path) as conn:
        task_id = kb.create_task(conn, title="long goal", assignee="default")
        task = kb.claim_task(conn, task_id)
        request_id = "kanban:" + json.dumps([kb.get_current_board(), task_id, task.current_run_id],
                                            separators=(",", ":"))
        with sqlite3.connect(ledger) as store:
            store.execute("CREATE TABLE session_admissions "
                          "(target_session_id TEXT, request_id TEXT, status TEXT, outcome TEXT)")
            store.execute("INSERT INTO session_admissions VALUES ('s1', ?, ?, NULL)", (request_id, admission))
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "owner_admitted", {
                "db": str(ledger), "session_id": "s1", "request_id": request_id,
                "pid": os.getpid(), "birth": process_birth(psutil.Process())}, run_id=task.current_run_id)
            kb._append_event(conn, task_id, "worker_bound", {
                "pid": bound_pid or os.getpid(), "claim_lock": task.claim_lock}, run_id=task.current_run_id)
    context = {"db": str(path), "task_id": task_id, "run_id": task.current_run_id, "claim_lock": task.claim_lock,
               "skills": [], "context": "", "goal_mode": True, "goal_text": "g", "goal_max_turns": 2}
    return path, ledger, context, {"text": "work", "policy": {"kanban_json": json.dumps(context)}}


@pytest.fixture
def judge_continues(monkeypatch):
    monkeypatch.setattr(goals, "judge_goal", lambda *_a, **_k: ("continue", "more to do", False, False, False))


def _events(conn, task_id):
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT kind, run_id FROM task_events WHERE task_id = ? ORDER BY id", (task_id,))]


def test_spent_budget_hands_back_while_owner_admission_is_active(tmp_path, monkeypatch, judge_continues):
    path, ledger, context, frame = _frame(tmp_path, monkeypatch)
    run_id = context["run_id"]
    with connect_closing(path) as conn:
        assert not owner_reclaim_paused(conn, context["task_id"])  # live owner, admission running

    session_kanban.run_worker_turns(_Agent(), frame, [])

    with connect_closing(path) as conn:
        task = kb.get_task(conn, context["task_id"])
        assert (task.status, task.claim_lock, task.current_run_id) == ("ready", None, None)
        assert task.consecutive_failures == 0
        run = conn.execute("SELECT status, outcome, ended_at, summary FROM task_runs WHERE id = ?",
                           (run_id,)).fetchone()
        assert (run["status"], run["outcome"]) == ("budget_handback", "budget_handback")
        assert run["ended_at"] is not None and "slice 2 progress" in run["summary"]
        events = _events(conn, context["task_id"])
        assert ("budget_handback", run_id) in events
        assert not any(kind in ("blocked", "gave_up") for kind, _ in events)
        # Owner receipts stay consistent: the admission marker and the worker's result both name
        # the closed run; the admission row is the owner's to settle and was not touched.
        assert ("owner_admitted", run_id) in events and ("worker_result", run_id) in events
        assert session_kanban.worker_exit_code(path, context) == 0
        # The reclaim fences agree there is nothing left to recover.
        assert not owner_reclaim_paused(conn, context["task_id"])
        assert not owner_reclaim_blocked(conn, context["task_id"], crash_sweep=True)
        assert kbd.detect_crashed_workers(conn) == []
        # The next attempt is a new run; the handed-back run's claim can never be admitted again.
        again = kb.claim_task(conn, context["task_id"])
        assert again.current_run_id != run_id
    with sqlite3.connect(ledger) as store:
        assert store.execute("SELECT status FROM session_admissions").fetchone()[0] == "running"


def test_unbound_interpreter_cannot_hand_back_and_the_block_stands(tmp_path, monkeypatch, judge_continues):
    path, _ledger, context, frame = _frame(tmp_path, monkeypatch, bound_pid=os.getpid() + 1)

    session_kanban.run_worker_turns(_Agent(), frame, [])

    with connect_closing(path) as conn:
        assert kb.get_task(conn, context["task_id"]).status == "blocked"
        assert not any(kind == "budget_handback" for kind, _ in _events(conn, context["task_id"]))


def test_judged_unachievable_still_blocks(tmp_path, monkeypatch):
    path, _ledger, context, frame = _frame(tmp_path, monkeypatch)
    monkeypatch.setattr(goals, "judge_goal", lambda *_a, **_k: ("blocked", "impossible", False, False, False))

    session_kanban.run_worker_turns(_Agent(), frame, [])

    with connect_closing(path) as conn:
        assert kb.get_task(conn, context["task_id"]).status == "blocked"


def test_stale_run_or_claim_hand_back_touches_nothing(tmp_path, monkeypatch):
    path, _ledger, context, _frame_ = _frame(tmp_path, monkeypatch)
    with connect_closing(path) as conn:
        before = conn.total_changes
        assert not handback_budget(conn, context["task_id"], expected_run_id=context["run_id"] + 1,
                                   claim_lock=context["claim_lock"], reason="spent")
        assert not handback_budget(conn, context["task_id"], expected_run_id=context["run_id"],
                                   claim_lock="someone-else", reason="spent")
        assert not handback_budget(conn, context["task_id"], expected_run_id=context["run_id"],
                                   claim_lock=context["claim_lock"], reason="spent", owner_check=lambda _c: False)
        assert conn.total_changes == before
        assert kb.get_task(conn, context["task_id"]).status == "running"
