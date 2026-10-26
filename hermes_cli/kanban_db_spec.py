"""No-LLM reviewed-spec release with a SHA-256 compare-and-swap (fleet carry K04).

Moving a card out of triage must not rewrite instructions a human already reviewed:
the model-backed ``specify_triage_task`` regenerates title/body, which would silently
discard human edits. ``keep_spec_triage_task`` releases the exact reviewed bytes, bound
to a digest checked inside the same write transaction as the status change.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Optional

HASH_ENCODING = (
    'SHA-256 of the UTF-8 bytes of compact JSON [title,body] '
    '(ensure_ascii=False, separators (",",":"), no trailing newline; null body distinct from empty string)'
)


def compute_task_sha256(title: Optional[str], body: Optional[str]) -> str:
    """Deterministic SHA-256 digest of compact JSON [title, body].

    No whitespace stripping or Unicode normalization; a null body is distinct from ''.
    """
    payload = json.dumps([title, body], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalized_digest(expected_sha256: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if expected_sha256 is None:
        return None, "expected_sha256 cannot be None"
    clean = expected_sha256.strip().lower()
    if len(clean) != 64 or not all(c in "0123456789abcdef" for c in clean):
        return None, f"invalid sha256 hex digest: {expected_sha256!r}"
    return clean, None


def _hold_refusal(conn: sqlite3.Connection, task_id: str, row: sqlite3.Row) -> Optional[str]:
    """Holds are refused, never cleared: active breakers and markers survive release."""
    from hermes_cli import kanban_db as kb

    if kb._is_foreign_fleet_mirror(conn, task_id, kb._fleet_adapter_installed_node_id(conn)):
        return "foreign fleet mirror cannot be promoted locally"
    if kb._has_turner_hold(conn, task_id, row["block_kind"], row["title"], row["body"]):
        return "task has active hold (needs_input, younger than 2 h)"
    if kb._has_explicit_block(conn, task_id):
        return "task has active sticky block hold"
    return None


def keep_spec_triage_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    expected_sha256: str,
    author: str,
) -> tuple[bool, str, Optional[str], Optional[str], bool, Optional[str]]:
    """Verify the in-txn SHA-256 and holds, promote triage -> todo, audit, emit ``specified``.

    Returns ``(ok, reason, computed_sha256, status_after, committed, recompute_error)``.
    ``recompute_ready`` runs after the release transaction; under a Board Ops composed
    transaction (``kanban_board_ops_transactions``) both nest as savepoints of the one
    outer transaction and promotion is limited to this card.
    """
    from hermes_cli import kanban_db as kb

    kb._assert_not_delegated_child_mutation()
    if not author or not author.strip():
        return False, "author cannot be blank", None, None, False, None
    clean_author = author.strip()
    clean_expected, invalid = _normalized_digest(expected_sha256)
    if invalid:
        return False, invalid, None, None, False, None

    with kb.write_txn(conn):
        row = conn.execute(
            "SELECT id, status, title, body, block_kind, block_recurrences FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return False, "task not found", None, None, False, None
        if row["status"] != "triage":
            return False, f"task is not in triage (status is {row['status']!r})", None, row["status"], False, None
        refusal = _hold_refusal(conn, task_id, row)
        if refusal:
            return False, refusal, None, "triage", False, None
        computed = compute_task_sha256(row["title"], row["body"])
        if computed.lower() != clean_expected:
            return False, f"sha256 mismatch: expected {clean_expected!r}, got {computed!r}", computed, "triage", False, None
        cur = conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'triage'", (task_id,))
        if cur.rowcount != 1:
            return False, "task moved out of triage concurrently", None, None, False, None
        kb._insert_comment(
            conn, task_id, clean_author,
            f"Specified by {clean_author} (kept original spec, verified sha256 {computed}) and promoted to todo.",
            int(time.time()),
        )
        kb._append_event(conn, task_id, "specified", {
            "author": clean_author, "kept_spec": True, "sha256": computed, "changed_fields": [],
        })

    recompute_err = None
    try:
        kb.recompute_ready(conn)
    except Exception as exc:
        msg = str(exc).strip()
        recompute_err = f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__
    final = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    status_after = final["status"] if final else "todo"
    if recompute_err is not None:
        return True, f"kept_spec (recompute_ready deferred: {recompute_err})", computed, status_after, True, recompute_err
    return True, "kept_spec", computed, status_after, True, None
