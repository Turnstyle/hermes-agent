"""``hermes doctor`` warns about long-held Bot Chat turn locks without failing the run."""

from hermes_cli.doctor_state import _check_bot_turn_locks
from tools import bot_relay_lock_watchdog as watchdog


def test_stale_lock_is_a_warning_not_an_issue(monkeypatch, capsys):
    row = {"profile": "ops", "lock": "/x/ops.lock", "pid": 4242, "age_seconds": 7200.0, "command": "hermes chat"}
    monkeypatch.setattr(watchdog, "default_root", lambda: "/x")
    monkeypatch.setattr(watchdog, "find_stale_locks", lambda *a, **k: [row])
    finding = _check_bot_turn_locks(False)
    out = capsys.readouterr().out
    assert "ops" in out and "4242" in out and "120 min" in out
    assert not finding.issues and not finding.manual_issues


def test_no_stale_lock_prints_nothing(monkeypatch, capsys):
    monkeypatch.setattr(watchdog, "default_root", lambda: "/x")
    monkeypatch.setattr(watchdog, "find_stale_locks", lambda *a, **k: [])
    _check_bot_turn_locks(False)
    assert capsys.readouterr().out == ""
