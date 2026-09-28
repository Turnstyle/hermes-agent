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


# --- concurrent claim / rollback / recovery tests ---

def test_begin_restart_budget_two_threads_one_refused(tmp_path):
    import threading

    from hermes_cli.restart_budget import begin_restart_budget

    barrier = threading.Barrier(2)
    results = []

    def contender():
        barrier.wait()
        try:
            begin_restart_budget(force=False, home=tmp_path)
            results.append("allowed")
        except SystemExit:
            results.append("refused")

    threads = [threading.Thread(target=contender) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert sorted(results) == ["allowed", "refused"]


def test_begin_restart_budget_two_processes_one_refused(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    start_flag = tmp_path / "go"
    child = r"""
import sys
import time
from pathlib import Path
home = Path(sys.argv[1])
flag = Path(sys.argv[2])
while not flag.exists():
    time.sleep(0.01)
from hermes_cli.restart_budget import begin_restart_budget
try:
    begin_restart_budget(force=False, home=home)
    print("allowed", flush=True)
except SystemExit:
    print("refused", flush=True)
"""
    env = {**__import__("os").environ, "PYTHONPATH": str(repo_root)}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", child, str(tmp_path), str(start_flag)],
            cwd=str(repo_root),
            env=env,
            stdout=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    start_flag.write_text("1", encoding="utf-8")
    lines = []
    for p in procs:
        out, _ = p.communicate(timeout=30)
        lines.append(out.strip())
    assert sorted(lines) == ["allowed", "refused"]


def test_wedged_loop_bypasses_budget_inside_window(tmp_path, monkeypatch):
    import json
    import os

    from hermes_cli import gateway as gw
    from hermes_cli.restart_budget import begin_restart_budget, record_restart_budget

    monkeypatch.setattr(
        "hermes_cli.restart_budget.lifecycle_indicates_crash_recovery", lambda home=None: False
    )
    tmp_path.joinpath("gateway_state.json").write_text(
        json.dumps({"pid": os.getpid(), "gateway_state": "running"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        gw, "probe_gateway_loop_liveness", lambda pid, home=None: gw.GATEWAY_LOOP_WEDGED
    )
    t0 = 1_700_000_000.0
    record_restart_budget(now=t0, home=tmp_path)
    claim = begin_restart_budget(force=False, home=tmp_path, now=t0 + 30)
    assert claim is not None


def test_cmd_restart_wedged_systemd_escalates_without_budget_refusal(
    tmp_path, monkeypatch, capsys
):
    import json
    import os

    from hermes_cli import gateway as gw

    t0 = 1_700_000_000.0
    record_restart_budget(now=t0, home=tmp_path)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.restart_budget.time.time", lambda: t0 + 30)
    monkeypatch.setattr(
        "hermes_cli.restart_budget.lifecycle_indicates_crash_recovery", lambda home=None: False
    )
    tmp_path.joinpath("gateway_state.json").write_text(
        json.dumps({"pid": os.getpid(), "gateway_state": "running"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(gw, "_refuse_from_inside_gateway", lambda *a, **k: None)
    monkeypatch.setattr(
        "hermes_cli.gateway_profile_lifecycle.profile_lifecycle", lambda *a, **k: False
    )
    monkeypatch.setattr(gw, "_guard_named_profile_under_multiplexer", lambda **k: None)
    monkeypatch.setattr(gw, "_dispatch_all_via_service_manager_if_s6", lambda *a, **k: False)
    monkeypatch.setattr(gw, "_dispatch_via_service_manager_if_s6", lambda *a, **k: False)
    monkeypatch.setattr(gw, "_installed_service_kind_for", lambda *a, **k: "systemd")
    monkeypatch.setattr(gw, "_systemd_scope_preamble", lambda *a, **k: False)
    monkeypatch.setattr(gw, "refresh_systemd_unit_if_needed", lambda **k: None)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda *a, **k: os.getpid())
    monkeypatch.setattr(
        gw, "probe_gateway_loop_liveness", lambda pid, home=None: gw.GATEWAY_LOOP_WEDGED
    )
    escalated = []
    monkeypatch.setattr(
        gw, "_escalate_wedged_gateway", lambda pid: escalated.append(pid) or True
    )
    monkeypatch.setattr(gw, "_run_systemctl", lambda *a, **k: None)
    monkeypatch.setattr(gw, "_wait_for_systemd_service_restart", lambda **k: True)
    monkeypatch.setattr(gw, "get_service_name", lambda: "hermes-gateway")

    gw._cmd_restart(SimpleNamespace(system=False, all=False, force=False))
    assert escalated == [os.getpid()]
    assert "Restart budget blocked" not in capsys.readouterr().err


def test_health_check_failed_bypasses_budget(tmp_path, monkeypatch):
    import json
    import os

    from hermes_cli import gateway as gw
    from hermes_cli.restart_budget import begin_restart_budget, record_restart_budget

    monkeypatch.setattr(
        "hermes_cli.restart_budget.lifecycle_indicates_crash_recovery", lambda home=None: False
    )
    monkeypatch.setattr(
        gw, "probe_gateway_loop_liveness", lambda pid, home=None: gw.GATEWAY_LOOP_ALIVE
    )
    t0 = 1_700_000_000.0
    record_restart_budget(now=t0, home=tmp_path)
    tmp_path.joinpath("gateway_state.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "gateway_state": "running",
                "health_check": "failed",
            }
        ),
        encoding="utf-8",
    )
    begin_restart_budget(force=False, home=tmp_path, now=t0 + 30)


def test_degraded_alive_bypasses_budget(tmp_path, monkeypatch):
    import json
    import os

    from hermes_cli import gateway as gw
    from hermes_cli.restart_budget import begin_restart_budget, record_restart_budget

    monkeypatch.setattr(
        "hermes_cli.restart_budget.lifecycle_indicates_crash_recovery", lambda home=None: False
    )
    monkeypatch.setattr(
        gw, "probe_gateway_loop_liveness", lambda pid, home=None: gw.GATEWAY_LOOP_ALIVE
    )
    t0 = 1_700_000_000.0
    record_restart_budget(now=t0, home=tmp_path)
    tmp_path.joinpath("gateway_state.json").write_text(
        json.dumps({"pid": os.getpid(), "gateway_state": "degraded"}),
        encoding="utf-8",
    )
    begin_restart_budget(force=False, home=tmp_path, now=t0 + 30)


def test_guard_refusal_does_not_consume_budget(tmp_path, monkeypatch):
    import time

    from hermes_cli import gateway as gw

    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(gw, "_refuse_from_inside_gateway", lambda *a, **k: None)
    monkeypatch.setattr(
        "hermes_cli.gateway_profile_lifecycle.profile_lifecycle", lambda *a, **k: False
    )

    def refuse_guard(**kwargs):
        raise SystemExit(78)

    monkeypatch.setattr(gw, "_guard_named_profile_under_multiplexer", refuse_guard)

    with pytest.raises(SystemExit) as exc:
        gw._cmd_restart(SimpleNamespace(system=False, all=False, force=False))
    assert exc.value.code == 78

    allowed, _ = evaluate_restart_budget(now=time.time(), home=tmp_path)
    assert allowed

    ran = []
    monkeypatch.setattr(gw, "_guard_named_profile_under_multiplexer", lambda **k: None)
    monkeypatch.setattr(gw, "_dispatch_all_via_service_manager_if_s6", lambda *a, **k: False)
    monkeypatch.setattr(gw, "_dispatch_via_service_manager_if_s6", lambda *a, **k: False)
    monkeypatch.setattr(gw, "_installed_service_kind_for", lambda *a, **k: None)
    monkeypatch.setattr(gw, "stop_profile_gateway", lambda: False)
    monkeypatch.setattr(gw, "_wait_for_gateway_exit", lambda **k: None)
    monkeypatch.setattr(gw, "_wait_for_api_server_port_free", lambda: None)
    monkeypatch.setattr(
        gw, "run_gateway", lambda **k: ran.append(True)
    )
    t1 = 1_800_000_000.0
    monkeypatch.setattr("hermes_cli.restart_budget.time.time", lambda: t1)

    gw._cmd_restart(SimpleNamespace(system=False, all=False, force=False))
    assert ran

    allowed2, minutes = evaluate_restart_budget(now=t1 + 120, home=tmp_path)
    assert not allowed2
    assert minutes is not None


def test_failed_service_restart_does_not_consume_budget(tmp_path, monkeypatch):
    import subprocess

    from hermes_cli import gateway as gw

    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(gw, "_refuse_from_inside_gateway", lambda *a, **k: None)
    monkeypatch.setattr(
        "hermes_cli.gateway_profile_lifecycle.profile_lifecycle", lambda *a, **k: False
    )
    monkeypatch.setattr(gw, "_guard_named_profile_under_multiplexer", lambda **k: None)
    monkeypatch.setattr(gw, "_dispatch_all_via_service_manager_if_s6", lambda *a, **k: False)
    monkeypatch.setattr(gw, "_dispatch_via_service_manager_if_s6", lambda *a, **k: False)
    monkeypatch.setattr(gw, "_installed_service_kind_for", lambda *a, **k: "systemd")
    monkeypatch.setattr(gw, "supports_systemd_services", lambda: True)
    monkeypatch.setattr(gw, "get_systemd_linger_status", lambda: (True, "ok"))

    def fail_restart(kind, verb, system=False):
        raise subprocess.CalledProcessError(1, "systemctl")

    monkeypatch.setattr(gw, "_service_call", fail_restart)

    with pytest.raises(SystemExit) as exc:
        gw._cmd_restart(SimpleNamespace(system=False, all=False, force=False))
    assert exc.value.code == 1

    t0 = 1_700_000_000.0
    allowed, _ = evaluate_restart_budget(now=t0, home=tmp_path)
    assert allowed

    success = []
    monkeypatch.setattr(
        gw, "_service_call", lambda *a, **k: success.append(True)
    )
    monkeypatch.setattr("hermes_cli.restart_budget.time.time", lambda: t0)

    gw._cmd_restart(SimpleNamespace(system=False, all=False, force=False))
    assert success

    allowed2, _ = evaluate_restart_budget(now=t0 + 120, home=tmp_path)
    assert not allowed2


@pytest.mark.parametrize(
    "payload",
    [
        '{"last_restart_unix": Infinity}',
        '{"last_restart_unix": NaN}',
    ],
)
def test_invalid_timestamp_constants_ignored(tmp_path, caplog, payload):
    import logging

    path = restart_budget_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    caplog.set_level(logging.WARNING, logger="hermes_cli.restart_budget")
    now = 1_700_000_000.0
    allowed, _ = evaluate_restart_budget(now=now, home=tmp_path)
    assert allowed
    assert any("ignored" in r.message for r in caplog.records)


def test_far_future_timestamp_ignored(tmp_path, caplog):
    import json
    import logging

    now = 1_700_000_000.0
    path = restart_budget_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"last_restart_unix": now + 1_000_000}),
        encoding="utf-8",
    )
    caplog.set_level(logging.WARNING, logger="hermes_cli.restart_budget")
    allowed, _ = evaluate_restart_budget(now=now, home=tmp_path)
    assert allowed
    assert any("ignored" in r.message for r in caplog.records)


def test_negative_timestamp_ignored(tmp_path, caplog):
    import logging

    path = restart_budget_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"last_restart_unix": -5}', encoding="utf-8")
    caplog.set_level(logging.WARNING, logger="hermes_cli.restart_budget")
    allowed, _ = evaluate_restart_budget(now=1_700_000_000.0, home=tmp_path)
    assert allowed
    assert any("ignored" in r.message for r in caplog.records)


