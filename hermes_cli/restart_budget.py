"""Intentional gateway restart rate limit (one per Hermes home per hour).

State lives at ``<HERMES_HOME>/gateway/restart_budget.json``. Check and claim are one
step under a process-shared ``fcntl.flock`` on the sidecar lock file
``gateway/restart_budget.lock`` plus an in-process lock (BSD flock does not exclude
threads). The claim is recorded when the slot is taken and rolled back unless the
caller keeps it after a post-check observes a new live gateway PID (see
``hermes_cli.gateway_restart_observe``). ``RestartSignal`` from a backend is only a
hint for messaging; it does not keep the hour. Crash recovery, a wedged live
event loop, a health-check-failed runtime state, and ``--force`` bypass the window.
Non-finite, negative, or far-future timestamps and unreadable files count as no
prior restart.
"""

from __future__ import annotations

import enum
import json
import logging
import math
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

RESTART_BUDGET_SECONDS = 60 * 60
CRASH_RECOVERY_BYPASS = "crash_recovery"


class RestartSignal(enum.Enum):
    """What a backend actually did with a restart request.

    These values classify the supervisor outcome for CLI messaging and for
    deciding that a backend handled the verb. They do not keep the hourly
    restart budget; only an observed new live PID does. ``NOOP`` means the
    helper returned without asking any supervisor to restart. ``REJECTED``
    means the supervisor refused (start-limit, every service call failed).
    Members stay truthy so existing "this backend handled the verb" checks
    still skip the host fallback.
    """

    INITIATED = "initiated"
    NOOP = "noop"
    REJECTED = "rejected"


def restart_signal_was_initiated(outcome) -> bool:
    """True when ``outcome`` reports that a restart was actually started.

    ``None`` and ``True`` are the legacy "helper returned normally / dispatched"
    results. An explicit noop or rejection is not initiated. ``False`` means the
    helper did not handle the verb at all.
    """
    if outcome is False or outcome is RestartSignal.NOOP or outcome is RestartSignal.REJECTED:
        return False
    return True
FUTURE_TIMESTAMP_SLACK_SECONDS = 120
_HEALTH_FAILED = frozenset({
    "failed",
    "unhealthy",
    "health-check-failed",
    "health_check_failed",
})

_THREAD_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


def restart_budget_path(home: Optional[Path] = None) -> Path:
    from hermes_constants import get_hermes_home

    root = home if home is not None else get_hermes_home()
    return Path(root) / "gateway" / "restart_budget.json"


def restart_budget_lock_path(home: Optional[Path] = None) -> Path:
    return restart_budget_path(home).with_name("restart_budget.lock")


def _thread_lock(lock_path: Path) -> threading.RLock:
    key = str(lock_path.resolve(strict=False))
    with _THREAD_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _budget_lock(home: Optional[Path]):
    """In-process RLock plus an exclusive flock on the sidecar lock file."""
    lock_path = restart_budget_lock_path(home)
    from hermes_constants import mkdir_under_hermes_home

    mkdir_under_hermes_home(lock_path.parent)
    thread_lock = _thread_lock(lock_path)
    with thread_lock:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:  # pragma: no cover - Windows
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                else:  # pragma: no cover - Windows
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)


def _warn_ignored(path: Path, reason: str) -> None:
    logger.warning(
        "Restart budget file %s ignored (%s); treating as no prior restart",
        path,
        reason,
    )


def _parse_json_constant(token: str) -> float:
    return float(token)


def _load_last_restart_unix(path: Path, *, now: float, warn: bool = True) -> Optional[float]:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        if warn:
            _warn_ignored(path, f"unreadable: {exc}")
        return None
    try:
        data = json.loads(text, parse_constant=_parse_json_constant)
    except ValueError as exc:
        if warn:
            _warn_ignored(path, f"unreadable: {exc}")
        return None
    if not isinstance(data, dict):
        if warn:
            _warn_ignored(path, "unreadable: not a JSON object")
        return None
    ts = data.get("last_restart_unix")
    if ts is None:
        return None
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        if warn:
            _warn_ignored(path, f"timestamp type {type(ts).__name__}")
        return None
    try:
        value = float(ts)
    except (TypeError, ValueError, OverflowError):
        if warn:
            _warn_ignored(path, "timestamp not numeric")
        return None
    if (
        not math.isfinite(value)
        or value < 0
        or value > now + FUTURE_TIMESTAMP_SLACK_SECONDS
    ):
        if warn:
            _warn_ignored(path, f"timestamp {value!r}")
        return None
    return value


