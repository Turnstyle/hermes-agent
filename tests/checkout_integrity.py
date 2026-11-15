"""Checkout write tripwire, not an OS sandbox.

One audit hook fences Python writes during collection/tests. Git's stat-cached
session-boundary check detects persistent tracked child/native damage, reporting
all collected test IDs (the stock runner runs one file per process). It cannot
see transient restored writes, assume-unchanged paths, or native untracked files.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

_GIT = shutil.which("git")
_REAL_POPEN = subprocess.Popen
_GUARDS = []
_CHECKING = threading.local()
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def _fd_path(fd):
    if sys.platform == "darwin":
        import fcntl
        return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0])
    return os.readlink(f"/proc/self/fd/{fd}")


class CheckoutGuard:
    def __init__(self, root, allowed=()):
        self.root = os.path.normcase(os.path.realpath(root))
        self.allowed = tuple(os.path.normcase(os.path.realpath(p)) for p in allowed)
        self.active = False

    def check(self, value, dir_fd=None, *, ancestors=False):
        if value is None:
            return
        if isinstance(value, int):
            try:
                value = _fd_path(value)
            except OSError:
                return  # Pipes/sockets have no filesystem target (e.g. subprocess stdin).
        path = os.fsdecode(value)
        if dir_fd not in (None, -1) and not os.path.isabs(path):
            path = os.path.join(_fd_path(dir_fd), path)
        absolute = os.path.normcase(os.path.abspath(path))
        resolved = os.path.normcase(os.path.realpath(absolute))
        for candidate in (absolute, resolved):
            # Only explicit pytest artefacts/temp roots are exempt, not all ignored files.
            if "__pycache__" in Path(candidate).parts or any(
                candidate == p or candidate.startswith(p + os.sep) for p in self.allowed
            ):
                continue
            if (candidate == self.root or candidate.startswith(self.root + os.sep)
                    or (ancestors and self.root.startswith(candidate.rstrip(os.sep) + os.sep))):
                pytest.fail(f"TEST BUG: write against repository checkout: {value}\nWrites must go to tmp_path.")

    def start(self):
        if self in _GUARDS:
            return self
        self.patcher = pytest.MonkeyPatch()
        original_open, original_connect = os.open, sqlite3.connect

        def open_at(path, flags, *args, dir_fd=None, **kwargs):
            # Python's open audit event omits dir_fd; no descriptor table needed.
            previous = getattr(_CHECKING, "open_dir_fd", None)
            _CHECKING.open_dir_fd = dir_fd
            try:
                return original_open(path, flags, *args, dir_fd=dir_fd, **kwargs)
            finally:
                _CHECKING.open_dir_fd = previous

        def connect(database, *args, **kwargs):
            # sqlite's audit event omits uri; retain it only across this call.
            previous = getattr(_CHECKING, "sqlite_uri", False)
            _CHECKING.sqlite_uri = args[6] if len(args) > 6 else kwargs.get("uri", False)
            try:
                return original_connect(database, *args, **kwargs)
            finally:
                _CHECKING.sqlite_uri = previous

        self.patcher.setattr(os, "open", open_at)
        self.patcher.setattr(sqlite3, "connect", connect)
        _GUARDS.append(self)
        self.active = True
        return self

    def stop(self):
        self.active = False
        if self in _GUARDS:
            _GUARDS.remove(self)
            self.patcher.undo()


def _audit(event, args):
    if not _GUARDS or getattr(_CHECKING, "active", False):
        return
    targets = []
    ancestors = event in ("os.rename", "os.remove", "os.rmdir", "shutil.rmtree")
    if event == "open":
        if not args[2] & _WRITE_FLAGS:
            return
        targets = [(args[0], getattr(_CHECKING, "open_dir_fd", None))]
    elif event == "os.rename":
        targets = [(args[0], args[2]), (args[1], args[3])]
    elif event in ("os.remove", "os.rmdir", "shutil.rmtree"):
        targets = [(args[0], args[1])]
    elif event in ("os.mkdir", "os.chmod", "os.utime"):
        targets = [(args[0], args[-1])]
    elif event == "os.truncate":
        targets = [(args[0], None)]
    elif event in ("os.symlink", "os.link"):
        targets = [(args[1], args[2] if event == "os.symlink" else args[3])]
    elif event == "sqlite3.connect":
        raw = os.fsdecode(args[0])
        if raw not in ("", ":memory:"):
            if raw.startswith("file:") and getattr(_CHECKING, "sqlite_uri", False):
                uri = urlsplit(raw)
                modes = parse_qs(uri.query).get("mode", [])
                if modes == ["ro"] or unquote(uri.path) == ":memory:":
                    return
                raw = unquote(uri.path)
            targets = [(raw, None)]
    else:
        return
    _CHECKING.active = True
    try:
        for guard in tuple(_GUARDS):
            if guard.active:
                for value, directory in targets:
                    guard.check(value, directory, ancestors=ancestors)
    finally:
        _CHECKING.active = False


sys.addaudithook(_audit)


def snapshot_tracked_paths(root):
    env = {k: v for k, v in os.environ.items()
           if k in ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_COUNT="0", GIT_TERMINAL_PROMPT="0")
    if not _GIT:
        raise RuntimeError("git is required for checkout integrity")
    proc = _REAL_POPEN([_GIT, "--no-optional-locks", "-c", "core.fsmonitor=false",
                       "-c", "core.filemode=true", "diff", "--name-only", "-z",
                       "--no-ext-diff", "--no-textconv", "HEAD", "--"],
                      cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate()
    if proc.returncode:
        raise RuntimeError(f"git checkout check failed: {os.fsdecode(err)}")
    return set(os.fsdecode(p) for p in out.split(b"\0") if p)


def pytest_sessionstart(session):
    start_checkout_guard(session, session.config.rootpath)


def start_checkout_guard(session, root):
    config = session.config
    if hasattr(config, "_checkout_integrity"):
        return
    allowed = [os.environ.get("TMPDIR"), config.option.basetemp,
               str(config.cache._cachedir) if getattr(config, "cache", None) else None]
    guard = CheckoutGuard(root, [p for p in allowed if p])
    config._checkout_integrity = (Path(root), snapshot_tracked_paths(root), guard)
    guard.start()


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    root, baseline, guard = session.config._checkout_integrity
    guard.stop()  # BEFORE cacheprovider, junitxml, coverage and other session writers.
    try:
        yield
    finally:
        changed = sorted(snapshot_tracked_paths(root) - baseline)
        if changed:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
            reporter = session.config.pluginmanager.get_plugin("terminalreporter")
            if reporter:
                reporter.write_sep("=", "tracked checkout integrity FAILED", red=True)
                reporter.write_line("Changed paths: " + ", ".join(changed), red=True)
                reporter.write_line("Session test IDs (collection/session damage may precede tests):", red=True)
                for item in session.items:
                    reporter.write_line(item.nodeid, red=True)


def pytest_unconfigure(config):
    state = getattr(config, "_checkout_integrity", None)
    if state:
        state[2].stop()
