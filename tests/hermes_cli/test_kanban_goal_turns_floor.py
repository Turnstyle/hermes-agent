"""Goal budgets must persist as positive integers or NULL on every create surface."""

import json
import shlex
from pathlib import Path

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.mark.parametrize("surface", ["direct", "cli"])
@pytest.mark.parametrize("goal_mode", [False, True])
@pytest.mark.parametrize("turns", [0, -1, None, 1, 5, 1.5, 1.0, "invalid", True, False])
def test_kanban_goal_budget_contract(tmp_path, monkeypatch, surface, goal_mode, turns):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    valid = turns is None or type(turns) is int
    with kbc.connect_closing() as conn:
        if surface == "direct":
            if not valid:
                with pytest.raises(ValueError, match="goal_max_turns must be an integer"):
                    kb.create_task(conn, title="budget", goal_mode=goal_mode, goal_max_turns=turns)
            else:
                tid = kb.create_task(conn, title="budget", goal_mode=goal_mode, goal_max_turns=turns)
        else:
            command = "create budget --json"
            if goal_mode:
                command += " --goal"
            if turns is not None:
                command += " --goal-max-turns " + shlex.quote(str(turns))
            result = cli.run_slash(command)
            if not valid:
                assert "invalid int value" in result
            else:
                tid = json.loads(result)["id"]
        if not valid:
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
            return
        row = conn.execute("SELECT goal_max_turns, goal_mode FROM tasks WHERE id = ?", (tid,)).fetchone()
        expected = turns if turns is not None and turns > 0 else None
        assert row["goal_max_turns"] == expected
        assert bool(row["goal_mode"]) is goal_mode
        assert kb.get_task(conn, tid).goal_max_turns == expected