def lifecycle_indicates_crash_recovery(home: Optional[Path] = None) -> bool:
    """True when the previous gateway life ended uncleanly (lifecycle sentinel)."""
    try:
        from gateway.lifecycle_ledger import detect_unclean_exit

        return detect_unclean_exit(home) is not None
    except Exception:
        return False


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_runtime_state(home: Optional[Path]) -> dict:
    from hermes_constants import get_hermes_home

    path = (Path(home) if home is not None else get_hermes_home()) / "gateway_state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _health_check_failed(home: Optional[Path]) -> bool:
    """True for an explicit health-check-failed runtime state, or degraded while the pid is alive."""
    state = _read_runtime_state(home)
    if not state:
        return False
    pid = 0
    try:
        pid = int(state.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    # "degraded while alive" only counts for the verified gateway of this home.
    alive = pid > 0 and _live_pid_for_home(home) == pid

    def _failed_text(raw: object) -> bool:
        if isinstance(raw, str) and raw.strip().casefold() in _HEALTH_FAILED:
            return True
        if isinstance(raw, dict):
            for key in ("status", "state"):
                val = raw.get(key)
                if isinstance(val, str) and val.strip().casefold() in _HEALTH_FAILED:
                    return True
        return False

    for key in ("health_check", "health_status", "health"):
        if _failed_text(state.get(key)):
            return True
    if str(state.get("gateway_state") or "").casefold() == "degraded" and alive:
        return True
    return False


def _live_pid_for_home(home: Optional[Path]) -> Optional[int]:
    """Verified live gateway PID for ``home`` (None if none or unproven).

    Recovery exemptions bypass the hourly budget, so they must not trust a bare
    PID from ``gateway_state.json``: a recycled PID (unrelated process, another
    profile, new incarnation) could look wedged or degraded (r8 HIGH). Use the
    one identity check every other reader uses: ``live_gateway_pid_for_home``
    (start-time reuse guard + live command line owned by ``home``).
    """
    try:
        from gateway.status import live_gateway_pid_for_home
        from hermes_constants import get_hermes_home

        pid = live_gateway_pid_for_home(Path(home) if home is not None else Path(get_hermes_home()))
    except Exception:
        return None
    return int(pid) if isinstance(pid, int) and pid > 0 else None


def _live_loop_wedged(home: Optional[Path]) -> bool:
    """True when a live gateway's event loop is the dead-loop / wedged state the restart path escalates."""
    try:
        from hermes_cli.gateway import GATEWAY_LOOP_WEDGED, probe_gateway_loop_liveness
    except Exception:
        return False
    pid = _live_pid_for_home(home)
    if not pid:
        return False
    try:
        return probe_gateway_loop_liveness(pid, home=home) == GATEWAY_LOOP_WEDGED
    except Exception:
        return False


def recovery_exempts_restart_budget(home: Optional[Path] = None) -> bool:
    """Crash (dead prior pid), wedged live loop, or health-check-failed state."""
    if lifecycle_indicates_crash_recovery(home):
        return True
    if _live_loop_wedged(home):
        return True
    return _health_check_failed(home)


def evaluate_restart_budget(
    *,
    force: bool = False,
    crash_recovery: bool = False,
    bypass_reason: Optional[str] = None,
    now: Optional[float] = None,
    home: Optional[Path] = None,
    budget_seconds: int = RESTART_BUDGET_SECONDS,
) -> Tuple[bool, Optional[int]]:
    """Return ``(allowed, minutes_remaining)``; ``minutes_remaining`` is set only when blocked."""
    if force or crash_recovery or bypass_reason == CRASH_RECOVERY_BYPASS:
        return True, None
    clock = now if now is not None else time.time()
    try:
        clock = float(clock)
    except (TypeError, ValueError):
        clock = time.time()
    if not math.isfinite(clock):
        clock = time.time()
    last = _load_last_restart_unix(restart_budget_path(home), now=clock)
    if last is None:
        return True, None
    try:
        elapsed = clock - last
        if elapsed >= budget_seconds:
            return True, None
        remaining_s = budget_seconds - elapsed
        minutes = max(1, math.ceil(remaining_s / 60.0))
    except (OverflowError, ValueError):
        _warn_ignored(restart_budget_path(home), "timestamp arithmetic failed")
        return True, None
    return False, minutes


def _write_stamp(path: Path, stamp: float) -> None:
    from hermes_constants import mkdir_under_hermes_home
    from utils import atomic_json_write

    mkdir_under_hermes_home(path.parent)
    atomic_json_write(path, {"last_restart_unix": stamp}, indent=None)


def record_restart_budget(*, now: Optional[float] = None, home: Optional[Path] = None) -> None:
    """Record a restart that already happened. Never refuses."""
    stamp = time.time() if now is None else float(now)
    if not math.isfinite(stamp) or stamp < 0:
        _warn_ignored(restart_budget_path(home), f"refusing to record timestamp {stamp!r}")
        return
    with _budget_lock(home):
        _write_stamp(restart_budget_path(home), stamp)


def format_restart_budget_refusal(minutes_remaining: int) -> str:
    return (
        "Restart budget blocked this gateway restart "
        f"({minutes_remaining} minute(s) remaining). "
        "Pass --force to override."
    )


class RestartBudgetClaim:
    """A timestamp written under the budget lock. ``keep`` retains it; otherwise roll it back."""

    def __init__(self, home: Optional[Path], previous: Optional[bytes], written: bytes):
        self.home = home
        self._previous = previous
        self._written = written
        self.kept = False

    def keep(self) -> None:
        self.kept = True

    def rollback(self) -> None:
        if self.kept:
            return
        path = restart_budget_path(self.home)
        with _budget_lock(self.home):
            try:
                current = path.read_bytes() if path.exists() else None
            except OSError:
                return
            if current != self._written:
                return
            if self._previous is None:
                try:
                    path.unlink()
                except OSError:
                    pass
                return
            path.write_bytes(self._previous)


def _claim_restart_budget_under_lock(
    home: Path,
    *,
    now: Optional[float] = None,
) -> RestartBudgetClaim:
    """Write the budget stamp; caller must already hold ``_budget_lock``."""
    path = restart_budget_path(home)
    previous = None
    try:
        if path.exists():
            previous = path.read_bytes()
    except OSError:
        previous = None
    clock = time.time() if now is None else float(now)
    _write_stamp(path, clock)
    written = path.read_bytes()
    return RestartBudgetClaim(home, previous, written)


def begin_restart_budget(
    *,
    force: bool,
    home: Optional[Path] = None,
    now: Optional[float] = None,
) -> RestartBudgetClaim:
    """Atomically check and claim. SystemExit(1) when the window is closed.

    The stamp is written before return so a concurrent contender sees it. The caller
    rolls the claim back unless it keeps the stamp after observing a new live PID.
    """
    if home is None:
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
    with _budget_lock(home):
        allowed, minutes = evaluate_restart_budget(
            force=force,
            crash_recovery=False,
            now=now,
            home=home,
        )
        if allowed:
            return _claim_restart_budget_under_lock(home, now=now)
    if not force and recovery_exempts_restart_budget(home):
        with _budget_lock(home):
            return _claim_restart_budget_under_lock(home, now=now)
    print(format_restart_budget_refusal(minutes or 1), file=sys.stderr)
    raise SystemExit(1)


@contextmanager
def restart_budget_session(
    *,
    force: bool,
    home: Optional[Path] = None,
    now: Optional[float] = None,
) -> Iterator[RestartBudgetClaim]:
    """Claim the slot; roll back when the body raised or ``keep()`` was not called."""
    claim = begin_restart_budget(force=force, home=home, now=now)
    try:
        yield claim
    except BaseException:
        claim.rollback()
        raise
    else:
        if not claim.kept:
            claim.rollback()


def guard_cli_gateway_restart(*, force: bool, home: Optional[Path] = None) -> RestartBudgetClaim:
    """Exit 1 when blocked. Returns a claim the caller must keep or roll back."""
    return begin_restart_budget(force=force, home=home)
