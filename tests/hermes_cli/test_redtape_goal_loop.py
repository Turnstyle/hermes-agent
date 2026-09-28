"""Redtape goal-loop ranks 6–11: template, run_turn retry, block callbacks."""

from __future__ import annotations

import logging

import pytest

from hermes_cli import goals


def _patch_judge(monkeypatch, verdicts):
    seq = list(verdicts)

    def _fake_judge(goal, response, subgoals=None, background_processes=None, **_kw):
        v = seq.pop(0) if seq else "done"
        return v, f"scripted:{v}", False, None, False

    monkeypatch.setattr(goals, "judge_goal", _fake_judge)


# --- Rank 6: continuation template ---


def test_continuation_template_denies_time_gate_and_tool_timeout():
    text = goals.KANBAN_GOAL_CONTINUATION_TEMPLATE
    lower = text.lower()
    assert "future time gate" in lower
    assert "tool timeout" in lower
    assert "is not kanban_block" in lower
    assert "real external hold" in lower
    assert "kanban_block" in text


# --- Rank 7: run_turn retry ---


def test_run_turn_retries_once_then_completes(monkeypatch):
    _patch_judge(monkeypatch, ["continue", "continue"])
    calls: list[str] = []
    statuses = iter(["running", "done"])

    def run_turn(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            raise RuntimeError("short")
        return "ok"

    blocks: list[str] = []

    res = goals.run_kanban_goal_loop(
        task_id="t1",
        goal_text="goal",
        run_turn=run_turn,
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: blocks.append(r) or pytest.fail("must not block"),
        first_response="first",
    )
    assert res["outcome"] == "completed_by_worker"
    assert len(calls) == 2
    assert blocks == []


def test_run_turn_double_failure_stops_without_block(monkeypatch, caplog):
    _patch_judge(monkeypatch, ["continue"])
    n = {"count": 0}

    def run_turn(_prompt):
        n["count"] += 1
        raise RuntimeError("short")

    blocks: list[str] = []
    with caplog.at_level(logging.WARNING):
        res = goals.run_kanban_goal_loop(
            task_id="t1",
            goal_text="goal",
            run_turn=run_turn,
            task_status_fn=lambda: "running",
            block_fn=lambda r: blocks.append(r),
            first_response="first",
        )
    assert res["outcome"] == "stopped"
    assert "run_turn" in res["reason"].lower()
    assert n["count"] == 2
    assert blocks == []
    assert any("run_turn" in rec.message.lower() for rec in caplog.records)


# --- Rank 10: unchanged block ---


def test_blocked_unchanged_callback_stops_without_block_fn(monkeypatch):
    _patch_judge(monkeypatch, ["continue"])
    blocks: list[str] = []

    res = goals.run_kanban_goal_loop(
        task_id="t1",
        goal_text="goal",
        run_turn=lambda p: pytest.fail("no turn"),
        task_status_fn=lambda: "blocked",
        block_fn=lambda r: blocks.append(r),
        block_reason_unchanged_fn=lambda: True,
        first_response="x",
    )
    assert res["outcome"] == "unchanged_block"
    assert blocks == []


def test_blocked_unchanged_false_uses_worker_stop(monkeypatch):
    _patch_judge(monkeypatch, ["continue"])
    blocks: list[str] = []

    res = goals.run_kanban_goal_loop(
        task_id="t1",
        goal_text="goal",
        run_turn=lambda p: pytest.fail("no turn"),
        task_status_fn=lambda: "blocked",
        block_fn=lambda r: blocks.append(r),
        block_reason_unchanged_fn=lambda: False,
        first_response="x",
    )
    assert res["outcome"] == "blocked_by_worker"
    assert blocks == []


def test_blocked_without_callbacks_still_blocked_by_worker(monkeypatch):
    _patch_judge(monkeypatch, ["continue"])

    res = goals.run_kanban_goal_loop(
        task_id="t1",
        goal_text="goal",
        run_turn=lambda p: pytest.fail("no turn"),
        task_status_fn=lambda: "blocked",
        block_fn=lambda r: pytest.fail("block_fn"),
        first_response="x",
    )
    assert res["outcome"] == "blocked_by_worker"


# --- Rank 11: bookkeeping vs terminal blocked ---


def test_blocked_bookkeeping_continues_loop(monkeypatch):
    _patch_judge(monkeypatch, ["continue"])
    turns: list[str] = []
    statuses = iter(["blocked", "done"])

    res = goals.run_kanban_goal_loop(
        task_id="t1",
        goal_text="goal",
        run_turn=lambda p: turns.append(p) or "step",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("must not block"),
        block_is_bookkeeping_fn=lambda: True,
        first_response="first",
    )
    assert res["outcome"] == "completed_by_worker"
    assert len(turns) == 1


def test_blocked_bookkeeping_before_unchanged(monkeypatch):
    """Bookkeeping True must continue even if unchanged would stop."""
    _patch_judge(monkeypatch, ["continue", "continue"])
    statuses = iter(["blocked", "done"])

    res = goals.run_kanban_goal_loop(
        task_id="t1",
        goal_text="goal",
        run_turn=lambda p: "ok",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("must not block"),
        block_is_bookkeeping_fn=lambda: True,
        block_reason_unchanged_fn=lambda: True,
        first_response="first",
    )
    assert res["outcome"] == "completed_by_worker"


def test_blocked_explicit_kanban_block_still_worker_stop(monkeypatch):
    _patch_judge(monkeypatch, ["continue"])

    res = goals.run_kanban_goal_loop(
        task_id="t1",
        goal_text="goal",
        run_turn=lambda p: pytest.fail("no turn"),
        task_status_fn=lambda: "blocked",
        block_fn=lambda r: None,
        block_is_bookkeeping_fn=lambda: False,
        block_reason_unchanged_fn=lambda: False,
        first_response="x",
    )
    assert res["outcome"] == "blocked_by_worker"
