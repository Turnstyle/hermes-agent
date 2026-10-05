"""Tests for decompose-only profile routing and tasker re-routing logic.

Verifies:
1. Profile role and description recognition for decompose-only profiles.
2. Node builder resolution on Sheldon and other nodes.
3. Execution vs decomposition card classification.
4. Auto-decomposer roster exclusion and child assignee re-routing.
5. Single-task fanout=false promotion re-routing to node builder with audit comment.
6. Dispatcher-level re-routing of execution cards from decompose-only profiles to builder.
7. Dispatcher retention of decomposition/planning cards on decompose-only profiles.
8. Scoped worker CLI fence blocking foreign-card mutations. An own-card
   handback leaves a still-live worker's claim in place so the card cannot
   be claimed by a second worker.
"""

from __future__ import annotations

import contextlib
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


def test_live_own_card_handback_stays_unclaimable(kanban_home):
    """A handback while the recorded worker is alive must not ready the card.

    Uses a real throwaway-board claim and this process as the live worker, then
    a second claim_task. The first run stays open and the second claim loses.
    """
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="live claimed card", assignee="worker-profile")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        dispatch_mod._set_worker_pid(conn, task_id, os.getpid())
        assert kb.is_task_claimed_by_caller(conn, task_id) is True
        lock_before = claimed.claim_lock
        run_before = claimed.current_run_id

        ok = kb.reassign_task(conn, task_id, "builder-profile")

        assert ok is False
        updated = kb.get_task(conn, task_id)
        assert updated.status == "running"
        assert updated.assignee == "worker-profile"
        assert updated.claim_lock == lock_before
        assert int(updated.worker_pid) == os.getpid()
        assert updated.current_run_id == run_before
        open_run = conn.execute(
            "SELECT ended_at, outcome, worker_pid FROM task_runs WHERE id = ?",
            (run_before,),
        ).fetchone()
        assert open_run["ended_at"] is None
        assert open_run["outcome"] is None
        assert int(open_run["worker_pid"]) == os.getpid()
        assert kb.claim_task(conn, task_id) is None
        still = kb.get_task(conn, task_id)
        assert still.status == "running"
        assert still.claim_lock == lock_before
        assert int(still.worker_pid) == os.getpid()


