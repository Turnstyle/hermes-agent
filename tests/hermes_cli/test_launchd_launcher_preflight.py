"""A failed launchd launcher must not replace a working gateway definition."""

import plistlib
import subprocess

from hermes_cli import gateway as gw
from hermes_cli import gateway_launchd


def test_refresh_preserves_plist_when_new_launcher_fails(tmp_path, monkeypatch, capsys):
    plist_path = tmp_path / "gateway.plist"
    original = b"working gateway plist\n"
    plist_path.write_bytes(original)
    broken = tmp_path / "broken-hermes"
    broken.write_text("#!/bin/sh\necho 'no dependency environment' >&2\nexit 1\n")
    broken.chmod(0o755)
    command = [str(broken), "--run-module", "hermes_cli.stderr_timestamp", "--error-log",
               str(tmp_path / "error.log"), "--", str(broken), "gateway", "run"]
    generated = plistlib.dumps({"ProgramArguments": gateway_launchd.launchd_program_arguments(
        command, tmp_path / "gateway.log", tmp_path / "error.log")
    }).decode()
    monkeypatch.setattr(gw, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(gw, "launchd_plist_is_current", lambda: False)
    monkeypatch.setattr(gw, "generate_launchd_plist", lambda: generated)
    monkeypatch.setattr(gw, "_refuse_temp_home_service_write", lambda *args: False)
    monkeypatch.setattr(gw, "_prepare_service_launcher", lambda: None)
    real_run = subprocess.run
    calls = []

    def guarded_run(argv, **kwargs):
        calls.append(argv)
        assert argv == [str(broken), "--version"]
        assert "HERMES_HOME" not in kwargs["env"]
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    assert gw.refresh_launchd_plist_if_needed() is False
    assert plist_path.read_bytes() == original
    assert calls == [[str(broken), "--version"]]
    assert "no dependency environment" in capsys.readouterr().out
