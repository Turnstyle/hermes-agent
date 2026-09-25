"""Key helper for an SSH-tunnelled Supermemory proxy: authenticate the listener BEFORE fetching a key.

Unit tests drive ``resolve()`` with fake system operations; the subprocess tests run the real script
against loopback-only fakes (a local HTTP server and PATH stubs for lsof/ps/ssh). No real key, host or
tunnel is touched.
"""

import http.server
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from plugins.memory.supermemory import _key_fingerprint, SupermemoryMemoryProvider
from plugins.memory.supermemory import tunnel_key_helper as helper

HELPER = Path(helper.__file__)
FORWARD = "127.0.0.1:16768:127.0.0.1:6768"
KEY = "synthetic-proxy-key-0002"
SSH_ARGV = f"/usr/bin/ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -L {FORWARD} rosie"
PROXY_401 = (401, {"server": "RosieAuthProxy/1.0 Python/3.12.3"}, b'{"error": "Unauthorized", "message": "Invalid or missing Bearer token"}')
PROXY_200 = (200, {"server": "RosieAuthProxy/1.0"}, b'{"status": "healthy", "service": "supermemory-fleet-pilot"}')


class FakeOps:
    def __init__(self, **overrides):
        self.calls = []
        self.port_is_open = True
        self.listener_entries = [(4242, os.getuid())]
        self.cmdline = SSH_ARGV
        self.unauth = PROXY_401
        self.key = KEY
        self.auth = PROXY_200
        self.__dict__.update(overrides)

    def uid(self):
        return os.getuid()

    def port_open(self, host, port, timeout):
        self.calls.append("port_open")
        return self.port_is_open

    def listeners(self, port, timeout):
        self.calls.append("listeners")
        return self.listener_entries

    def command_line(self, pid, timeout):
        self.calls.append("command_line")
        return self.cmdline

    def get_health(self, host, port, timeout, bearer=None):
        self.calls.append("auth_health" if bearer else "unauth_health")
        if bearer:
            assert bearer == self.key
            return self.auth
        return self.unauth

    def fetch_key(self, ssh_host, remote_path, timeout):
        self.calls.append("fetch_key")
        return self.key


def _run(ops, *extra):
    args = helper.parse_args(["--hermes-pid", "777", "--forward", FORWARD, "--ssh-host", "rosie",
                              "--remote-key-file", "~/pilot/data/api-key", "--server-prefix", "RosieAuthProxy/",
                              "--service", "supermemory-fleet-pilot", *extra])
    return helper.resolve(args, ops)


def _down(reason):
    return [f"SUPERMEMORY_AVAILABILITY_PROOF=down:777:{reason}"]


def test_success_prints_key_and_proof_after_listener_and_identity_checks():
    ops = FakeOps()
    assert _run(ops) == [f"SUPERMEMORY_API_KEY={KEY}", f"SUPERMEMORY_AVAILABILITY_PROOF=v1:777:{_key_fingerprint(KEY)}"]
    assert ops.calls == ["port_open", "listeners", "command_line", "unauth_health", "fetch_key", "auth_health"]


def test_port_closed_is_down_without_any_other_probe():
    ops = FakeOps(port_is_open=False)
    assert _run(ops) == _down("port_closed")
    assert ops.calls == ["port_open"]


@pytest.mark.parametrize("overrides", [
    {"cmdline": "/usr/bin/python3 -m http.server 16768"},                               # not ssh
    {"cmdline": "/usr/bin/ssh -N -L 127.0.0.1:16768:127.0.0.1:9999 rosie"},              # wrong forward
    {"cmdline": f"/usr/bin/ssh -N -L {FORWARD} otherhost"},                              # wrong host
    {"listener_entries": [(4242, os.getuid() + 1)]},                                    # another user's process
    {"listener_entries": [(4242, os.getuid()), (4243, os.getuid())]},                   # two owners
    {"listener_entries": []},                                                           # vanished
])
def test_unverified_listener_never_fetches_the_key(overrides):
    ops = FakeOps(**overrides)
    assert _run(ops) == _down("listener_unverified")
    assert "fetch_key" not in ops.calls


