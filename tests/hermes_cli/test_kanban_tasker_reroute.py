"""Tests for decompose-only profile routing and tasker re-routing logic.

Verifies:
1. Profile role and description recognition for decompose-only profiles.
2. Node builder resolution on Sheldon and other nodes.
3. Execution vs decomposition card classification.
4. Auto-decomposer roster exclusion and child assignee re-routing.
5. Single-task fanout=false promotion re-routing to node builder with audit comment.
6. Dispatcher-level re-routing of execution cards from decompose-only profiles to builder.
7. Dispatcher retention of decomposition/planning cards on decompose-only profiles.
8. Scoped worker CLI fence allowing own-card reassignments during handbacks while blocking foreign card mutations.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch_mod
from hermes_cli import kanban_decompose as decomp
from hermes_cli import profiles as profiles_mod


ROOT = Path(__file__).parents[2]


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_is_decompose_only_profile_detection():
    # By role
    p_role = profiles_mod.ProfileInfo(
        name="tasker", path=Path("/fake/tasker"), is_default=False, gateway_running=False, role="decompose_only"
    )
    assert profiles_mod.is_decompose_only_profile(p_role) is True

    p_role2 = profiles_mod.ProfileInfo(
        name="decomposer-bot", path=Path("/fake/dec"), is_default=False, gateway_running=False, role="decomposer"
    )
    assert profiles_mod.is_decompose_only_profile(p_role2) is True

    # By description
    p_desc = profiles_mod.ProfileInfo(
        name="shld-tasker",
        path=Path("/fake/shld-tasker"),
        is_default=False,
        gateway_running=False,
        description="Sheldon's task decomposition and specification bot (decomposition-only)",
    )
    assert profiles_mod.is_decompose_only_profile(p_desc) is True

    p_desc2 = profiles_mod.ProfileInfo(
        name="planner",
        path=Path("/fake/planner"),
        is_default=False,
        gateway_running=False,
        description="Decomposes sprint goals into atomic tasks; planning only, never child execution.",
    )
    assert profiles_mod.is_decompose_only_profile(p_desc2) is True

    # Regular builder profile
    p_builder = profiles_mod.ProfileInfo(
        name="shld-bot-maker",
        path=Path("/fake/shld-bot-maker"),
        is_default=False,
        gateway_running=False,
        description="Sheldon's profile builder and automation maker",
    )
    assert profiles_mod.is_decompose_only_profile(p_builder) is False

    # Empty / none
    assert profiles_mod.is_decompose_only_profile(None) is False
    assert profiles_mod.is_decompose_only_profile("") is False


def test_resolve_node_builder_sheldon():
    # Sheldon tenant
    with patch("hermes_cli.profiles.profile_exists", side_effect=lambda name: name == "shld-bot-maker"):
        builder = profiles_mod.resolve_node_builder("sheldon")
        assert builder == "shld-bot-maker"

    # Sheldon tenant with shld-king when bot-maker is missing
    with patch("hermes_cli.profiles.profile_exists", side_effect=lambda name: name == "shld-king"):
        builder = profiles_mod.resolve_node_builder("shld")
        assert builder == "shld-king"


def test_resolve_node_builder_generic():
    profiles = [
        profiles_mod.ProfileInfo(name="node1-tasker", path=Path("/p1"), is_default=False, gateway_running=False, role="decompose_only"),
        profiles_mod.ProfileInfo(name="node1-builder", path=Path("/p2"), is_default=False, gateway_running=False, description="Node 1 builder"),
    ]
    with patch("hermes_cli.profiles.list_profiles", return_value=profiles):
        with patch("hermes_cli.profiles.profile_exists", return_value=True):
            builder = profiles_mod.resolve_node_builder("node1")
            assert builder == "node1-builder"


def test_is_execution_card_for_decomposer():
    # Build / execution cards -> True
    assert dispatch_mod.is_execution_card_for_decomposer(
        "Build Sheldon custom system-prompt override profile shld-cndr-prompt-base"
    ) is True
    assert dispatch_mod.is_execution_card_for_decomposer(
        "Build test fixture helper profile shld-fixture-test"
    ) is True
    assert dispatch_mod.is_execution_card_for_decomposer("Implement OAuth2 login") is True
    assert dispatch_mod.is_execution_card_for_decomposer("Fix memory leak in websocket listener") is True
    assert dispatch_mod.is_execution_card_for_decomposer("Setup database migrations") is True
    assert dispatch_mod.is_execution_card_for_decomposer("Refactor auth module") is True

    # Decomposition / planning cards -> False
    assert dispatch_mod.is_execution_card_for_decomposer(
        "Decompose sprint backlog into atomic task cards"
    ) is False
    assert dispatch_mod.is_execution_card_for_decomposer("Break down epic into user stories") is False
    assert dispatch_mod.is_execution_card_for_decomposer("Triage incoming issue backlog") is False
    assert dispatch_mod.is_execution_card_for_decomposer("Specify acceptance criteria for billing API") is False
    assert dispatch_mod.is_execution_card_for_decomposer("Decompose: build user login") is False

    # Execution card mentioning decomposer -> True
    assert dispatch_mod.is_execution_card_for_decomposer("Build decomposer tool for kanban") is True


def test_decompose_roster_excludes_decompose_only():
    profiles = [
        profiles_mod.ProfileInfo(name="shld-tasker", path=Path("/p1"), is_default=False, gateway_running=False, role="decompose_only"),
        profiles_mod.ProfileInfo(name="shld-bot-maker", path=Path("/p2"), is_default=False, gateway_running=False, description="builder"),
        profiles_mod.ProfileInfo(name="default", path=Path("/p3"), is_default=True, gateway_running=False, description="default worker"),
    ]
    with patch("hermes_cli.profiles.list_profiles", return_value=profiles):
        roster, valid = decomp._build_roster()
        # shld-tasker should NOT be in roster
        assert "shld-tasker" not in valid
        assert "shld-bot-maker" in valid
        assert "default" in valid


def test_decompose_normalize_assignee_choice_reroutes_decompose_only():
    with patch("hermes_cli.profiles.is_decompose_only_profile", side_effect=lambda p: p == "shld-tasker"):
        with patch("hermes_cli.profiles.resolve_node_builder", return_value="shld-bot-maker"):
            chosen = decomp._normalize_assignee_choice(
                "shld-tasker",
                default_assignee="default",
                valid_names={"default", "shld-bot-maker"},
                tenant="sheldon",
            )
            assert chosen == "shld-bot-maker"


def test_apply_single_reroutes_execution_card_from_decompose_only(kanban_home):
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="Build custom profile",
            assignee="shld-tasker",
            tenant="sheldon",
            triage=True,
        )
        task = kb.get_task(conn, task_id)

    parsed = {
        "fanout": False,
        "title": "Build Sheldon custom system-prompt override profile shld-cndr-prompt-base",
        "body": "Create profile configuration and test it.",
    }
    routing = decomp._Routing(
        orchestrator="default",
        default_assignee="shld-bot-maker",
        auto_promote=True,
        roster=[],
        valid_names={"shld-bot-maker"},
        tenant="sheldon",
    )

    with patch("hermes_cli.profiles.is_decompose_only_profile", side_effect=lambda p: p == "shld-tasker"):
        with patch("hermes_cli.profiles.resolve_node_builder", return_value="shld-bot-maker"):
            outcome = decomp._apply_single(task, parsed, routing, author="test-decomposer")
            assert outcome.ok is True

    with kbc.connect_closing() as conn:
        updated = kb.get_task(conn, task_id)
        assert updated.status == "ready"
        assert updated.assignee == "shld-bot-maker"
        comments = kb.list_comments(conn, task_id)
        assert any("[auto-reroute]" in c.body for c in comments)


def test_dispatcher_reroutes_execution_card_on_decompose_only(kanban_home):
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="Build Sheldon custom system-prompt override profile shld-cndr-prompt-base",
            assignee="shld-tasker",
            tenant="sheldon",
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))

    spawned_tasks = []

    def mock_spawn(task, ws):
        spawned_tasks.append(task.id)
        return 12345

    with patch("hermes_cli.profiles.is_decompose_only_profile", side_effect=lambda p: p == "shld-tasker"):
        with patch("hermes_cli.profiles.resolve_node_builder", return_value="shld-bot-maker"):
            with patch("hermes_cli.kanban_db_dispatch._profile_exists_fn", return_value=lambda p: True):
                with kbc.connect_closing() as conn:
                    res = dispatch_mod._dispatch_once_locked(
                        conn,
                        spawn_fn=mock_spawn,
                        default_assignee="shld-bot-maker",
                    )

    # Must NOT spawn shld-tasker
    assert task_id not in spawned_tasks
    # Must record auto_rerouted_builder
    assert (task_id, "shld-tasker", "shld-bot-maker") in res.auto_rerouted_builder

    # Verify DB state: assignee is shld-bot-maker, audit comment added
    with kbc.connect_closing() as conn:
        updated = kb.get_task(conn, task_id)
        assert updated.assignee == "shld-bot-maker"
        assert updated.status == "ready"
        comments = kb.list_comments(conn, task_id)
        assert any("[auto-reroute]" in c.body and "shld-bot-maker" in c.body for c in comments)


def test_dispatcher_retains_decomposition_card_on_decompose_only(kanban_home):
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="Decompose sprint backlog into atomic task cards",
            assignee="shld-tasker",
            tenant="sheldon",
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))

    spawned_tasks = []

    def mock_spawn(task, ws):
        spawned_tasks.append(task.id)
        return 12345

    with patch("hermes_cli.profiles.is_decompose_only_profile", side_effect=lambda p: p == "shld-tasker"):
        with patch("hermes_cli.kanban_db_dispatch._profile_exists_fn", return_value=lambda p: True):
            with kbc.connect_closing() as conn:
                res = dispatch_mod._dispatch_once_locked(
                    conn,
                    spawn_fn=mock_spawn,
                    default_assignee="shld-bot-maker",
                )

    # Decomposition task should proceed to spawn on shld-tasker
    assert task_id in spawned_tasks
    assert len(res.auto_rerouted_builder) == 0


def _run_hermes_cli(home: Path, *args: str, env_vars: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_KANBAN_HOME"] = str(home)
    for name in ("HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT"):
        env.pop(name, None)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if env_vars:
        env.update(env_vars)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_delegated_child_cli_fence_allows_own_card_reassign(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()

    # Create task
    created = _run_hermes_cli(home, "kanban", "create", "test card for worker handback", "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]

    # In delegated child context, reassigning own card should SUCCEED
    res_own = _run_hermes_cli(
        home,
        "kanban",
        "reassign",
        task_id,
        "new-assignee",
        env_vars={
            "HERMES_DELEGATED_CHILD_CONTEXT": "1",
            "HERMES_KANBAN_TASK": task_id,
        },
    )
    assert res_own.returncode == 0, res_own.stderr
    assert "delegate_task child contexts cannot mutate" not in res_own.stderr


def test_delegated_child_cli_fence_denies_other_card_reassign(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()

    created1 = _run_hermes_cli(home, "kanban", "create", "worker own card", "--json")
    task_id_1 = json.loads(created1.stdout)["id"]

    created2 = _run_hermes_cli(home, "kanban", "create", "other card", "--json")
    task_id_2 = json.loads(created2.stdout)["id"]

    # In delegated child context with HERMES_KANBAN_TASK=task_id_1, reassigning task_id_2 should FAIL
    res_other = _run_hermes_cli(
        home,
        "kanban",
        "reassign",
        task_id_2,
        "new-assignee",
        env_vars={
            "HERMES_DELEGATED_CHILD_CONTEXT": "1",
            "HERMES_KANBAN_TASK": task_id_1,
        },
    )
    assert res_other.returncode == 1
    assert "delegate_task child contexts cannot mutate Kanban tasks via the CLI" in res_other.stderr
