"""Key helper for an SSH-tunnelled Supermemory proxy.

Contracts: the credential never crosses the local forward (it travels only inside the helper's own ssh session);
the forward's local end is a Unix socket in a 0700 directory of this user, never a TCP port; and its listener is
accepted only when its exact argv is an ssh forward to the expected destination. Unit tests drive ``resolve()``
with fake system operations; the subprocess tests run the real script against local-only fakes: a real process
posing as the tunnel (argv ``ssh rosie -N -L <forward>``) serving the socket, a loopback HTTP server posing as the
proxy's remote end, and a PATH stub for ``ssh`` that runs the remote program locally. No real key, host or tunnel
is touched.
"""

import http.server
import json
import os
import shutil
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from plugins.memory.supermemory import _key_fingerprint, SupermemoryMemoryProvider
from plugins.memory.supermemory import tunnel_key_helper as helper

HELPER = Path(helper.__file__)
FORWARD = "/tunnel-dir/rosie.sock:127.0.0.1:6768"
KEY = "synthetic-proxy-key-0002"
PLIST_ARGV = ["/usr/bin/ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
              "-o", "ServerAliveCountMax=3", "-o", "StreamLocalBindMask=0177", "-o", "StreamLocalBindUnlink=yes",
              "-L", FORWARD, "rosie"]
PROXY_401 = (401, {"server": "RosieAuthProxy/1.0 Python/3.12.3"}, b'{"error": "Unauthorized", "message": "Invalid or missing Bearer token"}')
REMOTE_200 = (KEY, 200, b'{"status": "healthy", "service": "supermemory-fleet-pilot"}')


class FakeOps:
    def __init__(self, **overrides):
        self.calls = []
        self.socket = ("", (1, 100))
        self.peer = (4242, os.getuid())
        self.argv = list(PLIST_ARGV)
        self.unauth = PROXY_401
        self.remote = REMOTE_200
        self.__dict__.update(overrides)

    def uid(self):
        return os.getuid()

    def check_socket(self, socket_path):
        self.calls.append("check_socket")
        return self.socket

    def socket_peer(self, socket_path, timeout):
        self.calls.append("socket_peer")
        return self.peer

    def command_argv(self, pid, timeout):
        self.calls.append("command_argv")
        return self.argv

    def get_health(self, socket_path, timeout, **credentials):
        assert not credentials, "a credential was offered to the local socket"
        self.calls.append("local_health")
        return self.unauth

    def fetch_key_and_health(self, ssh_host, key_path, proxy_host, proxy_port, timeout):
        self.calls.append(("fetch", ssh_host, key_path, proxy_host, proxy_port))
        return self.remote


def _run(ops, *extra):
    args = helper.parse_args(["--hermes-pid", "777", "--forward", FORWARD, "--ssh-host", "rosie",
                              "--remote-key-file", "~/pilot/data/api-key", "--server-prefix", "RosieAuthProxy/",
                              "--service", "supermemory-fleet-pilot", *extra])
    return helper.resolve(args, ops)


def _down(reason):
    return [f"SUPERMEMORY_AVAILABILITY_PROOF=down:777:{reason}"]


def test_success_fetches_key_and_authenticated_health_in_one_ssh_session_after_bearer_free_checks():
    ops = FakeOps()
    assert _run(ops) == [f"SUPERMEMORY_API_KEY={KEY}", f"SUPERMEMORY_AVAILABILITY_PROOF=v1:777:{_key_fingerprint(KEY)}"]
    # The authenticated request targets the forward's REMOTE end, over ssh; locally only bearer-free probes run.
    assert ops.calls == ["check_socket", "socket_peer", "command_argv", "local_health",
                         ("fetch", "rosie", "~/pilot/data/api-key", "127.0.0.1", 6768)]


@pytest.mark.parametrize("reason", ["socket_missing", "socket_dir_wrong_mode", "socket_dir_wrong_owner",
                                    "socket_wrong_owner", "socket_path_invalid"])
def test_unsafe_or_missing_socket_is_down_without_any_other_probe(reason):
    ops = FakeOps(socket=(reason, None))
    assert _run(ops) == _down(reason)
    assert ops.calls == ["check_socket"]


