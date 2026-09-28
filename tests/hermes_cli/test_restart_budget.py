"""Gateway restart budget — one intentional restart per Hermes home per hour."""

from types import SimpleNamespace

import pytest

from hermes_cli.restart_budget import (
    CRASH_RECOVERY_BYPASS,
    RESTART_BUDGET_SECONDS,
    evaluate_restart_budget,
    format_restart_budget_refusal,
    guard_cli_gateway_restart,
    record_restart_budget,
    restart_budget_path,
)


def test_first_restart_allowed(tmp_path):
    allowed, minutes = evaluate_restart_budget(now=1_000_000.0, home=tmp_path)
    assert allowed and minutes is None


def test_second_restart_inside_window_refused(tmp_path):
    t0 = 1_700_000_000.0
    record_restart_budget(now=t0, home=tmp_path)
    allowed, minutes = evaluate_restart_budget(now=t0 + 30 * 60, home=tmp_path)
    assert not allowed
    assert minutes == 30


def test_restart_after_budget_elapsed_allowed(tmp_path):
    t0 = 1_700_000_000.0
    record_restart_budget(now=t0, home=tmp_path)
    allowed, _ = evaluate_restart_budget(
        now=t0 + RESTART_BUDGET_SECONDS, home=tmp_path
    )
    assert allowed


def test_force_bypasses_budget(tmp_path):
    t0 = 1_700_000_000.0
    record_restart_budget(now=t0, home=tmp_path)
    allowed, _ = evaluate_restart_budget(now=t0 + 60, force=True, home=tmp_path)
    assert allowed


def test_crash_recovery_bypass_reason(tmp_path):
    t0 = 1_700_000_000.0
    record_restart_budget(now=t0, home=tmp_path)
    allowed, _ = evaluate_restart_budget(
        now=t0 + 60,
        bypass_reason=CRASH_RECOVERY_BYPASS,
        home=tmp_path,
    )
    assert allowed


def test_crash_recovery_flag(tmp_path):
    t0 = 1_700_000_000.0
    record_restart_budget(now=t0, home=tmp_path)
    allowed, _ = evaluate_restart_budget(now=t0 + 60, crash_recovery=True, home=tmp_path)
    assert allowed


def test_record_writes_under_gateway_dir(tmp_path):
    record_restart_budget(now=42.0, home=tmp_path)
    path = restart_budget_path(tmp_path)
    assert path.exists()
    assert '"last_restart_unix": 42.0' in path.read_text(encoding="utf-8")


def test_refusal_message_includes_minutes():
    msg = format_restart_budget_refusal(17)
    assert "17" in msg and "Restart budget blocked" in msg


def test_cmd_restart_skips_stop_when_budget_refuses(monkeypatch, tmp_path, capsys):
    from hermes_cli import gateway as gw

    t0 = 1_700_000_000.0
    record_restart_budget(now=t0, home=tmp_path)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.restart_budget.time.time", lambda: t0 + 120)

    monkeypatch.setattr(gw, "_refuse_from_inside_gateway", lambda *a, **k: None)
    monkeypatch.setattr(
        "hermes_cli.gateway_profile_lifecycle.profile_lifecycle", lambda *a, **k: False
    )
    stopped = []
    monkeypatch.setattr(gw, "stop_profile_gateway", lambda: stopped.append(True) or True)
    monkeypatch.setattr(gw, "_service_call", lambda *a, **k: stopped.append("service") or None)

    with pytest.raises(SystemExit) as exc:
        gw._cmd_restart(SimpleNamespace(system=False, all=False, force=False))
    assert exc.value.code == 1
    assert not stopped
    err = capsys.readouterr().err
    assert "Restart budget blocked" in err


def test_lifecycle_unclean_exit_allows_immediate_restart(tmp_path):
    from hermes_cli.restart_budget import lifecycle_indicates_crash_recovery

    state = tmp_path / "state"
    state.mkdir()
    (state / "gateway.lifecycle.json").write_text(
        '{"phase": "running", "pid": 99999}',
        encoding="utf-8",
    )
    assert lifecycle_indicates_crash_recovery(tmp_path)
    t0 = 1_700_000_000.0
    record_restart_budget(now=t0, home=tmp_path)
    allowed, _ = evaluate_restart_budget(
        now=t0 + 10,
        crash_recovery=lifecycle_indicates_crash_recovery(tmp_path),
        home=tmp_path,
    )
    assert allowed
