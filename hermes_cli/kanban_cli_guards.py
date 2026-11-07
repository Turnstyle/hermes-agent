"""Refusal text and the no-LLM ``specify --keep-spec`` release for ``hermes kanban`` verbs.

Sibling of the ``kanban.py`` facade. ``complete`` refusals name the card's real state
(waiting in todo/triage, already terminal, unknown) instead of one generic line, so an
operator knows whether to promote, unlink a parent or stop. ``--keep-spec`` releases a
human-reviewed triage spec verbatim, bound to the SHA-256 the reviewer saw.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_output import _err, _obj_dict

_KEEP_SPEC_FIELDS = (
    "task_id", "ok", "reason", "kept_spec", "sha256",
    "hash_encoding", "status_after", "committed", "recompute_error",
)


def parent_blocker_message(task_id: str, blockers: list[tuple[str, str]],
                           status: Optional[str] = None) -> str:
    detail = ", ".join(f"{pid} ({parent_status})" for pid, parent_status in blockers)
    status_text = f"card is {status} and has " if status else ""
    return (f"cannot complete {task_id}: {status_text}unsatisfied parent dependencies: {detail}; "
            f"complete the parents first, or `hermes kanban unlink <parent> {task_id}`.")


def completion_refusal(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """The refusal ``complete`` must print for a card it cannot close, or None to try."""
    task = kb.get_task(conn, task_id)
    if task is None:
        return f"cannot complete {task_id}: unknown id"
    if task.status in ("done", "archived"):
        return f"cannot complete {task_id}: already {task.status}"
    if task.status in ("todo", "triage"):
        blockers = kb.unsatisfied_parents(conn, task_id)
        if blockers:
            return parent_blocker_message(task_id, blockers, task.status)
        return (f"cannot complete {task_id}: card is {task.status}; promote it to ready first "
                f"(hermes kanban promote {task_id})")
    return None


def _keep_spec_usage_error(args: argparse.Namespace) -> Optional[int]:
    keep_spec = bool(getattr(args, "keep_spec", False))
    expect = getattr(args, "expect_sha256", None)
    if expect is not None and not keep_spec:
        return _err("kanban: --expect-sha256 requires --keep-spec", 2)
    if getattr(args, "all_triage", False):
        return _err("kanban: --all cannot be used with --keep-spec", 2)
    if not args.task_id:
        return _err("kanban: specify --keep-spec requires a task id", 2)
    if expect is None:
        return _err("kanban: specify --keep-spec requires --expect-sha256", 2)
    digest = expect.strip().lower()
    if len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest):
        return _err(f"kanban: specify --keep-spec: invalid sha256 hex digest: {expect!r}", 2)
    author = getattr(args, "author", None)
    if author is None:
        return _err("kanban: specify --keep-spec requires --author", 2)
    if not author.strip():
        return _err("kanban: specify --keep-spec: author cannot be blank", 2)
    return None


def cmd_keep_spec(args: argparse.Namespace) -> int:
    """``specify --keep-spec``: promote one triage card without any model call.

    The model-backed specify regenerates title/body, which would silently replace a
    spec a human already reviewed; this releases the exact reviewed bytes instead
    (hash and triage status re-checked inside the promoting write transaction).
    """
    from hermes_cli import kanban_specify as spec

    rc = _keep_spec_usage_error(args)
    if rc is not None:
        return rc
    outcome = spec.keep_spec_task(
        args.task_id,
        expect_sha256=args.expect_sha256.strip().lower(),
        author=args.author.strip(),
    )
    if getattr(args, "json", False):
        print(json.dumps(_obj_dict(outcome, _KEEP_SPEC_FIELDS)))
    elif outcome.ok:
        short_hash = outcome.sha256[:8] if outcome.sha256 else "unknown"
        print(f"Kept spec for {outcome.task_id} → {outcome.status_after} (sha256: {short_hash})")
        print(f"Encoding: {outcome.hash_encoding or kb.HASH_ENCODING}")
        if outcome.recompute_error:
            print(f"warning: recompute_ready deferred: {outcome.recompute_error}")
    else:
        print(f"kanban: specify {outcome.task_id}: {outcome.reason}", file=sys.stderr)
    return 0 if outcome.ok else 1
