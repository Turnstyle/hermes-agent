"""Poll gateway PID sources after a restart attempt to confirm a new live process."""

from __future__ import annotations

import time
from pathlib import Path

RESTART_OBSERVE_TIMEOUT_S = 90.0
RESTART_OBSERVE_POLL_S = 0.25


def _pids_for_home(home: Path, *, active_home: bool) -> set[int]:
    """Verified live gateway PID for ``home`` (empty set if none).

    Uses ``gateway.status.live_gateway_pid_for_home``, the one identity check
    every other reader of a home's gateway uses: pid file + runtime lock, then
    the runtime status record, with the start-time reuse guard and a live
    command line that belongs to ``home``. A recycled PID (unrelated process,
    another profile's gateway, or a new incarnation) is not a restart.
    ``active_home`` is kept for callers; both paths never unlink identity files.
    """
    try:
        from gateway.status import live_gateway_pid_for_home

        pid = live_gateway_pid_for_home(Path(home))
    except Exception:
        return set()
    return {pid} if isinstance(pid, int) and pid > 0 else set()


def _current_live_pids(*, all_profiles: bool) -> frozenset[int]:
    from hermes_constants import get_hermes_home

    active = Path(get_hermes_home())
    pids = _pids_for_home(active, active_home=True)
    if not all_profiles:
        return frozenset(pids)
    seen_homes = {active.resolve(strict=False)}
    try:
        from hermes_cli.profiles import list_profiles

        for info in list_profiles():
            home = Path(info.path)
            key = home.resolve(strict=False)
            if key in seen_homes:
                continue
            seen_homes.add(key)
            pids.update(_pids_for_home(home, active_home=False))
    except Exception:
        pass
    return frozenset(pids)


def snapshot_gateway_pids(*, all_profiles: bool) -> frozenset[int]:
    return _current_live_pids(all_profiles=all_profiles)


def replacement_was_observed(before: frozenset[int], *, all_profiles: bool) -> bool:
    """Poll until a live PID exists that was not in ``before``, or the timeout expires.

    Reads ``RESTART_OBSERVE_TIMEOUT_S`` and ``RESTART_OBSERVE_POLL_S`` on each call
    (not as default args) so tests can monkeypatch them to 0 and finish in one poll.
    """
    timeout_s = RESTART_OBSERVE_TIMEOUT_S
    poll_s = RESTART_OBSERVE_POLL_S
    deadline = time.monotonic() + timeout_s
    while True:
        current = _current_live_pids(all_profiles=all_profiles)
        if any(pid not in before for pid in current):
            return True
        now = time.monotonic()
        if now >= deadline:
            return False
        if timeout_s <= 0:
            return False
        remaining = deadline - now
        sleep_s = min(poll_s, remaining)
        if sleep_s > 0:
            time.sleep(sleep_s)