def test_garbage_json_ignored(tmp_path, caplog):
    import logging

    path = restart_budget_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")
    caplog.set_level(logging.WARNING, logger="hermes_cli.restart_budget")
    allowed, _ = evaluate_restart_budget(now=1_700_000_000.0, home=tmp_path)
    assert allowed
    assert any("ignored" in r.message for r in caplog.records)


def test_unreadable_budget_path_ignored(tmp_path, caplog):
    import logging

    path = restart_budget_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()
    caplog.set_level(logging.WARNING, logger="hermes_cli.restart_budget")
    allowed, _ = evaluate_restart_budget(now=1_700_000_000.0, home=tmp_path)
    assert allowed
    assert any("ignored" in r.message for r in caplog.records)


def test_near_future_slack_timestamp_still_enforces_budget(tmp_path):
    now = 1_700_000_000.0
    record_restart_budget(now=now + 60, home=tmp_path)
    allowed, minutes = evaluate_restart_budget(now=now, home=tmp_path)
    assert not allowed
    assert minutes is not None


def _bind_restart_cli(monkeypatch, gw, tmp_path):
    """Point the budget at ``tmp_path`` and skip guards that run before a backend."""
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(gw, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(gw, "_refuse_from_inside_gateway", lambda *a, **k: None)
    monkeypatch.setattr(
        "hermes_cli.gateway_profile_lifecycle.profile_lifecycle", lambda *a, **k: False
    )
    monkeypatch.setattr(gw, "_guard_named_profile_under_multiplexer", lambda **k: None)


def _assert_retry_then_consume(monkeypatch, gw, tmp_path, retry):
    """The failed attempt left the hour free; ``retry`` may take it."""
    t0 = 1_700_000_000.0
    allowed, _ = evaluate_restart_budget(now=t0, home=tmp_path)
    assert allowed
    monkeypatch.setattr("hermes_cli.restart_budget.time.time", lambda: t0)
    retry()
    allowed2, _ = evaluate_restart_budget(now=t0 + 120, home=tmp_path)
    assert not allowed2


def test_empty_s6_all_restart_allows_immediate_retry(tmp_path, monkeypatch):
    """s6 ``--all`` with no registered gateways must not keep the hour."""
    from hermes_cli import gateway as gw

    _bind_restart_cli(monkeypatch, gw, tmp_path)
    monkeypatch.setattr(
        "hermes_cli.service_manager.detect_service_manager", lambda: "s6"
    )

    class _Empty:
        def list_profile_gateways(self):
            return []

        def restart(self, name):
            raise AssertionError(name)

    monkeypatch.setattr(
        "hermes_cli.service_manager.get_service_manager", lambda: _Empty()
    )
    gw._cmd_restart(SimpleNamespace(system=False, all=True, force=False))

    restarted = []

    class _One:
        def list_profile_gateways(self):
            return ["coder"]

        def restart(self, name):
            restarted.append(name)

    monkeypatch.setattr(
        "hermes_cli.service_manager.get_service_manager", lambda: _One()
    )

    def retry():
        gw._cmd_restart(SimpleNamespace(system=False, all=True, force=False))

    _assert_retry_then_consume(monkeypatch, gw, tmp_path, retry)
    assert restarted == ["gateway-coder"]


def test_systemd_start_limit_allows_immediate_retry(tmp_path, monkeypatch, capsys):
    """A start-limit rejection from ``_systemd_reset_and_run`` must not keep the hour."""
    import subprocess

    from hermes_cli import gateway as gw

    _bind_restart_cli(monkeypatch, gw, tmp_path)
    monkeypatch.setattr(gw, "_dispatch_all_via_service_manager_if_s6", lambda *a, **k: False)
    monkeypatch.setattr(gw, "_dispatch_via_service_manager_if_s6", lambda *a, **k: False)
    monkeypatch.setattr(gw, "_installed_service_kind_for", lambda *a, **k: "systemd")
    monkeypatch.setattr(gw, "_systemd_scope_preamble", lambda *a, **k: False)
    monkeypatch.setattr(gw, "refresh_systemd_unit_if_needed", lambda **k: None)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda *a, **k: None)
    monkeypatch.setattr(gw, "_systemd_main_pid", lambda **k: None)
    monkeypatch.setattr(gw, "_recover_pending_systemd_restart", lambda **k: False)
    monkeypatch.setattr(gw, "get_service_name", lambda: "hermes-gateway")
    monkeypatch.setattr(gw, "_wait_for_systemd_service_restart", lambda **k: True)

    def reject_restart(args, **kwargs):
        if args and args[0] == "reset-failed":
            return subprocess.CompletedProcess(args, 0, "", "")
        raise subprocess.CalledProcessError(
            1, ["systemctl", *args], stderr="start-limit-hit"
        )

    monkeypatch.setattr(gw, "_run_systemctl", reject_restart)
    gw._cmd_restart(SimpleNamespace(system=False, all=False, force=False))
    assert "rate-limited" in capsys.readouterr().out

    started = []

    def accept_restart(args, **kwargs):
        started.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(gw, "_run_systemctl", accept_restart)

    def retry():
        gw._cmd_restart(SimpleNamespace(system=False, all=False, force=False))

    _assert_retry_then_consume(monkeypatch, gw, tmp_path, retry)
    assert ["restart", "hermes-gateway"] in started