def test_live_own_card_handback_unreadable_fingerprint_stays_unclaimable(kanban_home, monkeypatch):
    """An own-card handback where the worker PID is alive but its fingerprint
    read fails (None/mismatch) must still fail closed and not ready the card.
    """
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="unreadable fingerprint card", assignee="worker-profile")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        dispatch_mod._set_worker_pid(conn, task_id, os.getpid())
        assert kb.is_task_claimed_by_caller(conn, task_id) is True
        lock_before = claimed.claim_lock
        run_before = claimed.current_run_id

        # Monkeypatch fingerprint read so it returns None / mismatches,
        # making _worker_alive False while the PID is still alive.
        monkeypatch.setattr(dispatch_mod, "_process_fingerprint", lambda pid: None)
        task_row = conn.execute("SELECT worker_started_at FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert dispatch_mod._worker_alive(os.getpid(), task_row["worker_started_at"]) is False

        ok = kb.reassign_task(conn, task_id, "builder-profile")

        assert ok is False
        updated = kb.get_task(conn, task_id)
        assert updated.status == "running"
        assert updated.assignee == "worker-profile"
        assert updated.claim_lock == lock_before
        assert int(updated.worker_pid) == os.getpid()
        assert updated.current_run_id == run_before
        open_run = conn.execute(
            "SELECT ended_at, outcome, worker_pid FROM task_runs WHERE id = ?",
            (run_before,),
        ).fetchone()
        assert open_run["ended_at"] is None
        assert open_run["outcome"] is None
        assert int(open_run["worker_pid"]) == os.getpid()
        assert kb.claim_task(conn, task_id) is None
        still = kb.get_task(conn, task_id)
        assert still.status == "running"
        assert still.claim_lock == lock_before
        assert int(still.worker_pid) == os.getpid()


def test_dispatcher_claimed_card_reassign_by_worker(tmp_path):
    """CLI handback of a live ancestor claim does not release it for a second worker.
    """
    home = tmp_path / "hermes"
    home.mkdir()

    db_path = home / "kanban.db"
    conn = kbc.connect(db_path)
    task_id = kb.create_task(conn, title="running claimed card", assignee="worker-profile")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    # This process is an ancestor of the CLI child, so the child sees a live own-card claim.
    dispatch_mod._set_worker_pid(conn, task_id, os.getpid())
    conn.close()

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
    assert res_reassign.returncode != 0

    conn = kbc.connect(db_path)
    updated = kb.get_task(conn, task_id)
    assert updated.status == "running"
    assert updated.assignee == "worker-profile"
    assert updated.claim_lock is not None
    assert int(updated.worker_pid) == os.getpid()
    open_runs = conn.execute(
        "SELECT id FROM task_runs WHERE task_id = ? AND ended_at IS NULL", (task_id,),
    ).fetchall()
    assert len(open_runs) == 1
    assert kb.claim_task(conn, task_id) is None
    conn.close()


def _claim_own_running_card(conn, *, title: str, assignee: str = "worker-profile"):
    task_id = kb.create_task(conn, title=title, assignee=assignee)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    dispatch_mod._set_worker_pid(conn, task_id, os.getpid())
    assert kb.is_task_claimed_by_caller(conn, task_id) is True
    return task_id


def test_reassign_race_complete_between_read_and_write_keeps_done(kanban_home, monkeypatch):
    """Completion between reassign's pre-read and write must not revive a done card."""
    with kbc.connect_closing() as conn:
        task_id = _claim_own_running_card(conn, title="race complete during handback")
        assignee_before = kb.get_task(conn, task_id).assignee

        def _complete_then_report_dead(_pid):
            with kbc.connect_closing() as other:
                assert kb.complete_task(other, task_id, result="finished", force=True) is True
            return False

        monkeypatch.setattr(kb, "_pid_alive", _complete_then_report_dead)
        ok = kb.reassign_task(conn, task_id, "builder-profile")

        updated = kb.get_task(conn, task_id)
        assert ok is False
        assert updated.status == "done"
        assert updated.assignee == assignee_before
        assert kb.claim_task(conn, task_id) is None


def test_reassign_handback_and_assign_commit_together(kanban_home, monkeypatch):
    """A successful own-card handback must never commit ready under the old assignee."""
    with kbc.connect_closing() as conn:
        old_assignee = "worker-profile"
        new_assignee = "builder-profile"
        task_id = _claim_own_running_card(conn, title="atomic handback assign", assignee=old_assignee)
        committed = []
        real_write_txn = kb.write_txn

        @contextlib.contextmanager
        def _spy_write_txn(c, **kwargs):
            with real_write_txn(c, **kwargs) as inner:
                yield inner
            with kbc.connect_closing() as other:
                row = other.execute(
                    "SELECT status, assignee FROM tasks WHERE id = ?", (task_id,),
                ).fetchone()
                committed.append((row["status"], row["assignee"]))

        monkeypatch.setattr(kb, "write_txn", _spy_write_txn)
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        ok = kb.reassign_task(conn, task_id, new_assignee)

        assert ok is True
        assert not any(status == "ready" and assignee == old_assignee for status, assignee in committed)
        updated = kb.get_task(conn, task_id)
        assert updated.status == "ready"
        assert updated.assignee == new_assignee
        assert updated.claim_lock is None


def test_reassign_reclaim_first_race_complete_keeps_done(kanban_home, monkeypatch):
    """Completion between reclaim_first's terminate and DB write must leave done done."""
    with kbc.connect_closing() as conn:
        task_id = _claim_own_running_card(conn, title="race complete during reclaim_first")
        assignee_before = kb.get_task(conn, task_id).assignee

        def _terminate_then_complete(*_args, **_kwargs):
            with kbc.connect_closing() as other:
                assert kb.complete_task(other, task_id, result="finished", force=True) is True
            return {
                "prev_pid": os.getpid(),
                "host_local": True,
                "termination_attempted": False,
                "terminated": True,
                "sigkill": False,
            }

        monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _terminate_then_complete)
        ok = kb.reassign_task(conn, task_id, "builder-profile", reclaim_first=True)

        updated = kb.get_task(conn, task_id)
        assert ok is False
        assert updated.status == "done"
        assert updated.assignee == assignee_before
        assert kb.claim_task(conn, task_id) is None


