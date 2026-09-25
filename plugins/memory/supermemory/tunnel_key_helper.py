#!/usr/bin/env python3
"""``secrets.command`` key helper for a Supermemory proxy reached through an SSH local forward.

Prints KEY=VALUE lines for Hermes's command secret source. Stdlib only and never imports Hermes, so each
Hermes start pays only interpreter start-up plus the probes below.

The key never crosses the local forward port. It is read AND used in ONE ssh session to the tunnel's host:
a small program sent on stdin runs there and sends it only to the forward's REMOTE end (the proxy's own
loopback port on that host). ssh authenticates the host by its host key for the whole session, so the
credential-bearing request is bound to the verified peer from start to finish. Whatever owns the local port,
now or a moment later, never receives the key from this helper.

The local checks carry no credential; they decide AVAILABILITY, because the provider's ``base_url`` is the
local forward:

1. the local forward port accepts a connection;
2. its only listener is a process of this user whose exact argv is an ssh forward of ``--forward`` to
   ``--ssh-host`` (``is_tunnel_argv``: parsed the way ssh parses it; anything that could change the host or
   how its identity is checked fails closed);
3. unauthenticated ``GET /health`` through the forward answers like the expected proxy (401, Server header
   prefix, JSON ``"error": "Unauthorized"``);
4. over ssh: the key file is read and an authenticated ``GET /health`` to the forward's remote end reports
   the expected service.

Success prints ``SUPERMEMORY_API_KEY=<key>`` and ``SUPERMEMORY_AVAILABILITY_PROOF=v1:<hermes pid>:<fingerprint>``.
Any failure prints only ``SUPERMEMORY_AVAILABILITY_PROOF=down:<hermes pid>:<reason>`` and exits 0: the provider
(``require_availability_proof: true``) then stays inert without a startup warning. Nothing is written to
stderr or disk. While it runs, the provider repeats checks 1-2 through ``tunnel_down_reason`` (supermemory.json
``tunnel`` block). Config example (``$PPID`` in ``sh -c`` is the Hermes process)::

    secrets:
      command:
        enabled: true
        override_existing: true
        helper_timeout_seconds: 8
        command: exec /path/to/python -I /path/to/tunnel_key_helper.py --hermes-pid "$PPID" --forward 127.0.0.1:16768:127.0.0.1:6768 --ssh-host rosie --remote-key-file supermemory-fleet-pilot/data/api-key --server-prefix RosieAuthProxy/ --service supermemory-fleet-pilot
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

KEY_ENV = "SUPERMEMORY_API_KEY"
PROOF_ENV = "SUPERMEMORY_AVAILABILITY_PROOF"
_MAX_BODY = 64 * 1024

# ssh(1) options a tunnel's command line may use: none of them can change which host is reached or how its
# identity is checked. Any other option letter (-F -J -S -O -p -W -g -A -D -R -M ...), and any other -o keyword
# (HostName, ProxyCommand, ProxyJump, ControlPath, StrictHostKeyChecking, UserKnownHostsFile ...), fails closed.
_SSH_FLAGS = frozenset("46CNTafknqvx")
_SSH_VALUE_OPTIONS = frozenset("ELilo")
_SSH_CONFIG_KEYWORDS = frozenset({
    "addressfamily", "batchmode", "compression", "connectionattempts", "connecttimeout", "exitonforwardfailure",
    "identitiesonly", "identityfile", "loglevel", "serveralivecountmax", "serveraliveinterval", "tcpkeepalive", "user"})

# Runs on the ssh host as ``python3 -I -`` (this text on stdin): reads the key file and sends the key only to the
# proxy's loopback port there. Prints one JSON line; failures leave "key" empty or "status" 0.
_REMOTE_PROGRAM = r'''
import http.client, json, os, sys
path, host, port, timeout = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
reply = {"key": None, "status": 0, "body": ""}
try:
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        reply["key"] = f.read().strip()
except (OSError, ValueError):
    pass
if reply["key"]:
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", "/health", headers={"Authorization": "Bearer " + reply["key"]})
        resp = conn.getresponse()
        reply["status"], reply["body"] = resp.status, resp.read(65536).decode("utf-8", "replace")
    except (OSError, ValueError, http.client.HTTPException):
        pass
    finally:
        conn.close()
sys.stdout.write(json.dumps(reply) + "\n")
'''


def key_fingerprint(api_key: str) -> str:
    """First 16 hex chars of sha256(key); must match the provider's ``_key_fingerprint``."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def split_forward(forward: str) -> Tuple[str, int, str, int]:
    """``bind_host:port:remote_host:remote_port`` -> its four parts; ValueError for any other shape."""
    parts = forward.split(":")
    if len(parts) != 4 or not all(parts) or not (parts[1].isdigit() and parts[3].isdigit()):
        raise ValueError(f"forward must look like 127.0.0.1:16768:127.0.0.1:6768, not {forward!r}")
    return parts[0], int(parts[1]), parts[2], int(parts[3])


