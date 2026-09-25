#!/usr/bin/env python3
"""``secrets.command`` key helper for a Supermemory proxy reached through an SSH local forward.

Prints KEY=VALUE lines for Hermes's command secret source. Stdlib only and never imports Hermes, so each
Hermes start pays only interpreter start-up plus the probes below. Order matters: the listener is
authenticated BEFORE any key is read, so a stray local service on the port never triggers key resolution
and never receives the key.

1. the local forward port accepts a connection;
2. its only listener is this user's ``ssh ... -L <forward> <ssh host>`` process (lsof + ps);
3. unauthenticated ``GET /health`` answers like the expected proxy (401, Server header prefix, JSON
   ``"error": "Unauthorized"``);
4. the key is read over SSH (BatchMode, stderr discarded) into memory only;
5. authenticated ``GET /health`` through the forward reports the expected service.

Success prints ``SUPERMEMORY_API_KEY=<key>`` and ``SUPERMEMORY_AVAILABILITY_PROOF=v1:<hermes pid>:<fingerprint>``.
Any failure prints only ``SUPERMEMORY_AVAILABILITY_PROOF=down:<hermes pid>:<reason>`` and exits 0: the provider
(``require_availability_proof: true``) then stays inert without a startup warning. Nothing is written to
stderr or disk. Config example (``$PPID`` in ``sh -c`` is the Hermes process)::

    secrets:
      command:
        enabled: true
        override_existing: true
        helper_timeout_seconds: 8
        command: exec /path/to/python /path/to/tunnel_key_helper.py --hermes-pid "$PPID" --forward 127.0.0.1:16768:127.0.0.1:6768 --ssh-host rosie --remote-key-file supermemory-fleet-pilot/data/api-key --server-prefix RosieAuthProxy/ --service supermemory-fleet-pilot
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from typing import List, Optional, Tuple

KEY_ENV = "SUPERMEMORY_API_KEY"
PROOF_ENV = "SUPERMEMORY_AVAILABILITY_PROOF"
_MAX_BODY = 64 * 1024


def key_fingerprint(api_key: str) -> str:
    """First 16 hex chars of sha256(key); must match the provider's ``_key_fingerprint``."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def _tool(name: str, fallback: str) -> str:
    return shutil.which(name) or fallback


def _run_bounded(argv: List[str], timeout: float) -> Tuple[int, bytes]:
    """(returncode, stdout) of ``argv``; stderr discarded. It runs in its own process group so a timeout kills
    the child's children too (an ssh ProxyCommand, say), not only the direct child. Raises TimeoutExpired."""
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    try:
        stdout, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        try:
            proc.communicate(timeout=1.0)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            pass
        raise
    return proc.returncode, stdout


class SystemOps:
    """The real probes. Every method raises or returns a falsy value on failure; ``resolve`` maps both to down."""

    def uid(self) -> int:
        return os.getuid()

    def port_open(self, host: str, port: int, timeout: float) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def listeners(self, port: int, timeout: float) -> List[Tuple[int, int]]:
        """(pid, uid) of every process listening on TCP ``port`` on any address."""
        _, out = _run_bounded([_tool("lsof", "/usr/sbin/lsof"), "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpu"], timeout)
        entries, pid = [], None
        for line in out.decode("utf-8", errors="replace").splitlines():
            if line.startswith("p") and line[1:].isdigit():
                pid = int(line[1:])
            elif line.startswith("u") and line[1:].isdigit() and pid is not None:
                entries.append((pid, int(line[1:])))
                pid = None
        return entries

    def command_line(self, pid: int, timeout: float) -> str:
        _, out = _run_bounded([_tool("ps", "/bin/ps"), "-o", "command=", "-p", str(pid)], timeout)
        return out.decode("utf-8", errors="replace").strip()

    def get_health(self, host: str, port: int, timeout: float, bearer: Optional[str] = None) -> Tuple[int, dict, bytes]:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            conn.request("GET", "/health", headers={"Authorization": f"Bearer {bearer}"} if bearer else {})
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read(_MAX_BODY)
        finally:
            conn.close()

    def fetch_key(self, ssh_host: str, remote_path: str, timeout: float) -> Optional[str]:
        path = remote_path[2:] if remote_path.startswith("~/") else remote_path  # remote commands start in $HOME
        code, out = _run_bounded([_tool("ssh", "/usr/bin/ssh"), "-o", "BatchMode=yes",
                                  "-o", f"ConnectTimeout={max(1, int(timeout))}", ssh_host, f"cat -- {shlex.quote(path)}"],
                                 timeout)
        return out.decode("utf-8", errors="replace").strip() if code == 0 else None


