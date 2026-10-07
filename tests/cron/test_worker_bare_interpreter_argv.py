"""The restart-safe cron worker argv must bootstrap a bare store interpreter.

Regression: after the gateways moved onto Hermes's own launcher (a bare PM store Python, no venv),
the cron scheduler spawned workers as ``<store python> -m cron.scheduler`` with an env whose Hermes-owned
PYTHONPATH was stripped. Python imports the ``cron`` package before ``cron.scheduler`` runs, so every
worker died with "No module named 'ruamel'" before its ownership acknowledgement and every scheduled job
on a systemd gateway failed (Max and Snowdrop, 2026-10-07 01:33 CDT onward).
"""

import sys
from pathlib import Path

import cron.scheduler_worker_env as worker_env

ROOT = Path(worker_env.__file__).resolve().parents[1]
ARGS = ["--external-worker-file", "payload.json", "--ack-file", "payload.ready"]


def test_bare_interpreter_uses_installation_bound_runtime_command(monkeypatch):
    monkeypatch.setattr(sys, "base_prefix", sys.prefix)
    argv = worker_env.external_worker_argv(ROOT, ARGS)
    assert argv[0] == sys.executable
    assert argv[1] == "-I" and argv[2] == "-c"
    bootstrap = argv[3]
    assert repr(str(ROOT)) in bootstrap
    assert "import hermes_bootstrap" in bootstrap
    assert "cron.scheduler" in bootstrap
    assert argv[4:] == ARGS
    assert "-m" not in argv


def test_venv_interpreter_keeps_plain_module_form(monkeypatch):
    monkeypatch.setattr(sys, "base_prefix", sys.prefix + "-base")
    assert worker_env.external_worker_argv(ROOT, ARGS) == [sys.executable, "-m", "cron.scheduler", *ARGS]


def test_runtime_command_failure_falls_back_to_module_form(monkeypatch):
    import hermes_cli._launchers as launchers

    monkeypatch.setattr(sys, "base_prefix", sys.prefix)

    def boom(*a, **k):
        raise RuntimeError("no store")

    monkeypatch.setattr(launchers, "runtime_command", boom)
    assert worker_env.external_worker_argv(ROOT, ARGS) == [sys.executable, "-m", "cron.scheduler", *ARGS]