def _ssh_options(args: List[str], i: int, forwards: List[str]) -> Tuple[Optional[int], bool]:
    """Consume ssh options from ``args[i:]`` like getopt, collecting ``-L`` values. Returns (index of the first
    operand, whether ``--`` ended the options); index None when an option is unsupported or lacks its value."""
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return i + 1, True
        if len(arg) < 2 or arg[0] != "-":
            return i, False
        for j in range(1, len(arg)):
            letter = arg[j]
            if letter in _SSH_FLAGS:
                continue
            if letter not in _SSH_VALUE_OPTIONS:
                return None, False
            if j + 1 < len(arg):
                value = arg[j + 1:]
            elif i + 1 < len(args):
                i += 1
                value = args[i]
            else:
                return None, False
            if letter == "L":
                forwards.append(value)
            elif letter == "o":
                keyword = re.match(r"\s*([A-Za-z]*)", value).group(1).lower()
                if keyword not in _SSH_CONFIG_KEYWORDS:
                    return None, False
            break
        i += 1
    return i, False


def parse_ssh_argv(argv: List[str]) -> Optional[Tuple[str, List[str]]]:
    """(destination host, ``-L`` specs) of a forward-only ssh command line, else None.

    Mirrors ssh's own parsing: options, then the destination (the first operand), then options again (ssh
    re-parses after the destination unless ``--`` ended them); whatever remains is a remote command, which a
    tunnel does not have. ``[user@]host`` only; the ``ssh://`` form, unknown or redirecting options and missing
    option values are unsupported, so they fail closed."""
    if not argv or os.path.basename(argv[0]) != "ssh":
        return None
    args, forwards = argv[1:], []
    i, terminated = _ssh_options(args, 0, forwards)
    if i is None or i >= len(args):
        return None
    destination, rest = args[i], i + 1
    if not terminated:
        rest, _ = _ssh_options(args, rest, forwards)
        if rest is None:
            return None
    host = destination.rpartition("@")[2]
    if rest < len(args) or not host or "://" in destination or "/" in host or host.startswith("-"):
        return None
    return host, forwards


def is_tunnel_argv(argv: List[str], forward: str, ssh_host: str) -> bool:
    parsed = parse_ssh_argv(argv)
    return parsed is not None and parsed[0] == ssh_host and forward in parsed[1]


def _tool(name: str, fallback: str) -> str:
    return shutil.which(name) or fallback


def _run_bounded(argv: List[str], timeout: float, stdin_data: Optional[bytes] = None) -> Tuple[int, bytes]:
    """(returncode, stdout) of ``argv``; stderr discarded. It runs in its own process group so a timeout kills
    the child's children too (an ssh ProxyCommand, say), not only the direct child. Raises TimeoutExpired."""
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL if stdin_data is None else subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        stdout, _ = proc.communicate(input=stdin_data, timeout=timeout)
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


def _darwin_argv(pid: int) -> Optional[List[str]]:
    """argv from sysctl KERN_PROCARGS2: int argc, exec path, NUL padding, then argc NUL-terminated strings."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    argmax, size = ctypes.c_int(0), ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
    if libc.sysctl((ctypes.c_int * 2)(1, 8), 2, ctypes.byref(argmax), ctypes.byref(size), None, 0) != 0:  # KERN_ARGMAX
        return None
    buf, size = ctypes.create_string_buffer(argmax.value), ctypes.c_size_t(argmax.value)
    if libc.sysctl((ctypes.c_int * 3)(1, 49, pid), 3, buf, ctypes.byref(size), None, 0) != 0:  # KERN_PROCARGS2
        return None
    data = buf.raw[:size.value]
    argc, rest = int.from_bytes(data[:4], sys.byteorder), data[4:]
    start = rest.find(b"\0")
    while 0 <= start < len(rest) and rest[start] == 0:
        start += 1
    args = rest[start:].split(b"\0")[:argc] if start > 0 else []
    return [os.fsdecode(a) for a in args] if argc > 0 and len(args) == argc else None


class SystemOps:
    """The real probes. Every method raises or returns a falsy value on failure; callers map both to down."""

    def __init__(self, remote_python: str = "python3"):
        self.remote_python = remote_python

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

    def command_argv(self, pid: int, timeout: float) -> Optional[List[str]]:
        """Exact argv of ``pid`` (``ps`` joins it with spaces, which is ambiguous); None when unreadable."""
        if sys.platform == "darwin":
            return _darwin_argv(pid)
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return None
        return [os.fsdecode(a) for a in raw.split(b"\0")[:-1]] or None

    def get_health(self, host: str, port: int, timeout: float) -> Tuple[int, dict, bytes]:
        """Unauthenticated ``GET /health`` through the local forward. It takes no credential by design."""
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            conn.request("GET", "/health")
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read(_MAX_BODY)
        finally:
            conn.close()

    def fetch_key_and_health(self, ssh_host: str, key_path: str, proxy_host: str, proxy_port: int,
                             timeout: float) -> Optional[Tuple[Optional[str], int, bytes]]:
        """(key, status, body) from ONE ssh session in which the host reads the key file and makes the authenticated
        health request to its own ``proxy_host:proxy_port``. Forwards from ssh config are cleared, so this session
        opens no port. None when ssh fails."""
        remote = " ".join(shlex.quote(a) for a in (self.remote_python, "-I", "-", key_path, proxy_host, str(proxy_port),
                                                   f"{max(0.5, timeout - 1.0):.1f}"))
        code, out = _run_bounded([_tool("ssh", "/usr/bin/ssh"), "-T", "-o", "BatchMode=yes", "-o", "ClearAllForwardings=yes",
                                  "-o", f"ConnectTimeout={max(1, int(timeout))}", ssh_host, remote],
                                 timeout, stdin_data=_REMOTE_PROGRAM.encode("utf-8"))
        lines = out.decode("utf-8", errors="replace").strip().splitlines()
        if code != 0 or not lines:
            return None
        reply = json.loads(lines[-1])  # a login banner on stdout may precede it
        return reply.get("key"), int(reply.get("status") or 0), str(reply.get("body") or "").encode("utf-8")


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
    """Run one probe; any exception (missing tool, timeout, socket error, bad reply) is the step's down reason."""
    try:
        return fn()
    except _Down:
        raise
    except Exception:
        raise _Down(reason) from None