@pytest.mark.parametrize("forward", ["127.0.0.1:16768:127.0.0.1:6768", "16768:127.0.0.1:6768",
                                     "relative/rosie.sock:127.0.0.1:6768", "/a/../rosie.sock:127.0.0.1:6768",
                                     "/tunnel-dir/rosie.sock:127.0.0.1:0", f"/{'x' * 120}.sock:127.0.0.1:6768"])
def test_forward_must_be_a_canonical_absolute_socket_path(forward):
    with pytest.raises(ValueError):
        helper.split_forward(forward)
    with pytest.raises(SystemExit):
        helper.parse_args(["--forward", forward, "--ssh-host", "rosie", "--remote-key-file", "k"])


@pytest.mark.parametrize("overrides", [
    {"argv": ["/usr/bin/python3", "-m", "http.server"]},                                 # not ssh
    {"argv": ["/usr/bin/ssh", "-N", "-L", "/tunnel-dir/other.sock:127.0.0.1:6768", "rosie"]},  # another forward
    {"argv": ["/usr/bin/ssh", "-N", "-L", FORWARD, "otherhost"]},                        # wrong host
    {"argv": None},                                                                      # argv unreadable
    {"peer": (4242, os.getuid() + 1)},                                                  # another user's process
    {"peer": None},                                                                      # no peer credentials
])
def test_unverified_listener_never_fetches_the_key(overrides):
    ops = FakeOps(**overrides)
    assert _run(ops) == _down("listener_unverified")
    assert not any(isinstance(c, tuple) for c in ops.calls)


@pytest.mark.parametrize("unauth", [
    (401, {"server": "nginx/1.25"}, b'{"error": "Unauthorized"}'),                      # other server
    (200, {"server": "RosieAuthProxy/1.0"}, b'{"status": "healthy"}'),                  # no auth enforced
    (401, {"server": "RosieAuthProxy/1.0"}, b"not json"),
])
def test_identity_mismatch_never_fetches_the_key(unauth):
    ops = FakeOps(unauth=unauth)
    assert _run(ops) == _down("identity_mismatch")
    assert not any(isinstance(c, tuple) for c in ops.calls)


@pytest.mark.parametrize("remote", [None, (None, 0, b""), ("", 200, b""), ("two words", 200, b""), ("x" * 600, 200, b"")])
def test_unusable_key_is_down(remote):
    assert _run(FakeOps(remote=remote)) == _down("key_fetch_failed")


@pytest.mark.parametrize("remote", [
    (KEY, 403, b'{"error": "Forbidden"}'),
    (KEY, 200, b'{"status": "healthy", "service": "something-else"}'),
    (KEY, 0, b""),                                                                       # proxy down on the host
])
def test_failed_authenticated_health_withholds_the_key(remote):
    out = _run(FakeOps(remote=remote))
    assert out == _down("auth_health_failed")
    assert not any(KEY in line or _key_fingerprint(KEY) in line for line in out)


def test_probe_exception_is_down_not_a_crash():
    class Refused(FakeOps):
        def socket_peer(self, socket_path, timeout):
            raise ConnectionRefusedError("stale socket")

    class NoArgv(FakeOps):
        def command_argv(self, pid, timeout):
            raise OSError("sysctl failed")
    assert _run(Refused()) == _down("socket_stale")
    assert _run(NoArgv()) == _down("listener_unverified")


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

    down = helper.resolve(helper.parse_args(args), FakeOps(socket=("socket_missing", None)))
    monkeypatch.setenv("SUPERMEMORY_AVAILABILITY_PROOF", down[0].partition("=")[2])
    assert SupermemoryMemoryProvider().is_available() is False
    assert "SUPERMEMORY_API_KEY" in os.environ


# ---- independent checker reproductions (round 2), kept as regressions ----------------------------


