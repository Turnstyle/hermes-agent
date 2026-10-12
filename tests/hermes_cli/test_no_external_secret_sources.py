"""No-secret automation must not contact external secret managers at startup.

Authoring: protects unattended CLI startup; catches the roster-watch popup;
existing updater-only guard misses bot callers; no test-only product seam.
"""
import pytest


@pytest.mark.parametrize("opt_out", ["", "0", "1", "true"])
def test_no_secret_guard_preserves_update_behavior(monkeypatch, opt_out):
    from hermes_cli import _early_recovery

    monkeypatch.setattr(_early_recovery, "_UPDATE_RETRY_RECOVERED", False)
    monkeypatch.setenv("HERMES_SKIP_EXTERNAL_SECRET_SOURCES", opt_out)
    monkeypatch.setattr(_early_recovery.sys, "argv", ["hermes", "update"])
    assert _early_recovery._should_skip_external_secret_sources()
    monkeypatch.setattr(_early_recovery.sys, "argv", ["hermes", "kanban", "list"])
    assert _early_recovery._should_skip_external_secret_sources() == (opt_out == "1")
    monkeypatch.setattr(_early_recovery, "_UPDATE_RETRY_RECOVERED", True)
    assert _early_recovery._should_skip_external_secret_sources()


def test_unattended_loader_opt_out(monkeypatch, tmp_path):
    from hermes_cli import _early_recovery, env_loader

    monkeypatch.setattr(_early_recovery, "_UPDATE_RETRY_RECOVERED", False)
    monkeypatch.setattr(_early_recovery.sys, "argv", ["hermes", "kanban", "list"])
    monkeypatch.setenv("HERMES_SKIP_EXTERNAL_SECRET_SOURCES", "1")
    calls = []
    monkeypatch.setattr(env_loader, "_apply_external_secret_sources", lambda home: calls.append(home))
    monkeypatch.setattr(env_loader, "_apply_managed_env", lambda **kwargs: None)
    monkeypatch.setattr(env_loader, "_reapply_terminal_config_bridge", lambda home: None)
    env_loader.load_hermes_dotenv(hermes_home=tmp_path)
    assert calls == [], "no-secret bot startup contacted an external secret source"
    monkeypatch.delenv("HERMES_SKIP_EXTERNAL_SECRET_SOURCES")
    env_loader.load_hermes_dotenv(hermes_home=tmp_path)
    assert calls == [tmp_path], "ordinary callers must keep existing secret loading"
