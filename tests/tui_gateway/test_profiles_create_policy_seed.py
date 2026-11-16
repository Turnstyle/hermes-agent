"""Native ``profiles.create`` (the Desktop/TUI "New bot" path Forge-style creators also reach through
``hermes profile create``) gives a new profile the 10/10 delegation caps and the launch profile's
explicit auxiliary policy, with the endpoint definitions those auxiliary-only pins need.

- Fresh, voice-precreated config: voice mirroring writes ``config.yaml`` before the model seed, so a
  file-existence gate must not skip anything; the profile still runs on the defaults (10/10).
- Fresh: an explicit ``auxiliary.<task>`` block (effort/tier/route) travels together with the custom
  endpoint it names, as a ``${VAR}`` ref; endpoints nothing references stay behind, and the primary is
  the launch model (never a hardcoded one).
- Clone: a copy of the source config, so a cap the source saved travels as-is and a source that never
  saved one leaves the clone on the defaults.
"""

from __future__ import annotations

from pathlib import Path

import hermes_yaml as yaml
import pytest

from hermes_cli.config import DEFAULT_CONFIG, atomic_config_write
from tools.delegate_tool_config import _get_max_concurrent_children, _get_oneshot_max_children
from tui_gateway import server

from tests._fixtures.update_recovery_metadata import isolated_update_recovery_metadata

pytestmark = pytest.mark.usefixtures("isolated_update_recovery_metadata")

_PRIMARY_GW = {"name": "primary-gw", "base_url": "http://127.0.0.1:8317/v1", "api_key": "${PRIMARY_GW_TOKEN}"}
_AUX_RELAY = {"name": "aux-relay", "base_url": "http://127.0.0.1:8318/v1", "api_key": "${AUX_RELAY_TOKEN}"}
_UNRELATED = {"name": "unrelated", "base_url": "http://127.0.0.1:8319/v1"}
_BG_REVIEW = {"provider": "custom:aux-relay", "model": "aux-model", "reasoning_effort": "medium",
              "extra_body": {"service_tier": "default"}}
_CURATOR = {"reasoning_effort": "medium", "extra_body": {"service_tier": "default"}}
_TTS = {"provider": "openai"}


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
        "providers": {"primary-gw": _PRIMARY_GW, "aux-relay": _AUX_RELAY, "unrelated": _UNRELATED},
        # Voice mirroring precreates the new profile's config.yaml. Not the default ``edge``:
        # save_config strips default-equal values, which would leave nothing to precreate.
        "tts": _TTS,
        "_config_version": DEFAULT_CONFIG["_config_version"],
        **extra,
    })


def _create(params: dict) -> Path:
    # Clones copy the source's skills by definition, so ``no_skills`` is a fresh-create option only.
    fresh = {} if params.get("clone_from") else {"no_skills": True}
    resp = server._methods["profiles.create"](1, {"no_alias": True, **fresh, **params})
    assert "error" not in resp, resp.get("error")
    return Path(resp["result"]["path"])


def _caps(monkeypatch, profile: Path) -> tuple[int, int]:
    monkeypatch.setenv("HERMES_HOME", str(profile))
    return _get_oneshot_max_children(), _get_max_concurrent_children()


def _raw(profile: Path) -> dict:
    return yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8")) or {}


def test_fresh_voice_precreated_profile_gets_ten_and_ten(launch_home, monkeypatch):
    _write_launch(launch_home)

    profile = _create({"name": "scout"})

    assert _raw(profile)["tts"] == _TTS  # the voice step really did precreate the file
    assert _caps(monkeypatch, profile) == (10, 10)


def test_fresh_profile_carries_explicit_aux_policy_and_its_endpoint(launch_home):
    _write_launch(launch_home, auxiliary={"background_review": _BG_REVIEW, "curator": _CURATOR})

    cfg = _raw(_create({"name": "scout"}))

    assert (cfg["model"]["provider"], cfg["model"]["default"]) == ("custom:primary-gw", "primary-model")
    assert cfg["auxiliary"]["background_review"] == _BG_REVIEW
    assert cfg["auxiliary"]["curator"] == _CURATOR
    providers = cfg["providers"]
    assert providers["aux-relay"] == _AUX_RELAY  # the ref travels, never the launch secret
    assert "unrelated" not in providers
    written = (launch_home / "profiles" / "scout" / "config.yaml").read_text(encoding="utf-8")
    assert "aux-FAKE-222" not in written and "primary-FAKE-111" not in written


def test_fresh_profile_does_not_copy_launch_caps_over_defaults(launch_home, monkeypatch):
    """Caps are per-profile policy, not part of the seed: a fresh bot starts on 10/10."""
    _write_launch(launch_home, delegation={"oneshot_max_children": 2, "max_concurrent_children": 4})

    assert _caps(monkeypatch, _create({"name": "scout"})) == (10, 10)


@pytest.mark.parametrize("source_delegation,expected", [
    (None, (10, 10)),
    ({"oneshot_max_children": 3}, (3, 10)),
    ({"oneshot_max_children": 25, "max_concurrent_children": 12}, (25, 12)),
])
def test_clone_keeps_saved_caps_and_defaults_the_rest(launch_home, monkeypatch, source_delegation, expected):
    extra = {"delegation": source_delegation} if source_delegation else {}
    _write_launch(launch_home, **extra)

    profile = _create({"name": "twin", "clone_from": "default"})

    assert _caps(monkeypatch, profile) == expected
