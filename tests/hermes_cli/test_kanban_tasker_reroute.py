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


def test_delegated_child_cli_fence_denies_env_var_spoof(tmp_path):
    """Proves that a delegated child setting HERMES_KANBAN_TASK=<victim> is refused."""
    home = tmp_path / "hermes"
    home.mkdir()

    # Create a victim card
    created = _run_hermes_cli(home, "kanban", "create", "victim card", "--json")
    assert created.returncode == 0, created.stderr
    victim_id = json.loads(created.stdout)["id"]

    # In delegated child context, spoofing HERMES_KANBAN_TASK to victim_id must FAIL
    res_spoof = _run_hermes_cli(
        home,
        "kanban",
        "reassign",
        victim_id,
        "attacker-profile",
        env_vars={
            "HERMES_DELEGATED_CHILD_CONTEXT": "1",
            "HERMES_KANBAN_TASK": victim_id,
        },
    )
    assert res_spoof.returncode == 1
    assert "delegate_task child contexts cannot mutate Kanban tasks via the CLI" in res_spoof.stderr

    # Verify victim card was NOT reassigned
    show = _run_hermes_cli(home, "kanban", "show", victim_id, "--json")
    assert show.returncode == 0, show.stderr
    card = json.loads(show.stdout)["task"]
    assert card["assignee"] != "attacker-profile"


def test_delegated_child_cli_fence_denies_env_var_spoof_with_reclaim(tmp_path):
    """Proves the exact REVIEW-r1.md exploit is refused:
    HERMES_KANBAN_TASK=<victim> hermes kanban reassign <victim> <profile> --reclaim
    """
    home = tmp_path / "hermes"
    home.mkdir()

    created = _run_hermes_cli(home, "kanban", "create", "victim card", "--json")
    assert created.returncode == 0, created.stderr
    victim_id = json.loads(created.stdout)["id"]

    res_exploit = _run_hermes_cli(
        home,
        "kanban",
        "reassign",
        victim_id,
        "attacker-profile",
        "--reclaim",
        env_vars={
            "HERMES_DELEGATED_CHILD_CONTEXT": "1",
            "HERMES_KANBAN_TASK": victim_id,
        },
    )
    assert res_exploit.returncode == 1
    assert ("cannot reclaim" in res_exploit.stderr or "cannot mutate" in res_exploit.stderr)

    # Verify victim card is intact
    show = _run_hermes_cli(home, "kanban", "show", victim_id, "--json")
    assert show.returncode == 0, show.stderr
    card = json.loads(show.stdout)["task"]
    assert card["assignee"] != "attacker-profile"


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


_FOREIGN_WORKER_PID = 2**20


def _assert_foreign_pid_not_in_caller_tree():
    assert not kb._is_pid_in_caller_tree(_FOREIGN_WORKER_PID)


def _set_running_claim(conn, task_id: str, *, worker_pid: int, claim_lock: str = "test-host:spoof") -> None:
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'running', claim_lock = ?, claim_expires = ?, "
            "worker_pid = ?, worker_started_at = ? WHERE id = ?",
            (claim_lock, int(__import__("time").time()) + 3600, worker_pid, int(__import__("time").time()), task_id),
        )


def test_is_task_claimed_by_caller_predicate(kanban_home):
    """Env match alone is false; pid-tree live claim is the proof."""
    with kbc.connect_closing() as conn:
        ready_id = kb.create_task(conn, title="ready card", assignee="p")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (ready_id,))

        running_own = kb.create_task(conn, title="own running", assignee="p")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (running_own,))
        assert kb.claim_task(conn, running_own) is not None
        dispatch_mod._set_worker_pid(conn, running_own, os.getpid())

        running_foreign = kb.create_task(conn, title="foreign running", assignee="p")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (running_foreign,))
        assert kb.claim_task(conn, running_foreign) is not None
        dispatch_mod._set_worker_pid(conn, running_foreign, _FOREIGN_WORKER_PID)
        _assert_foreign_pid_not_in_caller_tree()

        other_id = kb.create_task(conn, title="other", assignee="p")

    with patch.dict(os.environ, {"HERMES_KANBAN_TASK": ready_id}, clear=False):
        with kbc.connect_closing() as conn:
            assert kb.is_task_claimed_by_caller(conn, ready_id) is False

    with patch.dict(os.environ, {"HERMES_KANBAN_TASK": running_foreign}, clear=False):
        with kbc.connect_closing() as conn:
            assert kb.is_task_claimed_by_caller(conn, running_foreign) is False

    with patch.dict(os.environ, {"HERMES_KANBAN_TASK": running_own}, clear=False):
        with kbc.connect_closing() as conn:
            assert kb.is_task_claimed_by_caller(conn, running_own) is True

    with patch.dict(os.environ, {"HERMES_KANBAN_TASK": other_id}, clear=False):
        with kbc.connect_closing() as conn:
            assert kb.is_task_claimed_by_caller(conn, running_own) is False

    with patch.dict(
        os.environ,
        {"HERMES_DELEGATED_CHILD_CONTEXT": "1", "HERMES_KANBAN_TASK": ""},
        clear=False,
    ):
        with kbc.connect_closing() as conn:
            assert kb.is_task_claimed_by_caller(conn, running_own) is True


def test_dispatcher_worker_env_var_does_not_prove_own_card(kanban_home):
    """HERMES_KANBAN_TASK matching an unclaimed ready card is not caller ownership."""
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="worker card", assignee="old")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))

    with patch.dict(os.environ, {"HERMES_KANBAN_TASK": task_id}, clear=False):
        with kbc.connect_closing() as conn:
            assert kb.is_task_claimed_by_caller(conn, task_id) is False


