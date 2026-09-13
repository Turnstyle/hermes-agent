"""Durable, non-executable recovery for input accepted before a turn-start failure.

The transcript and recovery record are changed in one SessionDB transaction.  A
recovery is deliberately inert: restoring it may prefill a composer, but only an
explicit, idempotently claimed resend may execute it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

from agent.context_compressor import (
    COMPRESSED_SUMMARY_METADATA_KEY,
    _DB_PERSISTED_MARKER,
    split_user_originated_turn,
)


PENDING_INPUT_RECOVERY_PREFIX = "pending_input_recovery:v1:"
PENDING_INPUT_SURFACE_HEAD_PREFIX = "pending_input_recovery_surface_head:v1:"
PENDING_INPUT_DISPLAY_METADATA_KEY = "_hermes_pending_input_recovery"
PENDING_INPUT_ADMISSION_ID_KEY = "admission_id"
PENDING_INPUT_SURFACE_KEY = "surface_key"
PENDING_INPUT_MERGED_CARRIER_KEY = "merged_carrier"
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_MAX_RECOVERY_TEXT_BYTES = 64 * 1024
_CLAIM_LEASE_SECONDS = 30.0


class PendingInputRecoveryError(RuntimeError):
    """The accepted input could not be moved to durable inert recovery."""


class PendingInputRecoverySuperseded(PendingInputRecoveryError):
    """A stale resend lost its claim before its own recovery could be staged."""

    def __init__(self, recovery: Dict[str, Any], live_messages: List[Dict[str, Any]]) -> None:
        super().__init__("pending-input recovery resend claim was superseded")
        self.recovery = recovery
        self.live_messages = live_messages


class PendingInputDispatchUncertainError(PendingInputRecoveryError):
    """The provider may have received a recovered input, but no response was observed."""

    def __init__(self, original: BaseException, *, resend_id: str = "") -> None:
        super().__init__(
            "provider dispatch outcome is uncertain; recovery was retained for review"
        )
        self.original = original
        self.resend_id = resend_id


def _process_incarnation(pid: Any) -> Optional[float]:
    """Return a PID's OS creation time when it can be proven."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        import psutil

        process = psutil.Process(pid)
        if not process.is_running():
            return None
        return float(process.create_time())
    except Exception:
        # PID existence alone cannot distinguish a restarted process that reused
        # the number. After the short claim lease, the durable turn lease is the
        # stronger fence; an unprovable process incarnation is reclaimable.
        return None


def _claim_owner_is_alive(pid: Any, incarnation: Any = None) -> bool:
    """Prove that the claimant PID is still the same OS process incarnation."""
    try:
        expected = float(incarnation)
    except (TypeError, ValueError):
        return False
    actual = _process_incarnation(pid)
    return actual is not None and abs(actual - expected) < 0.001


