"""Board Ops authority from the node-root configuration and runtime profile.

This is cooperative local-process authorization, like existing Kanban fences.
An OS user able to rewrite configuration, board SQL or process environments is
inside the trust boundary. Request JSON never supplies the authenticated actor.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from agent.delegation_context import is_delegated_child_process_context
from hermes_constants import get_hermes_home
from hermes_cli import kanban_db as kb

OPERATIONS = frozenset({"keep_spec", "record_input", "inspect_wait", "interrupted_cli", "escalate"})
NONDELEGABLE = (
    "control", "grant", "extend", "revoke", "model", "config", "persona",
    "restart", "install", "kill", "archive", "delete", "unlink", "edit_locks",
    "release_hold", "reassign", "complete", "dispatch",
)


class Refused(ValueError):
    """Authorization or a checked precondition failed without task mutation."""


def canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def runtime_home() -> str:
    if is_delegated_child_process_context():
        raise Refused("delegated child cannot operate Board Ops")
    if os.environ.get("HERMES_KANBAN_TASK") or os.environ.get("HERMES_KANBAN_RUN_ID"):
        raise Refused("ordinary Kanban worker cannot operate Board Ops")
    return str(get_hermes_home().expanduser().resolve())


def profile_home(profile: str) -> str:
    from hermes_cli.profiles import get_profile_dir, normalize_profile_name, profile_exists
    if not isinstance(profile, str) or not profile_exists(profile):
        raise Refused("profile is not a locally existing identity")
    if normalize_profile_name(profile) != profile:
        raise Refused("profile must use its canonical local identity")
    return str(get_profile_dir(profile).resolve())


def startup_authority(board: str) -> dict:
    """Only the owner-configured node-root policy can bootstrap a board.

    Reading the root file avoids accepting an operator's profile-local config
    as node authority. No configuration is created or repaired by this API.
    """
    import yaml

    path = kb.kanban_home() / "config.yaml"
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
        policy = cfg["kanban"]["board_ops"]
        if policy["enabled"] is not True or board not in policy["boards"]:
            raise Refused("Board Ops disabled or board not admitted in node configuration")
        node = nonblank(policy["node_id"], "node_id")
        owner_profile = nonblank(policy["owner_profile"], "owner_profile")
        executor_profile = nonblank(policy["executor_profile"], "executor_profile")
        return {
            "node_id": node, "board": board,
            "owner_profile": owner_profile, "owner_home": profile_home(owner_profile),
            "executor_profile": executor_profile, "executor_home": profile_home(executor_profile),
        }
    except (OSError, KeyError, TypeError, yaml.YAMLError) as exc:
        raise Refused(f"node Board Ops authority unavailable: {type(exc).__name__}") from exc


def nonblank(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Refused(f"{field} must be a nonblank string")
    return value.strip()


def assert_board_path(conn, board: str) -> None:
    slug = kb._normalize_board_slug(board)
    if not slug or slug != board:
        raise Refused("explicit canonical board slug required")
    expected = (kb.kanban_home() / "kanban.db" if board == "default"
                else kb.board_dir(board) / "kanban.db").resolve()
    files = [Path(row[2]).resolve() for row in conn.execute("PRAGMA database_list") if row[1] == "main" and row[2]]
    if files != [expected]:
        raise Refused("board database does not match canonical board path")


def validate_grant(contract: dict, control: dict, now: int) -> dict:
    required = {
        "grant_id", "node_id", "board", "task_ids", "tenant", "operator_profile",
        "owner_chain", "human_attribution", "authority_provenance", "allowed_operations",
        "expires_at", "escalation_recipient", "review_authors",
    }
    if set(contract) != required:
        raise Refused("grant fields must match the documented contract exactly")
    data = dict(contract)
    for field in ("grant_id", "node_id", "board", "operator_profile", "human_attribution", "authority_provenance"):
        data[field] = nonblank(data[field], field)
    if data["node_id"] != control["node_id"] or data["board"] != control["board"]:
        raise Refused("grant node/board differs from admitted control")
    if data["tenant"] is not None and not isinstance(data["tenant"], str):
        raise Refused("tenant must be an exact string or null")
    for field in ("task_ids", "allowed_operations", "owner_chain", "review_authors"):
        values = data[field]
        if not isinstance(values, list) or not values or any(not isinstance(x, str) or not x.strip() for x in values):
            raise Refused(f"{field} must be an explicit nonempty string list")
        if len(set(values)) != len(values):
            raise Refused(f"{field} cannot contain duplicates")
    if not set(data["allowed_operations"]) <= OPERATIONS:
        raise Refused("operation is nondelegable or unsupported")
    if data["owner_chain"][-1] != control["owner_profile"]:
        raise Refused("owner chain must end with the admitted node Conductor")
    if data["owner_chain"][0] != data["human_attribution"] or len(data["task_ids"]) > 64:
        raise Refused("owner chain must start with human attribution; grants name at most 64 tasks")
    if type(data["expires_at"]) is not int or not now < data["expires_at"] <= now + 86400:
        raise Refused("grant expiry must be in the next 24 hours")
    recipient = data["escalation_recipient"]
    if not isinstance(recipient, dict) or set(recipient) != {"profile", "platform", "chat_id", "thread_id"}:
        raise Refused("recipient requires exact profile/platform/chat_id/thread_id")
    for field in recipient:
        if field == "thread_id" and recipient[field] == "":
            continue
        nonblank(recipient[field], "recipient." + field)
    profile_home(recipient["profile"])
    data.update(
        operator_home=profile_home(data["operator_profile"]),
        issued_at=now, revoked_at=None, nondelegable_actions=list(NONDELEGABLE),
        owner_home=control["owner_home"],
    )
    return data


def recipient_subscription(conn, task_id: str, recipient: dict) -> dict:
    from hermes_cli.kanban_db_notify import list_notify_subs
    rows = list_notify_subs(conn, task_id=task_id)
    for row in rows:
        if all(row.get(key, "") == recipient[key] for key in ("platform", "chat_id", "thread_id")) and row.get("notifier_profile") == recipient["profile"]:
            # A Board Ops exception never creates an autonomous model turn.
            return row
    raise Refused("escalation recipient has no locally owned task subscription")
