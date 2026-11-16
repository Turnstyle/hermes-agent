"""Native ``create_profile`` (``hermes profile create``; the call Forge-style creators make with either
``--clone-from default`` or a clean stage home) seeds a fresh profile with the launch profile's model AND
its explicit text auxiliary policy, plus the endpoint entries those auxiliary pins name.

- Fresh: explicit ``auxiliary.<text task>`` blocks travel as written (``${VAR}`` refs stay refs); non-text
  blocks (vision), unset blocks and the launch profile's delegation caps stay behind, so the new profile
  runs on the 10/10 cap defaults.
- Clean stage (no launch config): nothing to seed; the profile runs on the defaults.
- Clone: a copy of the source config, so the source's saved caps and aux blocks travel as-is.

Real config files against a temp ``HERMES_HOME``; the external Forge repo is not exercised here.
"""

from __future__ import annotations

from pathlib import Path

import hermes_yaml as yaml
import pytest

from hermes_cli.config import DEFAULT_CONFIG, atomic_config_write
from tools.delegate_tool_config import _get_max_concurrent_children, _get_oneshot_max_children

from tests._fixtures.update_recovery_metadata import isolated_update_recovery_metadata

pytestmark = pytest.mark.usefixtures("isolated_update_recovery_metadata")

_PRIMARY_GW = {"name": "primary-gw", "base_url": "http://127.0.0.1:8317/v1", "api_key": "${PRIMARY_GW_TOKEN}"}
_AUX_RELAY = {"name": "aux-relay", "base_url": "http://127.0.0.1:8318/v1", "api_key": "${AUX_RELAY_TOKEN}"}
_VISION_GW = {"name": "vision-gw", "base_url": "http://127.0.0.1:8320/v1"}
_UNRELATED = {"name": "unrelated", "base_url": "http://127.0.0.1:8319/v1"}
_MEDIUM_NORMAL = {"reasoning_effort": "medium", "extra_body": {"service_tier": "default"}}
_RELAY_PIN = {"provider": "custom:aux-relay", "model": "aux-model", **_MEDIUM_NORMAL}
_TEXT_POLICY = {
    "review": _RELAY_PIN,
    "background_review": _RELAY_PIN,
    "curator": _MEDIUM_NORMAL,
    "moa_reference": {"reasoning_effort": "medium"},
    "moa_aggregator": _MEDIUM_NORMAL,
}


@pytest.fixture
def launch_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("DELEGATION_MAX_CONCURRENT_CHILDREN", raising=False)
    monkeypatch.setenv("PRIMARY_GW_TOKEN", "primary-FAKE-111")
    monkeypatch.setenv("AUX_RELAY_TOKEN", "aux-FAKE-222")
    return home


def _write_launch(home: Path, **extra) -> None:
    atomic_config_write(home / "config.yaml", {
        "model": {"provider": "custom:primary-gw", "default": "primary-model"},
        "providers": {"primary-gw": _PRIMARY_GW, "aux-relay": _AUX_RELAY, "vision-gw": _VISION_GW,
                      "unrelated": _UNRELATED},
        "_config_version": DEFAULT_CONFIG["_config_version"],
        **extra,
    })


def _create(name: str, **kwargs) -> Path:
    from hermes_cli.profiles import create_profile
    return Path(create_profile(name, no_alias=True, **kwargs))


def _raw(profile: Path) -> dict:
    path = profile / "config.yaml"
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def _caps(monkeypatch, profile: Path) -> tuple[int, int]:
    monkeypatch.setenv("HERMES_HOME", str(profile))
    return _get_oneshot_max_children(), _get_max_concurrent_children()


def test_fresh_profile_carries_explicit_text_aux_policy_and_its_endpoint(launch_home, monkeypatch):
    _write_launch(launch_home, auxiliary={
        **_TEXT_POLICY,
        "vision": {"provider": "custom:vision-gw", "model": "vision-model"},  # non-text: stays behind
        "compression": {"provider": "auto", "model": "", "reasoning_effort": ""},  # unset: stays behind
    }, delegation={"oneshot_max_children": 2, "max_concurrent_children": 4})

    profile = _create("scout")
    cfg = _raw(profile)

    assert (cfg["model"]["provider"], cfg["model"]["default"]) == ("custom:primary-gw", "primary-model")
    assert cfg["auxiliary"] == _TEXT_POLICY
    assert cfg["providers"] == {"primary-gw": _PRIMARY_GW, "aux-relay": _AUX_RELAY}
    written = (profile / "config.yaml").read_text(encoding="utf-8")
    assert "aux-FAKE-222" not in written and "primary-FAKE-111" not in written
    assert "delegation" not in cfg
    assert _caps(monkeypatch, profile) == (10, 10)


def test_fresh_profile_without_aux_policy_seeds_only_the_model(launch_home, monkeypatch):
    _write_launch(launch_home, auxiliary={"curator": {"provider": "auto", "reasoning_effort": ""}})

    cfg = _raw(_create("plain"))

    assert "auxiliary" not in cfg
    assert cfg["providers"] == {"primary-gw": _PRIMARY_GW}


def test_clean_stage_home_creates_a_default_profile(launch_home, monkeypatch):
    """Forge's clean-stage path: a stage home with no config.yaml seeds nothing; caps are the defaults."""
    profile = _create("staged")

    assert "auxiliary" not in _raw(profile)
    assert _caps(monkeypatch, profile) == (10, 10)


@pytest.mark.parametrize("source_delegation,expected", [
    (None, (10, 10)),
    ({"oneshot_max_children": 3}, (3, 10)),
    ({"oneshot_max_children": 25, "max_concurrent_children": 12}, (25, 12)),
])
def test_clone_from_default_keeps_the_source_config(launch_home, monkeypatch, source_delegation, expected):
    """Forge's ``--clone-from default`` path: a clone is a copy, so saved caps and aux policy travel as-is."""
    extra = {"delegation": source_delegation} if source_delegation else {}
    _write_launch(launch_home, auxiliary=_TEXT_POLICY, **extra)

    profile = _create("twin", clone_from="default", clone_config=True)

    assert _raw(profile)["auxiliary"] == _TEXT_POLICY
    assert _caps(monkeypatch, profile) == expected
