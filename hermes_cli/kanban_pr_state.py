"""Board-local PR state cache; only the explicit refresher invokes GitHub CLI."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

PR_STATE_MAX_AGE_SECONDS = 1800
PR_REFRESH_MAX = 20
PR_REFRESH_TIMEOUT_SECONDS = 5
_STATES = frozenset({"OPEN", "CLOSED", "MERGED"})


def cache_path() -> Path:
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli import kanban_db as kb

    kanban = (load_user_config_effective(fail_closed=True) or {}).get("kanban", {})
    if not isinstance(kanban, dict):
        raise ValueError("kanban config must be an object")
    configured = kanban.get("pr_state_cache_path")
    if configured not in (None, "") and not isinstance(configured, str):
        raise ValueError("kanban.pr_state_cache_path must be a path string")
    if isinstance(configured, str) and configured.strip():
        path = Path(configured).expanduser()
        return path if path.is_absolute() else kb.board_dir() / path
    return kb.board_dir() / "pr-state.json"


def pr_cache_key(pr: tuple[str, str, int]) -> str:
    owner, repo, number = pr
    return f"{owner}/{repo}#{number}"


def read_cache(path: Optional[Path] = None) -> dict:
    """A broken cache is equivalent to no evidence, including oversized files."""
    path = path or cache_path()
    try:
        if path.stat().st_size > 1024 * 1024:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def all_terminal(prs: set[tuple[str, str, int]], now: int,
                 path: Optional[Path] = None) -> bool:
    if not prs:
        return False
    try:
        cache = read_cache(path)
    except Exception:  # A broken config cannot lift a dispatch hold.
        return False
    for pr in prs:
        entry = cache.get(pr_cache_key(pr))
        if not isinstance(entry, dict) or entry.get("state") not in {"MERGED", "CLOSED"}:
            return False
        fetched_at = entry.get("fetched_at")
        if type(fetched_at) is not int or not 0 <= now - fetched_at <= PR_STATE_MAX_AGE_SECONDS:
            return False
    return True


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as stream:
            temp_name = stream.name
            json.dump(data, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def refresh_pr_state(conn, *, path: Optional[Path] = None,
                     max_prs: int = PR_REFRESH_MAX,
                     timeout: float = PR_REFRESH_TIMEOUT_SECONDS) -> dict[str, int]:
    """Refresh at most max_prs local ready-card PRs, once each, with no retries."""
    if not 1 <= max_prs <= PR_REFRESH_MAX or not 0 < timeout <= PR_REFRESH_TIMEOUT_SECONDS:
        raise ValueError("PR refresh bounds exceeded")
    from hermes_cli import kanban_db_dispatch as dispatch

    now = int(time.time())
    prs: set[tuple[str, str, int]] = set()
    for row in conn.execute("SELECT id, assignee, body FROM tasks WHERE status = 'ready'"):
        prs.update(dispatch._active_pr_keys(conn, row["id"], row["assignee"], row["body"], now))
    path = path or cache_path()
    cache = read_cache(path)
    def fetched_at(pr: tuple[str, str, int]) -> int:
        entry = cache.get(pr_cache_key(pr))
        value = entry.get("fetched_at") if isinstance(entry, dict) else None
        return value if type(value) is int else -1

    selected = sorted(prs, key=lambda pr: (fetched_at(pr), pr))[:max_prs]
    refreshed = failed = 0
    for owner, repo, number in selected:
        try:
            result = subprocess.run(
                ["gh", "pr", "view", str(number), "--repo", f"{owner}/{repo}",
                 "--json", "state,mergedAt"],
                capture_output=True, text=True, timeout=timeout, check=False,
            )
            if result.returncode:
                failed += 1
                continue
            payload = json.loads(result.stdout)
            state = payload.get("state") if isinstance(payload, dict) else None
            if state not in _STATES:
                failed += 1
                continue
            cache[pr_cache_key((owner, repo, number))] = {
                "state": state, "fetched_at": int(time.time()),
            }
            refreshed += 1
        except (OSError, subprocess.TimeoutExpired, ValueError, UnicodeError):
            failed += 1
    if refreshed:
        _atomic_write(path, cache)
    return {"candidates": len(prs), "attempted": len(selected),
            "refreshed": refreshed, "failed": failed}
