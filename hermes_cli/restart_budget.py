"""Intentional gateway restart rate limit (one per Hermes home per hour).

State lives at ``<HERMES_HOME>/gateway/restart_budget.json``. Crash recovery and
``--force`` bypass the window; refusal is decided before any restart signal is sent.
"""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

RESTART_BUDGET_SECONDS = 60 * 60
CRASH_RECOVERY_BYPASS = "crash_recovery"


def restart_budget_path(home: Optional[Path] = None) -> Path:
    from hermes_constants import get_hermes_home

    root = home if home is not None else get_hermes_home()
    return Path(root) / "gateway" / "restart_budget.json"


def _load_last_restart_unix(path: Path) -> Optional[float]:
    from utils import read_json_or_empty

    ts = read_json_or_empty(path).get("last_restart_unix")
    if isinstance(ts, (int, float)):
        return float(ts)
    return None


def lifecycle_indicates_crash_recovery(home: Optional[Path] = None) -> bool:
    """True when the previous gateway life ended uncleanly (lifecycle sentinel)."""
    try:
        from gateway.lifecycle_ledger import detect_unclean_exit

        return detect_unclean_exit(home) is not None
    except Exception:
        return False


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
    last = _load_last_restart_unix(restart_budget_path(home))
    if last is None:
        return True, None
    elapsed = clock - last
    if elapsed >= budget_seconds:
        return True, None
    remaining_s = budget_seconds - elapsed
    minutes = max(1, math.ceil(remaining_s / 60.0))
    return False, minutes


def record_restart_budget(*, now: Optional[float] = None, home: Optional[Path] = None) -> None:
    path = restart_budget_path(home)
    payload = {"last_restart_unix": now if now is not None else time.time()}
    from hermes_constants import mkdir_under_hermes_home
    from utils import atomic_json_write

    mkdir_under_hermes_home(path.parent)
    atomic_json_write(path, payload, indent=None)


def format_restart_budget_refusal(minutes_remaining: int) -> str:
    return (
        "Restart budget blocked this gateway restart "
        f"({minutes_remaining} minute(s) remaining). "
        "Pass --force to override."
    )


def guard_cli_gateway_restart(*, force: bool, home: Optional[Path] = None) -> None:
    """Exit 1 on stderr when blocked; record budget timestamp when allowed."""
    crash = lifecycle_indicates_crash_recovery(home)
    allowed, minutes = evaluate_restart_budget(force=force, crash_recovery=crash, home=home)
    if not allowed:
        print(format_restart_budget_refusal(minutes or 1), file=sys.stderr)
        raise SystemExit(1)
    record_restart_budget(home=home)
