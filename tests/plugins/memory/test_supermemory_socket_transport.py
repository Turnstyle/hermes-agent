"""Supermemory through the tunnel's Unix socket: the real SDK, the real provider, real processes.

Regression for the round-3 checker's reproduction: with the forward on a local TCP port, a credential-bearing SDK
request reached whatever listened on that port during the cached re-check window. Contract now: with a ``tunnel``
block the SDK's only route is the tunnel's Unix socket, in a 0700 directory of this user, checked before every
request. A TCP listener on the old port receives nothing in any state; a stale or replaced socket, or an unsafe
directory, is refused.

The listener is a real process whose exact argv is ``<dir>/ssh rosie -N -L <forward>`` (a python symlinked as
``ssh``) serving HTTP on the forward's local end; the provider checks it with real lstat, real peer credentials and
the real kernel argv. Only the uid is modeled, and only where a test says so. Synthetic key; no ssh, Rosie or tunnel.
"""

import http.server
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

import plugins.memory.supermemory as sm
from plugins.memory.supermemory import SupermemoryMemoryProvider, _key_fingerprint
from plugins.memory.supermemory import tunnel_key_helper as helper

KEY = "synthetic-socket-key-0004"
PROOF_ENV = "SUPERMEMORY_AVAILABILITY_PROOF"

# Poses as the ssh client's end of the forward: serves HTTP on the -L spec's local end (a Unix socket path, or
# host:port for the round-3 TCP shape) and records, per request, only whether it carried an Authorization header.
_TUNNEL_PROGRAM = '''
import http.server, os, socketserver, sys
local = sys.argv[sys.argv.index("-L") + 1].split(":")
class Handler(http.server.BaseHTTPRequestHandler):
    def _answer(self):
        with open("requests", "a") as f:
            f.write("B\\n" if self.headers.get("Authorization") else "-\\n")
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = b'{"results": []}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    do_GET = do_POST = _answer
    def address_string(self):
        return "local"
    def log_message(self, *args):
        pass
if local[0].startswith("/"):
    os.umask(0o177)  # what StreamLocalBindMask=0177 gives the real tunnel
    server = socketserver.ThreadingUnixStreamServer(local[0], Handler)
else:
    server = http.server.ThreadingHTTPServer((local[0], int(local[1])), Handler)
open("ready", "w").close()
server.serve_forever()
'''