class CheckerOps:
    """The checker's fake. It answers both the round-2 probe interface (command_line, fetch_key, get_health with a
    bearer) and the current one, so these reproductions run unchanged against either helper."""

    def __init__(self, key, command=None, swap=False):
        self.key = key
        self.command = command or f"/usr/bin/ssh -N -L {FORWARD} rosie"
        self.swap = swap
        self.fetches = 0
        self.bearer_delivered_to_replacement = False

    def uid(self):
        return os.getuid()

    def check_socket(self, *args):
        return "", (1, 100)

    def socket_peer(self, *args):
        return 101, os.getuid()

    def command_line(self, *args):
        return self.command

    def command_argv(self, *args):
        return self.command.split()

    def fetch_key(self, *args):
        self.fetches += 1
        return self.key

    def fetch_key_and_health(self, *args):
        self.fetches += 1
        return self.key, 200, b'{"service":"supermemory-fleet-pilot"}'

    def get_health(self, *args, bearer=None):
        if bearer is None:
            return 401, {"server": "RosieAuthProxy/1"}, b'{"error":"Unauthorized"}'
        if self.swap:  # the verified tunnel vanished and a replacement now owns the local end
            self.bearer_delivered_to_replacement = bool(bearer)
            return 503, {}, b"{}"
        return 200, {}, b'{"service":"supermemory-fleet-pilot"}'


def _checker_resolve(ops):
    args = helper.parse_args(["--hermes-pid", str(os.getpid()), "--forward", FORWARD,
                              "--ssh-host", "rosie", "--remote-key-file", "unused-offline"])
    return any(line.startswith(helper.KEY_ENV + "=") for line in helper.resolve(args, ops))


def test_helper_rejects_expected_host_only_in_remote_command():
    ops = CheckerOps(KEY, command=f"/usr/bin/ssh -L {FORWARD} other-host echo rosie")
    emitted = _checker_resolve(ops)
    assert ops.fetches == 0, "Wrong SSH destination was accepted; helper fetched a credential"
    assert not emitted


def test_helper_never_sends_bearer_to_replacement_listener():
    ops = CheckerOps(KEY, swap=True)
    _checker_resolve(ops)
    assert not ops.bearer_delivered_to_replacement, "Helper sent the bearer to whatever owned the local end"


# ---- ssh argv: destination = first non-option argument, parsed the way ssh parses it ----------------


@pytest.mark.parametrize("argv", [
    PLIST_ARGV,                                                                          # the launchd plist
    ["ssh", "-f", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
     "-o", "ServerAliveCountMax=3", "-L", FORWARD, "rosie"],                             # tunnel.sh
    ["ssh", "-fNL", FORWARD, "rosie"],                                                   # clustered flags
    ["ssh", "-N", f"-L{FORWARD}", "rosie"],                                              # attached value
    ["ssh", "-oBatchMode=yes", "-o", "serveraliveinterval 30", "-N", "-L", FORWARD, "rosie"],
    ["ssh", "rosie", "-N", "-L", FORWARD],                                               # options after the destination
    ["ssh", "-N", "-L", FORWARD, "turner@rosie"],
    ["ssh", "-l", "turner", "-N", "-L", FORWARD, "rosie"],
    ["ssh", "-N", "-L", FORWARD, "--", "rosie"],
])
def test_tunnel_argv_accepted(argv):
    assert helper.is_tunnel_argv(argv, FORWARD, "rosie") is True