@pytest.mark.parametrize("unauth", [
    (401, {"server": "nginx/1.25"}, b'{"error": "Unauthorized"}'),                      # other server
    (200, {"server": "RosieAuthProxy/1.0"}, b'{"status": "healthy"}'),                  # no auth enforced
    (401, {"server": "RosieAuthProxy/1.0"}, b"not json"),
])
def test_identity_mismatch_never_fetches_the_key(unauth):
    ops = FakeOps(unauth=unauth)
    assert _run(ops) == _down("identity_mismatch")
    assert "fetch_key" not in ops.calls


@pytest.mark.parametrize("key", [None, "", "two words", "x" * 600])
def test_unusable_key_is_down(key):
    assert _run(FakeOps(key=key)) == _down("key_fetch_failed")


@pytest.mark.parametrize("auth", [
    (403, {}, b'{"error": "Forbidden"}'),
    (200, {}, b'{"status": "healthy", "service": "something-else"}'),
])
def test_failed_authenticated_health_withholds_the_key(auth):
    out = _run(FakeOps(auth=auth))
    assert out == _down("auth_health_failed")
    assert not any(KEY in line or _key_fingerprint(KEY) in line for line in out)


def test_probe_exception_is_down_not_a_crash():
    class Boom(FakeOps):
        def listeners(self, port, timeout):
            raise OSError("lsof missing")
    assert _run(Boom()) == _down("listener_unverified")


def test_spent_budget_is_down():
    assert _run(FakeOps(), "--budget", "0") == _down("budget_exhausted")


def test_fingerprint_matches_the_provider():
    assert helper.key_fingerprint(KEY) == _key_fingerprint(KEY)


