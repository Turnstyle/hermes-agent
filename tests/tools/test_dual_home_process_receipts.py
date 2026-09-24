"""Regression: multiplexed registries must not fork divergent process-result receipts."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest


def _isolate_hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME (and DB-adjacent paths) at scratch before registry imports."""
    root_home = tmp_path / "hermes-root"
    root_home.mkdir(parents=True, exist_ok=True)
    (root_home / "config.yaml").write_text("model:\n  provider: custom\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root_home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return root_home


@pytest.fixture(autouse=True)
def _stub_host_signals(monkeypatch):
    """Patch kill boundaries before any PID fixtures (TASK rule 4 / Snowdrop lesson)."""
    monkeypatch.setattr(
        "tools.process_registry.os.kill",
        lambda *args, **kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        "tools.process_registry.ProcessRegistry._terminate_host_pid",
        lambda self, *args, **kwargs: None,
        raising=False,
    )


def _receipt_path(home: Path, proc_id: str) -> Path:
    return home / "logs" / "process-results" / f"{proc_id}.json"


def test_detached_refresh_at_root_does_not_shadow_profile_receipt(tmp_path, monkeypatch):
    root_home = _isolate_hermes_home(tmp_path, monkeypatch)
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
    from tools.process_registry import ProcessRegistry, ProcessSession
    from tools.process_registry_results import save_completed_result

    assert get_hermes_home() == root_home
    profile_home = root_home / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    proc_id = "proc_aabbccddeeff"
    started_at = 1_700_000_000.0
    cwd = str(tmp_path)

    token = set_hermes_home_override(profile_home)
    try:
        profile_session = ProcessSession(
            id=proc_id,
            command="echo profile",
            cwd=cwd,
            started_at=started_at,
            receipt_home=str(profile_home),
            exited=True,
            exit_code=42,
            completion_reason="exited",
        )
        save_completed_result(profile_session)
    finally:
        reset_hermes_home_override(token)

    profile_receipt = json.loads(_receipt_path(profile_home, proc_id).read_text(encoding="utf-8"))
    assert profile_receipt["exit_code"] == 42
    assert not _receipt_path(root_home, proc_id).exists()

    root_registry = ProcessRegistry()
    stale = ProcessSession(
        id=proc_id,
        command="echo profile",
        cwd=cwd,
        started_at=started_at,
        detached=True,
        pid_scope="host",
        pid=424242,
        exited=False,
    )
    root_registry._running[proc_id] = stale
    with patch.object(ProcessRegistry, "_host_pid_is_ours", return_value=False):
        root_registry._refresh_detached_session(stale)

    assert not _receipt_path(root_home, proc_id).exists()
    assert json.loads(_receipt_path(profile_home, proc_id).read_text(encoding="utf-8"))["exit_code"] == 42


def test_kill_all_at_root_preserves_natural_exit_and_records_late_kill(tmp_path, monkeypatch):
    root_home = _isolate_hermes_home(tmp_path, monkeypatch)
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
    from tools.process_registry import ProcessRegistry, ProcessSession
    from tools.process_registry_results import save_completed_result

    assert get_hermes_home() == root_home
    profile_home = root_home / "profiles" / "coder"
    profile_home.mkdir(parents=True)

    proc_id = "proc_112233445566"
    started_at = 1_700_000_100.0
    cwd = str(tmp_path)

    token = set_hermes_home_override(profile_home)
    try:
        profile_session = ProcessSession(
            id=proc_id,
            command="sleep 9",
            cwd=cwd,
            started_at=started_at,
            receipt_home=str(profile_home),
            exited=True,
            exit_code=255,
            completion_reason="exited",
            termination_source="",
        )
        save_completed_result(profile_session)
    finally:
        reset_hermes_home_override(token)

    root_registry = ProcessRegistry()
    running = ProcessSession(
        id=proc_id,
        command="sleep 9",
        cwd=cwd,
        started_at=started_at,
        detached=True,
        pid_scope="host",
        pid=515151,
        exited=False,
    )
    root_registry._running[proc_id] = running

    with patch.object(ProcessRegistry, "_host_pid_is_ours", return_value=True), patch.object(
        ProcessRegistry, "_signal_kill", return_value=None
    ), patch.object(ProcessRegistry, "_post_kill_survivors", return_value=[]):
        result = root_registry.kill_process(proc_id, source="kill_all")

    assert result["status"] == "killed"
    assert not _receipt_path(root_home, proc_id).exists()
    receipt = json.loads(_receipt_path(profile_home, proc_id).read_text(encoding="utf-8"))
    assert receipt["completion_reason"] == "exited"
    assert receipt["exit_code"] == 255
    assert receipt["late_kill_source"] == "kill_all"
    assert isinstance(receipt["late_kill_requested_at"], (int, float))


def test_kill_with_no_prior_exit_writes_killed_receipt(tmp_path, monkeypatch):
    root_home = _isolate_hermes_home(tmp_path, monkeypatch)
    from hermes_constants import get_hermes_home
    from tools.process_registry import ProcessRegistry, ProcessSession

    assert get_hermes_home() == root_home
    proc_id = "proc_deadbeefcafe"
    cwd = str(tmp_path)
    root_registry = ProcessRegistry()
    running = ProcessSession(
        id=proc_id,
        command="sleep 9",
        cwd=cwd,
        started_at=1_700_000_200.0,
        receipt_home=str(root_home),
        detached=True,
        pid_scope="host",
        pid=616161,
        exited=False,
    )
    root_registry._running[proc_id] = running

    with patch.object(ProcessRegistry, "_host_pid_is_ours", return_value=True), patch.object(
        ProcessRegistry, "_signal_kill", return_value=None
    ), patch.object(ProcessRegistry, "_post_kill_survivors", return_value=[]):
        result = root_registry.kill_process(proc_id, source="kill_all")

    assert result["status"] == "killed"
    killed = json.loads(_receipt_path(root_home, proc_id).read_text(encoding="utf-8"))
    assert killed["completion_reason"] == "killed"
    assert killed["termination_source"] == "kill_all"
    assert killed["exit_code"] == -15
    assert not killed.get("late_kill_source")
    assert killed.get("late_kill_requested_at") in (None, "")


def test_kill_with_survivors_writes_no_killed_receipt(tmp_path, monkeypatch):
    root_home = _isolate_hermes_home(tmp_path, monkeypatch)
    from hermes_constants import get_hermes_home
    from tools.process_registry import ProcessRegistry, ProcessSession

    assert get_hermes_home() == root_home
    proc_id = "proc_survivor1234"
    root_registry = ProcessRegistry()
    running = ProcessSession(
        id=proc_id,
        command="sleep 9",
        cwd=str(tmp_path),
        started_at=1_700_000_300.0,
        receipt_home=str(root_home),
        pid=717171,
        exited=False,
    )
    running.process = type("P", (), {"pid": 717171, "poll": lambda self: None})()
    root_registry._running[proc_id] = running

    with patch.object(ProcessRegistry, "_terminate_host_pid", return_value=None), patch.object(
        ProcessRegistry, "_post_kill_survivors", return_value=[717171]
    ), patch.object(ProcessRegistry, "_write_checkpoint", return_value=None):
        result = root_registry.kill_process(proc_id, source="kill_all")

    assert result["status"] == "error"
    assert not _receipt_path(root_home, proc_id).exists()