class FakeTunnel:
    def __init__(self, work: Path, forward: str):
        self.work, self.forward = work, forward
        work.mkdir(parents=True)
        (work / "rosie").write_text(_TUNNEL_PROGRAM, encoding="utf-8")
        # The base interpreter: a venv python finds its stdlib through a pyvenv.cfg next to it, which a symlink
        # elsewhere does not have. The program needs only the stdlib.
        (work / "ssh").symlink_to(os.path.realpath(getattr(sys, "_base_executable", sys.executable)))
        self.proc = subprocess.Popen([str(work / "ssh"), "rosie", "-N", "-L", forward], cwd=work,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while not (work / "ready").exists() and self.proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (work / "ready").exists(), "fake tunnel did not start"

    def requests(self) -> list:
        log = self.work / "requests"
        return log.read_text().split() if log.exists() else []

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(timeout=5)


@pytest.fixture
def sock_dir():
    # Short and canonical: sun_path holds 104 bytes on macOS, and the socket path may not pass through a symlink.
    path = os.path.realpath(tempfile.mkdtemp(prefix="smt-", dir=tempfile.gettempdir()))
    os.chmod(path, 0o700)
    try:
        reason, _ = helper.check_socket(os.path.join(path, "probe.sock"), os.getuid())
        if reason == "socket_dir_unsafe":
            pytest.skip("socket integration requires trusted ancestors of TMPDIR")
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def tunnels(tmp_path):
    started = []

    def start(forward: str) -> FakeTunnel:
        started.append(FakeTunnel(tmp_path / f"tunnel-{len(started)}", forward))
        return started[-1]
    yield start
    for tunnel in started:
        tunnel.kill()


class _Counting(http.server.BaseHTTPRequestHandler):
    def _answer(self):
        self.server.seen.append("B" if self.headers.get("Authorization") else "-")
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = b'{"results": []}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    do_GET = do_POST = do_CONNECT = _answer

    def log_message(self, *args):
        pass


@pytest.fixture
def tcp_listener():
    """A TCP listener standing on the forward's old local port; records only whether each request had a bearer."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Counting)
    server.seen = []
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield server
    server.shutdown()
    server.server_close()
    worker.join(timeout=5)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr("pm.ensure_import", lambda *a, **k: None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in [n for n in os.environ if n.startswith("SUPERMEMORY_") or n.lower().endswith("_proxy")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("SUPERMEMORY_API_KEY", KEY)
    monkeypatch.setenv(PROOF_ENV, f"v1:{os.getpid()}:{_key_fingerprint(KEY)}")
    monkeypatch.setattr(sm, "_dropped_key_reason", "", raising=False)
    return tmp_path


@pytest.fixture
def proxies_to(monkeypatch):
    """Point every proxy variable httpx could honour at ``url``: a client that read them would reach the listener."""
    def point(url: str) -> None:
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.setenv(name, url)
    return point


def _provider(home: Path, config: dict) -> SupermemoryMemoryProvider:
    (home / "supermemory.json").write_text(json.dumps(config), encoding="utf-8")
    p = SupermemoryMemoryProvider()
    p.initialize("session-1", hermes_home=str(home), platform="cli", agent_identity="tb_king")
    return p


def _tunnel_config(forward: str, **extra) -> dict:
    return {"require_availability_proof": True, "auto_recall": False, "auto_capture": False,
            "tunnel": {"ssh_host": "rosie", "forward": forward}, **extra}


def _search(p: SupermemoryMemoryProvider) -> dict:
    return json.loads(p.handle_tool_call("supermemory_search", {"query": "synthetic"}))


def _url(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


def test_positive_control_the_tcp_listener_counts_a_real_sdk_request(env, tcp_listener):
    """Without a tunnel the real SDK does reach a TCP base_url, so the zero counts below are not vacuous."""
    p = _provider(env, {"base_url": _url(tcp_listener), "auto_recall": False, "auto_capture": False})
    assert "error" not in _search(p)
    assert tcp_listener.seen == ["B"]


def test_real_sdk_request_goes_only_to_the_tunnel_socket(env, sock_dir, tunnels, tcp_listener, proxies_to):
    proxies_to(_url(tcp_listener))
    tunnel = tunnels(f"{sock_dir}/rosie.sock:127.0.0.1:6768")
    p = _provider(env, _tunnel_config(tunnel.forward))
    assert p._active
    assert "error" not in _search(p) and "error" not in _search(p)
    assert tunnel.requests() == ["B", "B"]
    assert tcp_listener.seen == []


@pytest.mark.parametrize("loss,reason", [("listener_killed_socket_left_stale", "socket_stale"),
                                         ("socket_removed", "socket_missing")])
def test_lost_tunnel_never_falls_back_to_the_old_tcp_port(env, sock_dir, tunnels, tcp_listener, proxies_to, loss, reason):
    """Port of the checker's cached-window reproduction. Round 3: after the tunnel went away, the next use inside the
    re-check interval sent the bearer to a replacement TCP listener. Now the next use is refused and nothing reaches
    TCP; the client and key are dropped."""
    proxies_to(_url(tcp_listener))
    tunnel = tunnels(f"{sock_dir}/rosie.sock:127.0.0.1:6768")
    p = _provider(env, _tunnel_config(tunnel.forward))
    assert "error" not in _search(p)
    tunnel.kill()
    if loss == "socket_removed":
        os.unlink(f"{sock_dir}/rosie.sock")
    out = _search(p)
    assert "disabled" in out["error"] and reason in out["error"]
    assert p._client is None and p._api_key == ""
    assert tunnel.requests() == ["B"]
    assert tcp_listener.seen == []


def test_replaced_socket_is_refused_and_the_replacement_gets_nothing(env, sock_dir, tunnels, tcp_listener):
    """A new listener at the same path, even one whose argv is a proper tunnel, is not the socket this session
    verified: the next use is refused."""
    first = tunnels(f"{sock_dir}/rosie.sock:127.0.0.1:6768")
    p = _provider(env, _tunnel_config(first.forward))
    assert "error" not in _search(p)
    first.kill()
    os.unlink(f"{sock_dir}/rosie.sock")
    second = tunnels(first.forward)
    out = _search(p)
    assert "disabled" in out["error"] and "socket_replaced" in out["error"]
    assert first.requests() == ["B"] and second.requests() == [] and tcp_listener.seen == []


def test_every_request_rechecks_the_socket_inside_the_sdk_transport(env, sock_dir, tunnels):
    """Below the provider: the SDK client itself refuses a replaced socket, so no caller can skip the check."""
    first = tunnels(f"{sock_dir}/rosie.sock:127.0.0.1:6768")
    client = sm._SupermemoryClient(api_key=KEY, timeout=5.0, container_tag="hermes_tb_king",
                                   tunnel={"ssh_host": "rosie", "forward": first.forward})
    assert client.search_memories("synthetic") == []
    first.kill()
    os.unlink(f"{sock_dir}/rosie.sock")
    second = tunnels(first.forward)
    with pytest.raises(Exception) as refused:
        client.search_memories("synthetic")
    causes, exc = [], refused.value
    while exc is not None:  # the SDK wraps the transport's error in its own APIConnectionError
        causes.append(str(exc))
        exc = exc.__cause__ or exc.__context__
    assert any("socket_replaced" in c for c in causes)
    assert first.requests() == ["B"] and second.requests() == []


def test_start_up_verifies_the_tunnel_once_for_the_gate_and_the_client(env, sock_dir, tunnels, monkeypatch):
    """The socket the start-up gate verified is the one the SDK transport pins: one check, one pin, no second
    verification that could land on a different socket."""
    calls = []
    real = helper.verify_tunnel
    monkeypatch.setattr(helper, "verify_tunnel", lambda *a, **k: calls.append(a) or real(*a, **k))
    tunnel = tunnels(f"{sock_dir}/rosie.sock:127.0.0.1:6768")
    p = _provider(env, _tunnel_config(tunnel.forward))
    assert p._active and len(calls) == 1
    assert "error" not in _search(p) and len(calls) == 2  # then once per use


def test_setup_probe_applies_the_same_gate(env, sock_dir, tunnels, tcp_listener, monkeypatch, capsys):
    """`hermes memory setup` must not print "Connected" for a config the next session refuses (a base_url next to a
    tunnel), and must send nothing while saying so."""
    monkeypatch.setattr("hermes_cli.memory_setup._prompt", lambda *a, **k: "")
    monkeypatch.setattr("hermes_cli.config.save_config", lambda cfg: None)
    tunnel = tunnels(f"{sock_dir}/rosie.sock:127.0.0.1:6768")
    (env / "supermemory.json").write_text(json.dumps({**_tunnel_config(tunnel.forward), "base_url": _url(tcp_listener)}))
    SupermemoryMemoryProvider().post_setup(str(env), {"memory": {}})
    out = capsys.readouterr().out
    assert "✓ Connected" not in out and "base_url" in out
    assert tunnel.requests() == [] and tcp_listener.seen == []


def _chmod_dir(sock_dir, monkeypatch):
    os.chmod(sock_dir, 0o755)


def _chmod_socket(sock_dir, monkeypatch):
    os.chmod(f"{sock_dir}/rosie.sock", 0o666)


def _other_uid(sock_dir, monkeypatch):
    # Modeled: the directory's real owner is this test's uid; the provider now runs as another user.
    real = helper.current_uid()
    monkeypatch.setattr(helper, "current_uid", lambda: real + 1)


@pytest.mark.parametrize("breakage,reason", [
    (_chmod_dir, "socket_dir_wrong_mode"),
    (_chmod_socket, "socket_wrong_mode"),
    (_other_uid, "socket_dir_wrong_owner"),
])
def test_unsafe_socket_or_directory_is_refused_before_any_request(env, sock_dir, tunnels, monkeypatch, breakage, reason):
    tunnel = tunnels(f"{sock_dir}/rosie.sock:127.0.0.1:6768")
    live = _provider(env, _tunnel_config(tunnel.forward))
    assert "error" not in _search(live)
    breakage(sock_dir, monkeypatch)
    out = _search(live)                                   # a running session refuses its next use ...
    assert "disabled" in out["error"] and reason in out["error"]
    monkeypatch.setenv("SUPERMEMORY_API_KEY", KEY)
    monkeypatch.setattr(sm, "_dropped_key_reason", "")
    fresh = SupermemoryMemoryProvider()                   # ... and a new one never starts
    assert fresh.is_available() is False and reason in fresh.unavailable_reason()
    assert tunnel.requests() == ["B"]


def test_symlinked_socket_directory_is_refused(env, sock_dir, tunnels, tmp_path):
    tunnel = tunnels(f"{sock_dir}/rosie.sock:127.0.0.1:6768")
    link = Path(tempfile.mkdtemp(prefix="sml-", dir=tempfile.gettempdir()))
    try:
        (link / "d").symlink_to(sock_dir)
        p = _provider(env, _tunnel_config(f"{os.path.realpath(link)}/d/rosie.sock:127.0.0.1:6768"))
        assert p._client is None and "socket_path_invalid" in p.unavailable_reason()
    finally:
        shutil.rmtree(link, ignore_errors=True)
    assert tunnel.requests() == []


def test_a_directory_owned_by_another_user_is_refused(env):
    """Real, not modeled: /usr/bin belongs to root. (A root test run owns it, so only the refusal is asserted.)"""
    (env / "supermemory.json").write_text(json.dumps(_tunnel_config("/usr/bin/rosie.sock:127.0.0.1:6768")))
    p = SupermemoryMemoryProvider()
    assert p.is_available() is False
    if os.getuid() != 0:
        assert "socket_dir_wrong_owner" in p.unavailable_reason()


def test_round3_tcp_tunnel_config_now_fails_closed(env, tunnels, tcp_listener):
    """The round-3 proposed shape: a TCP forward and a TCP base_url, with a real ssh-named process owning the port.
    Round 3 accepted it and the SDK sent the bearer to that port; a tunnel is now a Unix socket or nothing."""
    port = tcp_listener.server_address[1]
    tcp_listener.shutdown()
    tcp_listener.server_close()
    tunnel = tunnels(f"127.0.0.1:{port}:127.0.0.1:6768")
    p = _provider(env, {**_tunnel_config(tunnel.forward), "base_url": f"http://127.0.0.1:{port}"})
    out = _search(p)
    assert tunnel.requests() == [], "the SDK sent a request to the TCP port"
    assert "error" in out and p._client is None and "tunnel" in p.unavailable_reason()


@pytest.mark.parametrize("where", ["config", "env"])
def test_a_tcp_base_url_next_to_a_socket_tunnel_fails_closed(env, sock_dir, tunnels, tcp_listener, monkeypatch, where):
    tunnel = tunnels(f"{sock_dir}/rosie.sock:127.0.0.1:6768")
    config = _tunnel_config(tunnel.forward)
    if where == "config":
        config["base_url"] = _url(tcp_listener)
    else:
        monkeypatch.setenv("SUPERMEMORY_BASE_URL", _url(tcp_listener))
    p = _provider(env, config)
    assert p._client is None and "base_url" in p.unavailable_reason()
    assert "error" in _search(p)
    assert tunnel.requests() == [] and tcp_listener.seen == []
