"""Track admitted cron work before releasing the board's launch lock."""
import contextlib

from hermes_cli.kanban_launch import launch_guard


@contextlib.contextmanager
def cron_launch_guard(job):
    from cron import scheduler

    token = object()
    with launch_guard(wait_seconds=1.0) as admitted:
        if admitted:
            home = scheduler._get_hermes_home().resolve()
            key = scheduler._inflight_key(job["id"])
            claim = job.get("fire_claim")
            owner = str(claim.get("by") or "") if isinstance(claim, dict) else ""
            with scheduler._running_lock:
                scheduler._running_fire_owners.setdefault(key, {})[token] = (owner or None, home)
    if not admitted:
        yield False
        return
    try:
        yield token
    finally:
        with scheduler._running_lock:
            executions = scheduler._running_fire_owners.get(key)
            if executions is not None:
                executions.pop(token, None)
                if not executions:
                    scheduler._running_fire_owners.pop(key, None)