def _verify_listener(ops, forward: str, ssh_host: str, deadline: float) -> None:
    """Checks 1-2, credential-free: the forward's local port is open and its only listener is this user's ssh
    forwarding it to ``ssh_host``. Raises _Down."""
    bind_host, port, _, _ = split_forward(forward)
    if not _step("port_closed", lambda: ops.port_open(bind_host, port, _left(deadline, 1.0))):
        raise _Down("port_closed")
    entries = _step("listener_unverified", lambda: ops.listeners(port, _left(deadline, 2.0)))
    if len({p for p, _ in entries}) != 1 or entries[0][1] != ops.uid():
        raise _Down("listener_unverified")
    argv = _step("listener_unverified", lambda: ops.command_argv(entries[0][0], _left(deadline, 2.0)))
    if not is_tunnel_argv(argv or [], forward, ssh_host):
        raise _Down("listener_unverified")


def tunnel_down_reason(forward: str, ssh_host: str, budget: float = 3.0) -> str:
    """"" while the local end of ``forward`` is still this user's ssh forward to ``ssh_host``, else the down reason.
    The provider's re-check while it runs; sends nothing but a TCP connect to the port."""
    try:
        _verify_listener(SystemOps(), forward, ssh_host, time.monotonic() + budget)
    except _Down as down:
        return down.reason
    return ""


def _json_field(body: bytes, field: str) -> Optional[str]:
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    return data.get(field) if isinstance(data, dict) else None


def _plausible_key(key: Optional[str]) -> bool:
    return isinstance(key, str) and 8 <= len(key) <= 512 and all(33 <= ord(c) <= 126 for c in key)


def resolve(args: argparse.Namespace, ops) -> List[str]:
    """The helper's stdout lines. Never raises; never includes the key unless every check passed."""
    pid = args.hermes_pid
    deadline = time.monotonic() + args.budget
    try:
        _verify_listener(ops, args.forward, args.ssh_host, deadline)

        status, headers, body = _step("identity_mismatch", lambda: ops.get_health(args.bind_host, args.port, _left(deadline, 2.0)))
        if status != 401 or not headers.get("server", "").startswith(args.server_prefix) \
                or _json_field(body, "error") != "Unauthorized":
            raise _Down("identity_mismatch")

        fetched = _step("key_fetch_failed", lambda: ops.fetch_key_and_health(
            args.ssh_host, args.remote_key_file, args.remote_host, args.remote_port, _left(deadline, 5.0)))
        key, status, body = fetched or (None, 0, b"")
        if not _plausible_key(key):
            raise _Down("key_fetch_failed")
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
    parser.add_argument("--remote-python", default="python3", help="interpreter on the ssh host that runs the key check")
    parser.add_argument("--server-prefix", default="RosieAuthProxy/", help="expected Server header prefix of the proxy")
    parser.add_argument("--service", default="supermemory-fleet-pilot", help="expected service in authenticated /health")
    parser.add_argument("--budget", type=float, default=7.0, help="total seconds; keep below helper_timeout_seconds")
    args = parser.parse_args(argv)
    try:
        args.bind_host, args.port, args.remote_host, args.remote_port = split_forward(args.forward)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    sys.stdout.write("".join(f"{line}\n" for line in resolve(args, SystemOps(remote_python=args.remote_python))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