def test_reassign_reclaim_first_aborts_when_rival_claims_after_preread(kanban_home, monkeypatch):
    """Unclaimed ready at pre-read, then a rival claim before the txn, must abort."""
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="unclaimed ready reclaim race", assignee="worker-profile")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        assignee_before = kb.get_task(conn, task_id).assignee
        real_write_txn = kb.write_txn
        rival: dict = {}

        @contextlib.contextmanager
        def _claim_then_txn(c, **kwargs):
            monkeypatch.setattr(kb, "write_txn", real_write_txn)
            with kbc.connect_closing() as other:
                claimed = kb.claim_task(other, task_id)
                assert claimed is not None
                dispatch_mod._set_worker_pid(other, task_id, os.getpid())
                rival["lock"] = claimed.claim_lock
                rival["run_id"] = claimed.current_run_id
            with real_write_txn(c, **kwargs) as inner:
                yield inner

        monkeypatch.setattr(kb, "write_txn", _claim_then_txn)
        ok = kb.reassign_task(conn, task_id, "builder-profile", reclaim_first=True)

        updated = kb.get_task(conn, task_id)
        assert ok is False
        assert updated.status == "running"
        assert updated.claim_lock == rival["lock"]
        assert int(updated.worker_pid) == os.getpid()
        assert updated.assignee == assignee_before
        open_run = conn.execute(
            "SELECT ended_at FROM task_runs WHERE id = ?",
            (rival["run_id"],),
        ).fetchone()
        assert open_run["ended_at"] is None
        assert kb.claim_task(conn, task_id) is None


def test_reassign_reclaim_first_aborts_when_claim_swaps_before_txn(kanban_home, monkeypatch):
    """Pre-read claim A replaced by claim B before the txn must leave B intact."""
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="claim A to B reclaim race", assignee="worker-profile")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        claimed_a = kb.claim_task(conn, task_id)
        assert claimed_a is not None
        dispatch_mod._set_worker_pid(conn, task_id, 999_999_999)
        assignee_before = kb.get_task(conn, task_id).assignee
        lock_a = claimed_a.claim_lock
        rival: dict = {}
        terminate_calls = {"n": 0}

        def _terminate_then_swap(*_args, **_kwargs):
            terminate_calls["n"] += 1
            if terminate_calls["n"] > 1:
                return {
                    "prev_pid": 999_999_999,
                    "host_local": True,
                    "termination_attempted": False,
                    "terminated": True,
                    "sigkill": False,
                }
            with kbc.connect_closing() as other:
                assert kb.reclaim_task(other, task_id) is True
                claimed_b = kb.claim_task(other, task_id, claimer="rival-b")
                assert claimed_b is not None
                dispatch_mod._set_worker_pid(other, task_id, os.getpid())
                rival["lock"] = claimed_b.claim_lock
                rival["run_id"] = claimed_b.current_run_id
            return {
                "prev_pid": 999_999_999,
                "host_local": True,
                "termination_attempted": False,
                "terminated": True,
                "sigkill": False,
            }

        monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _terminate_then_swap)
        ok = kb.reassign_task(conn, task_id, "builder-profile", reclaim_first=True)

        updated = kb.get_task(conn, task_id)
        assert ok is False
        assert rival["lock"] != lock_a
        assert updated.status == "running"
        assert updated.claim_lock == rival["lock"]
        assert int(updated.worker_pid) == os.getpid()
        assert updated.assignee == assignee_before
        open_run = conn.execute(
            "SELECT ended_at FROM task_runs WHERE id = ?",
            (rival["run_id"],),
        ).fetchone()
        assert open_run["ended_at"] is None
        assert kb.claim_task(conn, task_id) is None