def test_dispatcher_worker_unfenced_reassign_ready_card(tmp_path):
    """Unfenced operator CLI may still reassign a ready card; that is not env-based ownership."""
    home = tmp_path / "hermes"
    home.mkdir()

    created = _run_hermes_cli(home, "kanban", "create", "worker card for handback", "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]

    with patch.dict(os.environ, {"HERMES_KANBAN_TASK": task_id}, clear=False):
        with kbc.connect_closing() as conn:
            assert kb.is_task_claimed_by_caller(conn, task_id) is False

    res_own = _run_hermes_cli(
        home,
        "kanban",
        "reassign",
        task_id,
        "new-assignee",
        env_vars={
            "HERMES_KANBAN_TASK": task_id,
        },
    )
    assert res_own.returncode == 0, res_own.stderr
    assert "Reassigned" in res_own.stdout

    show = _run_hermes_cli(home, "kanban", "show", task_id, "--json")
    assert show.returncode == 0, show.stderr
    card = json.loads(show.stdout)["task"]
    assert card["assignee"] == "new-assignee"


def test_env_spoof_cannot_reassign_foreign_running_claim(tmp_path):
    """HERMES_KANBAN_TASK on a victim with a foreign live claim must not reassign."""
    _assert_foreign_pid_not_in_caller_tree()
    home = tmp_path / "hermes"
    home.mkdir()

    created = _run_hermes_cli(home, "kanban", "create", "victim running", "--json")
    assert created.returncode == 0, created.stderr
    victim_id = json.loads(created.stdout)["id"]

    db_path = home / "kanban.db"
    conn = kbc.connect(db_path)
    _set_running_claim(conn, victim_id, worker_pid=_FOREIGN_WORKER_PID)
    conn.close()

    res = _run_hermes_cli(
        home,
        "kanban",
        "reassign",
        victim_id,
        "attacker-profile",
        env_vars={"HERMES_KANBAN_TASK": victim_id},
    )
    assert res.returncode != 0

    conn = kbc.connect(db_path)
    row = conn.execute(
        "SELECT assignee, status, claim_lock, worker_pid FROM tasks WHERE id = ?", (victim_id,),
    ).fetchone()
    conn.close()
    assert row["status"] == "running"
    assert row["claim_lock"] is not None
    assert int(row["worker_pid"]) == _FOREIGN_WORKER_PID
    assert row["assignee"] != "attacker-profile"


def test_env_spoof_other_card_foreign_running_claim_two_cards(tmp_path):
    """Env names worker card but reassign targets another card with a foreign claim."""
    _assert_foreign_pid_not_in_caller_tree()
    home = tmp_path / "hermes"
    home.mkdir()

    own = _run_hermes_cli(home, "kanban", "create", "worker own", "--json")
    own_id = json.loads(own.stdout)["id"]
    victim = _run_hermes_cli(home, "kanban", "create", "victim other", "--json")
    victim_id = json.loads(victim.stdout)["id"]

    db_path = home / "kanban.db"
    conn = kbc.connect(db_path)
    _set_running_claim(conn, victim_id, worker_pid=_FOREIGN_WORKER_PID)
    conn.close()

    res = _run_hermes_cli(
        home,
        "kanban",
        "reassign",
        victim_id,
        "attacker-profile",
        env_vars={"HERMES_KANBAN_TASK": own_id},
    )
    assert res.returncode != 0

    conn = kbc.connect(db_path)
    row = conn.execute(
        "SELECT assignee, status, claim_lock, worker_pid FROM tasks WHERE id = ?", (victim_id,),
    ).fetchone()
    conn.close()
    assert row["status"] == "running"
    assert row["claim_lock"] is not None
    assert int(row["worker_pid"]) == _FOREIGN_WORKER_PID


def test_dispatcher_claimed_card_reassign_by_worker(tmp_path):
    """A running task with a dispatcher-recorded claim matching the caller's process tree
    can be reassigned without --reclaim even in a child context.
    """
    home = tmp_path / "hermes"
    home.mkdir()

    # Create and claim task via DB directly
    db_path = home / "kanban.db"
    conn = kbc.connect(db_path)
    task_id = kb.create_task(conn, title="running claimed card", assignee="worker-profile")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    # Set worker_pid to current Python PID so the spawned CLI process is a child
    dispatch_mod._set_worker_pid(conn, task_id, os.getpid())
    conn.close()

    # In delegated child context, since the worker_pid matches this parent process,
    # reassign without --reclaim should SUCCEED
    res_reassign = _run_hermes_cli(
        home,
        "kanban",
        "reassign",
        task_id,
        "builder-profile",
        env_vars={
            "HERMES_DELEGATED_CHILD_CONTEXT": "1",
        },
    )
    assert res_reassign.returncode == 0, res_reassign.stderr

    conn = kbc.connect(db_path)
    updated = kb.get_task(conn, task_id)
    assert updated.assignee == "builder-profile"
    assert updated.claim_lock is None
    assert updated.worker_pid is None
    open_runs = conn.execute(
        "SELECT id FROM task_runs WHERE task_id = ? AND ended_at IS NULL", (task_id,),
    ).fetchall()
    assert open_runs == []
    last_run = conn.execute(
        "SELECT outcome FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    assert last_run is not None
    assert last_run["outcome"] == "reclaimed"
    conn.close()
