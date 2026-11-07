"""Board-local PR state cache; only the explicit refresher invokes GitHub CLI.

The dispatcher's ready-lane ``active_pr`` hold may be lifted early only by
fresh terminal evidence read from this cache (:func:`all_terminal`); the
dispatcher tick itself never runs ``gh``. :func:`active_pr_keys` is the single
definition of which PRs hold a card, shared by that hold and by
:func:`refresh_pr_state`, so the refresher fetches exactly the PRs the hold
would consult.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

PR_STATE_MAX_AGE_SECONDS = 1800
PR_REFRESH_MAX = 20
PR_REFRESH_TIMEOUT_SECONDS = 5
_STATES = frozenset({"OPEN", "CLOSED", "MERGED"})
_LOG = logging.getLogger(__name__)
_WARNED_OUTSIDE_PATHS: set[tuple[Path, Path]] = set()
_WARN_LOCK = threading.Lock()

PrKey = tuple[str, str, int]


def cache_path() -> Path:
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli import kanban_db as kb

    kanban = (load_user_config_effective(fail_closed=True) or {}).get("kanban", {})
    if not isinstance(kanban, dict):
        raise ValueError("kanban config must be an object")
    configured = kanban.get("pr_state_cache_path")
    if configured not in (None, "") and not isinstance(configured, str):
        raise ValueError("kanban.pr_state_cache_path must be a path string")
    # Resolve both sides so traversal and symlinks cannot import another board's evidence.
    board_dir = kb.board_dir().resolve()
    path = (
        Path(configured).expanduser()
        if isinstance(configured, str) and configured.strip()
        else Path("pr-state.json")
    )
    resolved = (path if path.is_absolute() else board_dir / path).resolve()
    if not resolved.is_relative_to(board_dir):
        warning_key = (board_dir, resolved)
        with _WARN_LOCK:
            if warning_key not in _WARNED_OUTSIDE_PATHS:
                _WARNED_OUTSIDE_PATHS.add(warning_key)
                _LOG.warning(
                    "Kanban PR state cache path is outside its board directory; "
                    "active_pr hold remains"
                )
        raise ValueError("kanban.pr_state_cache_path must remain inside the board directory")
    return resolved


def pr_cache_key(pr: PrKey) -> str:
    owner, repo, number = pr
    return f"{owner}/{repo}#{number}"


def pr_key(match: re.Match[str]) -> PrKey:
    """Compare PR links by owner, repo and number, independent of URL spelling."""
    owner, repo, _, number = match.group(0).casefold().split("github.com/", 1)[1].split("/")
    return owner, repo, int(number)


def active_pr_keys(conn: sqlite3.Connection, task_id: str, assignee: Optional[str],
                   task_body: Optional[str], now: int) -> set[PrKey]:
    """PRs behind the ready-lane ``active_pr`` hold, without network or cache.

    Only the current assignee's recent comments count: a PR link in the task
    body is an input, not that worker's output. An explicit ``unblocked``
    event after a comment releases that comment; a handoff after the newest
    PR comment releases the hold (the named profile must work on that PR).
    """
    if not assignee:
        return set()
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import kanban_db as kb

    url_re = dispatch._RESPAWN_GUARD_PR_URL_RE
    input_prs = {pr_key(m) for m in url_re.finditer(kb._lossy_text(task_body) or "")}
    guarded: set[PrKey] = set()
    comments = sorted(dispatch._rows_since(
        conn, "task_comments", task_id, now - dispatch._RESPAWN_GUARD_PR_WINDOW,
        cols=", author, body", unreadable=now,  # duplicate-PR risk: fail closed
    ), key=lambda item: item[0], reverse=True)
    for created_at, c in comments:
        if c["author"] != assignee:
            continue
        prs = {key for m in url_re.finditer(kb._lossy_text(c["body"]) or "")
               if (key := pr_key(m)) not in input_prs}
        if not prs:
            continue
        if any(dispatch._rows_since(conn, "task_events", task_id, created_at,
                                    "AND kind = 'unblocked'", strict=True)):
            continue
        events = dispatch._rows_since(
            conn, "task_events", task_id, created_at,
            "AND kind IN ('assigned', 'changes_requested', 'review_reopened')",
            cols=", kind, payload", strict=True,
        )
        if any(dispatch._is_handoff_event(e["kind"], e["payload"]) for _, e in events):
            return guarded
        guarded.update(prs)
    return guarded


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


def all_terminal(prs: set[PrKey], now: int, path: Optional[Path] = None) -> bool:
    """True only when every PR has fresh MERGED/CLOSED evidence in the cache."""
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


def refresh_pr_state(conn: sqlite3.Connection, *, path: Optional[Path] = None,
                     max_prs: int = PR_REFRESH_MAX,
                     timeout: float = PR_REFRESH_TIMEOUT_SECONDS) -> dict[str, int]:
    """Refresh at most ``max_prs`` ready-card PRs, once each, with no retries.

    The least recently fetched PRs go first so repeated bounded runs cover
    the whole candidate set. A failed fetch never writes an entry: missing
    evidence keeps the hold.
    """
    if not 1 <= max_prs <= PR_REFRESH_MAX or not 0 < timeout <= PR_REFRESH_TIMEOUT_SECONDS:
        raise ValueError("PR refresh bounds exceeded")
    now = int(time.time())
    prs: set[PrKey] = set()
    for row in conn.execute("SELECT id, assignee, body FROM tasks WHERE status = 'ready'"):
        prs.update(active_pr_keys(conn, row["id"], row["assignee"], row["body"], now))
    path = path or cache_path()
    cache = read_cache(path)

    def fetched_at(pr: PrKey) -> int:
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
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=timeout, check=False,
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
