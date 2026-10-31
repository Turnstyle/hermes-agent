"""Canonical Fleet ownership fence for a synced board (fleet carry K04).

The external Fleet adapter installs ``fleet_kanban_task_insert`` (its body embeds the
installed node id) and ``fleet_kanban_issue_map`` (canonical ``current_node``/``source_node``).
Automatic promotion, recovery and Board Ops must decide ownership from that installed
schema BEFORE any write: a mirror of another node's card has no local authority.
Boards without the adapter are unaffected (every task is local).
"""
from __future__ import annotations

import logging
import re
import sqlite3
from typing import Optional

# Same logger as the facade so existing log filters/assertions keep matching.
_log = logging.getLogger("hermes_cli.kanban_db")

# Sentinel: Fleet adapter trigger present but its node id is unreadable.
_FLEET_NODE_UNPARSEABLE = "\x00fleet-node-unparseable"

# Tolerant of case, whitespace/newlines between tokens, and SQL-escaped
# quotes ('') inside the node literal.
_FLEET_TRIGGER_NODE_RE = re.compile(
    r"INSERT\s+INTO\s+fleet_kanban_issue_map\s*\([^)]*\)\s*VALUES\s*\(\s*"
    r"NEW\s*\.\s*id\s*,\s*.+?\s*,\s*NEW\s*\.\s*title\s*,\s*NEW\s*\.\s*body\s*,\s*"
    r"'((?:[^']|'')+)'",
    re.IGNORECASE | re.DOTALL,
)