class _Down(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _left(deadline: float, cap: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _Down("budget_exhausted")
    return min(cap, remaining)


def _step(reason: str, fn):
    """Run one probe; any exception (missing tool, timeout, socket error) is the step's down reason."""
    try:
        return fn()
    except _Down:
        raise
    except Exception:
        raise _Down(reason) from None


def _is_tunnel_argv(command: str, forward: str, ssh_host: str) -> bool:
    argv = command.split()
    if not argv or os.path.basename(argv[0]) != "ssh":
        return False
    has_forward = any((a == "-L" and i + 1 < len(argv) and argv[i + 1] == forward) or a == f"-L{forward}"
                      for i, a in enumerate(argv))
    return has_forward and any(a == ssh_host or a.endswith(f"@{ssh_host}") for a in argv[1:])


def _json_field(body: bytes, field: str) -> Optional[str]:
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    return data.get(field) if isinstance(data, dict) else None


def _plausible_key(key: Optional[str]) -> bool:
    return bool(key) and 8 <= len(key) <= 512 and all(33 <= ord(c) <= 126 for c in key)


def resolve(args: argparse.Namespace, ops) -> List[str]:
    """The helper's stdout lines. Never raises; never includes the key unless every check passed."""
    pid = args.hermes_pid
    deadline = time.monotonic() + args.budget
    bind_host, port = args.bind_host, args.port
    try:
        if not _step("port_closed", lambda: ops.port_open(bind_host, port, _left(deadline, 1.0))):
            raise _Down("port_closed")

        entries = _step("listener_unverified", lambda: ops.listeners(port, _left(deadline, 2.0)))
        if len({p for p, _ in entries}) != 1 or entries[0][1] != ops.uid():
            raise _Down("listener_unverified")
        command = _step("listener_unverified", lambda: ops.command_line(entries[0][0], _left(deadline, 2.0)))
        if not _is_tunnel_argv(command, args.forward, args.ssh_host):
            raise _Down("listener_unverified")

        status, headers, body = _step("identity_mismatch", lambda: ops.get_health(bind_host, port, _left(deadline, 2.0)))
        if status != 401 or not headers.get("server", "").startswith(args.server_prefix) \
                or _json_field(body, "error") != "Unauthorized":
            raise _Down("identity_mismatch")

        key = _step("key_fetch_failed", lambda: ops.fetch_key(args.ssh_host, args.remote_key_file, _left(deadline, 5.0)))
        if not _plausible_key(key):
            raise _Down("key_fetch_failed")

        status, _, body = _step("auth_health_failed",
                                lambda: ops.get_health(bind_host, port, _left(deadline, 3.0), bearer=key))
        if status != 200 or _json_field(body, "service") != args.service:
            raise _Down("auth_health_failed")
    except _Down as down:
        return [f"{PROOF_ENV}=down:{pid}:{down.reason}"]
    return [f"{KEY_ENV}={key}", f"{PROOF_ENV}=v1:{pid}:{key_fingerprint(key)}"]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hermes-pid", type=int, default=os.getppid(),
                        help='pid the proof is issued to; pass "$PPID" from the secrets.command shell')
    parser.add_argument("--forward", required=True, help="the tunnel's -L spec, bind_host:port:remote_host:remote_port")
    parser.add_argument("--ssh-host", required=True, help="ssh destination that owns the forward and holds the key file")
    parser.add_argument("--remote-key-file", required=True, help="key file on the ssh host (relative to its $HOME)")
    parser.add_argument("--server-prefix", default="RosieAuthProxy/", help="expected Server header prefix of the proxy")
    parser.add_argument("--service", default="supermemory-fleet-pilot", help="expected service in authenticated /health")
    parser.add_argument("--budget", type=float, default=7.0, help="total seconds; keep below helper_timeout_seconds")
    args = parser.parse_args(argv)
    bind_host, port, *_ = args.forward.split(":") + ["", ""]
    if not port.isdigit():
        parser.error("--forward must look like 127.0.0.1:16768:127.0.0.1:6768")
    args.bind_host, args.port = bind_host or "127.0.0.1", int(port)
    return args


def main(argv: Optional[List[str]] = None) -> int:
    sys.stdout.write("".join(f"{line}\n" for line in resolve(parse_args(argv), SystemOps())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