@pytest.mark.parametrize("argv", [
    ["/usr/bin/ssh", "-L", FORWARD, "other-host", "echo", "rosie"],                      # host only in the command
    ["ssh", "-N", "-L", FORWARD, "rosie", "true"],                                       # a tunnel runs no command
    ["ssh", "rosie", "echo", "-L", FORWARD],                                             # forward only in the command
    ["ssh", "--", "rosie", "-L", FORWARD],                                               # no option parsing after --
    ["ssh", "-N", "-L", FORWARD, "-l", "rosie", "other"],                                # host given as the user
    ["ssh", "-N", "-L", FORWARD, "rosie.example.com"],
    ["ssh", "-N", "-L", FORWARD, "ssh://rosie"],                                         # URI form: unsupported
    ["ssh", "-N", "-L", FORWARD],                                                        # no destination
    ["ssh", "-N", "rosie", "-L"],                                                        # option missing its value
    ["ssh", "-Z", "-N", "-L", FORWARD, "rosie"],                                         # unknown flag
    ["ssh", "-o", "HostName=evil.example", "-N", "-L", FORWARD, "rosie"],                # destination redirected
    ["ssh", "-N", "-L", FORWARD, "rosie", "-oHostname=evil"],                            # ... after the destination
    ["ssh", "-o", "ProxyCommand=nc evil 22", "-N", "-L", FORWARD, "rosie"],
    ["ssh", "-J", "jump", "-N", "-L", FORWARD, "rosie"],
    ["ssh", "-F", "/tmp/other_config", "-N", "-L", FORWARD, "rosie"],
    ["ssh", "-S", "/tmp/ctl", "-N", "-L", FORWARD, "rosie"],                             # someone else's master
    ["ssh", "-o", "ControlPath=/tmp/ctl", "-N", "-L", FORWARD, "rosie"],
    ["ssh", "-o", "StrictHostKeyChecking=no", "-N", "-L", FORWARD, "rosie"],             # host key not verified
    ["ssh", "-o", "UserKnownHostsFile=/dev/null", "-N", "-L", FORWARD, "rosie"],
    ["ssh", "-p", "2222", "-N", "-L", FORWARD, "rosie"],
    ["ssh", "-g", "-N", "-L", FORWARD, "rosie"],
    ["ssh", "-W", "x:1", "rosie"],
    ["ssh", "-N", "-L", "/tunnel-dir/rosie.sock:127.0.0.1:9999", "rosie"],              # another remote end
    ["ssh", "-N", "-L", "127.0.0.1:16768:127.0.0.1:6768", "rosie"],                     # round 3's TCP forward
    ["/usr/bin/autossh", "-N", "-L", FORWARD, "rosie"],
    [],
])
def test_tunnel_argv_rejected(argv):
    assert helper.is_tunnel_argv(argv, FORWARD, "rosie") is False


# ---- check_socket: the directory and the socket, lstat only ----------------------------------------


@pytest.fixture
def sock_dir():
    # Short and canonical: sun_path holds 104 bytes on macOS, and the socket path may not pass through a symlink.
    path = os.path.realpath(tempfile.mkdtemp(prefix="smh-", dir=tempfile.gettempdir()))
    os.chmod(path, 0o700)
    try:
        reason, _ = helper.check_socket(os.path.join(path, "probe.sock"), os.getuid())
        if reason == "socket_dir_unsafe":
            pytest.skip("socket integration requires trusted ancestors of TMPDIR")
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _bound_socket(path: str, mode: int = 0o600) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(path)
    sock.listen(1)
    os.chmod(path, mode)
    return sock


def test_check_socket_accepts_this_users_socket_in_a_0700_directory_and_pins_it(sock_dir):
    path = f"{sock_dir}/rosie.sock"
    with _bound_socket(path):
        reason, socket_id = helper.check_socket(path, os.getuid())
        assert reason == "" and socket_id == (os.lstat(path).st_dev, os.lstat(path).st_ino)
        assert helper.check_socket(path, os.getuid(), socket_id) == ("", socket_id)
        assert helper.check_socket(path, os.getuid(), (socket_id[0], socket_id[1] + 1)) == ("socket_replaced", None)
        assert helper.check_socket(path, os.getuid() + 1)[0] == "socket_dir_wrong_owner"


@pytest.mark.parametrize("setup,reason", [
    (lambda d: None, "socket_missing"),
    (lambda d: Path(f"{d}/rosie.sock").write_text("x"), "socket_not_a_socket"),
    (lambda d: os.chmod(d, 0o750), "socket_dir_wrong_mode"),
    (lambda d: os.chmod(d, 0o701), "socket_dir_wrong_mode"),
])
def test_check_socket_refusals(sock_dir, setup, reason):
    setup(sock_dir)
    assert helper.check_socket(f"{sock_dir}/rosie.sock", os.getuid()) == (reason, None)


