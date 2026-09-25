"""F3: legacy receipt_home-empty fallback must prove ownership before adoption."""

import json
from pathlib import Path

import pytest


def _isolate_hermes_home(tmp_path, monkeypatch):
    root_home = tmp_path / "hermes-root"
    root_home.mkdir(parents=True, exist_ok=True)
    (root_home / "config.yaml").write_text("model:\n  provider: custom\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root_home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return root_home


@pytest.fixture(autouse=True)
def _stub_host_signals(monkeypatch):
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


def _write_receipt(home: Path, proc_id: str, record: dict) -> None:
    directory = home / "logs" / "process-results"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{proc_id}.json").write_text(json.dumps(record), encoding="utf-8")


def test_find_existing_receipt_adopts_single_identity_match(tmp_path, monkeypatch):
    root_home = _isolate_hermes_home(tmp_path, monkeypatch)
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
    from tools.process_registry import ProcessSession
    from tools.process_registry_results import _find_existing_receipt_path, save_completed_result

    assert get_hermes_home() == root_home
    profile_home = root_home / "profiles" / "coder"
    profile_home.mkdir(parents=True)

    proc_id = "proc_identity01"
    started_at = 1_700_001_000.0
    cwd = str(tmp_path / "work")
    Path(cwd).mkdir()

    _write_receipt(
        profile_home,
        proc_id,
        {
            "id": proc_id,
            "command": "echo hi",
            "cwd": cwd,
            "started_at": started_at,
            "owner_task_id": "owner-a",
            "session_key": "sess-a",
            "parent_session_id": "parent-a",
            "exit_code": 3,
            "completion_reason": "exited",
            "termination_source": "",
            "notify_on_complete": False,
            "output": "",
        },
    )

    session = ProcessSession(
        id=proc_id,
        command="echo hi",
        cwd=cwd,
        started_at=started_at,
        owner_task_id="owner-a",
        session_key="sess-a",
        parent_session_id="parent-a",
        exited=True,
        exit_code=9,
        completion_reason="exited",
    )

    token = set_hermes_home_override(root_home)
    try:
        assert get_hermes_home() == root_home
        adopted = _find_existing_receipt_path(session)
        assert adopted == profile_home / "logs" / "process-results" / f"{proc_id}.json"
        save_completed_result(session)
    finally:
        reset_hermes_home_override(token)

    updated = json.loads(
        (profile_home / "logs" / "process-results" / f"{proc_id}.json").read_text(encoding="utf-8")
    )
    assert updated["exit_code"] == 9
    assert not (root_home / "logs" / "process-results" / f"{proc_id}.json").exists()


def test_find_existing_receipt_rejects_mismatched_candidate(tmp_path, monkeypatch):
    root_home = _isolate_hermes_home(tmp_path, monkeypatch)
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
    from tools.process_registry import ProcessSession
    from tools.process_registry_results import _find_existing_receipt_path, save_completed_result

    assert get_hermes_home() == root_home
    profile_home = root_home / "profiles" / "coder"
    profile_home.mkdir(parents=True)

    proc_id = "proc_identity02"
    started_at = 1_700_001_100.0
    cwd = str(tmp_path)

    _write_receipt(
        profile_home,
        proc_id,
        {
            "id": proc_id,
            "command": "echo hi",
            "cwd": cwd,
            "started_at": started_at,
            "owner_task_id": "other-owner",
            "exit_code": 1,
            "completion_reason": "exited",
            "termination_source": "",
            "notify_on_complete": False,
            "output": "",
        },
    )

    session = ProcessSession(
        id=proc_id,
        command="echo hi",
        cwd=cwd,
        started_at=started_at,
        owner_task_id="expected-owner",
        exited=True,
        exit_code=2,
        completion_reason="exited",
    )

    token = set_hermes_home_override(root_home)
    try:
        assert _find_existing_receipt_path(session) is None
        save_completed_result(session)
    finally:
        reset_hermes_home_override(token)

    root_receipt = json.loads(
        (root_home / "logs" / "process-results" / f"{proc_id}.json").read_text(encoding="utf-8")
    )
    profile_receipt = json.loads(
        (profile_home / "logs" / "process-results" / f"{proc_id}.json").read_text(encoding="utf-8")
    )
    assert root_receipt["exit_code"] == 2
    assert profile_receipt["exit_code"] == 1


def test_find_existing_receipt_refuses_two_matches(tmp_path, monkeypatch, caplog):
    root_home = _isolate_hermes_home(tmp_path, monkeypatch)
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
    from tools.process_registry import ProcessSession
    from tools.process_registry_results import _find_existing_receipt_path

    assert get_hermes_home() == root_home
    profile_a = root_home / "profiles" / "coder"
    profile_b = root_home / "profiles" / "writer"
    profile_a.mkdir(parents=True)
    profile_b.mkdir(parents=True)

    proc_id = "proc_identity03"
    started_at = 1_700_001_200.0
    cwd = str(tmp_path)
    base = {
        "id": proc_id,
        "command": "echo hi",
        "cwd": cwd,
        "started_at": started_at,
        "exit_code": 0,
        "completion_reason": "exited",
        "termination_source": "",
        "notify_on_complete": False,
        "output": "",
    }
    _write_receipt(profile_a, proc_id, base)
    _write_receipt(profile_b, proc_id, dict(base))

    session = ProcessSession(
        id=proc_id,
        command="echo hi",
        cwd=cwd,
        started_at=started_at,
        exited=True,
        exit_code=0,
        completion_reason="exited",
    )

    token = set_hermes_home_override(root_home)
    try:
        with caplog.at_level("WARNING"):
            assert _find_existing_receipt_path(session) is None
        assert "Refusing cross-profile receipt adoption" in caplog.text
    finally:
        reset_hermes_home_override(token)


def test_find_existing_receipt_empty_profiles_dir(tmp_path, monkeypatch):
    root_home = _isolate_hermes_home(tmp_path, monkeypatch)
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
    from tools.process_registry import ProcessSession
    from tools.process_registry_results import _find_existing_receipt_path

    assert get_hermes_home() == root_home
    # No profiles/ directory created — empty/missing profiles tree.
    assert not (root_home / "profiles").exists()

    session = ProcessSession(
        id="proc_identity04",
        command="echo hi",
        cwd=str(tmp_path),
        started_at=1_700_001_300.0,
        exited=True,
        exit_code=0,
        completion_reason="exited",
    )

    token = set_hermes_home_override(root_home)
    try:
        assert get_hermes_home() == root_home
        assert _find_existing_receipt_path(session) is None
    finally:
        reset_hermes_home_override(token)