def test_dispatcher_reroute_skips_comment_when_reassign_fails(kanban_home, monkeypatch):
    """A failed reclaim_first reassign must not write the auto-reroute audit comment."""
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

    monkeypatch.setattr(kb, "reassign_task", lambda *_a, **_k: False)

    with patch("hermes_cli.profiles.is_decompose_only_profile", side_effect=lambda p: p == "shld-tasker"):
        with patch("hermes_cli.profiles.resolve_node_builder", return_value="shld-bot-maker"):
            with patch("hermes_cli.kanban_db_dispatch._profile_exists_fn", return_value=lambda p: True):
                with kbc.connect_closing() as conn:
                    res = dispatch_mod._dispatch_once_locked(
                        conn,
                        spawn_fn=mock_spawn,
                        default_assignee="shld-bot-maker",
                    )

    assert task_id not in spawned_tasks
    assert (task_id, "shld-tasker", "shld-bot-maker") not in res.auto_rerouted_builder

    with kbc.connect_closing() as conn:
        updated = kb.get_task(conn, task_id)
        assert updated.assignee == "shld-tasker"
        assert updated.status == "ready"
        comments = kb.list_comments(conn, task_id)
        assert not any("[auto-reroute]" in c.body for c in comments)


def _ready_execution_card(conn, *, assignee: str = "shld-tasker", tenant: str = "sheldon") -> str:
    task_id = kb.create_task(
        conn,
        title="Build Sheldon custom system-prompt override profile shld-cndr-prompt-base",
        assignee=assignee,
        tenant=tenant,
    )
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
    return task_id


def _dispatch_execution_reroute(conn, spawn_fn):
    with patch("hermes_cli.profiles.is_decompose_only_profile", side_effect=lambda p: p == "shld-tasker"):
        with patch("hermes_cli.profiles.resolve_node_builder", return_value="shld-bot-maker"):
            with patch("hermes_cli.kanban_db_dispatch._profile_exists_fn", return_value=lambda p: True):
                return dispatch_mod._dispatch_once_locked(
                    conn,
                    spawn_fn=spawn_fn,
                    default_assignee="shld-bot-maker",
                )


def _fail_if_terminate(monkeypatch):
    """Record terminate calls; do not raise (dispatcher reroute swallows Exception)."""
    calls: list[tuple] = []

    def _record(*args, **kwargs):
        calls.append((args, kwargs))
        return {
            "prev_pid": args[0] if args else None,
            "host_local": True,
            "termination_attempted": True,
            "terminated": False,
            "sigkill": False,
        }

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _record)
    monkeypatch.setattr(dispatch_mod, "_terminate_reclaimed_worker", _record)
    return calls


def test_dispatcher_reroute_aborts_when_rival_claims_after_selection(kanban_home, monkeypatch):
    """Stale dispatcher selection must not reclaim a rival who claimed after the ready-row read."""
    with kbc.connect_closing() as conn:
        task_id = _ready_execution_card(conn)

    spawned_tasks = []
    rival: dict = {}
    real_claimed = kb.is_task_claimed_by_caller
    terminate_calls = _fail_if_terminate(monkeypatch)

    def _claim_then_check(conn, tid, *args, **kwargs):
        if tid == task_id and "lock" not in rival:
            claimed = kb.claim_task(conn, tid)
            assert claimed is not None
            dispatch_mod._set_worker_pid(conn, tid, os.getpid())
            rival["lock"] = claimed.claim_lock
            rival["run_id"] = claimed.current_run_id
        return real_claimed(conn, tid, *args, **kwargs)

    monkeypatch.setattr(kb, "is_task_claimed_by_caller", _claim_then_check)

    def mock_spawn(task, ws):
        spawned_tasks.append(task.id)
        return 12345

    with kbc.connect_closing() as conn:
        res = _dispatch_execution_reroute(conn, mock_spawn)
        updated = kb.get_task(conn, task_id)
        open_run = conn.execute(
            "SELECT ended_at, outcome, worker_pid FROM task_runs WHERE id = ?",
            (rival["run_id"],),
        ).fetchone()
        comments = kb.list_comments(conn, task_id)
        second_claim = kb.claim_task(conn, task_id)

    assert not terminate_calls
    assert task_id not in spawned_tasks
    assert (task_id, "shld-tasker", "shld-bot-maker") not in res.auto_rerouted_builder
    assert updated.assignee == "shld-tasker"
    assert updated.status == "running"
    assert updated.claim_lock == rival["lock"]
    assert int(updated.worker_pid) == os.getpid()
    assert open_run["ended_at"] is None
    assert open_run["outcome"] is None
    assert int(open_run["worker_pid"]) == os.getpid()
    assert not any("[auto-reroute]" in c.body for c in comments)
    assert second_claim is None