def test_helper_output_satisfies_the_provider_gate(monkeypatch, tmp_path):
    """End to end on the contract: the lines the helper prints (for this pid) make the gate pass; down does not."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "supermemory.json").write_text(json.dumps({"require_availability_proof": True}), encoding="utf-8")
    args = ["--hermes-pid", str(os.getpid()), "--forward", FORWARD, "--ssh-host", "rosie", "--remote-key-file", "k",
            "--server-prefix", "RosieAuthProxy/", "--service", "supermemory-fleet-pilot"]
    for line in helper.resolve(helper.parse_args(args), FakeOps()):
        name, _, value = line.partition("=")
        monkeypatch.setenv(name, value)
    assert SupermemoryMemoryProvider().is_available() is True

    monkeypatch.setenv("SUPERMEMORY_AVAILABILITY_PROOF", helper.resolve(helper.parse_args(args), FakeOps(port_is_open=False))[0].partition("=")[2])
    assert SupermemoryMemoryProvider().is_available() is False
    assert "SUPERMEMORY_API_KEY" not in os.environ


# ---- real script, loopback only -------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _script(args, env=None, timeout=20):
    started = time.monotonic()
    proc = subprocess.run([sys.executable, str(HELPER), *args], capture_output=True, text=True, timeout=timeout,
                          env=env, stdin=subprocess.DEVNULL)
    return proc, time.monotonic() - started


def test_script_with_closed_port_prints_only_the_down_proof_quickly():
    port = _free_port()
    proc, elapsed = _script(["--hermes-pid", "4321", "--forward", f"127.0.0.1:{port}:127.0.0.1:6768",
                             "--ssh-host", "rosie", "--remote-key-file", "k"])
    assert proc.returncode == 0
    assert proc.stdout == "SUPERMEMORY_AVAILABILITY_PROOF=down:4321:port_closed\n"
    assert proc.stderr == ""
    assert elapsed < 5


class _FakeProxy(http.server.BaseHTTPRequestHandler):
    server_version = "RosieAuthProxy/1.0"

    def do_GET(self):
        ok = self.headers.get("Authorization") == f"Bearer {KEY}"
        body = json.dumps({"status": "healthy", "service": "supermemory-fleet-pilot"} if ok
                          else {"error": "Unauthorized", "message": "Invalid or missing Bearer token"}).encode()
        self.send_response(200 if ok else 401)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_proxy():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeProxy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_script_end_to_end_with_stubbed_system_tools(fake_proxy, tmp_path):
    """Real script, real HTTP to a loopback fake proxy; lsof/ps/ssh are PATH stubs posing as the tunnel."""
    forward = f"127.0.0.1:{fake_proxy}:127.0.0.1:6768"
    bin_dir, ssh_log = tmp_path / "bin", tmp_path / "ssh.log"
    bin_dir.mkdir()
    _stub(bin_dir, "lsof", f'printf "p4242\\nu{os.getuid()}\\nf3\\n"\n')
    _stub(bin_dir, "ps", f'echo "/usr/bin/ssh -N -L {forward} rosie"\n')
    _stub(bin_dir, "ssh", f'echo "$@" >> "{ssh_log}"\necho "{KEY}"\n')
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    proc, _ = _script(["--hermes-pid", "55", "--forward", forward, "--ssh-host", "rosie",
                       "--remote-key-file", "~/pilot/data/api-key"], env=env)
    assert proc.returncode == 0 and proc.stderr == ""
    assert proc.stdout.splitlines() == [f"SUPERMEMORY_API_KEY={KEY}",
                                        f"SUPERMEMORY_AVAILABILITY_PROOF=v1:55:{_key_fingerprint(KEY)}"]
    ssh_args = ssh_log.read_text().split()
    assert "BatchMode=yes" in ssh_args and "rosie" in ssh_args and ssh_args[-1] == "pilot/data/api-key"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_script_hanging_ssh_is_bounded_and_leaves_no_orphan(fake_proxy, tmp_path):
    """ssh that never returns, with a child of its own (like a ProxyCommand): the helper must return within
    --budget AND kill the whole group, not leave the child running after every hung start."""
    forward = f"127.0.0.1:{fake_proxy}:127.0.0.1:6768"
    bin_dir, child_pid_file = tmp_path / "bin", tmp_path / "child.pid"
    bin_dir.mkdir()
    _stub(bin_dir, "lsof", f'printf "p4242\\nu{os.getuid()}\\n"\n')
    _stub(bin_dir, "ps", f'echo "/usr/bin/ssh -N -L {forward} rosie"\n')
    _stub(bin_dir, "ssh", f'sleep 30 &\necho $! > "{child_pid_file}"\nwait\n')
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    proc, elapsed = _script(["--hermes-pid", "77", "--forward", forward, "--ssh-host", "rosie",
                             "--remote-key-file", "k", "--budget", "1.5"], env=env, timeout=40)
    assert proc.stdout in ("SUPERMEMORY_AVAILABILITY_PROOF=down:77:key_fetch_failed\n",
                           "SUPERMEMORY_AVAILABILITY_PROOF=down:77:budget_exhausted\n")
    assert elapsed < 6
    child = int(child_pid_file.read_text())
    deadline = time.monotonic() + 2
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(child), "ssh's child outlived the helper"  # (a leaked `sleep 30` exits on its own)


@pytest.mark.skipif(shutil.which("lsof") is None, reason="needs lsof")
def test_script_refuses_a_real_non_ssh_listener_and_never_runs_ssh(fake_proxy, tmp_path):
    """A look-alike proxy served by THIS python process (not ssh): real lsof/ps must reject it before any key fetch."""
    forward = f"127.0.0.1:{fake_proxy}:127.0.0.1:6768"
    bin_dir, ssh_log = tmp_path / "bin", tmp_path / "ssh.log"
    bin_dir.mkdir()
    _stub(bin_dir, "ssh", f'echo called >> "{ssh_log}"\necho "{KEY}"\n')
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}{os.pathsep}/usr/sbin"}
    proc, _ = _script(["--hermes-pid", "66", "--forward", forward, "--ssh-host", "rosie", "--remote-key-file", "k"], env=env)
    assert proc.stdout == "SUPERMEMORY_AVAILABILITY_PROOF=down:66:listener_unverified\n"
    assert not ssh_log.exists()


@pytest.mark.skipif(shutil.which("lsof") is None, reason="needs lsof")
def test_system_ops_reads_real_listener_owner(fake_proxy):
    ops = helper.SystemOps()
    assert ops.listeners(fake_proxy, 5) == [(os.getpid(), os.getuid())]
    assert "python" in ops.command_line(os.getpid(), 5).lower()
