"""Fresh homes and profiles get the 10/10 delegation caps; a cap the user saved is never rewritten.

Real config loading against a temp ``HERMES_HOME`` (no ``_load_config`` patch): the value under test is what
``delegate_task`` actually reads for a home whose config.yaml carries no ``delegation`` section.
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


def test_fresh_home_without_delegation_section_gets_ten_and_ten(fresh_home):
    _write_config(fresh_home, {"model": {"provider": "custom", "default": "some-model"}})

    assert _get_oneshot_max_children() == 10
    assert _get_max_concurrent_children() == 10


@pytest.mark.parametrize("saved", [0, 2, 3, 25])
def test_saved_oneshot_cap_is_kept(fresh_home, saved):
    _write_config(fresh_home, {"delegation": {"oneshot_max_children": saved, "max_concurrent_children": 4}})

    assert _get_oneshot_max_children() == saved
    assert _get_max_concurrent_children() == 4


def test_fresh_native_profile_gets_ten_and_ten(fresh_home, monkeypatch):
    from hermes_cli.profiles import create_profile

    profile_dir = create_profile("capcheck", no_alias=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))

    assert _get_oneshot_max_children() == 10
    assert _get_max_concurrent_children() == 10


def test_cloned_profile_keeps_the_source_saved_caps(fresh_home, monkeypatch):
    """A clone is a copy of its source's config: an explicit source cap travels as-is; a source that never set
    one leaves the clone on the 10/10 defaults."""
    from hermes_cli.profiles import create_profile

    _write_config(fresh_home, {"model": {"provider": "custom", "default": "some-model"},
                               "delegation": {"oneshot_max_children": 3}})
    profile_dir = create_profile("capclone", clone_config=True, no_alias=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))

    assert _get_oneshot_max_children() == 3
    assert _get_max_concurrent_children() == 10
