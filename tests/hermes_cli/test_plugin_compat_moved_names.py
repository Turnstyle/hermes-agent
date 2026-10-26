"""External plugins still import the moved-name helpers from ``hermes_cli.plugin_compat``
(fleet carry X09); the expired compat policy itself stays removed."""
from __future__ import annotations

import logging
import warnings

import pytest

from hermes_cli import plugin_compat


@pytest.fixture(autouse=True)
def _fresh_seen(monkeypatch):
    monkeypatch.setattr(plugin_compat, "_seen", set())


def test_warn_once_warns_and_logs_once_per_moved_name(caplog):
    with caplog.at_level(logging.WARNING, logger="hermes_cli.plugin_compat"):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for _ in range(2):
                plugin_compat.warn_once("hermes_cli.config", "load_config", "hermes_cli.config_io", "load_config")
    assert [w.category for w in caught] == [plugin_compat.HermesPluginCompatWarning]
    assert "hermes_cli.config_io.load_config" in str(caught[0].message)
    assert len([r for r in caplog.records if "plugin compat" in r.getMessage()]) == 1


def test_quiet_for_interactive_hides_the_category_but_explicit_error_wins():
    with warnings.catch_warnings():
        warnings.resetwarnings()
        plugin_compat.quiet_for_interactive()
        with warnings.catch_warnings(record=True) as caught:
            plugin_compat.warn_once("a", "b", "c", "b")
        assert caught == []

    plugin_compat._seen.clear()
    with warnings.catch_warnings():
        warnings.resetwarnings()
        warnings.filterwarnings("error", category=plugin_compat.HermesPluginCompatWarning)
        plugin_compat.quiet_for_interactive()
        with pytest.raises(plugin_compat.HermesPluginCompatWarning):
            plugin_compat.warn_once("a", "b", "c", "b")


def test_expired_policy_is_not_restored():
    assert plugin_compat.removal_in_effect() is True
    assert plugin_compat.compat_report() == {}
    assert plugin_compat.summary_lines({}) == []