def _fleet_adapter_installed_node_id(conn: sqlite3.Connection) -> Optional[str]:
    """This board's own installed Fleet-adapter node identity, or ``None`` when no
    Fleet adapter is installed at all.

    Authoritative source is the ``fleet_kanban_task_insert`` AFTER-INSERT trigger
    the external adapter installs once per board: its body literally embeds the
    node id the board was installed with, so reading it back from
    ``sqlite_master`` queries this board's actually-installed schema rather than
    re-deriving it from a mutable env var. Deliberately no cross-repo import —
    only SQL text the standard ``sqlite_master`` table already exposes.

    ``None`` means no ownership filtering applies: a plain board with no adapter
    has no concept of a "foreign-owned mirror", so every task on it is local and
    legacy semantics are preserved exactly.

    Fails CLOSED: when the trigger EXISTS but its text cannot be parsed
    (reformatted SQL, a future adapter version) this returns
    :data:`_FLEET_NODE_UNPARSEABLE` and logs a WARNING, so callers treat every
    mapped Fleet row as foreign rather than as "no adapter, all local" — which
    would silently re-open the auto-promotion incident.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'trigger' AND name = 'fleet_kanban_task_insert'"
    ).fetchone()
    if row is None or not row[0]:
        return None
    match = _FLEET_TRIGGER_NODE_RE.search(row[0])
    if match is None:
        _log.warning(
            "kanban recompute_ready: fleet_kanban_task_insert trigger exists "
            "but its installed node id could not be parsed. Failing closed: "
            "every Fleet-mapped task is treated as foreign (not auto-promoted) "
            "until the trigger text is recognized again."
        )
        return _FLEET_NODE_UNPARSEABLE
    return match.group(1).replace("''", "'")


def _is_foreign_fleet_mirror(
    conn: sqlite3.Connection,
    task_id: str,
    installed_node_id: Optional[str],
) -> bool:
    """True iff ``task_id`` is a Fleet-mirrored task this board's own installed
    node does not currently own, per the adapter's authoritative
    ``current_node``/``source_node`` columns.

    ``installed_node_id is None`` (no Fleet adapter) always returns ``False`` —
    legacy boards are untouched. A task with NO ``fleet_kanban_issue_map`` row is
    also local: the AFTER-INSERT trigger fires unconditionally, so a missing row
    means the task predates the adapter, not that it is silently foreign.

    Ownership is SQL ``COALESCE(current_node, source_node)``, matching the
    adapter's own resolution order: ``source_node`` applies only when
    ``current_node`` IS NULL — a present-but-blank ``current_node`` is the
    owner, not a cue to fall back to historical source ownership. An owner that
    is not a non-blank string (both NULL, empty, whitespace) is UNKNOWN: that
    counts as foreign (safe — never auto-promote what this node cannot
    positively attribute to itself) and logs a WARNING (visible — an operator
    can find and fix the corrupt mapping row instead of the task silently never
    promoting).
    """
    if installed_node_id is None:
        return False
    row = conn.execute(
        "SELECT current_node, source_node FROM fleet_kanban_issue_map "
        "WHERE local_task_id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return False
    current_node, source_node = row["current_node"], row["source_node"]
    owner = source_node if current_node is None else current_node
    if not isinstance(owner, str) or not owner.strip():
        _log.warning(
            "kanban recompute_ready: task %s has a fleet_kanban_issue_map row "
            "whose owner COALESCE(current_node, source_node) is not a node id "
            "(current_node=%r, source_node=%r) — ownership is UNKNOWN. "
            "Treating as foreign (not auto-promoted) rather than risk promoting "
            "a task this node cannot positively attribute to itself. This "
            "indicates a corrupt/partial adapter mapping row and should be "
            "investigated.",
            task_id, current_node, source_node,
        )
        return True
    return owner != installed_node_id


# Verdicts of :func:`_fleet_row_ownership`.
FLEET_OWNER_LOCAL = "local"
FLEET_OWNER_FOREIGN = "foreign"
FLEET_OWNER_UNKNOWN = "unknown"


def _fleet_row_ownership(
    conn: sqlite3.Connection,
    task_id: str,
    installed_node_id: Optional[str],
) -> tuple[str, str]:
    """Verdict ``(owner, detail)`` on whether THIS node may recover ``task_id``.

    Recovery paths (orphan reconciliation, expired-claim release of a claim this
    host did not take) write ``running -> ready``. On a synced Fleet board the
    local copy of another node's ``running`` card has no local claim, worker or
    lease, and the lifecycle fence refuses every such write (t_23c40d89). This
    decides ownership BEFORE any host-local PID probe or write is attempted.

    ``FLEET_OWNER_LOCAL``: no adapter installed (legacy board), no map row (the
    card predates the adapter — same rule as :func:`_is_foreign_fleet_mirror`),
    or the canonical home ``COALESCE(current_node, source_node)`` is this node
    AND ``tasks.tenant`` (where the assignee-home rule records the card's home)
    does not disagree. A NULL/blank tenant is no evidence either way.

    ``FLEET_OWNER_FOREIGN``: the canonical home is another node. Expected,
    silent: the owner node recovers its own card.

    ``FLEET_OWNER_UNKNOWN``: ownership cannot be verified — unreadable adapter
    node id, unreadable map, blank/non-text canonical home, or a canonical home
    that contradicts ``tenant``. The caller must leave the row untouched AND
    surface it; ``detail`` names why. Fails closed on any read error.
    """
    if installed_node_id is None:
        return FLEET_OWNER_LOCAL, ""
    if installed_node_id == _FLEET_NODE_UNPARSEABLE:
        return FLEET_OWNER_UNKNOWN, "fleet adapter node id is unreadable"
    try:
        row = conn.execute(
            "SELECT m.current_node, m.source_node, t.tenant "
            "FROM tasks t LEFT JOIN fleet_kanban_issue_map m ON m.local_task_id = t.id "
            "WHERE t.id = ?",
            (task_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        return FLEET_OWNER_UNKNOWN, f"ownership read failed: {exc}"
    if row is None:
        return FLEET_OWNER_LOCAL, ""
    current_node, source_node, tenant = row[0], row[1], row[2]
    if current_node is None and source_node is None:
        # LEFT JOIN found no map row (both columns NULL) — or a row with no owner
        # at all. Tell the two apart: a missing row predates the adapter (local).
        try:
            mapped = conn.execute(
                "SELECT 1 FROM fleet_kanban_issue_map WHERE local_task_id = ?", (task_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            return FLEET_OWNER_UNKNOWN, f"ownership read failed: {exc}"
        if mapped is None:
            return FLEET_OWNER_LOCAL, ""
    owner = source_node if current_node is None else current_node
    if not isinstance(owner, str) or not owner.strip():
        return FLEET_OWNER_UNKNOWN, (
            f"canonical home is not a node id (current_node={current_node!r}, "
            f"source_node={source_node!r})"
        )
    if owner != installed_node_id:
        return FLEET_OWNER_FOREIGN, f"canonical home is {owner!r}"
    if isinstance(tenant, str) and tenant.strip() and tenant != owner:
        return FLEET_OWNER_UNKNOWN, (
            f"canonical home {owner!r} contradicts card home (tenant) {tenant!r}"
        )
    return FLEET_OWNER_LOCAL, ""