def test_check_socket_refuses_group_or_other_access_to_the_socket(sock_dir):
    path = f"{sock_dir}/rosie.sock"
    with _bound_socket(path, 0o660):
        assert helper.check_socket(path, os.getuid()) == ("socket_wrong_mode", None)


def test_check_socket_refuses_an_ancestor_others_can_write(sock_dir):
    inner = f"{sock_dir}/open/inner"
    os.makedirs(inner, mode=0o700)
    os.chmod(f"{sock_dir}/open", 0o777)  # not sticky: anyone could rename inner and put their own there
    assert helper.check_socket(f"{inner}/rosie.sock", os.getuid()) == ("socket_dir_unsafe", None)
    os.chmod(f"{sock_dir}/open", 0o1777)  # sticky, like /tmp: only the owner may rename inner
    assert helper.check_socket(f"{inner}/rosie.sock", os.getuid()) == ("socket_missing", None)


# ---- real script and real processes, local only -----------------------------------------------------


def _script(args, env=None, timeout=20):
    started = time.monotonic()
    proc = subprocess.run([sys.executable, str(HELPER), *args], capture_output=True, text=True, timeout=timeout,
                          env=env, stdin=subprocess.DEVNULL)
    return proc, time.monotonic() - started


def test_script_with_no_socket_prints_only_the_down_proof_quickly(sock_dir):
    proc, elapsed = _script(["--hermes-pid", "4321", "--forward", f"{sock_dir}/rosie.sock:127.0.0.1:6768",
                             "--ssh-host", "rosie", "--remote-key-file", "k"])
    assert proc.returncode == 0
    assert proc.stdout == "SUPERMEMORY_AVAILABILITY_PROOF=down:4321:socket_missing\n"
    assert proc.stderr == ""
    assert elapsed < 5


class _RemoteProxy(http.server.BaseHTTPRequestHandler):
    """The proxy as seen on the ssh host (the forward's remote end); records what it was sent."""
    server_version = "RosieAuthProxy/1.0"

    def do_GET(self):
        ok = self.headers.get("Authorization") == f"Bearer {KEY}"
        self.server.authorized += ok
        body = json.dumps({"status": "healthy", "service": "supermemory-fleet-pilot"} if ok
                          else {"error": "Unauthorized", "message": "Invalid or missing Bearer token"}).encode()
        self.send_response(200 if ok else 401)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def remote_proxy():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RemoteProxy)
    server.authorized = 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


# Poses as the ssh client's end of the tunnel: serves the forward's local socket (mode 0600, as
# StreamLocalBindMask=0177 makes it), answers like the proxy does without a credential, and records any
# credential it is offered.
_TUNNEL_PROGRAM = '''
import http.server, os, pathlib, socketserver, sys
path = sys.argv[sys.argv.index("-L") + 1].split(":")[0]
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "RosieAuthProxy/1.0"
    def do_GET(self):
        if self.headers.get("Authorization"):
            with open("credential-received", "a") as f:
                f.write("1\\n")
        body = b'{"error": "Unauthorized"}'
        self.send_response(401)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def address_string(self):
        return "local"
    def log_message(self, *args):
        pass
os.umask(0o177)
server = socketserver.UnixStreamServer(path, Handler)
pathlib.Path("ready").write_text("1")
server.serve_forever()
'''


@pytest.fixture
def tunnel(tmp_path, sock_dir, remote_proxy):
    """A real process whose exact argv is ``<dir>/ssh rosie -N -L <forward>``: a python symlinked as ``ssh``
    that runs the program file named ``rosie`` (ssh parses options after the destination, so this is a valid
    tunnel command line)."""
    work, bin_dir = tmp_path / "tunnel", tmp_path / "tunnel-bin"
    work.mkdir()
    bin_dir.mkdir()
    (work / "rosie").write_text(_TUNNEL_PROGRAM, encoding="utf-8")
    # The base interpreter, not a venv binary: a copied venv python finds its stdlib through a pyvenv.cfg
    # next to it, which a symlink elsewhere does not have. The program needs only the stdlib.
    (bin_dir / "ssh").symlink_to(os.path.realpath(getattr(sys, "_base_executable", sys.executable)))
    forward = f"{sock_dir}/rosie.sock:127.0.0.1:{remote_proxy.server_address[1]}"
    with open(work / "stderr", "wb") as err:
        proc = subprocess.Popen([str(bin_dir / "ssh"), "rosie", "-N", "-L", forward], cwd=work,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err)
    deadline = time.monotonic() + 10
    while not (work / "ready").exists() and proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (work / "ready").exists(), f"fake tunnel did not start: {(work / 'stderr').read_text()[-800:]}"
    yield {"forward": forward, "socket": f"{sock_dir}/rosie.sock", "pid": proc.pid,
           "credential_log": work / "credential-received"}
    proc.kill()
    proc.wait(timeout=5)


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _env_with(bin_dir: Path) -> dict:
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}


