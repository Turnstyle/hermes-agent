"""Release contracts for saved delegation capacity and task-scoped concurrency.

Exercise real config loading in isolated homes without patching the config getters.
"""

from pathlib import Path

import pytest

from hermes_cli.config import atomic_config_write
from tools.delegate_tool_config import _get_max_concurrent_children, _get_oneshot_max_children

from tests._fixtures.update_recovery_metadata import isolated_update_recovery_metadata

pytestmark = pytest.mark.usefixtures("isolated_update_recovery_metadata")


@pytest.fixture()
def fresh_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("DELEGATION_MAX_CONCURRENT_CHILDREN", raising=False)
    return home


def _write_config(home: Path, data: dict) -> None:
    atomic_config_write(home / "config.yaml", data)


def test_saved_twenty_and_twenty_remains_authoritative(fresh_home, monkeypatch):
    _write_config(fresh_home, {"delegation": {
        "oneshot_max_children": 20, "max_concurrent_children": 20,
    }})
    monkeypatch.setenv("DELEGATION_MAX_CONCURRENT_CHILDREN", "30")

    assert _get_oneshot_max_children() == 20
    assert _get_max_concurrent_children() == 20


def test_cloned_profile_keeps_explicit_twenty_and_twenty(fresh_home, monkeypatch):
    from hermes_cli.profiles import create_profile

    _write_config(fresh_home, {
        "model": {"provider": "custom", "default": "some-model"},
        "delegation": {"oneshot_max_children": 20, "max_concurrent_children": 20},
    })
    profile_dir = create_profile("savedcapacity", clone_config=True, no_alias=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))

    assert _get_oneshot_max_children() == 20
    assert _get_max_concurrent_children() == 20


def test_task_scoped_explicit_config_uplift_restores_saved_capacity(fresh_home, monkeypatch):
    _write_config(fresh_home, {"delegation": {
        "oneshot_max_children": 20, "max_concurrent_children": 20,
    }})
    task_home = fresh_home.parent / "task-hermes"
    task_home.mkdir()
    _write_config(task_home, {"delegation": {
        "oneshot_max_children": 30, "max_concurrent_children": 30,
    }})

    assert _get_oneshot_max_children() == 20
    assert _get_max_concurrent_children() == 20
    with monkeypatch.context() as task_env:
        task_env.setenv("HERMES_HOME", str(task_home))

        assert _get_oneshot_max_children() == 30
        assert _get_max_concurrent_children() == 30

    assert _get_oneshot_max_children() == 20
    assert _get_max_concurrent_children() == 20


def test_unsaved_capacity_defaults_to_ten_and_ten(fresh_home):
    _write_config(fresh_home, {"model": {"provider": "custom", "default": "some-model"}})

    assert _get_oneshot_max_children() == 10
    assert _get_max_concurrent_children() == 10
