#!/usr/bin/env python3
"""``secrets.command`` key helper for a Supermemory proxy reached through an SSH forward to a local Unix socket.

Prints KEY=VALUE lines for Hermes's command secret source. Stdlib only and never imports Hermes, so each
Hermes start pays only interpreter start-up plus the probes below.

The tunnel forwards a Unix socket, never a TCP port: ``ssh -N -o StreamLocalBindMask=0177 -o
StreamLocalBindUnlink=yes -L <socket>:<proxy host>:<proxy port> <ssh host>``, with ``<socket>`` in a directory
owned by the Hermes user with mode 0700. Another local user can neither connect to that socket nor put a
listener in its place, and the provider's SDK client has no route but that socket (no TCP base_url, no fallback).

The key never crosses the local socket either. It is read AND used in ONE ssh session to the tunnel's host:
a small program sent on stdin runs there and sends it only to the forward's REMOTE end (the proxy's own
loopback port on that host). ssh authenticates the host by its host key for the whole session.

The local checks carry no credential; they decide AVAILABILITY, because the provider's requests go through the
socket:

1. the socket passes ``check_socket``: every directory above it is a real directory owned by root or this user
   and not writable by others (unless sticky), its own directory is this user's with mode 0700, and it is a
   socket of this user with no group/other permission;
2. the process listening on it (read from a fresh connection's peer credentials) is this user's, and its exact
   argv is an ssh forward of ``--forward`` to ``--ssh-host`` (``is_tunnel_argv``: parsed the way ssh parses it;
   anything that could change the host or how its identity is checked fails closed);
3. unauthenticated ``GET /health`` through the socket answers like the expected proxy (401, Server header
   prefix, JSON ``"error": "Unauthorized"``);
4. over ssh: the key file is read and an authenticated ``GET /health`` to the forward's remote end reports
   the expected service.

None of this protects against this same user or root, who can replace or read anything here, or against a
compromised ssh host. Success prints ``SUPERMEMORY_API_KEY=<key>`` and
``SUPERMEMORY_AVAILABILITY_PROOF=v1:<hermes pid>:<fingerprint>``. Any failure prints only
``SUPERMEMORY_AVAILABILITY_PROOF=down:<hermes pid>:<reason>`` and exits 0: the provider
(``require_availability_proof: true``) then stays inert without a startup warning. Nothing is written to
stderr or disk. While it runs, the provider repeats checks 1-2 through ``verify_tunnel`` before every use and
check 1 before every request (supermemory.json ``tunnel`` block). Config example (``$PPID`` in ``sh -c`` is the
Hermes process)::

    secrets:
      command:
        enabled: true
        override_existing: true
        helper_timeout_seconds: 8
        command: exec /path/to/python -I /path/to/tunnel_key_helper.py --hermes-pid "$PPID" --forward /Users/me/.hermes/supermemory-tunnel/rosie.sock:127.0.0.1:6768 --ssh-host rosie --remote-key-file supermemory-fleet-pilot/data/api-key --server-prefix RosieAuthProxy/ --service supermemory-fleet-pilot
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
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

KEY_ENV = "SUPERMEMORY_API_KEY"
PROOF_ENV = "SUPERMEMORY_AVAILABILITY_PROOF"
_MAX_BODY = 64 * 1024
_MAX_SOCKET_PATH = 103  # sun_path is 104 bytes on macOS (108 on Linux), NUL included

SocketId = Tuple[int, int]  # (st_dev, st_ino) of the socket file a check passed on

# ssh(1) options a tunnel's command line may use: none of them can change which host is reached or how its
# identity is checked. Any other option letter (-F -J -S -O -p -W -g -A -D -R -M ...), and any other -o keyword
# (HostName, ProxyCommand, ProxyJump, ControlPath, StrictHostKeyChecking, UserKnownHostsFile ...), fails closed.
_SSH_FLAGS = frozenset("46CNTafknqvx")
_SSH_VALUE_OPTIONS = frozenset("ELilo")
_SSH_CONFIG_KEYWORDS = frozenset({
    "addressfamily", "batchmode", "compression", "connectionattempts", "connecttimeout", "exitonforwardfailure",
    "identitiesonly", "identityfile", "loglevel", "serveralivecountmax", "serveraliveinterval",
    "streamlocalbindmask", "streamlocalbindunlink", "tcpkeepalive", "user"})

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


def split_forward(forward: str) -> Tuple[str, str, int]:
    """``/absolute/socket/path:remote_host:remote_port`` (ssh -L's local-socket form) -> its three parts. ValueError
    for any other shape, a TCP ``bind:port:host:port`` forward included: the local end is a Unix socket or nothing."""
    parts = forward.split(":")
    if len(parts) != 3 or not all(parts) or not parts[2].isdigit() or not 0 < int(parts[2]) < 65536 \
            or not parts[0].startswith("/") or os.path.normpath(parts[0]) != parts[0] \
            or len(os.fsencode(parts[0])) > _MAX_SOCKET_PATH:
        raise ValueError(f"forward must look like /Users/me/.hermes/supermemory-tunnel/rosie.sock:127.0.0.1:6768, "
                         f"not {forward!r}")
    return parts[0], parts[1], int(parts[2])


def current_uid() -> int:
    """This process's uid; -1 where there is none (Windows): no file owner matches it, so every check fails closed."""
    return os.getuid() if hasattr(os, "getuid") else -1


def check_socket(socket_path: str, uid: int, pinned: Optional[SocketId] = None) -> Tuple[str, Optional[SocketId]]:
    """("", socket id) when ``socket_path`` is a Unix socket that only ``uid`` (and root) can reach or replace, else
    (reason, None). lstat only: no connection, no credential. ``pinned`` = the id an earlier check passed on; any
    other file at the path, even a working one, is ``socket_replaced``. POSIX mode bits only: an ACL granting another
    user access to the directory is not seen here (the setup step checks for one)."""
    directory = os.path.dirname(socket_path)
    try:
        for level, path in enumerate([directory, *map(str, Path(directory).parents)]):
            st = os.lstat(path)
            if not stat.S_ISDIR(st.st_mode):  # a symlink anywhere on the way could be re-pointed
                return "socket_path_invalid", None
            if level == 0 and st.st_uid != uid:
                return "socket_dir_wrong_owner", None
            if level == 0 and stat.S_IMODE(st.st_mode) != 0o700:
                return "socket_dir_wrong_mode", None
            if level > 0 and (st.st_uid not in (0, uid) or st.st_mode & 0o022 and not st.st_mode & stat.S_ISVTX):
                return "socket_dir_unsafe", None  # someone else could rename the directory and put their own there
        st = os.lstat(socket_path)
    except FileNotFoundError:
        return "socket_missing", None
    except OSError:
        return "socket_unreadable", None
    if not stat.S_ISSOCK(st.st_mode):
        return "socket_not_a_socket", None
    if st.st_uid != uid:
        return "socket_wrong_owner", None
    if st.st_mode & 0o077:
        return "socket_wrong_mode", None
    if pinned is not None and (st.st_dev, st.st_ino) != pinned:
        return "socket_replaced", None
    return "", (st.st_dev, st.st_ino)


def _peer_credentials(sock: socket.socket) -> Tuple[int, int]:
    """(pid, uid) of the process at the other end of a connected Unix socket: the one that listens on it."""
    if sys.platform == "darwin":
        pid = sock.getsockopt(0, 0x002)  # SOL_LOCAL, LOCAL_PEERPID
        version, uid = struct.unpack_from("=II", sock.getsockopt(0, 0x001, 128))  # LOCAL_PEERCRED: struct xucred
        if version != 0:  # XUCRED_VERSION
            raise OSError("unexpected xucred version")
        return pid, uid
    pid, uid, _ = struct.unpack("iII", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("iII")))
    return pid, uid  # struct ucred: pid_t pid, uid_t uid, gid_t gid


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._socket_path)


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
        return current_uid()

    def check_socket(self, socket_path: str) -> Tuple[str, Optional[SocketId]]:
        return check_socket(socket_path, self.uid())

    def socket_peer(self, socket_path: str, timeout: float) -> Tuple[int, int]:
        """(pid, uid) of the listener on ``socket_path``, from the peer credentials of a fresh connection that sends
        nothing. Raises OSError when nothing accepts (a stale socket file) or the platform has no peer credentials."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(socket_path)
            return _peer_credentials(sock)

    def command_argv(self, pid: int, timeout: float) -> Optional[List[str]]:
        """Exact argv of ``pid`` (``ps`` joins it with spaces, which is ambiguous); None when unreadable."""
        if sys.platform == "darwin":
            return _darwin_argv(pid)
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return None
        return [os.fsdecode(a) for a in raw.split(b"\0")[:-1]] or None

    def get_health(self, socket_path: str, timeout: float) -> Tuple[int, dict, bytes]:
        """Unauthenticated ``GET /health`` through the forward's socket. It takes no credential by design."""
        conn = _UnixHTTPConnection(socket_path, timeout)
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