def test_launchd_rejected_kickstart_allows_immediate_retry(tmp_path, monkeypatch):
    """launchd has no success-without-a-restart return. A kickstart ``CalledProcessError``
    that is neither an unloaded job nor a domain-unsupported fallback still rolls the hour
    back, so a retry immediately afterward is allowed and then consumes it.
    """
    import subprocess

    from hermes_cli import gateway as gw
    import hermes_cli.gateway_launchd as launchd

    plist = tmp_path / "ai.hermes.gateway.plist"
    plist.write_text("<plist/>", encoding="utf-8")
    _bind_restart_cli(monkeypatch, gw, tmp_path)
    monkeypatch.setattr(gw, "_dispatch_all_via_service_manager_if_s6", lambda *a, **k: False)
    monkeypatch.setattr(gw, "_dispatch_via_service_manager_if_s6", lambda *a, **k: False)
    monkeypatch.setattr(gw, "_installed_service_kind_for", lambda *a, **k: "launchd")
    monkeypatch.setattr(gw, "refresh_launchd_plist_if_needed", lambda: True)
    monkeypatch.setattr(gw, "get_launchd_label", lambda: "ai.hermes.gateway")
    monkeypatch.setattr(gw, "_launchd_domain", lambda: "gui/501")
    monkeypatch.setattr(gw, "get_launchd_plist_path", lambda: plist)
    monkeypatch.setattr(gw, "_wait_for_api_server_port_free", lambda: None)
    monkeypatch.setattr(gw, "_clear_launchd_unsupported_marker", lambda: None)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda *a, **k: None)

    calls = []

    def failing_launchctl(cmd, check=False, timeout=None, **kwargs):
        calls.append(list(cmd))
        if check and cmd[:2] == ["launchctl", "kickstart"]:
            raise subprocess.CalledProcessError(1, cmd, stderr="launchctl failed")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(launchd.subprocess, "run", failing_launchctl)
    with pytest.raises(SystemExit) as exc:
        gw._cmd_restart(SimpleNamespace(system=False, all=False, force=False))
    assert exc.value.code == 1
    assert any(cmd[:2] == ["launchctl", "kickstart"] for cmd in calls)

    calls.clear()

    def accepting_launchctl(cmd, check=False, timeout=None, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(launchd.subprocess, "run", accepting_launchctl)

    def retry():
        gw._cmd_restart(SimpleNamespace(system=False, all=False, force=False))

    _assert_retry_then_consume(monkeypatch, gw, tmp_path, retry)
    assert any(cmd[:3] == ["launchctl", "kickstart", "-k"] for cmd in calls)
