"""Per-tick owner routing and durable missing-owner evidence."""
from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import socket as _socket
import sqlite3
from typing import Callable, Mapping, Optional


def _owner_config() -> Mapping:
    from hermes_cli import kanban_db as _kb
    # User-layer presence matters: an absent dispatch_profiles allows existing
    # profiles; a present empty value or unreadable config must claim nothing.
    try:
        from hermes_cli.config_effective import load_user_config_effective
        kanban = (load_user_config_effective(fail_closed=True) or {}).get("kanban", {})
    except Exception as exc:
        _kb._log.warning(
            "kanban: could not read owner routing config (%s) — "
            "this home claims no cards until the config is readable",
            type(exc).__name__,
        )
        return {"dispatch_profiles": []}
    return kanban if isinstance(kanban, Mapping) else {}


def _dispatch_profile_allowlist(kanban: Mapping, normalize_profile_name) -> Optional[frozenset]:
    from hermes_cli import kanban_db as _kb
    if "dispatch_profiles" not in kanban:
        return None
    raw = kanban["dispatch_profiles"]
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        _kb._log.warning(
            "kanban: kanban.dispatch_profiles is present but empty — this home "
            "claims no cards; omit the key to allow any existing profile"
        )
        return frozenset()
    names = [str(n) for n in raw] if isinstance(raw, (list, tuple)) else str(raw).split(",")
    allowed = set()
    for name in names:
        try:
            allowed.add(normalize_profile_name(name))
        except ValueError:
            continue
    return frozenset(allowed)


@dataclass(frozen=True)
class OwnerPolicy:
    """A single owning-home config snapshot; never cached across ticks/profiles."""

    profile_exists: Optional[Callable[[str], bool]]
    allowlist: Optional[frozenset]
    lane_patterns: tuple[str, ...]
    review_enabled: bool = True

    def classify(self, assignee: str) -> str:
        if self.profile_exists is None:
            # Preserve the dispatcher's historical fallback when profiles
            # cannot be imported; do not invent missing-owner evidence.
            return "spawnable"
        from hermes_cli.profiles import normalize_profile_name
        try:
            canon = normalize_profile_name(assignee)
        except ValueError:
            canon = None
        allowed = self.allowlist is None or canon in self.allowlist
        if allowed and self.profile_exists(assignee):
            return "spawnable"
        name = assignee.strip().lower()
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in self.lane_patterns):
            return "lane"
        if canon is not None and not allowed:
            return "foreign"
        return "missing"

    def can_spawn(self, assignee: str) -> bool:
        return self.classify(assignee) == "spawnable"


def load_owner_policy() -> OwnerPolicy:
    kanban = _owner_config()
    try:
        from hermes_cli.profiles import normalize_profile_name, profile_exists
    except Exception:
        return OwnerPolicy(None, None, (), bool(kanban.get("review_dispatch", True)))
    raw = kanban.get("control_plane_lanes") or ()
    names = [str(n) for n in raw] if isinstance(raw, (list, tuple)) else str(raw).split(",")
    patterns = tuple(n.strip().lower() for n in names if n and n.strip())
    return OwnerPolicy(
        profile_exists, _dispatch_profile_allowlist(kanban, normalize_profile_name),
        patterns, bool(kanban.get("review_dispatch", True)),
    )


def classify_owner_rows(conn, rows, result, policy: OwnerPolicy, *, dry_run: bool):
    """Record missing owners before capacity gates can hide the queue tail."""
    runnable = []
    for row in rows:
        assignee = row["assignee"]
        kind = policy.classify(assignee) if assignee else "spawnable"
        if kind == "spawnable":
            runnable.append(row)
            continue
        result.skipped_nonspawnable.append(row["id"])
        if kind == "missing":
            result.owner_unavailable.append((row["id"], assignee))
            if not dry_run:
                _record_owner_unavailable(conn, row["id"], assignee)
    return runnable


OWNER_UNAVAILABLE_EVENT = "owner_unavailable"


def _record_owner_unavailable(conn: sqlite3.Connection, task_id: str, assignee: str) -> bool:
    """Append ONE event and syncable comment per unavailability episode.

    Deduped against the task's latest ``owner_unavailable`` / ``assigned`` /
    ``claimed`` event: repeat ticks for the same assignee write nothing; a
    reassignment or a later claim starts a new episode. Returns True when a
    row was written. The common repeat-tick case is a plain read (no write
    lock); the check is repeated inside the write transaction. A write refused
    by a board fence is logged and skipped — it must never abort the tick.
    """
    from hermes_cli import kanban_db as _kb

    def _still_waiting() -> bool:
        # The row list was read at tick start; a reassign/claim may have
        # landed since. Only flag a card still queued for this assignee.
        row = conn.execute(
            "SELECT status, assignee, claim_lock FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        return (
            row is not None and row["status"] in ("ready", "review")
            and row["assignee"] == assignee and row["claim_lock"] is None
        )

    def _already_recorded() -> bool:
        last = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? "
            "AND kind IN (?, 'assigned', 'claimed') ORDER BY id DESC LIMIT 1",
            (task_id, OWNER_UNAVAILABLE_EVENT),
        ).fetchone()
        if last is None or last["kind"] != OWNER_UNAVAILABLE_EVENT:
            return False
        prev = _kb._json_or(last["payload"], {})
        return isinstance(prev, dict) and prev.get("assignee") == assignee

    try:
        if _already_recorded():
            return False
        with _kb.write_txn(conn):
            if _already_recorded() or not _still_waiting():
                return False
            node = _socket.gethostname()
            _kb._append_event(
                conn, task_id, OWNER_UNAVAILABLE_EVENT,
                {
                    "assignee": assignee,
                    "reason": "profile_not_found",
                    "node": node,
                    "detail": (
                        f"assignee {assignee!r} is not a runnable profile on this home and "
                        "is not a configured kanban.control_plane_lanes entry; recover with "
                        "one explicit reassign_task(..., reason=...)"
                    ),
                },
            )
            _kb.add_comment(
                conn, task_id, "kanban-dispatcher",
                f"owner_unavailable: assignee {assignee!r} has no runnable profile "
                f"on node {node!r} (profile_not_found). Explicit reassignment with "
                "a reason is required for recovery; no reassignment was performed.",
            )
        return True
    except sqlite3.Error as exc:
        _kb._log.warning(
            "kanban: could not record owner_unavailable on %s (%s: %s)",
            task_id, type(exc).__name__, exc,
        )
        return False


def dispatch_profile_allowlist_summary() -> str:
    """Human-readable claim scope for ``hermes kanban diagnostics``."""
    try:
        from hermes_cli.profiles import normalize_profile_name
    except Exception as exc:
        return f"none (fail-closed: profiles unavailable: {exc})"
    allowlist = _dispatch_profile_allowlist(_owner_config(), normalize_profile_name)
    if allowlist is None:
        return "any"
    if allowlist:
        return ", ".join(sorted(allowlist))
    return ("none (fail-closed: kanban.dispatch_profiles is present but names no valid "
            "profile, or the config could not be read — omit the key to allow any)")