def test_script_end_to_end_credential_travels_only_inside_ssh(tunnel, remote_proxy, tmp_path):
    """Real socket, peer-credential and argv checks against a real listener; the ssh stub runs the remote program
    locally, so the authenticated request really happens, against the forward's remote end. The local socket sees
    no credential."""
    bin_dir, ssh_log, key_file = tmp_path / "bin", tmp_path / "ssh.log", tmp_path / "api-key"
    bin_dir.mkdir()
    key_file.write_text(KEY + "\n", encoding="utf-8")
    _stub(bin_dir, "ssh", f'printf "%s\\n" "$@" > "{ssh_log}"\nfor last; do :; done\nexec /bin/sh -c "$last"\n')
    proc, _ = _script(["--hermes-pid", "55", "--forward", tunnel["forward"], "--ssh-host", "rosie",
                       "--remote-key-file", str(key_file), "--remote-python", sys.executable], env=_env_with(bin_dir))
    assert proc.returncode == 0 and proc.stderr == ""
    assert proc.stdout.splitlines() == [f"SUPERMEMORY_API_KEY={KEY}",
                                        f"SUPERMEMORY_AVAILABILITY_PROOF=v1:55:{_key_fingerprint(KEY)}"]
    assert remote_proxy.authorized == 1
    assert not tunnel["credential_log"].exists(), "the local forward received a credential"
    ssh_args = ssh_log.read_text().splitlines()
    assert "BatchMode=yes" in ssh_args and "ClearAllForwardings=yes" in ssh_args
    assert ssh_args[-2] == "rosie" and KEY not in ssh_log.read_text()


def test_script_missing_key_file_is_down(tunnel, remote_proxy, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir, "ssh", 'for last; do :; done\nexec /bin/sh -c "$last"\n')
    proc, _ = _script(["--hermes-pid", "56", "--forward", tunnel["forward"], "--ssh-host", "rosie",
                       "--remote-key-file", str(tmp_path / "absent"), "--remote-python", sys.executable], env=_env_with(bin_dir))
    assert proc.stdout == "SUPERMEMORY_AVAILABILITY_PROOF=down:56:key_fetch_failed\n"
    assert remote_proxy.authorized == 0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_script_hanging_ssh_is_bounded_and_leaves_no_orphan(tunnel, tmp_path):
    """ssh that never returns, with a child of its own (like a ProxyCommand): the helper must return within
    --budget AND kill the whole group, not leave the child running after every hung start."""
    bin_dir, child_pid_file = tmp_path / "bin", tmp_path / "child.pid"
    bin_dir.mkdir()
    _stub(bin_dir, "ssh", f'sleep 30 &\necho $! > "{child_pid_file}"\nwait\n')
    proc, elapsed = _script(["--hermes-pid", "77", "--forward", tunnel["forward"], "--ssh-host", "rosie",
                             "--remote-key-file", "k", "--budget", "2.5"], env=_env_with(bin_dir), timeout=40)
    assert proc.stdout in ("SUPERMEMORY_AVAILABILITY_PROOF=down:77:key_fetch_failed\n",
                           "SUPERMEMORY_AVAILABILITY_PROOF=down:77:budget_exhausted\n")
    assert elapsed < 8
    child = int(child_pid_file.read_text())
    deadline = time.monotonic() + 2
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(child), "ssh's child outlived the helper"  # (a leaked `sleep 30` exits on its own)


