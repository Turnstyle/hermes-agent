"""POSIX file-backed background processes survive their launching Hermes process."""

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.process_registry import ProcessRegistry


def _until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.platforms("posix")
def test_file_output_poll_log_wait_watch_and_completion(tmp_path):
    registry = ProcessRegistry()
    home = tmp_path / "profile"
    home.mkdir()
    token = set_hermes_home_override(home)
    try:
        start_file = tmp_path / "start"
        session = registry.spawn_local(
            f"while [ ! -f {shlex.quote(str(start_file))} ]; do sleep 0.05; done; "
            "printf 'ready\\n'; sleep 0.3; printf 'done\\n'; exit 7",
            task_id="task", session_key="conversation")
        session.notify_on_complete = True
        session.watch_patterns = ["ready"]
        start_file.touch()
        assert session.output_log_path == str(home / "logs" / "process-output" / f"{session.id}.log")
        assert Path(session.output_log_path).stat().st_mode & 0o777 == 0o600
        assert _until(lambda: "ready" in registry.poll(session.id)["output_preview"])
        assert "ready" in registry.read_log(session.id)["output"]
        result = registry.wait(session.id, timeout=3)
        assert result["status"] == "exited"
        assert result["exit_code"] == 7
        assert "done" in result["output"]
        assert Path(session.exit_file_path).read_text().strip() == "7"
        assert any(evt["type"] == "watch_match" for evt in list(registry.completion_queue.queue))
        assert _until(lambda: sum(evt["type"] == "completion"
                                  for evt in list(registry.completion_queue.queue)) == 1)
    finally:
        reset_hermes_home_override(token)
        registry.kill_all()


@pytest.mark.platforms("posix")
def test_output_paths_follow_profile_scope_a_b_a(tmp_path):
    paths = []
    for name in ("a", "b", "a"):
        home = tmp_path / name
        home.mkdir(exist_ok=True)
        token = set_hermes_home_override(home)
        registry = ProcessRegistry()
        try:
            session = registry.spawn_local("printf 'ok\\n'", task_id=name)
            assert registry.wait(session.id, timeout=3)["exit_code"] == 0
            paths.append(Path(session.output_log_path))
        finally:
            reset_hermes_home_override(token)
    assert paths[0].is_relative_to(tmp_path / "a")
    assert paths[1].is_relative_to(tmp_path / "b")
    assert paths[2].is_relative_to(tmp_path / "a")


@pytest.mark.platforms("posix")
def test_shared_registry_checkpoints_only_each_owning_profile(tmp_path):
    registry = ProcessRegistry()
    sessions = []
    for name in ("a", "b", "a"):
        home = tmp_path / name
        home.mkdir(exist_ok=True)
        token = set_hermes_home_override(home)
        try:
            sessions.append(registry.spawn_local("sleep 20", owner_task_id=name))
        finally:
            reset_hermes_home_override(token)
    try:
        for name in ("a", "b"):
            entries = json.loads((tmp_path / name / "processes.json").read_text())
            assert {row["session_id"] for row in entries} == {
                session.id for session in sessions if session.owner_task_id == name}
    finally:
        registry.kill_all()


_LAUNCH_PARENT = """
import json, sys
from tools.process_registry import ProcessRegistry
registry = ProcessRegistry()
session = registry.spawn_local(sys.argv[1], task_id='task', session_key='conversation')
session.notify_on_complete = True
session.watcher_interval = 1
session.watcher_platform = 'telegram'
session.watcher_chat_id = 'chat-1'
registry._write_checkpoint()
print(json.dumps({'id': session.id, 'pid': session.pid,
                  'log': session.output_log_path, 'exit': session.exit_file_path}), flush=True)
"""


def _launch_and_lose_parent(home: Path, command: str):
    env = dict(os.environ, HERMES_HOME=str(home))
    parent = subprocess.run([sys.executable, "-c", _LAUNCH_PARENT, command],
                            cwd=Path(__file__).resolve().parents[2], env=env,
                            capture_output=True, text=True, check=True, timeout=10)
    return json.loads(parent.stdout.strip().splitlines()[-1])


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("before_recovery", [False, True])
def test_recovery_reads_output_exit_and_notifies_once(tmp_path, before_recovery):
    home = tmp_path / "profile"
    home.mkdir()
    token = set_hermes_home_override(home)
    try:
        info = _launch_and_lose_parent(home, "sleep 0.5; printf 'after parent\\n'; exit 9")
        if before_recovery:
            assert _until(lambda: Path(info["exit"]).exists())
        registry = ProcessRegistry()
        assert registry.recover_from_checkpoint() == 1
        assert _until(lambda: registry.get(info["id"]).exited)
        result = registry.poll(info["id"])
        assert result["exit_code"] == 9
        assert "after parent" in registry.read_log(info["id"])["output"]
        assert Path(info["log"]).read_text().strip() == "after parent"
        notices = [evt for evt in list(registry.completion_queue.queue) if evt.get("type") == "completion"]
        assert len(notices) == 1
        assert notices[0]["exit_code"] == 9
        assert registry.recover_from_checkpoint() == 0
        assert len([evt for evt in list(registry.completion_queue.queue)
                    if evt.get("type") == "completion"]) == 1
        assert any(w["chat_id"] == "chat-1" for w in registry.pending_watchers)
    finally:
        reset_hermes_home_override(token)


@pytest.mark.platforms("posix")
def test_child_write_after_parent_exits_has_no_sigpipe(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    info = _launch_and_lose_parent(home, "sleep 0.4; printf 'survived\\n'; exit 0")
    assert _until(lambda: Path(info["exit"]).exists())
    assert Path(info["exit"]).read_text().strip() == "0"
    assert "survived" in Path(info["log"]).read_text()
