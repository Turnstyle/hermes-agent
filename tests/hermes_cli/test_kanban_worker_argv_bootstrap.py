"""The kanban worker argv must bootstrap a bare store interpreter.

Regression: after PM moved the backend onto the bare store Python, the dispatcher spawned
workers as ``<store python> -m hermes_cli.main`` with an env whose Hermes-owned PYTHONPATH
was stripped, so every worker died with "No module named 'hermes_cli'" (TurnerBook,
2026-09-25 14:50 CDT, 9 crashes in 5 minutes).
"""

import sys
from pathlib import Path

from hermes_cli import kanban_db_dispatch as kd


def test_bare_interpreter_uses_installation_bound_runtime_command(monkeypatch):
    monkeypatch.setattr(sys, "base_prefix", sys.prefix)
    argv = kd._module_hermes_argv()
    assert argv[0] == sys.executable
    assert argv[1] == "-I" and argv[2] == "-c"
    bootstrap = argv[3]
    root = str(Path(kd.__file__).resolve().parents[1])
    assert repr(root) in bootstrap
    assert "import hermes_bootstrap" in bootstrap
    assert "hermes_cli.main" in bootstrap
    assert "-m" not in argv


def test_venv_interpreter_keeps_plain_module_form(monkeypatch):
    monkeypatch.setattr(sys, "base_prefix", sys.prefix + "-base")
    assert kd._module_hermes_argv() == [sys.executable, "-m", "hermes_cli.main"]


def test_runtime_command_failure_falls_back_to_module_form(monkeypatch):
    import hermes_cli._launchers as launchers

    monkeypatch.setattr(sys, "base_prefix", sys.prefix)

    def boom(*a, **k):
        raise RuntimeError("no store")

    monkeypatch.setattr(launchers, "runtime_command", boom)
    assert kd._module_hermes_argv() == [sys.executable, "-m", "hermes_cli.main"]
