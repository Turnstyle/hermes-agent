"""Stock import/workspace isolation for native-agent tests in linked worktrees.

No guard, task ownership marker or runtime authority predicate is modified.
Updater recovery and dependency activation are unrelated to these tests: a linked
worktree points their metadata lookup into the installed checkout. Stock precedent:
tests/gateway/test_signature_change_releases_memory.py::no_source_updater.
"""
import pytest


@pytest.fixture(autouse=True)
def native_agent_isolation(_hermetic_environment, tmp_path, monkeypatch):
    from hermes_cli import _early_recovery, venv_sync
    from pm import environments
    monkeypatch.setattr(_early_recovery, 'restore_interrupted_pull', lambda *a: False)
    monkeypatch.setattr(_early_recovery, 'recover_if_needed', lambda *a: None)
    monkeypatch.setattr(venv_sync, 'prepare_launch', lambda *a: None)
    monkeypatch.setattr(environments, 'activate_dependencies', lambda *a: None)
    # The source checkout's parent is a real card directory. PATH construction
    # also queries payload_command_dir(source) independently of RUNTIME_DIR.
    # Run the actual manifest parser against a disposable checkout layout.
    payload_root = tmp_path / 'fixture-source'
    payload_root.mkdir()
    read_payload = environments._payload_manifest
    monkeypatch.setattr(environments, '_payload_manifest', lambda root: read_payload(payload_root))
    # TerminalEnvironment otherwise follows inherited WORKSPACE up to the real
    # card manifest before it ever evaluates descendant authority.
    # PM's source-root-relative payload detection probes repo.parent/manifest,
    # not WORKSPACE. Select its supported disposable runtime store as well.
    monkeypatch.setenv('HERMES_RUNTIME_DIR', str(tmp_path / 'tools'))
    monkeypatch.setenv('HERMES_KANBAN_WORKSPACE', str(tmp_path))
    monkeypatch.setenv('TERMINAL_CWD', str(tmp_path))
    monkeypatch.chdir(tmp_path)
