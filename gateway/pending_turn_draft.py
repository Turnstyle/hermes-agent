"""Private, durable preservation of accepted gateway input after preflight timeout.

Recovery rows are deliberately non-executable. They keep a bounded, redacted recovery
aid while the user starts a fresh session and manually resends the request. Admission
finds generations by a hashed route plus principal; mutable route metadata is not an
authority pointer.

There is one ``state_meta`` row per opaque draft generation. A later stage for the same
route receives a different key and cannot overwrite an earlier recovery generation.
SQLite does not enable forensic secure deletion, so deletion is logical erasure rather
than a promise about WAL pages or backups.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from gateway.whatsapp_identity import canonical_whatsapp_identifier


PENDING_TURN_DRAFT_PREFIX = "pending_turn_recovery:v1:"
PENDING_TURN_DRAFT_VERSION = 1
PENDING_TURN_DRAFT_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_PENDING_TURN_DRAFT_BYTES = 64 * 1024
MAX_PENDING_TURN_DRAFT_RECORD_BYTES = 512 * 1024
MAX_PENDING_TURN_DRAFT_RECORDS = 64
MAX_PENDING_TURN_DRAFT_MESSAGE_IDS = 64

PENDING_TURN_MANUAL_RESEND_NOTICE = (
    "A recovery record, if one is available for this chat, is private and cannot be "
    "executed. Resend the original request manually as a new message. Attachments and "
    "one-shot or per-turn semantics were not retained."
)
PENDING_TURN_REDELIVERY_NOTICE = (
    "This platform delivery was already preserved during a session reset and will not "
    "be run automatically. Resend the request as a new message. Attachments and one-shot "
    "or per-turn semantics were not retained."
)
PENDING_TURN_RECOVERY_UNAVAILABLE_NOTICE = (
    "Session state for this chat is unavailable, so this message was not processed. "
    "Ask the operator to inspect state.db, then resend after it is healthy. Start a new "
    "session only if you intentionally want to leave the current conversation."
)

_TRUNCATION_MARKER = "\n\n[recovery record omitted unsafe middle bytes]\n\n"
_SESSION_HASH_DOMAIN = b"pending-turn-recovery:v1\0"
_MANUAL_RESEND_HASH_DOMAIN = b"pending-turn-manual-resend:v1\0"
_PREFIX_UPPER_BOUND = f"{PENDING_TURN_DRAFT_PREFIX}\U0010ffff"
_INPUT_KINDS = frozenset({"plain_text", "structured", "native_image", "moa"})
_LOSS_REASONS = frozenset({
    "attachments_not_retained",
    "per_turn_semantics_not_retained",
    "redaction",
    "structured_components_not_retained",
    "surrogate_normalization",
    "truncation",
})
_KIND_REQUIRED_REASON = {
    "structured": "structured_components_not_retained",
    "native_image": "attachments_not_retained",
    "moa": "per_turn_semantics_not_retained",
}


class PendingTurnDraftError(RuntimeError):
    """Base class for fail-closed pending-recovery operations."""


class PendingTurnDraftConflict(PendingTurnDraftError):
    """The recovery generation changed before this caller could link it."""


class PendingTurnDraftUnauthorized(PendingTurnDraftError):
    """The caller does not own the recovery record's route and principal."""


@dataclass(frozen=True)
class PendingTurnDraft:
    """Validated projection of one private, manual-resend-only ``state_meta`` row."""

    version: int
    kind: str
    draft_id: str
    record_id: str
    disposition: str
    executable: bool
    text: str
    input_kind: str
    exact: bool
    lossy: bool
    loss_reasons: list[str]
    redaction_changed: bool
    normalization_changed: bool
    truncated: bool
    stored_bytes: int
    predecessor_session_id: str
    successor_session_id: Optional[str]
    session_key_hash: str
    principal_fingerprint: str
    platform_message_id: str
    platform_message_ids: list[str]
    manual_resend_acknowledged_at: Optional[str]
    manual_resend_message_id_hash: Optional[str]
    turn_exit_reason: str
    created_at: str
    updated_at: str
    expires_at: str


@dataclass(frozen=True)
class PendingTurnResendAcknowledgement:
    """Opaque binding between one recovery generation and one resend delivery."""

    draft_id: str
    session_key_hash: str
    principal_fingerprint: str
    manual_resend_message_id_hash: str


@dataclass(frozen=True)
class _PendingTurnDraftMetadata:
    """Validated recovery metadata with no retained user text."""

    key: str
    draft_id: str
    session_key_hash: str
    principal_fingerprint: str
    platform_message_ids: tuple[str, ...]
    acknowledged: bool
    acknowledgement_hash: Optional[str]
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


