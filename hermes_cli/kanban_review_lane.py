"""Review-only profile eligibility and durable skip diagnostics."""
from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Optional

_REVIEW_SKIP_WINDOW_SECONDS = 300


def review_lane_block_reason(assignee: str) -> Optional[str]:
    """Only an explicit ``kanban.review_lane: false`` opts a profile out.

    Read the assignee's config, not the dispatcher's. Missing files/keys retain
    default eligibility. A failed read defers review until config is readable;
    do not recover to defaults and silently bypass an existing opt-out.
    """
    try:
        from hermes_cli.config_effective import load_user_config_effective
        from hermes_cli.profiles import get_profile_dir

        config = load_user_config_effective(
            get_profile_dir(assignee) / "config.yaml", fail_closed=True,
        )
        kanban = config.get("kanban", {})
        if isinstance(kanban, Mapping) and kanban.get("review_lane") is False:
            return "review_lane_disabled"
    except Exception:
        # Config exceptions may contain private YAML values. Never persist them.
        return "review_lane_config_error"
    return None


def record_review_lane_skip(conn, task_id: str, assignee: str, reason: str) -> None:
    """One event per task/assignee/reason per five minutes, across restarts.

    Called only by a non-dry dispatch tick, never by health or budget probes.
    Use the existing event table and transaction so concurrent ticks cannot
    both emit. Unrelated events/comments do not reset the throttle.
    """
    from hermes_cli import kanban_db as kb

    message = (
        "Review dispatch skipped: assignee has kanban.review_lane: false; "
        "reassign the review or enable that profile's review lane."
        if reason == "review_lane_disabled" else
        "Review dispatch skipped: assignee config could not be read; "
        "check that profile's config before retrying review."
    )
    with kb.write_txn(conn):
        recent = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? "
            "AND kind='review_lane_skipped' AND created_at>?",
            (task_id, int(time.time()) - _REVIEW_SKIP_WINDOW_SECONDS),
        )
        if any(
            (payload := kb._json_dict(row["payload"])).get("assignee") == assignee
            and payload.get("reason") == reason
            for row in recent
        ):
            return
        kb._append_event(conn, task_id, "review_lane_skipped", {
            "assignee": assignee, "reason": reason, "message": message,
        })