def _valid_id(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not _ID_RE.fullmatch(normalized):
        raise ValueError(f"invalid {field}")
    return normalized


def mint_pending_input_admission_id() -> str:
    return uuid.uuid4().hex


def stamp_pending_input_admission(
    message: Dict[str, Any], *, admission_id: Optional[str] = None,
    surface_key: Optional[str] = None,
) -> str:
    """Stamp a user row before executor admission; never changes provider content.

    The identity lives in namespaced ``display_metadata`` so SessionDB carries it
    through both in-place compaction and child-session rotation.  Request assembly
    strips the whole display-only field before provider dispatch.
    """
    if not isinstance(message, dict) or message.get("role") != "user":
        raise ValueError("pending-input admission requires a user message")
    display_metadata = message.get("display_metadata")
    if display_metadata is None:
        display_metadata = {}
    if not isinstance(display_metadata, dict):
        raise ValueError("pending-input admission requires object display metadata")
    namespace = display_metadata.get(PENDING_INPUT_DISPLAY_METADATA_KEY)
    if namespace is None:
        namespace = {}
    if not isinstance(namespace, dict):
        raise ValueError("pending-input admission metadata is invalid")
    existing = namespace.get(PENDING_INPUT_ADMISSION_ID_KEY)
    resolved = _valid_id(existing or admission_id or mint_pending_input_admission_id(), field="admission id")
    if existing and admission_id and existing != admission_id:
        raise ValueError("pending-input admission id changed")
    namespace = dict(namespace)
    namespace[PENDING_INPUT_ADMISSION_ID_KEY] = resolved
    if surface_key:
        resolved_surface = _valid_id(surface_key, field="surface key")
        existing_surface = namespace.get(PENDING_INPUT_SURFACE_KEY)
        if existing_surface and existing_surface != resolved_surface:
            raise ValueError("pending-input surface key changed")
        namespace[PENDING_INPUT_SURFACE_KEY] = resolved_surface
    display_metadata = dict(display_metadata)
    display_metadata[PENDING_INPUT_DISPLAY_METADATA_KEY] = namespace
    message["display_metadata"] = display_metadata
    return resolved


def pending_input_admission(message: Any) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(message, dict):
        return None, None
    display_metadata = message.get("display_metadata")
    if not isinstance(display_metadata, dict):
        return None, None
    namespace = display_metadata.get(PENDING_INPUT_DISPLAY_METADATA_KEY)
    if not isinstance(namespace, dict):
        return None, None
    admission_id = namespace.get(PENDING_INPUT_ADMISSION_ID_KEY)
    surface_key = namespace.get(PENDING_INPUT_SURFACE_KEY)
    try:
        return (
            _valid_id(admission_id, field="admission id"),
            _valid_id(surface_key, field="surface key") if surface_key else None,
        )
    except ValueError:
        return None, None


def _merged_recovery_carrier(message: Any) -> Optional[Dict[str, Any]]:
    """Recover scaffolding that temporarily carried an admitted user anchor.

    Compaction sometimes has to merge the real user anchor into a trailing
    synthetic user row to preserve strict alternation.  The original carrier is
    retained inside the namespaced display metadata so timeout recovery can
    remove only the admitted text, not the todo/checkpoint snapshot beneath it.
    """
    if not isinstance(message, dict):
        return None
    metadata = message.get("display_metadata")
    if not isinstance(metadata, dict):
        return None
    namespace = metadata.get(PENDING_INPUT_DISPLAY_METADATA_KEY)
    if not isinstance(namespace, dict):
        return None
    carrier = namespace.get(PENDING_INPUT_MERGED_CARRIER_KEY)
    if not isinstance(carrier, dict) or carrier.get("role") != "user":
        return None
    restored = copy.deepcopy(carrier)
    restored_metadata = restored.get("display_metadata")
    if isinstance(restored_metadata, dict):
        restored_metadata = dict(restored_metadata)
        restored_metadata.pop(PENDING_INPUT_DISPLAY_METADATA_KEY, None)
        if restored_metadata:
            restored["display_metadata"] = restored_metadata
        else:
            restored.pop("display_metadata", None)
    restored[_DB_PERSISTED_MARKER] = True
    return restored


_RECOVERY_HANDOFF_ACK_CONTENT = "Continuing from the retained context handoff."


def _recovery_bridge_assistant() -> Dict[str, Any]:
    return {
        "role": "assistant",
        "content": _RECOVERY_HANDOFF_ACK_CONTENT,
        "display_kind": "hidden",
        _DB_PERSISTED_MARKER: True,
    }


def _recovery_row_replacements(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return inert rows that remain after removing one admitted user payload."""
    carrier = _merged_recovery_carrier(message)
    if carrier is not None:
        return [carrier, _recovery_bridge_assistant()]
    handoff, live_view = split_user_originated_turn(message)
    if live_view is None:
        raise PendingInputRecoveryError("admission marker is not on a user-originated turn")
    if handoff is None:
        return []
    handoff[_DB_PERSISTED_MARKER] = True
    # A summary handoff must remain role=user for strict provider templates.
    # Pair it with a hidden assistant acknowledgement so the next real user
    # input cannot create a durable user/user sequence.
    return [handoff, _recovery_bridge_assistant()]


def _bounded_redacted_recovery(value: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != 1:
        raise PendingInputRecoveryError("invalid accepted-input recovery payload")
    text = value.get("text")
    if not isinstance(text, str) or not text.strip():
        raise PendingInputRecoveryError("accepted input has no recoverable text")
    encoded = text.encode("utf-8")
    if len(encoded) > _MAX_RECOVERY_TEXT_BYTES:
        # Keep a small, user-visible, non-executable loss record and withdraw
        # the exact transcript row in the same transaction. Rejecting here
        # would leave a very large accepted prompt executable after the timeout.
        reasons = list(value.get("loss_reasons") or [])
        if "input_exceeds_recovery_size_limit" not in reasons:
            reasons.append("input_exceeds_recovery_size_limit")
        out = dict(value)
        out.update(
            text="[Oversized input was not retained in recovery]",
            exact=False,
            lossy=True,
            safe_to_replay=False,
            loss_reasons=reasons,
            original_size_bytes=len(encoded),
            original_sha256=hashlib.sha256(encoded).hexdigest(),
            executable=False,
            provider_call_sent=False,
        )
        return out
    from agent.redact import redact_sensitive_text

    redacted = redact_sensitive_text(text, force=True)
    out = dict(value)
    if redacted != text:
        reasons = list(out.get("loss_reasons") or [])
        if "sensitive_content_redacted" not in reasons:
            reasons.append("sensitive_content_redacted")
        out.update(
            text=redacted, exact=False, lossy=True, safe_to_replay=False,
            loss_reasons=reasons,
        )
    # Structured/media/MoA input is retained only as an explicit loss record.  It
    # must never silently degrade into a text replay.
    if out.get("input_kind") != "plain_text":
        out["safe_to_replay"] = False
    out["executable"] = False
    out["provider_call_sent"] = False
    return out


def _recovery_key(admission_id: str) -> str:
    return f"{PENDING_INPUT_RECOVERY_PREFIX}{_valid_id(admission_id, field='admission id')}"


def _surface_head_key(surface_key: str) -> str:
    return f"{PENDING_INPUT_SURFACE_HEAD_PREFIX}{_valid_id(surface_key, field='surface key')}"


def _clean_live_messages(
    messages: Iterable[Dict[str, Any]], admission_id: str,
) -> Tuple[List[Dict[str, Any]], int]:
    cleaned: List[Dict[str, Any]] = []
    removed = 0
    for message in messages:
        current_id, _ = pending_input_admission(message)
        if current_id != admission_id:
            cleaned.append(message)
            continue
        removed += 1
        cleaned.extend(_recovery_row_replacements(message))
    return cleaned, removed


def remove_pending_input_admission_from_messages(
    messages: Iterable[Dict[str, Any]], admission_id: str,
) -> Tuple[List[Dict[str, Any]], int]:
    """Remove an exact unentered resend and its local response tail.

    A middleware short-circuit can append an assistant/tool-shaped tail without
    physical provider entry.  Keep the process-local projection aligned with
    the durable quarantine by dropping non-user followers through the next
    admitted user boundary.
    """
    admission_id = _valid_id(admission_id, field="admission id")
    cleaned: List[Dict[str, Any]] = []
    removed = 0
    dropping_response_tail = False
    for message in messages:
        current_id, _surface = pending_input_admission(message)
        if current_id == admission_id:
            replacements = _recovery_row_replacements(message)
            removed += 1
            dropping_response_tail = True
            cleaned.extend(replacements)
            continue
        if dropping_response_tail:
            _handoff, live_view = split_user_originated_turn(message)
            if live_view is None and message.get("role") != "user":
                continue
            dropping_response_tail = False
        cleaned.append(message)
    return cleaned, removed


def _row_as_user_message(db: Any, row: Any) -> Dict[str, Any]:
    message: Dict[str, Any] = {
        "role": row["role"],
        "content": db._decode_content(row["content"]),
        "display_metadata": db._decode_display_metadata(row["display_metadata"]),
    }
    if row["display_kind"]:
        message["display_kind"] = row["display_kind"]
    if row["timestamp"] is not None:
        message["timestamp"] = row["timestamp"]
    if row["_compressed_summary"]:
        message[COMPRESSED_SUMMARY_METADATA_KEY] = True
    return message


def stage_pending_input_recovery(
    db: Any, *, accepted_input: Dict[str, Any], live_messages: List[Dict[str, Any]],
    session_ids: Iterable[str], admission_id: str, surface_key: str,
    accepted_input_durably_committed: bool, turn_lease_holder: Optional[str] = None,
    poisoned_compression_lock_identity: Optional[Tuple[str, str]] = None,
    predecessor_recovery_id: Optional[str] = None,
    predecessor_resend_id: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Atomically retain inert recovery and remove every executable durable copy.

    Rotation may have copied the accepted row into both the ended parent and live
    child.  ``session_ids`` names exactly that boundary; the durable UUID plus
    surface marker prevents unrelated rows from matching.
    """
    if db is None or not hasattr(db, "_execute_write"):
        raise PendingInputRecoveryError("durable session storage is unavailable")
    admission_id = _valid_id(admission_id, field="admission id")
    surface_key = _valid_id(surface_key, field="surface key")
    if bool(predecessor_recovery_id) != bool(predecessor_resend_id):
        raise PendingInputRecoveryError("recovery predecessor identity is incomplete")
    predecessor = None
    if predecessor_recovery_id and predecessor_resend_id:
        predecessor = (
            _valid_id(predecessor_recovery_id, field="predecessor recovery id"),
            _valid_id(predecessor_resend_id, field="predecessor resend id"),
        )
        if predecessor[0] == admission_id:
            raise PendingInputRecoveryError("recovery predecessor cannot equal its successor")
    candidates = tuple(dict.fromkeys(str(s or "").strip() for s in session_ids if str(s or "").strip()))
    if not candidates:
        raise PendingInputRecoveryError("recovery has no session boundary")
    tagged = [
        message for message in live_messages
        if pending_input_admission(message) == (admission_id, surface_key)
    ]
    if len(tagged) != 1:
        raise PendingInputRecoveryError("accepted input has no unique admission row")
    clean_messages, removed_live = _clean_live_messages(live_messages, admission_id)
    if removed_live != 1:
        raise PendingInputRecoveryError(
            f"expected one live accepted-input row, found {removed_live}"
        )
    recovery = _bounded_redacted_recovery(accepted_input)
    poison_identity = poisoned_compression_lock_identity
    if not (
        isinstance(poison_identity, tuple)
        and len(poison_identity) == 2
        and all(isinstance(value, str) and value.strip() for value in poison_identity)
    ):
        poison_identity = None
    now = time.time()
    key = _recovery_key(admission_id)
    superseded: Dict[str, Any] = {}

    def _write(conn):
        for sid in candidates:
            poisoned_holder = (
                poison_identity[1]
                if poison_identity is not None and poison_identity[0] == sid
                else None
            )
            db._check_transcript_write_guards(
                # A total-ceiling timeout may return while its uninterruptible
                # summary call still owns the durable lease.  That worker has
                # already lost commit admission to the poison fence, so this
                # exact holder is safe to cross for the recovery mutation.  A
                # different (or merely concurrent) compressor still fails
                # closed below.
                conn, sid, poisoned_holder,
                turn_lease_holder=turn_lease_holder,
                reject_active_turn_lease=not bool(turn_lease_holder),
                reject_active_compression_lock=True,
                allow_closed_compression_parent=True,
            )
        placeholders = ",".join("?" for _ in candidates)
        rows = conn.execute(
            "SELECT id, session_id, role, content, display_kind, display_metadata, "
            "timestamp, _compressed_summary FROM messages "
            f"WHERE active = 1 AND role = 'user' AND session_id IN ({placeholders}) "
            "AND json_valid(display_metadata) "
            "AND json_extract(display_metadata, ?) = ? "
            "AND json_extract(display_metadata, ?) = ?",
            (
                *candidates,
                f"$.{PENDING_INPUT_DISPLAY_METADATA_KEY}.{PENDING_INPUT_ADMISSION_ID_KEY}",
                admission_id,
                f"$.{PENDING_INPUT_DISPLAY_METADATA_KEY}.{PENDING_INPUT_SURFACE_KEY}",
                surface_key,
            ),
        ).fetchall()
        matches = []
        per_session: Dict[str, int] = {}
        for row in rows:
            if row["role"] != "user":
                raise PendingInputRecoveryError("admission marker is attached to a non-user row")
            if pending_input_admission(_row_as_user_message(db, row)) != (admission_id, surface_key):
                raise PendingInputRecoveryError("durable admission identity changed")
            sid = str(row["session_id"])
            per_session[sid] = per_session.get(sid, 0) + 1
            if per_session[sid] > 1:
                raise PendingInputRecoveryError("duplicate accepted-input rows in one session")
            matches.append(row)

        def _replace_matches() -> None:
            touched_sessions = set()
            for matched_row in matches:
                sid = str(matched_row["session_id"])
                message = _row_as_user_message(db, matched_row)
                replacements = _recovery_row_replacements(message)
                conn.execute(
                    "UPDATE messages SET active = 0, compacted = 0 "
                    "WHERE id = ? AND active = 1",
                    (int(matched_row["id"]),),
                )
                if replacements:
                    db._insert_message_rows(conn, sid, replacements)
                touched_sessions.add(sid)
            for sid in touched_sessions:
                message_count, tool_call_count = db._active_transcript_counts(conn, sid)
                conn.execute(
                    "UPDATE sessions SET message_count = ?, tool_call_count = ? WHERE id = ?",
                    (message_count, tool_call_count, sid),
                )

        predecessor_value = None
        predecessor_key = None
        if predecessor is not None:
            predecessor_key = _recovery_key(predecessor[0])
            predecessor_raw = conn.execute(
                "SELECT value FROM state_meta WHERE key = ?", (predecessor_key,),
            ).fetchone()
            if predecessor_raw is None:
                raise PendingInputRecoveryError("recovery predecessor was not found")
            try:
                predecessor_value = json.loads(predecessor_raw[0])
            except (TypeError, json.JSONDecodeError) as exc:
                raise PendingInputRecoveryError("recovery predecessor is invalid") from exc
            if not isinstance(predecessor_value, dict):
                raise PendingInputRecoveryError("recovery predecessor is invalid")
            if not (
                predecessor_value.get("status") == "claimed"
                and predecessor_value.get("resend_id") == predecessor[1]
            ):
                # Another claimant now owns the original recovery. Remove this
                # losing resend's exact executable row, but never mint a second
                # independently replayable recovery for the same intent.
                _replace_matches()
                superseded["recovery"] = predecessor_value
                return predecessor_value

        existing_raw = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        if accepted_input_durably_committed and not matches and existing_raw is None:
            raise PendingInputRecoveryError(
                "committed accepted input is missing from the compaction boundary"
            )
        if existing_raw is not None:
            try:
                existing = json.loads(existing_raw[0])
            except (TypeError, json.JSONDecodeError) as exc:
                raise PendingInputRecoveryError("existing recovery record is invalid") from exc
            if existing.get("admission_id") != admission_id or existing.get("surface_key") != surface_key:
                raise PendingInputRecoveryError("recovery admission identity conflict")
            if existing.get("status") not in {"pending", "claimed"}:
                raise PendingInputRecoveryError("recovery admission is already consumed")
            for immutable in ("text", "input_kind", "exact", "lossy", "provider_call_sent"):
                if existing.get(immutable) != recovery.get(immutable):
                    raise PendingInputRecoveryError("recovery payload identity conflict")
            record = existing
        else:
            record = {
                **recovery,
                "record_version": 1,
                "admission_id": admission_id,
                "surface_key": surface_key,
                "origin_session_id": candidates[0],
                "head_session_id": candidates[-1],
                "status": "pending",
                "created_at": now,
                "updated_at": now,
                "accepted_input_durably_committed": bool(matches) or bool(accepted_input_durably_committed),
            }
            conn.execute("INSERT INTO state_meta (key, value) VALUES (?, ?)", (
                key, json.dumps(record, ensure_ascii=False, sort_keys=True),
            ))

        # Surfaces without a native delivery id (ACP) retain an exact durable
        # pointer to the admission they most recently presented for recovery.
        # This avoids guessing among identical prompt texts after process restart.
        conn.execute(
            "INSERT INTO state_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (
                _surface_head_key(surface_key),
                json.dumps(
                    {
                        "admission_id": admission_id,
                        "surface_key": surface_key,
                        "updated_at": now,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )

        # The admitted row may intentionally carry an API-only prefix while
        # ``accepted_input.text`` is the clean durable/composer value.  Its UUID
        # + surface marker, not mutable provider-facing content, is the exact
        # identity across compaction and rotation.
        _replace_matches()
        if predecessor_value is not None and predecessor_key is not None:
            predecessor_value.update(
                status="consumed",
                resolution="durable_recovery_successor",
                consumed_at=now,
                provider_response_received=False,
                updated_at=now,
            )
            conn.execute(
                "UPDATE state_meta SET value = ? WHERE key = ?",
                (
                    json.dumps(predecessor_value, ensure_ascii=False, sort_keys=True),
                    predecessor_key,
                ),
            )
        return record

    record = db._execute_write(_write, patience_s=getattr(db, "_TRANSCRIPT_WRITE_PATIENCE_S", None))
    if superseded:
        raise PendingInputRecoverySuperseded(record, clean_messages)
    return record, clean_messages


def get_pending_input_recovery(db: Any, admission_id: str) -> Optional[Dict[str, Any]]:
    raw = db.get_meta(_recovery_key(admission_id)) if db is not None else None
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _durable_recovery_successor_exists_conn(conn: Any, resend_id: str) -> bool:
    """Whether a failed resend itself reached durable inert recovery."""
    return conn.execute(
        "SELECT 1 FROM state_meta WHERE key = ? LIMIT 1",
        (_recovery_key(resend_id),),
    ).fetchone() is not None


def _provider_returned_for_resend(recovery: Dict[str, Any], resend_id: str) -> bool:
    generation = int(recovery.get("claim_generation") or 0)
    return bool(
        generation > 0
        and recovery.get("provider_return_resend_id") == resend_id
        and int(recovery.get("provider_return_generation") or 0) == generation
    )


def _durable_resend_has_response_tail_conn(conn: Any, resend_id: str) -> bool:
    """Whether an exact resend is followed by a durable assistant row."""
    rows = conn.execute(
        "SELECT id, session_id FROM messages WHERE active = 1 AND role = 'user' "
        "AND json_valid(display_metadata) "
        "AND json_extract(display_metadata, ?) = ? ORDER BY id",
        (
            f"$.{PENDING_INPUT_DISPLAY_METADATA_KEY}.{PENDING_INPUT_ADMISSION_ID_KEY}",
            resend_id,
        ),
    ).fetchall()
    for row in rows:
        next_row = conn.execute(
            "SELECT role FROM messages WHERE active = 1 AND session_id = ? "
            "AND id > ? ORDER BY id LIMIT 1",
            (str(row["session_id"]), int(row["id"])),
        ).fetchone()
        if next_row is not None and str(next_row["role"]) == "assistant":
            return True
    return False


def _durable_resend_has_response_conn(
    conn: Any, resend_id: str, recovery: Dict[str, Any],
) -> bool:
    """Whether a proven physical callback has a durable response tail.

    Role adjacency alone is not proof: middleware or Relay may synthesize a
    model-shaped response after the transport raised.  Automatic settlement
    therefore requires the separately committed callback-return marker bound
    to this exact resend and claim generation.
    """
    return _provider_returned_for_resend(
        recovery, resend_id,
    ) and _durable_resend_has_response_tail_conn(conn, resend_id)


def _withdraw_unentered_successor_rows(
    conn: Any, db: Any, resend_id: str, *, turn_lease_holder: Optional[str] = None,
) -> int:
    """Remove an exact prior resend that never reached provider entry.

    This runs only while taking over a still-visible claim.  It prevents a
    restart retry from appending a second model-visible copy of the same intent.
    """
    rows = conn.execute(
        "SELECT id, session_id, role, content, display_kind, display_metadata, "
        "timestamp, _compressed_summary FROM messages WHERE active = 1 "
        "AND role = 'user' AND json_valid(display_metadata) "
        "AND json_extract(display_metadata, ?) = ?",
        (
            f"$.{PENDING_INPUT_DISPLAY_METADATA_KEY}.{PENDING_INPUT_ADMISSION_ID_KEY}",
            resend_id,
        ),
    ).fetchall()
    touched_sessions: set[str] = set()
    for row in rows:
        sid = str(row["session_id"])
        db._check_transcript_write_guards(
            conn, sid, None, turn_lease_holder=turn_lease_holder,
            reject_active_turn_lease=not bool(turn_lease_holder),
            reject_active_compression_lock=True,
            allow_closed_compression_parent=True,
        )
        replacements = _recovery_row_replacements(_row_as_user_message(db, row))
        conn.execute(
            "UPDATE messages SET active = 0, compacted = 0 WHERE id = ? AND active = 1",
            (int(row["id"]),),
        )
        # A local middleware/Relay short-circuit can finalize a synthetic
        # assistant/tool tail without ever entering the physical provider
        # callback. Quarantine that complete local turn with its exact user row
        # so role alternation cannot leave an orphaned assistant executable.
        followers = conn.execute(
            "SELECT id, role FROM messages WHERE active = 1 AND session_id = ? "
            "AND id > ? ORDER BY id",
            (sid, int(row["id"])),
        ).fetchall()
        for follower in followers:
            if str(follower["role"]) == "user":
                break
            conn.execute(
                "UPDATE messages SET active = 0, compacted = 0 "
                "WHERE id = ? AND active = 1",
                (int(follower["id"]),),
            )
        if replacements:
            db._insert_message_rows(conn, sid, replacements)
        touched_sessions.add(sid)
    for sid in touched_sessions:
        message_count, tool_call_count = db._active_transcript_counts(conn, sid)
        conn.execute(
            "UPDATE sessions SET message_count = ?, tool_call_count = ? WHERE id = ?",
            (message_count, tool_call_count, sid),
        )
    return len(rows)


def _fence_recovery_takeover(conn: Any, db: Any, value: Dict[str, Any]) -> None:
    """Reject takeover while the recovery's conversation has a live turn owner.

    The short recovery claim lease covers the gap before ``run_conversation`` acquires
    its durable turn lease.  Once that lease exists it is the stronger liveness fence:
    SessionDB keeps a live local PID, while atomically removing a provably dead/expired
    owner.  Check the recorded boundary even when the prior resend never flushed a row.
    """
    session_ids = tuple(
        dict.fromkeys(
            str(value.get(key) or "").strip()
            for key in ("claim_session_id", "origin_session_id", "head_session_id")
            if str(value.get(key) or "").strip()
        )
    )
    try:
        for session_id in session_ids:
            db._check_transcript_write_guards(
                conn, session_id, None,
                reject_active_turn_lease=True,
                reject_active_compression_lock=True,
                allow_closed_compression_parent=True,
            )
    except Exception as exc:
        raise PendingInputRecoveryError(
            "pending-input recovery resend is active in another process"
        ) from exc


def claim_pending_input_recovery(
    db: Any, admission_id: str, *, resend_id: str,
    claim_session_id: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Claim a recovery for explicit resend without making it unrecoverable.

    A crash before the successor is durable leaves ``status=claimed``.  The
    claim can be taken over with a new resend id only when no exact durable
    successor exists.
    """
    key = _recovery_key(admission_id)
    resend_id = _valid_id(resend_id, field="resend id")
    claim_session_id = (
        _valid_id(claim_session_id, field="claim session id")
        if claim_session_id else None
    )

    def _write(conn):
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise PendingInputRecoveryError("pending-input recovery was not found")
        try:
            value = json.loads(row[0])
        except (TypeError, json.JSONDecodeError) as exc:
            raise PendingInputRecoveryError("pending-input recovery is invalid") from exc
        status = value.get("status")
        if status == "consumed":
            raise PendingInputRecoveryError("pending-input recovery was already consumed")
        if status == "claimed":
            current_resend = value.get("resend_id")
            if current_resend == resend_id:
                if claim_session_id and value.get("claim_session_id") not in {
                    None, claim_session_id,
                }:
                    raise PendingInputRecoveryError(
                        "pending-input recovery claim session changed"
                    )
                return value, False
            if value.get("dispatch_state") in {"started", "uncertain"}:
                raise PendingInputRecoveryError(
                    "pending-input recovery has an uncertain provider dispatch"
                )
            try:
                claim_age = max(0.0, time.time() - float(value.get("claimed_at") or 0.0))
            except (TypeError, ValueError):
                claim_age = 0.0
            if claim_age < _CLAIM_LEASE_SECONDS:
                raise PendingInputRecoveryError("pending-input recovery has an active resend claim")
            if _claim_owner_is_alive(
                value.get("claim_owner_pid"), value.get("claim_owner_incarnation"),
            ):
                raise PendingInputRecoveryError(
                    "pending-input recovery has an active resend claim"
                )
            if current_resend and _durable_recovery_successor_exists_conn(conn, current_resend):
                raise PendingInputRecoveryError("pending-input recovery already has a durable successor")
            _fence_recovery_takeover(conn, db, value)
            if current_resend:
                _withdraw_unentered_successor_rows(conn, db, current_resend)
                superseded = list(value.get("superseded_resend_ids") or [])
                if current_resend not in superseded:
                    superseded.append(current_resend)
                value["superseded_resend_ids"] = superseded
        elif status != "pending":
            raise PendingInputRecoveryError("pending-input recovery is not claimable")
        now = time.time()
        value.update(
            status="claimed", resend_id=resend_id, claimed_at=now,
            claim_session_id=(
                claim_session_id
                or value.get("head_session_id")
                or value.get("origin_session_id")
            ),
            updated_at=now, claim_count=int(value.get("claim_count") or 0) + 1,
            claim_generation=int(value.get("claim_generation") or 0) + 1,
            claim_owner_pid=os.getpid(),
            claim_owner_incarnation=_process_incarnation(os.getpid()),
            claim_owner_token=uuid.uuid4().hex,
        )
        conn.execute("UPDATE state_meta SET value = ? WHERE key = ?", (
            json.dumps(value, ensure_ascii=False, sort_keys=True), key,
        ))
        return value, True

    return db._execute_write(_write)


def mark_pending_input_recovery_dispatch_started(
    db: Any, admission_id: str, *, resend_id: str,
    turn_lease_holder: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Durably mark the ambiguous boundary immediately before external dispatch.

    A process death after this commit but before a response is observable cannot be
    classified as "not sent".  The still-claimed recovery therefore remains visible,
    but callers must not project it as a safely replayable draft.
    """
    key = _recovery_key(admission_id)
    resend_id = _valid_id(resend_id, field="resend id")
    superseded_claim = {"rejected": False}

    def _write(conn):
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise PendingInputRecoveryError("pending-input recovery was not found")
        try:
            value = json.loads(row[0])
        except (TypeError, json.JSONDecodeError) as exc:
            raise PendingInputRecoveryError("pending-input recovery is invalid") from exc
        if value.get("status") == "consumed":
            if value.get("resend_id") == resend_id:
                return value, False
            raise PendingInputRecoveryError("pending-input recovery was already consumed")
        if value.get("status") != "claimed" or value.get("resend_id") != resend_id:
            if resend_id in set(value.get("superseded_resend_ids") or []):
                _withdraw_unentered_successor_rows(
                    conn, db, resend_id, turn_lease_holder=turn_lease_holder,
                )
                # Return normally so the quarantine commits; raise only after
                # the transaction closes or SQLite would roll it back.
                superseded_claim["rejected"] = True
                return value, False
            raise PendingInputRecoveryError("pending-input recovery has no matching claim")
        generation = int(value.get("claim_generation") or 0)
        if (
            value.get("dispatch_state") == "uncertain"
            and value.get("dispatch_resend_id") == resend_id
            and int(value.get("dispatch_generation") or 0) == generation
        ):
            raise PendingInputRecoveryError(
                "pending-input recovery provider dispatch is uncertain"
            )
        if (
            value.get("dispatch_state") == "started"
            and value.get("dispatch_resend_id") == resend_id
            and int(value.get("dispatch_generation") or 0) == generation
        ):
            return value, False
        now = time.time()
        value.update(
            dispatch_state="started",
            dispatch_resend_id=resend_id,
            dispatch_generation=generation,
            dispatch_started_at=now,
            updated_at=now,
        )
        conn.execute(
            "UPDATE state_meta SET value = ? WHERE key = ?",
            (json.dumps(value, ensure_ascii=False, sort_keys=True), key),
        )
        return value, True

    result = db._execute_write(_write)
    if superseded_claim["rejected"]:
        raise PendingInputRecoveryError(
            "pending-input recovery resend claim was superseded"
        )
    return result


def mark_pending_input_recovery_provider_returned(
    db: Any, admission_id: str, *, resend_id: str,
) -> Tuple[Dict[str, Any], bool]:
    """Record that the exact physical provider callback returned normally.

    This is deliberately separate from ``dispatch_state=started``.  A started
    socket may raise, and middleware can then synthesize an assistant-shaped
    response.  Only this resend/generation-fenced marker can make a durable
    response tail eligible for automatic restart settlement.
    """
    key = _recovery_key(admission_id)
    resend_id = _valid_id(resend_id, field="resend id")

    def _write(conn):
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise PendingInputRecoveryError("pending-input recovery was not found")
        try:
            value = json.loads(row[0])
        except (TypeError, json.JSONDecodeError) as exc:
            raise PendingInputRecoveryError("pending-input recovery is invalid") from exc
        generation = int(value.get("claim_generation") or 0)
        if (
            value.get("status") != "claimed"
            or value.get("resend_id") != resend_id
            or value.get("dispatch_state") != "started"
            or value.get("dispatch_resend_id") != resend_id
            or int(value.get("dispatch_generation") or 0) != generation
        ):
            raise PendingInputRecoveryError(
                "pending-input recovery has no matching started provider dispatch"
            )
        if (
            value.get("provider_return_resend_id") == resend_id
            and int(value.get("provider_return_generation") or 0) == generation
        ):
            return value, False
        now = time.time()
        value.update(
            provider_return_resend_id=resend_id,
            provider_return_generation=generation,
            provider_returned_at=now,
            updated_at=now,
        )
        conn.execute(
            "UPDATE state_meta SET value = ? WHERE key = ?",
            (json.dumps(value, ensure_ascii=False, sort_keys=True), key),
        )
        return value, True

    return db._execute_write(_write)


def consume_pending_input_recovery_without_durable_projection(
    db: Any, admission_id: str, *, resend_id: str,
    turn_lease_holder: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Settle a definitely accepted provider turn whose response was not persisted.

    The external acceptance is at-most-once evidence, but leaving its resend user
    row executable would let a later unrelated turn submit it again. Atomically
    withdraw that exact local turn, persist the generation-fenced return marker,
    and consume the recovery while preserving an explicit projection-loss reason.
    """
    key = _recovery_key(admission_id)
    resend_id = _valid_id(resend_id, field="resend id")

    def _write(conn):
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise PendingInputRecoveryError("pending-input recovery was not found")
        try:
            value = json.loads(row[0])
        except (TypeError, json.JSONDecodeError) as exc:
            raise PendingInputRecoveryError("pending-input recovery is invalid") from exc
        if value.get("status") == "consumed" and value.get("resend_id") == resend_id:
            return value, False
        generation = int(value.get("claim_generation") or 0)
        if (
            value.get("status") != "claimed"
            or value.get("resend_id") != resend_id
            or value.get("dispatch_state") != "started"
            or value.get("dispatch_resend_id") != resend_id
            or int(value.get("dispatch_generation") or 0) != generation
        ):
            raise PendingInputRecoveryError(
                "pending-input recovery has no matching accepted provider dispatch"
            )
        removed = _withdraw_unentered_successor_rows(
            conn, db, resend_id, turn_lease_holder=turn_lease_holder,
        )
        now = time.time()
        value.update(
            status="consumed",
            resend_id=resend_id,
            consumed_at=now,
            provider_response_received=True,
            provider_return_resend_id=resend_id,
            provider_return_generation=generation,
            provider_returned_at=value.get("provider_returned_at") or now,
            resolution="provider_return_without_durable_projection",
            projected_response_persisted=False,
            quarantined_successor_rows=(
                int(value.get("quarantined_successor_rows") or 0) + removed
            ),
            updated_at=now,
        )
        conn.execute(
            "UPDATE state_meta SET value = ? WHERE key = ?",
            (json.dumps(value, ensure_ascii=False, sort_keys=True), key),
        )
        return value, True

    return db._execute_write(_write)


def quarantine_pending_input_recovery_dispatch(
    db: Any, admission_id: str, *, resend_id: str,
    turn_lease_holder: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Atomically quarantine a resend whose external dispatch outcome is unknown."""
    key = _recovery_key(admission_id)
    resend_id = _valid_id(resend_id, field="resend id")

    def _write(conn):
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise PendingInputRecoveryError("pending-input recovery was not found")
        try:
            value = json.loads(row[0])
        except (TypeError, json.JSONDecodeError) as exc:
            raise PendingInputRecoveryError("pending-input recovery is invalid") from exc
        if value.get("status") != "claimed" or value.get("resend_id") != resend_id:
            raise PendingInputRecoveryError("pending-input recovery has no matching claim")
        if value.get("dispatch_state") not in {"started", "uncertain"}:
            raise PendingInputRecoveryError("pending-input recovery has no started provider dispatch")
        removed = _withdraw_unentered_successor_rows(
            conn, db, resend_id, turn_lease_holder=turn_lease_holder,
        )
        if value.get("dispatch_state") == "uncertain" and not removed:
            return value, False
        now = time.time()
        value.update(
            dispatch_state="uncertain",
            dispatch_uncertain_at=now,
            quarantined_successor_rows=int(value.get("quarantined_successor_rows") or 0) + removed,
            updated_at=now,
        )
        conn.execute(
            "UPDATE state_meta SET value = ? WHERE key = ?",
            (json.dumps(value, ensure_ascii=False, sort_keys=True), key),
        )
        return value, True

    return db._execute_write(_write)


def return_pending_input_recovery_without_dispatch(
    db: Any, admission_id: str, *, resend_id: str,
    turn_lease_holder: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Return a claimed resend to pending after definitive pre-dispatch failure."""
    key = _recovery_key(admission_id)
    resend_id = _valid_id(resend_id, field="resend id")

    def _write(conn):
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise PendingInputRecoveryError("pending-input recovery was not found")
        try:
            value = json.loads(row[0])
        except (TypeError, json.JSONDecodeError) as exc:
            raise PendingInputRecoveryError("pending-input recovery is invalid") from exc
        if value.get("status") == "pending" and value.get("previous_resend_id") == resend_id:
            return value, False
        if value.get("status") != "claimed" or value.get("resend_id") != resend_id:
            raise PendingInputRecoveryError("pending-input recovery has no matching claim")
        if value.get("dispatch_state") in {"started", "uncertain"}:
            raise PendingInputRecoveryError("pending-input recovery dispatch is not definitively absent")
        _withdraw_unentered_successor_rows(
            conn, db, resend_id, turn_lease_holder=turn_lease_holder,
        )
        now = time.time()
        superseded = list(value.get("superseded_resend_ids") or [])
        old_previous = value.get("previous_resend_id")
        if old_previous and old_previous != resend_id and old_previous not in superseded:
            superseded.append(old_previous)
        value.update(
            status="pending",
            previous_resend_id=resend_id,
            superseded_resend_ids=superseded,
            resend_id=None,
            claimed_at=None,
            claim_session_id=None,
            resolution="not_dispatched",
            resolved_at=now,
            updated_at=now,
        )
        conn.execute(
            "UPDATE state_meta SET value = ? WHERE key = ?",
            (json.dumps(value, ensure_ascii=False, sort_keys=True), key),
        )
        return value, True

    return db._execute_write(_write)


def reconcile_pending_input_recovery_rows(
    db: Any, messages: Iterable[Dict[str, Any]], *,
    current_recovery_id: Optional[str] = None,
    current_resend_id: Optional[str] = None,
    turn_lease_holder: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """Quarantine orphaned resend rows before a later turn builds its request.

    This closes both restart windows: a crash after transcript flush but before
    dispatch, and a crash after dispatch began but before its result was known.
    The explicit resend currently admitted under this turn's lease is excluded.
    """
    if db is None or not hasattr(db, "_execute_write"):
        return list(messages), 0
    current_recovery_id = str(current_recovery_id or "")
    current_resend_id = str(current_resend_id or "")

    def _write(conn):
        rows = conn.execute(
            "SELECT key, value FROM state_meta WHERE key LIKE ? ESCAPE '\\'",
            (_escape_like_meta_prefix(PENDING_INPUT_RECOVERY_PREFIX) + "%",),
        ).fetchall()
        resend_ids: List[str] = []
        for row in rows:
            try:
                value = json.loads(row["value"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict):
                continue
            previous_resend_id = value.get("previous_resend_id")
            if previous_resend_id:
                try:
                    previous_resend_id = _valid_id(
                        previous_resend_id, field="previous resend id",
                    )
                except ValueError:
                    previous_resend_id = None
                if previous_resend_id:
                    _withdraw_unentered_successor_rows(
                        conn, db, previous_resend_id,
                        turn_lease_holder=turn_lease_holder,
                    )
                    resend_ids.append(previous_resend_id)
            for superseded_resend_id in value.get("superseded_resend_ids") or []:
                try:
                    superseded_resend_id = _valid_id(
                        superseded_resend_id, field="superseded resend id",
                    )
                except ValueError:
                    continue
                _withdraw_unentered_successor_rows(
                    conn, db, superseded_resend_id,
                    turn_lease_holder=turn_lease_holder,
                )
                resend_ids.append(superseded_resend_id)
            if value.get("status") != "claimed":
                continue
            admission_id = str(value.get("admission_id") or "")
            resend_id = str(value.get("resend_id") or "")
            if not resend_id or (
                admission_id == current_recovery_id and resend_id == current_resend_id
            ):
                continue
            if _durable_resend_has_response_conn(conn, resend_id, value):
                # Provider return was proven by the persisted answer even though
                # the immediate consume write failed. Preserve the complete
                # user/assistant pair and settle the inert record in this same
                # transaction; never quarantine only half of an answered turn.
                now = time.time()
                value.update(
                    status="consumed",
                    resolution="durable_response_reconciled",
                    resolved_at=now,
                    consumed_at=now,
                    provider_response_received=True,
                    updated_at=now,
                )
                conn.execute(
                    "UPDATE state_meta SET value = ? WHERE key = ?",
                    (json.dumps(value, ensure_ascii=False, sort_keys=True), row["key"]),
                )
                continue
            removed = _withdraw_unentered_successor_rows(
                conn, db, resend_id, turn_lease_holder=turn_lease_holder,
            )
            if value.get("dispatch_state") in {"started", "uncertain"}:
                now = time.time()
                value.update(
                    dispatch_state="uncertain",
                    dispatch_uncertain_at=value.get("dispatch_uncertain_at") or now,
                    quarantined_successor_rows=(
                        int(value.get("quarantined_successor_rows") or 0) + removed
                    ),
                    updated_at=now,
                )
                conn.execute(
                    "UPDATE state_meta SET value = ? WHERE key = ?",
                    (json.dumps(value, ensure_ascii=False, sort_keys=True), row["key"]),
                )
            resend_ids.append(resend_id)
        return resend_ids

    resend_ids = db._execute_write(_write)
    cleaned = list(messages)
    removed = 0
    for resend_id in resend_ids:
        cleaned, count = remove_pending_input_admission_from_messages(cleaned, resend_id)
        removed += count
    return cleaned, removed


def resolve_pending_input_recovery_dispatch(
    db: Any, admission_id: str, *, resend_id: str, claim_generation: int,
    resolution: str,
) -> Tuple[Dict[str, Any], bool]:
    """Explicitly settle an ambiguous dispatch after an operator reviews it.

    ``delivered`` closes the recovery as consumed. ``retry`` returns it to the
    pending, non-executable queue so a later explicit composer submission can
    claim a fresh generation. Nothing invokes this transition automatically.
    """
    if resolution not in {"delivered", "retry"}:
        raise ValueError("resolution must be 'delivered' or 'retry'")
    key = _recovery_key(admission_id)
    resend_id = _valid_id(resend_id, field="resend id")
    claim_generation = int(claim_generation)
    if claim_generation <= 0:
        raise ValueError("claim generation must be positive")

    def _write(conn):
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise PendingInputRecoveryError("pending-input recovery was not found")
        try:
            value = json.loads(row[0])
        except (TypeError, json.JSONDecodeError) as exc:
            raise PendingInputRecoveryError("pending-input recovery is invalid") from exc
        if value.get("status") == "consumed" and value.get("resolution") == resolution:
            return value, False
        if (
            value.get("status") != "claimed"
            or value.get("resend_id") != resend_id
            or int(value.get("claim_generation") or 0) != claim_generation
            or value.get("dispatch_state") not in {"started", "uncertain"}
        ):
            raise PendingInputRecoveryError(
                "pending-input recovery has no matching uncertain dispatch"
            )
        # A process may have died after persisting the resend row but before it
        # could quarantine it.  Resolve-before-next-turn must remove that exact
        # executable tail while the record is still claimed; otherwise changing
        # status first makes later reconciliation skip it and a different prompt
        # can silently carry it back to the provider.
        answered = _durable_resend_has_response_conn(conn, resend_id, value)
        if _provider_returned_for_resend(value, resend_id) and resolution == "retry":
            raise PendingInputRecoveryError(
                "pending-input recovery has a proven provider return; mark it delivered"
            )
        if not answered:
            _withdraw_unentered_successor_rows(conn, db, resend_id)
        now = time.time()
        value.update(
            resolution=resolution,
            resolved_at=now,
            updated_at=now,
        )
        if resolution == "delivered":
            value.update(
                status="consumed",
                consumed_at=now,
                provider_response_received=bool(answered) or value.get(
                    "provider_response_received", False
                ),
            )
        else:
            value.update(
                status="pending",
                previous_resend_id=resend_id,
                resend_id=None,
                claimed_at=None,
                dispatch_state=None,
                dispatch_resend_id=None,
                dispatch_generation=None,
                dispatch_started_at=None,
                dispatch_uncertain_at=None,
                provider_return_resend_id=None,
                provider_return_generation=None,
                provider_returned_at=None,
            )
        conn.execute(
            "UPDATE state_meta SET value = ? WHERE key = ?",
            (json.dumps(value, ensure_ascii=False, sort_keys=True), key),
        )
        return value, True

    return db._execute_write(_write)


def acknowledge_pending_input_recovery_loss(
    db: Any, admission_id: str,
) -> Tuple[Dict[str, Any], bool]:
    """Explicitly close a surfaced lossy/non-replayable recovery notice."""
    key = _recovery_key(admission_id)

    def _write(conn):
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise PendingInputRecoveryError("pending-input recovery was not found")
        try:
            value = json.loads(row[0])
        except (TypeError, json.JSONDecodeError) as exc:
            raise PendingInputRecoveryError("pending-input recovery is invalid") from exc
        if value.get("status") == "consumed" and value.get("resolution") == "acknowledged_loss":
            return value, False
        if value.get("status") != "pending" or value.get("safe_to_replay") is not False:
            raise PendingInputRecoveryError("pending-input recovery is not a loss notice")
        now = time.time()
        value.update(
            status="consumed", resolution="acknowledged_loss",
            resolved_at=now, consumed_at=now, updated_at=now,
        )
        conn.execute(
            "UPDATE state_meta SET value = ? WHERE key = ?",
            (json.dumps(value, ensure_ascii=False, sort_keys=True), key),
        )
        return value, True

    return db._execute_write(_write)


def _escape_like_meta_prefix(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def get_surface_pending_input_recovery(db: Any, surface_key: str) -> Optional[Dict[str, Any]]:
    """Return the exact pending record named by a surface's durable head pointer."""
    if db is None:
        return None
    surface_key = _valid_id(surface_key, field="surface key")
    raw = db.get_meta(_surface_head_key(surface_key))
    record = None
    if raw is not None:
        try:
            pointer = json.loads(raw)
            if isinstance(pointer, dict) and pointer.get("surface_key") == surface_key:
                admission_id = _valid_id(pointer.get("admission_id"), field="admission id")
                candidate = get_pending_input_recovery(db, admission_id)
                if (
                    isinstance(candidate, dict)
                    and candidate.get("status") in {"pending", "claimed"}
                    and candidate.get("surface_key") == surface_key
                    and not (
                        candidate.get("status") == "claimed"
                        and _claimed_recovery_has_recovery_successor(db, candidate)
                    )
                ):
                    record = candidate
        except (TypeError, ValueError, json.JSONDecodeError):
            record = None
    if record is not None:
        return record

    # The pointed record may have settled while older work remains. Advance to
    # the newest still-outstanding record instead of stranding the surface queue.
    candidates = [
        value for value in list_pending_input_recoveries(db)
        if value.get("surface_key") == surface_key
    ]
    if not candidates:
        return None
    record = candidates[-1]
    db.set_meta(
        _surface_head_key(surface_key),
        json.dumps(
            {
                "admission_id": record["admission_id"],
                "surface_key": surface_key,
                "updated_at": time.time(),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )
    return record


def _claimed_recovery_has_recovery_successor(db: Any, record: Dict[str, Any]) -> bool:
    resend_id = record.get("resend_id")
    if not resend_id:
        return False

    try:
        resend_id = _valid_id(resend_id, field="resend id")
        return get_pending_input_recovery(db, resend_id) is not None
    except AttributeError:
        # SessionDB always supplies the read funnel; fail closed for substitutes.
        return True


def list_pending_input_recoveries(
    db: Any, *, surface_prefix: Optional[str] = None,
) -> List[Dict[str, Any]]:
    values: List[Dict[str, Any]] = []
    for _key, raw in db.list_meta_prefix(PENDING_INPUT_RECOVERY_PREFIX) if db is not None else ():
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict) or value.get("status") not in {"pending", "claimed"}:
            continue
        if surface_prefix and not str(value.get("surface_key") or "").startswith(surface_prefix):
            continue
        if value.get("status") == "claimed" and _claimed_recovery_has_recovery_successor(db, value):
            continue
        values.append(value)
    return sorted(values, key=lambda value: float(value.get("created_at") or 0.0))


def consume_pending_input_recovery(
    db: Any, admission_id: str, *, resend_id: str,
    provider_response_received: bool = False,
    require_durable_response: bool = False,
) -> Tuple[Dict[str, Any], bool]:
    """Consume only after provider response or a replacement recovery is durable."""
    key = _recovery_key(admission_id)
    resend_id = _valid_id(resend_id, field="resend id")

    def _write(conn):
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise PendingInputRecoveryError("pending-input recovery was not found")
        try:
            value = json.loads(row[0])
        except (TypeError, json.JSONDecodeError) as exc:
            raise PendingInputRecoveryError("pending-input recovery is invalid") from exc
        status = value.get("status")
        if status == "consumed":
            if value.get("resend_id") != resend_id:
                raise PendingInputRecoveryError("pending-input recovery was already consumed")
            return value, False
        if status != "claimed" or value.get("resend_id") != resend_id:
            raise PendingInputRecoveryError("pending-input recovery has no matching claim")
        if provider_response_received and not (
            value.get("dispatch_state") == "started"
            and value.get("dispatch_resend_id") == resend_id
            and int(value.get("dispatch_generation") or 0)
            == int(value.get("claim_generation") or 0)
        ):
            raise PendingInputRecoveryError(
                "pending-input recovery has no matching provider dispatch"
            )
        if (
            provider_response_received
            and require_durable_response
            and not _durable_resend_has_response_conn(
            conn, resend_id, value,
            )
        ):
            raise PendingInputRecoveryError(
                "pending-input recovery provider response is not durably projected"
            )
        if not provider_response_received and not _durable_recovery_successor_exists_conn(conn, resend_id):
            raise PendingInputRecoveryError("pending-input recovery has no provider or recovery successor")
        value.update(
            status="consumed",
            resend_id=resend_id,
            consumed_at=time.time(),
            provider_response_received=bool(provider_response_received),
            updated_at=time.time(),
        )
        conn.execute("UPDATE state_meta SET value = ? WHERE key = ?", (
            json.dumps(value, ensure_ascii=False, sort_keys=True), key,
        ))
        return value, True

    return db._execute_write(_write)
