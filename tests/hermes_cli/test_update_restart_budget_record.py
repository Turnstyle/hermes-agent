"""Updater restart paths record into the per-home restart budget (record, never refuse)."""

import subprocess
import time

import pytest

from hermes_cli import update_cmd_fleet
from hermes_cli.restart_budget import (
    evaluate_restart_budget,
    record_restart_budget,
    restart_budget_path,
)


def test_note_gateway_restart_budget_limits_manual_restart(tmp_path):
    t0 = time.time()
    update_cmd_fleet._note_gateway_restart_budget(tmp_path)
    assert restart_budget_path(tmp_path).exists()
    allowed, minutes = evaluate_restart_budget(now=t0 + 5, home=tmp_path)
    assert not allowed
    assert minutes is not None and minutes >= 1


def test_note_gateway_restart_budget_does_not_refuse_when_recent_stamp(tmp_path):
    t0 = 1_700_000_000.0
    record_restart_budget(now=t0, home=tmp_path)
    update_cmd_fleet._note_gateway_restart_budget(tmp_path)


def test_systemctl_reset_and_restart_records_on_success(monkeypatch):
    notes = []
    monkeypatch.setattr(update_cmd_fleet, "_systemd_restart_timeout", lambda *a, **k: 5)

    def fake_systemctl(cmd, *, timeout):
        rc = 0
        if "restart" in cmd:
            rc = 0
        return subprocess.CompletedProcess(cmd, rc)

    monkeypatch.setattr(update_cmd_fleet, "_systemctl", fake_systemctl)
    monkeypatch.setattr(update_cmd_fleet, "_note_gateway_restart_budget", notes.append)

    result = update_cmd_fleet._systemctl_reset_and_restart(
        ["systemctl", "--user"], "hermes-gateway", scope_cmd=["systemctl", "--user"],
    )
    assert result.returncode == 0
    assert notes == [None]


def test_systemctl_reset_and_restart_skips_record_on_restart_failure(monkeypatch):
    notes = []
    monkeypatch.setattr(update_cmd_fleet, "_systemd_restart_timeout", lambda *a, **k: 5)

    def fake_systemctl(cmd, *, timeout):
        if "reset-failed" in cmd:
            return subprocess.CompletedProcess(cmd, 0)
        if "restart" in cmd:
            return subprocess.CompletedProcess(cmd, 1)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(update_cmd_fleet, "_systemctl", fake_systemctl)
    monkeypatch.setattr(update_cmd_fleet, "_note_gateway_restart_budget", notes.append)

    result = update_cmd_fleet._systemctl_reset_and_restart(
        ["systemctl", "--user"], "hermes-gateway", scope_cmd=["systemctl", "--user"],
    )
    assert result.returncode == 1
    assert notes == []


def test_restart_launchd_gateway_after_update_records_on_success(monkeypatch, tmp_path):
    notes = []
    plist = tmp_path / "ai.hermes.gateway.plist"
    plist.write_text("{}", encoding="utf-8")

    monkeypatch.setattr("hermes_cli.gateway.get_launchd_plist_path", lambda: plist)
    monkeypatch.setattr("hermes_cli.gateway.get_launchd_label", lambda: "ai.hermes.gateway")
    monkeypatch.setattr("hermes_cli.gateway.launchd_restart", lambda: None)
    monkeypatch.setattr("hermes_cli.gateway._launchctl_supervised_pid", lambda label: None)
    monkeypatch.setattr(
        "hermes_cli.gateway.wait_for_launchd_gateway_supervision",
        lambda **kwargs: True,
    )
    monkeypatch.setattr(update_cmd_fleet, "_note_gateway_restart_budget", notes.append)

    restarted, failed = update_cmd_fleet._restart_launchd_gateway_after_update()
    assert restarted == ["ai.hermes.gateway"]
    assert failed == []
    assert notes == [None]


def test_restart_launchd_gateway_after_update_skips_record_on_launchd_failure(monkeypatch, tmp_path):
    notes = []
    plist = tmp_path / "ai.hermes.gateway.plist"
    plist.write_text("{}", encoding="utf-8")

    def boom():
        raise subprocess.CalledProcessError(1, ["launchctl"], stderr="fail")

    monkeypatch.setattr("hermes_cli.gateway.get_launchd_plist_path", lambda: plist)
    monkeypatch.setattr("hermes_cli.gateway.get_launchd_label", lambda: "ai.hermes.gateway")
    monkeypatch.setattr("hermes_cli.gateway.launchd_restart", boom)
    monkeypatch.setattr(update_cmd_fleet, "_note_gateway_restart_budget", notes.append)

    restarted, failed = update_cmd_fleet._restart_launchd_gateway_after_update()
    assert restarted == []
    assert failed == ["ai.hermes.gateway"]
    assert notes == []