def _verify_listener(ops, forward: str, ssh_host: str, deadline: float) -> SocketId:
    """Checks 1-2, credential-free: the forward's socket is safe (``check_socket``) and the process listening on it is
    this user's ssh forwarding it to ``ssh_host``. Returns the socket's id; raises _Down."""
    socket_path = split_forward(forward)[0]
    reason, socket_id = _step("socket_unreadable", lambda: ops.check_socket(socket_path))
    if reason:
        raise _Down(reason)
    peer = _step("socket_stale", lambda: ops.socket_peer(socket_path, _left(deadline, 1.0)))
    if not peer or peer[1] != ops.uid():
        raise _Down("listener_unverified")
    argv = _step("listener_unverified", lambda: ops.command_argv(peer[0], _left(deadline, 2.0)))
    if not is_tunnel_argv(argv or [], forward, ssh_host):
        raise _Down("listener_unverified")
    return socket_id


def verify_tunnel(forward: str, ssh_host: str, budget: float = 3.0) -> Tuple[str, Optional[SocketId]]:
    """("", socket id) while the local end of ``forward`` is this user's ssh forward to ``ssh_host``, else (reason,
    None). The provider's check before every use; sends nothing but an empty connection to the socket."""
    try:
        return "", _verify_listener(SystemOps(), forward, ssh_host, time.monotonic() + budget)
    except _Down as down:
        return down.reason, None
    except ValueError:
        return "forward_invalid", None


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

        status, headers, body = _step("identity_mismatch", lambda: ops.get_health(args.socket_path, _left(deadline, 2.0)))
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
    parser.add_argument("--forward", required=True, help="the tunnel's -L spec, /absolute/socket/path:remote_host:remote_port")
    parser.add_argument("--ssh-host", required=True, help="ssh destination that owns the forward and holds the key file")
    parser.add_argument("--remote-key-file", required=True, help="key file on the ssh host (relative to its $HOME)")
    parser.add_argument("--remote-python", default="python3", help="interpreter on the ssh host that runs the key check")
    parser.add_argument("--server-prefix", default="RosieAuthProxy/", help="expected Server header prefix of the proxy")
    parser.add_argument("--service", default="supermemory-fleet-pilot", help="expected service in authenticated /health")
    parser.add_argument("--budget", type=float, default=7.0, help="total seconds; keep below helper_timeout_seconds")
    args = parser.parse_args(argv)
    try:
        args.socket_path, args.remote_host, args.remote_port = split_forward(args.forward)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    sys.stdout.write("".join(f"{line}\n" for line in resolve(args, SystemOps(remote_python=args.remote_python))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
