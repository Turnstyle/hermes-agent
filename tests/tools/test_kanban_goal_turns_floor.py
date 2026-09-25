"""Real tool dispatch must not coerce invalid goal budgets into persisted cards."""

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tools import kanban_tools  # noqa: F401 -- registers the real tool handler
from tools.registry import registry


@pytest.mark.parametrize("goal_mode", [None, False, True])
@pytest.mark.parametrize("turns", [0, -1, None, 1, 5, 1.5, 1.0, "5", "invalid", True, False, [], {}])
def test_kanban_create_goal_budget_contract(tmp_path, monkeypatch, goal_mode, turns):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    args = {"title": "budget", "assignee": "worker", "goal_max_turns": turns}
    if goal_mode is not None:
        args["goal_mode"] = goal_mode
    result = json.loads(registry.dispatch("kanban_create", args))
    with kbc.connect_closing() as conn:
        if turns is not None and type(turns) is not int:
            assert "goal_max_turns must be an integer" in result.get("error", "")
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
            return
        assert result.get("ok"), result
        row = conn.execute(
            "SELECT goal_max_turns, goal_mode FROM tasks WHERE id = ?", (result["task_id"],)
        ).fetchone()
        expected = turns if turns is not None and turns > 0 else None
        assert row["goal_max_turns"] == expected
        assert bool(row["goal_mode"]) is bool(goal_mode)
        budget_schema = registry.get_schema("kanban_create")["parameters"]["properties"]["goal_max_turns"]
        assert budget_schema["minimum"] > 0
        assert expected is None or expected >= budget_schema["minimum"]