def _utc_now(now: Optional[datetime] = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def _session_key_hash(session_key: str) -> str:
    session_key = str(session_key or "").strip()
    if not session_key:
        raise PendingTurnDraftError("pending recovery session key is missing")
    return hashlib.sha256(
        _SESSION_HASH_DOMAIN + session_key.encode("utf-8")
    ).hexdigest()


def _valid_hex_digest(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(ch in "0123456789abcdef" for ch in value)
    )


def _validated_platform_message_ids(
    platform_message_id: Any,
    platform_message_ids: Optional[Sequence[Any]] = None,
) -> list[str]:
    """Return a bounded, ordered set of stable delivery identifiers."""

    if platform_message_ids is None:
        platform_message_ids = ()
    if isinstance(platform_message_ids, (str, bytes)) or not isinstance(
        platform_message_ids, Sequence
    ):
        raise PendingTurnDraftError("platform message ids are invalid")
    candidates = []
    if platform_message_id is not None:
        candidates.append(platform_message_id)
    candidates.extend(platform_message_ids)
    normalized: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        if isinstance(value, str) and not value.strip():
            raise PendingTurnDraftError(
                "platform message id is required for safe recovery"
            )
        if not isinstance(value, str) or value != value.strip() or len(value) > 512:
            raise PendingTurnDraftError("platform message id is invalid")
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    if not normalized:
        raise PendingTurnDraftError("platform message id is required for safe recovery")
    if len(normalized) > MAX_PENDING_TURN_DRAFT_MESSAGE_IDS:
        raise PendingTurnDraftError("too many platform message ids for safe recovery")
    return normalized


def _manual_resend_message_id_hash(message_ids: Sequence[str]) -> str:
    wire = json.dumps(
        list(message_ids), ensure_ascii=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(_MANUAL_RESEND_HASH_DOMAIN + wire.encode("utf-8")).hexdigest()


def _pointer_parts(draft_id: str) -> tuple[str, str, int]:
    value = str(draft_id or "").strip()
    parts = value.split(".", 2)
    if len(parts) != 3 or not _valid_hex_digest(parts[0]):
        raise PendingTurnDraftError("invalid pending recovery id")
    try:
        expires_epoch = int(parts[1], 16)
        record_id = str(uuid.UUID(parts[2]))
    except (ValueError, AttributeError) as exc:
        raise PendingTurnDraftError("invalid pending recovery id") from exc
    if (
        expires_epoch <= 0
        or len(parts[1]) > 16
        or format(expires_epoch, "x") != parts[1].lower()
        or record_id != parts[2].lower()
    ):
        raise PendingTurnDraftError("invalid pending recovery id")
    return parts[0], record_id, expires_epoch


def _draft_key(draft_id: str) -> str:
    """Return the immutable row key for one validated opaque generation."""

    _pointer_parts(draft_id)
    return f"{PENDING_TURN_DRAFT_PREFIX}{draft_id}"


def pending_turn_principal_fingerprint(source: Any) -> str:
    """Hash the authenticated sender plus its messaging route.

    A DM whose adapter has no separate sender id may use its unique chat id as
    the principal. A shared room without a sender id fails closed.
    """

    platform = getattr(getattr(source, "platform", None), "value", None)
    platform = str(platform or getattr(source, "platform", "") or "").strip().lower()
    chat_type = str(getattr(source, "chat_type", "") or "").lower()
    chat_id = str(getattr(source, "chat_id", "") or "")
    user_id = str(
        getattr(source, "user_id_alt", None) or getattr(source, "user_id", None) or ""
    )
    if platform == "whatsapp":
        if chat_type in {"", "dm", "direct", "private"}:
            chat_id = canonical_whatsapp_identifier(chat_id)
        if user_id:
            user_id = canonical_whatsapp_identifier(user_id)
    if not platform or not chat_id:
        raise PendingTurnDraftUnauthorized("gateway source lacks a stable route")
    if not user_id:
        if chat_type not in {"", "dm", "direct", "private"}:
            raise PendingTurnDraftUnauthorized(
                "shared gateway source lacks a stable principal"
            )
        user_id = f"dm-chat:{chat_id}"
    identity = {
        "platform": platform,
        "profile": str(getattr(source, "profile", "") or "default"),
        "scope_id": str(
            getattr(source, "scope_id", None) or getattr(source, "guild_id", None) or ""
        ),
        "chat_type": chat_type,
        "chat_id": chat_id,
        "thread_id": str(getattr(source, "thread_id", "") or ""),
        "principal": user_id,
    }
    wire = json.dumps(
        identity, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


def _bounded_text(text: str) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_PENDING_TURN_DRAFT_BYTES:
        return text, False
    marker = _TRUNCATION_MARKER.encode("utf-8")
    available = MAX_PENDING_TURN_DRAFT_BYTES - len(marker)
    head_n = available // 2
    tail_n = available - head_n
    head = encoded[:head_n].decode("utf-8", errors="ignore")
    tail = encoded[-tail_n:].decode("utf-8", errors="ignore")
    bounded = head + _TRUNCATION_MARKER + tail
    while len(bounded.encode("utf-8")) > MAX_PENDING_TURN_DRAFT_BYTES:
        tail = tail[1:]
        bounded = head + _TRUNCATION_MARKER + tail
    return bounded, True


def _decode(
    raw: Optional[str],
    *,
    expected_id: Optional[str] = None,
    expected_hash: Optional[str] = None,
) -> PendingTurnDraft:
    if raw is None:
        raise PendingTurnDraftError("pending recovery record not found")
    try:
        if len(raw.encode("utf-8")) > MAX_PENDING_TURN_DRAFT_RECORD_BYTES:
            raise ValueError("record exceeds size bound")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("record is not an object")
        value.setdefault("platform_message_ids", [value.get("platform_message_id")])
        value.setdefault("manual_resend_acknowledged_at", None)
        value.setdefault("manual_resend_message_id_hash", None)
        draft = PendingTurnDraft(**value)
        created_at = _parse_timestamp(draft.created_at)
        updated_at = _parse_timestamp(draft.updated_at)
        expires_at = _parse_timestamp(draft.expires_at)
        if not isinstance(draft.text, str):
            raise ValueError("record text is not a string")
        encoded_len = len(draft.text.encode("utf-8"))
        pointer_hash, pointer_record_id, pointer_expires_epoch = _pointer_parts(
            draft.draft_id
        )
        message_ids = _validated_platform_message_ids(
            draft.platform_message_id, draft.platform_message_ids
        )
        acknowledged_at = (
            _parse_timestamp(draft.manual_resend_acknowledged_at)
            if draft.manual_resend_acknowledged_at is not None
            else None
        )
    except (
        AttributeError,
        PendingTurnDraftError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        raise PendingTurnDraftError("pending recovery record is invalid") from exc

    flags_valid = all(
        isinstance(flag, bool)
        for flag in (
            draft.executable,
            draft.exact,
            draft.lossy,
            draft.redaction_changed,
            draft.normalization_changed,
            draft.truncated,
        )
    )
    reasons_valid = bool(
        isinstance(draft.loss_reasons, list)
        and all(
            isinstance(reason, str) and reason in _LOSS_REASONS
            for reason in draft.loss_reasons
        )
        and len(draft.loss_reasons) == len(set(draft.loss_reasons))
    )
    reason_set = set(draft.loss_reasons) if reasons_valid else set()
    required_reason = _KIND_REQUIRED_REASON.get(draft.input_kind)
    transformation_reasons_match = (
        draft.redaction_changed == ("redaction" in reason_set)
        and draft.normalization_changed == ("surrogate_normalization" in reason_set)
        and draft.truncated == ("truncation" in reason_set)
    )
    if (
        draft.version != PENDING_TURN_DRAFT_VERSION
        or draft.kind != "pending_turn_recovery"
        or draft.disposition != "manual_resend_only"
        or draft.executable is not False
        or not isinstance(draft.text, str)
        or not draft.text.strip()
        or not flags_valid
        or not reasons_valid
        or draft.input_kind not in _INPUT_KINDS
        or (required_reason is not None and required_reason not in reason_set)
        or not transformation_reasons_match
        or draft.lossy != bool(reason_set)
        or draft.exact != (not draft.lossy)
        or (draft.exact and draft.input_kind != "plain_text")
        or not isinstance(draft.stored_bytes, int)
        or isinstance(draft.stored_bytes, bool)
        or draft.stored_bytes != encoded_len
        or encoded_len > MAX_PENDING_TURN_DRAFT_BYTES
        or not isinstance(draft.predecessor_session_id, str)
        or not draft.predecessor_session_id.strip()
        or len(draft.predecessor_session_id) > 512
        or (
            draft.successor_session_id is not None
            and (
                not isinstance(draft.successor_session_id, str)
                or not draft.successor_session_id.strip()
                or len(draft.successor_session_id) > 512
            )
        )
        or not _valid_hex_digest(draft.session_key_hash)
        or not _valid_hex_digest(draft.principal_fingerprint)
        or pointer_hash != draft.session_key_hash
        or pointer_record_id != draft.record_id
        or pointer_expires_epoch != int(expires_at.timestamp())
        or draft.draft_id
        != f"{draft.session_key_hash}.{pointer_expires_epoch:x}.{draft.record_id}"
        or (expected_id is not None and draft.draft_id != expected_id)
        or (expected_hash is not None and draft.session_key_hash != expected_hash)
        or draft.platform_message_ids != message_ids
        or draft.platform_message_id != message_ids[0]
        or (
            (draft.manual_resend_acknowledged_at is None)
            != (draft.manual_resend_message_id_hash is None)
        )
        or (
            acknowledged_at is not None
            and (
                not _valid_hex_digest(draft.manual_resend_message_id_hash)
                or not (created_at <= acknowledged_at <= updated_at < expires_at)
            )
        )
        or draft.turn_exit_reason != "context_compression_timeout"
        or not (created_at <= updated_at < expires_at)
    ):
        raise PendingTurnDraftError("pending recovery record failed validation")
    return draft


def _encode(draft: PendingTurnDraft) -> str:
    return json.dumps(
        asdict(draft),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _is_expired(draft: PendingTurnDraft, now: datetime) -> bool:
    return _parse_timestamp(draft.expires_at) <= now


def _json_metadata_value(path: str) -> str:
    return (
        "CASE WHEN length(CAST(value AS BLOB)) > "
        f"{MAX_PENDING_TURN_DRAFT_RECORD_BYTES} THEN NULL "
        f"WHEN json_valid(value) THEN json_extract(value, '{path}') END"
    )


def _json_metadata_type(path: str) -> str:
    return (
        "CASE WHEN length(CAST(value AS BLOB)) > "
        f"{MAX_PENDING_TURN_DRAFT_RECORD_BYTES} THEN NULL "
        f"WHEN json_valid(value) THEN json_type(value, '{path}') END"
    )


_PENDING_TURN_DRAFT_METADATA_COLUMNS = (
    "key",
    "length(CAST(value AS BLOB))",
    "CASE WHEN length(CAST(value AS BLOB)) > "
    f"{MAX_PENDING_TURN_DRAFT_RECORD_BYTES} THEN 0 ELSE json_valid(value) END",
    _json_metadata_value("$.version"),
    _json_metadata_value("$.kind"),
    _json_metadata_value("$.draft_id"),
    _json_metadata_value("$.record_id"),
    _json_metadata_value("$.disposition"),
    _json_metadata_value("$.executable"),
    _json_metadata_type("$.executable"),
    _json_metadata_type("$.text"),
    "CASE WHEN length(CAST(value AS BLOB)) > "
    f"{MAX_PENDING_TURN_DRAFT_RECORD_BYTES} THEN NULL WHEN json_valid(value) "
    "THEN length(CAST(json_extract(value, '$.text') AS BLOB)) END",
    "CASE WHEN length(CAST(value AS BLOB)) > "
    f"{MAX_PENDING_TURN_DRAFT_RECORD_BYTES} THEN NULL WHEN json_valid(value) "
    "THEN length(trim(json_extract(value, '$.text'))) END",
    _json_metadata_value("$.stored_bytes"),
    _json_metadata_value("$.input_kind"),
    _json_metadata_value("$.exact"),
    _json_metadata_type("$.exact"),
    _json_metadata_value("$.lossy"),
    _json_metadata_type("$.lossy"),
    _json_metadata_value("$.loss_reasons"),
    _json_metadata_type("$.loss_reasons"),
    _json_metadata_value("$.redaction_changed"),
    _json_metadata_type("$.redaction_changed"),
    _json_metadata_value("$.normalization_changed"),
    _json_metadata_type("$.normalization_changed"),
    _json_metadata_value("$.truncated"),
    _json_metadata_type("$.truncated"),
    _json_metadata_value("$.predecessor_session_id"),
    _json_metadata_value("$.successor_session_id"),
    _json_metadata_value("$.session_key_hash"),
    _json_metadata_value("$.principal_fingerprint"),
    _json_metadata_value("$.platform_message_id"),
    _json_metadata_value("$.platform_message_ids"),
    _json_metadata_type("$.platform_message_ids"),
    _json_metadata_value("$.turn_exit_reason"),
    _json_metadata_value("$.created_at"),
    _json_metadata_value("$.updated_at"),
    _json_metadata_value("$.expires_at"),
    _json_metadata_value("$.manual_resend_acknowledged_at"),
    _json_metadata_type("$.manual_resend_acknowledged_at"),
    _json_metadata_value("$.manual_resend_message_id_hash"),
    _json_metadata_type("$.manual_resend_message_id_hash"),
)
_PENDING_TURN_DRAFT_METADATA_QUERY = (
    f"SELECT {', '.join(_PENDING_TURN_DRAFT_METADATA_COLUMNS)} "
    "FROM state_meta WHERE key >= ? AND key < ? ORDER BY key LIMIT ?"
)


def _json_boolean(value: Any, value_type: Any) -> Optional[bool]:
    if value_type == "true" and value == 1:
        return True
    if value_type == "false" and value == 0:
        return False
    return None


def _metadata_from_row(
    row: Any, *, now: datetime
) -> Optional[_PendingTurnDraftMetadata]:
    """Validate one bounded projection without materializing retained text."""

    values = tuple(row)
    if len(values) != len(_PENDING_TURN_DRAFT_METADATA_COLUMNS):
        return None
    (
        key,
        record_bytes,
        json_valid,
        version,
        kind,
        draft_id,
        record_id,
        disposition,
        executable,
        executable_type,
        text_type,
        text_bytes,
        text_trimmed_chars,
        stored_bytes,
        input_kind,
        exact_value,
        exact_type,
        lossy_value,
        lossy_type,
        loss_reasons_json,
        loss_reasons_type,
        redaction_value,
        redaction_type,
        normalization_value,
        normalization_type,
        truncated_value,
        truncated_type,
        predecessor_session_id,
        successor_session_id,
        session_hash,
        principal,
        platform_message_id,
        platform_message_ids_json,
        platform_message_ids_type,
        turn_exit_reason,
        created_at_raw,
        updated_at_raw,
        expires_at_raw,
        acknowledged_at_raw,
        acknowledged_at_type,
        acknowledgement_hash,
        acknowledgement_hash_type,
    ) = values
    try:
        pointer_hash, pointer_record_id, pointer_expires_epoch = _pointer_parts(
            draft_id
        )
        created_at = _parse_timestamp(created_at_raw)
        updated_at = _parse_timestamp(updated_at_raw)
        expires_at = _parse_timestamp(expires_at_raw)
        if loss_reasons_type != "array" or not isinstance(loss_reasons_json, str):
            return None
        loss_reasons = json.loads(loss_reasons_json)
        if platform_message_ids_type in (None, "null"):
            platform_message_ids: Sequence[Any] = ()
        elif platform_message_ids_type == "array" and isinstance(
            platform_message_ids_json, str
        ):
            platform_message_ids = json.loads(platform_message_ids_json)
        else:
            return None
        normalized_message_ids = _validated_platform_message_ids(
            platform_message_id, platform_message_ids
        )
        if acknowledged_at_raw is None and acknowledged_at_type in (None, "null"):
            acknowledged_at = None
        elif acknowledged_at_type == "text":
            acknowledged_at = _parse_timestamp(acknowledged_at_raw)
        else:
            return None
    except (
        AttributeError,
        PendingTurnDraftError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
    ):
        return None

    exact = _json_boolean(exact_value, exact_type)
    lossy = _json_boolean(lossy_value, lossy_type)
    redaction_changed = _json_boolean(redaction_value, redaction_type)
    normalization_changed = _json_boolean(normalization_value, normalization_type)
    truncated = _json_boolean(truncated_value, truncated_type)
    executable_flag = _json_boolean(executable, executable_type)
    reasons_valid = bool(
        isinstance(loss_reasons, list)
        and all(
            isinstance(reason, str) and reason in _LOSS_REASONS
            for reason in loss_reasons
        )
        and len(loss_reasons) == len(set(loss_reasons))
    )
    reason_set = set(loss_reasons) if reasons_valid else set()
    required_reason = _KIND_REQUIRED_REASON.get(input_kind)
    acknowledgement_valid = bool(
        (
            acknowledged_at is None
            and acknowledgement_hash is None
            and acknowledgement_hash_type in (None, "null")
        )
        or (
            acknowledged_at is not None
            and acknowledgement_hash_type == "text"
            and _valid_hex_digest(acknowledgement_hash)
            and created_at <= acknowledged_at <= updated_at < expires_at
        )
    )
    metadata_valid = bool(
        isinstance(key, str)
        and isinstance(record_bytes, int)
        and 0 < record_bytes <= MAX_PENDING_TURN_DRAFT_RECORD_BYTES
        and json_valid == 1
        and version == PENDING_TURN_DRAFT_VERSION
        and kind == "pending_turn_recovery"
        and isinstance(draft_id, str)
        and key == f"{PENDING_TURN_DRAFT_PREFIX}{draft_id}"
        and record_id == pointer_record_id
        and disposition == "manual_resend_only"
        and executable_flag is False
        and text_type == "text"
        and isinstance(text_bytes, int)
        and 0 < text_bytes <= MAX_PENDING_TURN_DRAFT_BYTES
        and text_bytes == stored_bytes
        and isinstance(text_trimmed_chars, int)
        and text_trimmed_chars > 0
        and input_kind in _INPUT_KINDS
        and exact is not None
        and lossy is not None
        and redaction_changed is not None
        and normalization_changed is not None
        and truncated is not None
        and reasons_valid
        and (required_reason is None or required_reason in reason_set)
        and redaction_changed == ("redaction" in reason_set)
        and normalization_changed == ("surrogate_normalization" in reason_set)
        and truncated == ("truncation" in reason_set)
        and lossy == bool(reason_set)
        and exact == (not lossy)
        and (not exact or input_kind == "plain_text")
        and isinstance(predecessor_session_id, str)
        and bool(predecessor_session_id.strip())
        and len(predecessor_session_id) <= 512
        and (
            successor_session_id is None
            or (
                isinstance(successor_session_id, str)
                and bool(successor_session_id.strip())
                and len(successor_session_id) <= 512
            )
        )
        and _valid_hex_digest(session_hash)
        and _valid_hex_digest(principal)
        and pointer_hash == session_hash
        and pointer_expires_epoch == int(expires_at.timestamp())
        and isinstance(record_id, str)
        and draft_id == f"{session_hash}.{pointer_expires_epoch:x}.{record_id}"
        and normalized_message_ids[0] == platform_message_id
        and (
            platform_message_ids_type in (None, "null")
            or list(platform_message_ids) == normalized_message_ids
        )
        and turn_exit_reason == "context_compression_timeout"
        and created_at <= updated_at < expires_at
        and expires_at > now
        and acknowledgement_valid
    )
    if not metadata_valid:
        return None
    return _PendingTurnDraftMetadata(
        key=key,
        draft_id=draft_id,
        session_key_hash=session_hash,
        principal_fingerprint=principal,
        platform_message_ids=tuple(normalized_message_ids),
        acknowledged=acknowledged_at is not None,
        acknowledgement_hash=(
            str(acknowledgement_hash) if acknowledged_at is not None else None
        ),
        created_at=created_at,
        updated_at=updated_at,
        expires_at=expires_at,
    )


def _metadata_rows(reader: Any, *, now: datetime) -> list[Any]:
    return list(
        reader(
            _PENDING_TURN_DRAFT_METADATA_QUERY,
            (
                PENDING_TURN_DRAFT_PREFIX,
                _PREFIX_UPPER_BOUND,
                MAX_PENDING_TURN_DRAFT_RECORDS + 1,
            ),
        )
    )


def _gc_pending_turn_drafts(conn: Any, now: datetime) -> int:
    """Delete a bounded batch of malformed/expired rows in the caller's transaction."""

    rows = conn.execute(
        "SELECT key, value FROM state_meta WHERE key >= ? AND key < ? "
        "ORDER BY key LIMIT ?",
        (
            PENDING_TURN_DRAFT_PREFIX,
            _PREFIX_UPPER_BOUND,
            MAX_PENDING_TURN_DRAFT_RECORDS + 1,
        ),
    ).fetchall()
    deleted = 0
    for key, raw in rows:
        draft_id = str(key)[len(PENDING_TURN_DRAFT_PREFIX) :]
        try:
            session_hash, _, _ = _pointer_parts(draft_id)
            draft = _decode(raw, expected_id=draft_id, expected_hash=session_hash)
        except PendingTurnDraftError:
            conn.execute("DELETE FROM state_meta WHERE key = ?", (key,))
            deleted += 1
            continue
        if _is_expired(draft, now):
            conn.execute("DELETE FROM state_meta WHERE key = ?", (key,))
            deleted += 1
    return deleted


def gc_pending_turn_drafts(db: Any, *, now: Optional[datetime] = None) -> int:
    """Atomically remove malformed/expired draft rows and return the delete count."""

    if db is None or not hasattr(db, "_execute_write"):
        raise PendingTurnDraftError("durable SessionDB is unavailable")
    timestamp = _utc_now(now)
    try:
        return int(
            db._execute_write(lambda conn: _gc_pending_turn_drafts(conn, timestamp))
        )
    except PendingTurnDraftError:
        raise
    except Exception as exc:
        raise PendingTurnDraftError(
            "pending recovery cleanup could not be persisted"
        ) from exc


def delete_pending_turn_draft(db: Any, draft_id: str) -> bool:
    """Delete exactly one opaque generation, never another draft for the same route."""

    if db is None or not hasattr(db, "_execute_write"):
        raise PendingTurnDraftError("durable SessionDB is unavailable")
    key = _draft_key(draft_id)

    def _delete(conn: Any) -> bool:
        cursor = conn.execute("DELETE FROM state_meta WHERE key = ?", (key,))
        rowcount = cursor.rowcount
        if rowcount is None or rowcount < 0:
            rowcount = conn.execute("SELECT changes()").fetchone()[0]
        return bool(rowcount)

    try:
        return bool(db._execute_write(_delete))
    except PendingTurnDraftError:
        raise
    except Exception as exc:
        raise PendingTurnDraftError(
            "pending recovery record could not be deleted"
        ) from exc


def stage_pending_turn_draft(
    db: Any,
    *,
    text: str,
    predecessor_session_id: str,
    session_key: str,
    principal_fingerprint: str,
    platform_message_id: Optional[str] = None,
    platform_message_ids: Optional[Sequence[str]] = None,
    turn_exit_reason: str = "context_compression_timeout",
    input_kind: str = "plain_text",
    input_loss_reasons: Sequence[str] = (),
    input_normalization_changed: bool = False,
    now: Optional[datetime] = None,
) -> PendingTurnDraft:
    """Redact, bound and atomically create one non-executable recovery generation."""

    if db is None or not hasattr(db, "_execute_write"):
        raise PendingTurnDraftError("durable SessionDB is unavailable")
    if not isinstance(text, str) or not text.strip():
        raise PendingTurnDraftError("accepted input recovery text is empty")
    if input_kind not in _INPUT_KINDS:
        raise PendingTurnDraftError("accepted input kind is invalid")
    if (
        isinstance(input_loss_reasons, (str, bytes))
        or not isinstance(input_loss_reasons, Sequence)
        or not all(
            isinstance(reason, str) and reason in _LOSS_REASONS
            for reason in input_loss_reasons
        )
    ):
        raise PendingTurnDraftError("accepted input loss reasons are invalid")
    if not isinstance(input_normalization_changed, bool):
        raise PendingTurnDraftError("input normalization marker is invalid")

    predecessor_session_id = str(predecessor_session_id or "").strip()
    session_key = str(session_key or "").strip()
    principal_fingerprint = str(principal_fingerprint or "").strip().lower()
    if (
        not predecessor_session_id
        or len(predecessor_session_id) > 512
        or not session_key
        or not _valid_hex_digest(principal_fingerprint)
    ):
        raise PendingTurnDraftError("pending recovery provenance is incomplete")
    if str(turn_exit_reason or "") != "context_compression_timeout":
        raise PendingTurnDraftError("pending recovery exit reason is invalid")
    message_ids = _validated_platform_message_ids(
        platform_message_id, platform_message_ids
    )
    platform_id = message_ids[0]

    normalized = text.encode("utf-8", errors="replace").decode("utf-8")
    normalization_changed = input_normalization_changed or normalized != text
    try:
        from agent.redact import redact_sensitive_text

        redacted = redact_sensitive_text(normalized, force=True)
    except Exception as exc:
        raise PendingTurnDraftError(
            "accepted input could not be redacted safely"
        ) from exc
    if not isinstance(redacted, str):
        raise PendingTurnDraftError("accepted input redactor returned invalid text")
    if not redacted.strip():
        raise PendingTurnDraftError("accepted input is empty after redaction")
    redaction_changed = redacted != normalized
    stored, truncated = _bounded_text(redacted)

    reasons = set(input_loss_reasons)
    required_reason = _KIND_REQUIRED_REASON.get(input_kind)
    if required_reason is not None:
        reasons.add(required_reason)
    if normalization_changed:
        reasons.add("surrogate_normalization")
    if redaction_changed:
        reasons.add("redaction")
    if truncated:
        reasons.add("truncation")
    loss_reasons = sorted(reasons)
    lossy = bool(loss_reasons)

    timestamp = _utc_now(now)
    expires_at = timestamp + timedelta(seconds=PENDING_TURN_DRAFT_TTL_SECONDS)
    session_hash = _session_key_hash(session_key)
    record_id = str(uuid.uuid4())
    draft_id = f"{session_hash}.{int(expires_at.timestamp()):x}.{record_id}"
    draft = PendingTurnDraft(
        version=PENDING_TURN_DRAFT_VERSION,
        kind="pending_turn_recovery",
        draft_id=draft_id,
        record_id=record_id,
        disposition="manual_resend_only",
        executable=False,
        text=stored,
        input_kind=input_kind,
        exact=not lossy,
        lossy=lossy,
        loss_reasons=loss_reasons,
        redaction_changed=redaction_changed,
        normalization_changed=normalization_changed,
        truncated=truncated,
        stored_bytes=len(stored.encode("utf-8")),
        predecessor_session_id=predecessor_session_id,
        successor_session_id=None,
        session_key_hash=session_hash,
        principal_fingerprint=principal_fingerprint,
        platform_message_id=platform_id,
        platform_message_ids=message_ids,
        manual_resend_acknowledged_at=None,
        manual_resend_message_id_hash=None,
        turn_exit_reason="context_compression_timeout",
        created_at=timestamp.isoformat(),
        updated_at=timestamp.isoformat(),
        expires_at=expires_at.isoformat(),
    )
    key, raw = _draft_key(draft_id), _encode(draft)
    if len(raw.encode("utf-8")) > MAX_PENDING_TURN_DRAFT_RECORD_BYTES:
        raise PendingTurnDraftError("pending recovery record exceeds its size bound")

    def _insert(conn):
        _gc_pending_turn_drafts(conn, timestamp)
        live = conn.execute(
            "SELECT key FROM state_meta WHERE key >= ? AND key < ? "
            "ORDER BY key LIMIT ?",
            (
                PENDING_TURN_DRAFT_PREFIX,
                _PREFIX_UPPER_BOUND,
                MAX_PENDING_TURN_DRAFT_RECORDS,
            ),
        ).fetchall()
        if len(live) >= MAX_PENDING_TURN_DRAFT_RECORDS:
            return False
        conn.execute("INSERT INTO state_meta(key, value) VALUES (?, ?)", (key, raw))
        return True

    try:
        persisted = db._execute_write(_insert)
    except PendingTurnDraftError:
        raise
    except Exception as exc:
        raise PendingTurnDraftError(
            "pending recovery record could not be persisted"
        ) from exc
    if not persisted:
        raise PendingTurnDraftError("pending recovery record capacity is full")
    return draft


def load_pending_turn_draft(
    db: Any, draft_id: str, *, now: Optional[datetime] = None
) -> PendingTurnDraft:
    """Load one exact generation, lazily deleting malformed or expired content."""

    if db is None or not hasattr(db, "_execute_write"):
        raise PendingTurnDraftError("durable SessionDB is unavailable")
    session_hash, _, _ = _pointer_parts(draft_id)
    key = _draft_key(draft_id)
    timestamp = _utc_now(now)

    def _load(conn):
        _gc_pending_turn_drafts(conn, timestamp)
        row = conn.execute(
            "SELECT value FROM state_meta WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            current = _decode(row[0], expected_id=draft_id, expected_hash=session_hash)
        except PendingTurnDraftError:
            conn.execute("DELETE FROM state_meta WHERE key = ?", (key,))
            return None
        if _is_expired(current, timestamp):
            conn.execute("DELETE FROM state_meta WHERE key = ?", (key,))
            return None
        return current

    try:
        draft = db._execute_write(_load)
    except Exception as exc:
        if isinstance(exc, PendingTurnDraftError):
            raise
        raise PendingTurnDraftError(
            "pending recovery record could not be loaded"
        ) from exc
    if draft is None:
        raise PendingTurnDraftError("pending recovery record is unavailable")
    return draft


def _validated_tombstone_owner(
    session_key: str, principal_fingerprint: str
) -> tuple[str, str]:
    try:
        session_hash = _session_key_hash(session_key)
    except PendingTurnDraftError as exc:
        raise PendingTurnDraftUnauthorized("pending recovery route is invalid") from exc
    principal = str(principal_fingerprint or "").strip().lower()
    if not _valid_hex_digest(principal):
        raise PendingTurnDraftUnauthorized("pending recovery principal is invalid")
    return session_hash, principal


def _metadata_matches_owner(
    metadata: _PendingTurnDraftMetadata,
    *,
    session_hash: str,
    principal_fingerprint: str,
) -> bool:
    return bool(
        hmac.compare_digest(metadata.session_key_hash, session_hash)
        and hmac.compare_digest(metadata.principal_fingerprint, principal_fingerprint)
    )


def has_pending_turn_draft_tombstone(
    db: Any,
    *,
    session_key: str,
    principal_fingerprint: str,
    platform_message_id: str,
    now: Optional[datetime] = None,
) -> bool:
    """Whether any live recovery generation owns this platform delivery id.

    The scan is independent of the route's current generation pointer so later
    resets cannot release an older platform redelivery. Only bounded metadata is
    selected; retained recovery text is never returned to Python or the caller.
    """

    if db is None or not hasattr(db, "_read_all"):
        raise PendingTurnDraftError("durable SessionDB is unavailable")
    session_hash, principal = _validated_tombstone_owner(
        session_key, principal_fingerprint
    )
    message_id = _validated_platform_message_ids(None, (platform_message_id,))[0]
    timestamp = _utc_now(now)
    try:
        rows = _metadata_rows(db._read_all, now=timestamp)
        if len(rows) > MAX_PENDING_TURN_DRAFT_RECORDS:
            if not hasattr(db, "_execute_write"):
                raise PendingTurnDraftError(
                    "pending recovery metadata capacity was exceeded"
                )
            metadata_rows = db._execute_write(
                lambda conn: _transaction_metadata_rows(conn, now=timestamp)
            )
        else:
            metadata_rows = [
                metadata
                for row in rows
                if (metadata := _metadata_from_row(row, now=timestamp)) is not None
            ]
    except PendingTurnDraftError:
        raise
    except Exception as exc:
        raise PendingTurnDraftError(
            "pending recovery tombstones could not be read"
        ) from exc
    for metadata in metadata_rows:
        if (
            _metadata_matches_owner(
                metadata,
                session_hash=session_hash,
                principal_fingerprint=principal,
            )
            and message_id in metadata.platform_message_ids
        ):
            return True
    return False


def _transaction_metadata_rows(
    conn: Any, *, now: datetime
) -> list[_PendingTurnDraftMetadata]:
    reader = lambda sql, params: conn.execute(sql, params).fetchall()

    def _validate_and_collect(rows: Sequence[Any]) -> tuple[
        list[_PendingTurnDraftMetadata], bool
    ]:
        valid: list[_PendingTurnDraftMetadata] = []
        deleted_malformed = False
        for row in rows:
            metadata = _metadata_from_row(row, now=now)
            if metadata is None:
                values = tuple(row)
                key = values[0] if values else None
                if isinstance(key, str):
                    conn.execute("DELETE FROM state_meta WHERE key = ?", (key,))
                    deleted_malformed = True
                continue
            valid.append(metadata)
        return valid, deleted_malformed

    def _has_unscanned_tail(rows: Sequence[Any]) -> bool:
        if not rows:
            return False
        values = tuple(rows[-1])
        last_key = values[0] if values else None
        if not isinstance(last_key, str):
            return True
        return (
            conn.execute(
                "SELECT 1 FROM state_meta WHERE key > ? AND key < ? "
                "ORDER BY key LIMIT 1",
                (last_key, _PREFIX_UPPER_BOUND),
            ).fetchone()
            is not None
        )

    rows = _metadata_rows(reader, now=now)
    valid, deleted_malformed = _validate_and_collect(rows)
    if len(valid) > MAX_PENDING_TURN_DRAFT_RECORDS:
        raise PendingTurnDraftError("pending recovery metadata capacity was exceeded")
    if len(rows) <= MAX_PENDING_TURN_DRAFT_RECORDS:
        return valid

    # A malformed row must not consume the overflow sentinel and hide a valid row
    # behind it. Re-read at most once after cleanup. The two capped projections
    # and two single-row tail probes keep this work bounded even if an attacker
    # fills the prefix with poison rows.
    if not deleted_malformed:
        raise PendingTurnDraftError("pending recovery metadata capacity was exceeded")
    if not _has_unscanned_tail(rows):
        return valid
    rows = _metadata_rows(reader, now=now)
    valid, deleted_malformed = _validate_and_collect(rows)
    if len(valid) > MAX_PENDING_TURN_DRAFT_RECORDS:
        raise PendingTurnDraftError("pending recovery metadata capacity was exceeded")
    if len(rows) <= MAX_PENDING_TURN_DRAFT_RECORDS:
        return valid
    if not deleted_malformed:
        raise PendingTurnDraftError("pending recovery metadata capacity was exceeded")
    if _has_unscanned_tail(rows):
        raise PendingTurnDraftError("pending recovery metadata capacity was exceeded")
    return valid


def has_unacknowledged_pending_turn_draft(
    db: Any,
    *,
    session_key: str,
    principal_fingerprint: str,
    now: Optional[datetime] = None,
) -> bool:
    """Atomically GC and detect live, unacknowledged recovery generations."""

    if db is None or not hasattr(db, "_execute_write"):
        raise PendingTurnDraftError("durable SessionDB is unavailable")
    session_hash, principal = _validated_tombstone_owner(
        session_key, principal_fingerprint
    )
    timestamp = _utc_now(now)

    def _check(conn: Any) -> bool:
        return any(
            not metadata.acknowledged
            and _metadata_matches_owner(
                metadata,
                session_hash=session_hash,
                principal_fingerprint=principal,
            )
            for metadata in _transaction_metadata_rows(conn, now=timestamp)
        )

    try:
        return bool(db._execute_write(_check))
    except PendingTurnDraftError:
        raise
    except Exception as exc:
        raise PendingTurnDraftError(
            "pending recovery acknowledgement state could not be read"
        ) from exc


def plan_pending_turn_resend_acknowledgement(
    db: Any,
    *,
    session_key: str,
    principal_fingerprint: str,
    manual_message_ids: Sequence[str],
    now: Optional[datetime] = None,
) -> tuple[Optional[PendingTurnResendAcknowledgement], bool]:
    """Bind a resend to one generation without consuming it.

    Returns ``(plan, replayed)``. ``replayed`` is true when these delivery ids
    already belong to either the preserved input or a committed resend. A plan
    contains hashes only and is safe to carry until turn preparation and provider
    admission have completed.
    """

    if db is None or not hasattr(db, "_execute_write"):
        raise PendingTurnDraftError("durable SessionDB is unavailable")
    session_hash, principal = _validated_tombstone_owner(
        session_key, principal_fingerprint
    )
    message_ids = _validated_platform_message_ids(None, manual_message_ids)
    message_id_set = set(message_ids)
    acknowledgement_hash = _manual_resend_message_id_hash(message_ids)
    timestamp = _utc_now(now)

    def _plan(conn: Any):
        matching = [
            metadata
            for metadata in _transaction_metadata_rows(conn, now=timestamp)
            if _metadata_matches_owner(
                metadata,
                session_hash=session_hash,
                principal_fingerprint=principal,
            )
        ]
        if any(
            message_id_set.intersection(item.platform_message_ids) for item in matching
        ):
            return None, True
        if any(
            item.acknowledgement_hash is not None
            and hmac.compare_digest(item.acknowledgement_hash, acknowledgement_hash)
            for item in matching
        ):
            return None, True
        unacknowledged = [item for item in matching if not item.acknowledged]
        if not unacknowledged:
            return None, False
        metadata = min(
            unacknowledged,
            key=lambda item: (item.created_at, item.key),
        )
        return PendingTurnResendAcknowledgement(
            draft_id=metadata.draft_id,
            session_key_hash=session_hash,
            principal_fingerprint=principal,
            manual_resend_message_id_hash=acknowledgement_hash,
        ), False

    try:
        result = db._execute_write(_plan)
    except PendingTurnDraftError:
        raise
    except Exception as exc:
        raise PendingTurnDraftError(
            "pending recovery acknowledgement could not be prepared"
        ) from exc
    if (
        not isinstance(result, tuple)
        or len(result) != 2
        or (
            result[0] is not None
            and not isinstance(result[0], PendingTurnResendAcknowledgement)
        )
        or not isinstance(result[1], bool)
    ):
        raise PendingTurnDraftError("pending recovery acknowledgement plan is invalid")
    return result


def commit_pending_turn_resend_acknowledgement(
    db: Any,
    acknowledgement: PendingTurnResendAcknowledgement,
    *,
    session_key: str,
    principal_fingerprint: str,
    manual_message_ids: Sequence[str],
    now: Optional[datetime] = None,
) -> bool:
    """Commit one exact acknowledgement; return false for an idempotent replay."""

    if db is None or not hasattr(db, "_execute_write"):
        raise PendingTurnDraftError("durable SessionDB is unavailable")
    if not isinstance(acknowledgement, PendingTurnResendAcknowledgement):
        raise PendingTurnDraftError("pending recovery acknowledgement plan is invalid")
    if not (
        _valid_hex_digest(acknowledgement.session_key_hash)
        and _valid_hex_digest(acknowledgement.principal_fingerprint)
        and _valid_hex_digest(acknowledgement.manual_resend_message_id_hash)
    ):
        raise PendingTurnDraftError("pending recovery acknowledgement plan is invalid")
    session_hash, principal = _validated_tombstone_owner(
        session_key, principal_fingerprint
    )
    message_ids = _validated_platform_message_ids(None, manual_message_ids)
    acknowledgement_hash = _manual_resend_message_id_hash(message_ids)
    try:
        plan_hash, _, _ = _pointer_parts(acknowledgement.draft_id)
    except PendingTurnDraftError:
        raise
    if not (
        hmac.compare_digest(acknowledgement.session_key_hash, session_hash)
        and hmac.compare_digest(acknowledgement.principal_fingerprint, principal)
        and hmac.compare_digest(
            acknowledgement.manual_resend_message_id_hash, acknowledgement_hash
        )
        and hmac.compare_digest(plan_hash, session_hash)
    ):
        raise PendingTurnDraftUnauthorized(
            "pending recovery acknowledgement identity changed"
        )

    timestamp = _utc_now(now)
    timestamp_text = timestamp.isoformat()

    def _acknowledge(conn: Any) -> bool:
        matching = [
            metadata
            for metadata in _transaction_metadata_rows(conn, now=timestamp)
            if metadata.draft_id == acknowledgement.draft_id
            and _metadata_matches_owner(
                metadata,
                session_hash=session_hash,
                principal_fingerprint=principal,
            )
        ]
        if len(matching) != 1:
            raise PendingTurnDraftConflict(
                "pending recovery acknowledgement generation is unavailable"
            )
        metadata = matching[0]
        if metadata.acknowledgement_hash is not None:
            if hmac.compare_digest(metadata.acknowledgement_hash, acknowledgement_hash):
                return False
            raise PendingTurnDraftConflict(
                "pending recovery acknowledgement belongs to another delivery"
            )
        if metadata.acknowledged:
            raise PendingTurnDraftConflict(
                "pending recovery acknowledgement is incomplete"
            )
        if not (metadata.updated_at <= timestamp < metadata.expires_at):
            raise PendingTurnDraftError(
                "pending recovery acknowledgement timestamp is invalid"
            )
        size_row = conn.execute(
            "SELECT length(CAST(json_set(value, "
            "'$.manual_resend_acknowledged_at', ?, "
            "'$.manual_resend_message_id_hash', ?, "
            "'$.updated_at', ?) AS BLOB)) FROM state_meta WHERE key = ?",
            (
                timestamp_text,
                acknowledgement_hash,
                timestamp_text,
                metadata.key,
            ),
        ).fetchone()
        if (
            size_row is None
            or not isinstance(size_row[0], int)
            or size_row[0] > MAX_PENDING_TURN_DRAFT_RECORD_BYTES
        ):
            raise PendingTurnDraftError(
                "pending recovery acknowledgement exceeds its size bound"
            )
        cursor = conn.execute(
            "UPDATE state_meta SET value = json_set(value, "
            "'$.manual_resend_acknowledged_at', ?, "
            "'$.manual_resend_message_id_hash', ?, "
            "'$.updated_at', ?) WHERE key = ? "
            "AND json_extract(value, '$.manual_resend_acknowledged_at') IS NULL "
            "AND json_extract(value, '$.manual_resend_message_id_hash') IS NULL",
            (
                timestamp_text,
                acknowledgement_hash,
                timestamp_text,
                metadata.key,
            ),
        )
        rowcount = cursor.rowcount
        if rowcount is None or rowcount < 0:
            rowcount = conn.execute("SELECT changes()").fetchone()[0]
        if rowcount != 1:
            raise PendingTurnDraftConflict(
                "pending recovery acknowledgement changed concurrently"
            )
        return True

    try:
        return bool(db._execute_write(_acknowledge))
    except PendingTurnDraftError:
        raise
    except Exception as exc:
        raise PendingTurnDraftError(
            "pending recovery acknowledgement could not be persisted"
        ) from exc


def acknowledge_pending_turn_drafts(
    db: Any,
    *,
    session_key: str,
    principal_fingerprint: str,
    manual_message_ids: Sequence[str],
    now: Optional[datetime] = None,
) -> int:
    """Plan and immediately acknowledge one generation (low-level compatibility helper)."""

    acknowledgement, replayed = plan_pending_turn_resend_acknowledgement(
        db,
        session_key=session_key,
        principal_fingerprint=principal_fingerprint,
        manual_message_ids=manual_message_ids,
        now=now,
    )
    if replayed or acknowledgement is None:
        return 0
    return int(
        commit_pending_turn_resend_acknowledgement(
            db,
            acknowledgement,
            session_key=session_key,
            principal_fingerprint=principal_fingerprint,
            manual_message_ids=manual_message_ids,
            now=now,
        )
    )


def link_pending_turn_draft(
    db: Any,
    draft_id: str,
    *,
    predecessor_session_id: str,
    successor_session_id: str,
    now: Optional[datetime] = None,
) -> PendingTurnDraft:
    """Record an audit-only successor backlink on the exact staged generation."""

    if db is None or not hasattr(db, "_execute_write"):
        raise PendingTurnDraftError("durable SessionDB is unavailable")
    predecessor_session_id = str(predecessor_session_id or "").strip()
    successor_session_id = str(successor_session_id or "").strip()
    if (
        not predecessor_session_id
        or len(predecessor_session_id) > 512
        or not successor_session_id
        or len(successor_session_id) > 512
    ):
        raise PendingTurnDraftError("pending recovery link provenance is incomplete")
    session_hash, _, _ = _pointer_parts(draft_id)
    key = _draft_key(draft_id)
    timestamp = _utc_now(now)

    def _link(conn):
        row = conn.execute(
            "SELECT value FROM state_meta WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            current = _decode(row[0], expected_id=draft_id, expected_hash=session_hash)
        except PendingTurnDraftError:
            return None
        if _is_expired(current, timestamp):
            conn.execute("DELETE FROM state_meta WHERE key = ?", (key,))
            return None
        if current.predecessor_session_id != predecessor_session_id:
            raise PendingTurnDraftConflict(
                "pending recovery cannot be linked from this predecessor"
            )
        if current.successor_session_id not in (None, successor_session_id):
            raise PendingTurnDraftConflict(
                "pending recovery is already linked elsewhere"
            )
        updated = replace(
            current,
            successor_session_id=successor_session_id,
            updated_at=timestamp.isoformat(),
        )
        raw = _encode(updated)
        if len(raw.encode("utf-8")) > MAX_PENDING_TURN_DRAFT_RECORD_BYTES:
            raise PendingTurnDraftError(
                "pending recovery record exceeds its size bound"
            )
        conn.execute("UPDATE state_meta SET value = ? WHERE key = ?", (raw, key))
        return updated

    try:
        linked = db._execute_write(_link)
    except PendingTurnDraftError:
        raise
    except Exception as exc:
        raise PendingTurnDraftError(
            "pending recovery link could not be persisted"
        ) from exc
    if linked is None:
        raise PendingTurnDraftConflict("pending recovery generation is unavailable")
    return linked