def test_dispatcher_reroute_aborts_when_operator_reassigns_after_selection(kanban_home, monkeypatch):
    """Stale dispatcher selection must not overwrite an operator reassignment to specialist."""
    with kbc.connect_closing() as conn:
        task_id = _ready_execution_card(conn)

    spawned_tasks = []
    real_claimed = kb.is_task_claimed_by_caller
    terminate_calls = _fail_if_terminate(monkeypatch)

    injected = {"done": False}

    def _operator_then_check(conn, tid, *args, **kwargs):
        if tid == task_id and not injected["done"]:
            injected["done"] = True
            assert kb.assign_task(conn, tid, "specialist") is True
        return real_claimed(conn, tid, *args, **kwargs)

    monkeypatch.setattr(kb, "is_task_claimed_by_caller", _operator_then_check)

    def mock_spawn(task, ws):
        spawned_tasks.append(task.id)
        return 12345

    with kbc.connect_closing() as conn:
        res = _dispatch_execution_reroute(conn, mock_spawn)
        updated = kb.get_task(conn, task_id)
        comments = kb.list_comments(conn, task_id)

    assert not terminate_calls
    assert task_id not in spawned_tasks
    assert (task_id, "shld-tasker", "shld-bot-maker") not in res.auto_rerouted_builder
    assert updated.assignee == "specialist"
    assert updated.status == "ready"
    assert updated.claim_lock is None
    assert not any("[auto-reroute]" in c.body for c in comments)


def test_reassign_task_expect_unclaimed_cas(kanban_home, monkeypatch):
    """expect_* CAS aborts a live claim and succeeds only on truly unclaimed ready."""
    terminate_calls = _fail_if_terminate(monkeypatch)

    with kbc.connect_closing() as conn:
        claimed_id = kb.create_task(conn, title="claimed expect cas", assignee="old")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (claimed_id,))
        claimed = kb.claim_task(conn, claimed_id)
        assert claimed is not None
        dispatch_mod._set_worker_pid(conn, claimed_id, os.getpid())
        lock_before = claimed.claim_lock
        run_before = claimed.current_run_id

        ok = kb.reassign_task(
            conn,
            claimed_id,
            "new",
            reclaim_first=True,
            expect_assignee="old",
            expect_status="ready",
            expect_unclaimed=True,
        )
        updated = kb.get_task(conn, claimed_id)
        open_run = conn.execute(
            "SELECT ended_at, outcome FROM task_runs WHERE id = ?",
            (run_before,),
        ).fetchone()
        assert ok is False
        assert updated.assignee == "old"
        assert updated.status == "running"
        assert updated.claim_lock == lock_before
        assert int(updated.worker_pid) == os.getpid()
        assert open_run["ended_at"] is None
        assert open_run["outcome"] is None
        assert kb.claim_task(conn, claimed_id) is None

        ready_id = kb.create_task(conn, title="unclaimed expect cas", assignee="old")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (ready_id,))
        ok_ready = kb.reassign_task(
            conn,
            ready_id,
            "new",
            reclaim_first=True,
            expect_assignee="old",
            expect_status="ready",
            expect_unclaimed=True,
        )
        ready = kb.get_task(conn, ready_id)
        assert ok_ready is True
        assert ready.assignee == "new"
        assert ready.status == "ready"
        assert ready.claim_lock is None
        assert not terminate_calls