class _LookAlike(http.server.BaseHTTPRequestHandler):
    server_version = "RosieAuthProxy/1.0"

    def do_GET(self):
        body = b'{"error": "Unauthorized"}'
        self.send_response(401)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def address_string(self):
        return "local"

    def log_message(self, *args):
        pass


def test_script_refuses_a_real_non_ssh_listener_and_never_runs_ssh(sock_dir, tmp_path):
    """A look-alike proxy on a safe socket, served by THIS python process (not ssh): the real peer credentials and
    argv must reject it before any key fetch."""
    path = f"{sock_dir}/rosie.sock"
    old_umask = os.umask(0o177)
    try:
        server = socketserver.ThreadingUnixStreamServer(path, _LookAlike)
    finally:
        os.umask(old_umask)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        bin_dir, ssh_log = tmp_path / "bin", tmp_path / "ssh.log"
        bin_dir.mkdir()
        _stub(bin_dir, "ssh", f'echo called >> "{ssh_log}"\necho "{KEY}"\n')
        proc, _ = _script(["--hermes-pid", "66", "--forward", f"{path}:127.0.0.1:6768", "--ssh-host", "rosie",
                           "--remote-key-file", "k"], env=_env_with(bin_dir))
        assert proc.stdout == "SUPERMEMORY_AVAILABILITY_PROOF=down:66:listener_unverified\n"
        assert not ssh_log.exists()
    finally:
        server.shutdown()
        server.server_close()


def test_system_ops_reads_the_listener_and_its_exact_argv(tunnel):
    """The listener comes from the socket's peer credentials; ``ps`` joins argv with spaces, which is ambiguous, so the
    exact argv must come back element by element."""
    ops = helper.SystemOps()
    assert ops.socket_peer(tunnel["socket"], 5) == (tunnel["pid"], os.getuid())
    argv = ops.command_argv(tunnel["pid"], 5)
    assert Path(argv[0]).name == "ssh" and argv[1:] == ["rosie", "-N", "-L", tunnel["forward"]]


def test_system_ops_exact_argv_keeps_arguments_with_spaces():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "two words", "x"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        argv = None
        while time.monotonic() < deadline and not (argv and argv[-1] == "x"):
            argv = helper.SystemOps().command_argv(proc.pid, 5)
            time.sleep(0.02)
        assert argv[-3:] == ["import time; time.sleep(30)", "two words", "x"]
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_running_provider_drops_client_and_key_when_the_real_tunnel_dies(tunnel, monkeypatch, tmp_path):
    """The provider's re-check with the real probes (no fake ops): kill the listener, and the very next client use is
    refused with the client and key dropped."""
    import plugins.memory.supermemory as sm

    searches = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def search_memories(self, query, **kwargs):
            searches.append(query)
            return []

    monkeypatch.setattr(sm, "_SupermemoryClient", Client)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("SUPERMEMORY_API_KEY", KEY)
    monkeypatch.setenv("SUPERMEMORY_AVAILABILITY_PROOF", f"v1:{os.getpid()}:{_key_fingerprint(KEY)}")
    (tmp_path / "supermemory.json").write_text(json.dumps({
        "require_availability_proof": True, "tunnel": {"ssh_host": "rosie", "forward": tunnel["forward"]}}),
        encoding="utf-8")
    p = SupermemoryMemoryProvider()
    assert p.is_available() is True
    p.initialize("session-1", hermes_home=str(tmp_path), platform="cli")
    assert "error" not in json.loads(p.handle_tool_call("supermemory_search", {"query": "one"}))

    os.kill(tunnel["pid"], signal.SIGKILL)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:  # the socket file stays behind; wait until nothing accepts on it
        try:
            helper.SystemOps().socket_peer(tunnel["socket"], 0.5)
        except OSError:
            break
        time.sleep(0.05)
    out = json.loads(p.handle_tool_call("supermemory_search", {"query": "two"}))
    assert "disabled" in out["error"] and "socket_stale" in out["error"]
    assert searches == ["one"] and p._client is None and p._api_key == KEY
