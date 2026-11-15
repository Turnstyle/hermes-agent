"""An explicit text ``auxiliary.<task>`` block on a built-in provider seeds that provider's exact
``providers.<built-in>`` tuning into a fresh profile, as the model seed does, next to (never over)
the primary's endpoint entry. Real ``create_profile`` against a temp ``HERMES_HOME``.
"""

from pathlib import Path

import pytest

from hermes_cli.config import DEFAULT_CONFIG, atomic_config_write, read_user_config_raw
from hermes_cli.profiles import create_profile

from tests._fixtures.update_recovery_metadata import isolated_update_recovery_metadata

pytestmark = pytest.mark.usefixtures("isolated_update_recovery_metadata")

_PRIMARY_GW = {"name": "primary-gw", "base_url": "http://127.0.0.1:8317/v1", "api_key": "${PRIMARY_GW_TOKEN}"}
_OPENROUTER_TUNING = {"request_timeout_seconds": 150}


def test_aux_builtin_provider_tuning_travels_beside_the_primary_endpoint(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    review = {"provider": "openrouter", "model": "review-model", "reasoning_effort": "medium"}
    atomic_config_write(home / "config.yaml", {
        "model": {"provider": "custom:primary-gw", "default": "primary-model"},
        "providers": {"primary-gw": _PRIMARY_GW, "openrouter": _OPENROUTER_TUNING,
                      "anthropic": {"request_timeout_seconds": 5}},
        "auxiliary": {"review": review},
        "_config_version": DEFAULT_CONFIG["_config_version"],
    })

    profile = Path(create_profile("aux-tuning-bot", no_alias=True, no_skills=True))
    created = read_user_config_raw(profile / "config.yaml")

    assert created["providers"] == {"primary-gw": _PRIMARY_GW, "openrouter": _OPENROUTER_TUNING}
    assert created["auxiliary"]["review"] == review
