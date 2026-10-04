"""Report Bot Chat turn locks that have been flock-held suspiciously long (never kills anything).

``python -m tools.bot_relay_lock_watchdog [--root DIR] [--max-age-minutes 15] [--json]``

A turn lock is held for the length of one bot turn, so a lock held for many minutes means a wedged
holder (the 2026-10-03 incident: a child hung 10h in interpreter finalization while queued DMs
expired). Held-ness comes from a non-blocking flock probe, never from the sidecar alone; the sidecar
(``<profile>.lock.holder.json``) only supplies the holder pid and start time. Exit status: 1 when any
stale lock was found, else 0.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_MAX_AGE_MINUTES = 15.0


def default_root() -> Path:
    from tools.bot_mode_probe import _default_home, _hermes_root
    return _hermes_root(Path(_default_home()))


def _flock_held(path: Path) -> bool:
    import fcntl
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def command_line(pid: int) -> str:
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.strip()


def _read_holder(lock: Path) -> dict[str, Any]:
    from tools.bot_relay import lock_holder_path
    try:
        data = json.loads(lock_holder_path(lock).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def find_stale_locks(root: Path | str, max_age_seconds: float, *, now: Callable[[], float] = time.time,
                     cmdline: Callable[[int], str] = command_line) -> list[dict[str, Any]]:
    """Every flock-held lock older than ``max_age_seconds``. Age comes from the sidecar's ``since``
    (only when its pid is still alive), else the lock file's mtime."""
    from tools.bot_relay import LOCKS_DIR, relay_root
    locks_dir = relay_root(root) / LOCKS_DIR
    try:
        locks = sorted(locks_dir.glob("*.lock"))
    except OSError:
        return []
    current = now()
    found = []
    for lock in locks:
        if not _flock_held(lock):
            continue
        holder = _read_holder(lock)
        pid = holder.get("pid") if isinstance(holder.get("pid"), int) else None
        alive = pid is not None and _pid_alive(pid)
        since = holder.get("since") if alive and isinstance(holder.get("since"), (int, float)) else None
        if since is None:
            with contextlib.suppress(OSError):
                since = lock.stat().st_mtime
        if since is None or current - since < max_age_seconds:
            continue
        found.append({
            "profile": lock.name[:-len(".lock")], "lock": str(lock), "pid": pid if alive else None,
            "age_seconds": round(current - since, 1), "command": cmdline(pid) if alive and pid else "",
        })
    return found


def _format(row: dict[str, Any]) -> str:
    holder = f"pid {row['pid']} ({row['command'] or 'command unknown'})" if row["pid"] else "holder unknown"
    return f"{row['profile']}: turn lock held {row['age_seconds'] / 60:.0f} min by {holder} [{row['lock']}]"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.bot_relay_lock_watchdog", description=__doc__.splitlines()[0])
    parser.add_argument("--root", help="Hermes root holding bot_relay/ (default: the active Hermes root)")
    parser.add_argument("--max-age-minutes", type=float, default=DEFAULT_MAX_AGE_MINUTES)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    stale = find_stale_locks(args.root or default_root(), args.max_age_minutes * 60)
    if args.json:
        print(json.dumps(stale, indent=2))
    else:
        for row in stale:
            print(_format(row))
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
