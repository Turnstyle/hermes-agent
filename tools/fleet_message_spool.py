"""Local durable spool of fleet DMs and the one execution claim both transports share.

A busy or unreachable local teammate's DM is written here (under the RECIPIENT's profile home)
before anything else, so the payload survives a publisher crash, a registry outage or a cloud
commit whose ACK never arrived. Layout under ``<recipient home>/durable/dm-spool``::

    <id>.json                       immutable envelope (sender, recipient, body, body digest)
    .state/<id>.published.json      the registry accepted (or already held) the same document
    .state/<id>.execution.json      THE execution claim: created no-clobber before any admission
    .state/<id>.conflict-<sha>.json a different payload offered under an existing id (kept)

Every executor — the recipient's local-first spool drain and its cloud claim of the registry doc
— must create ``<id>.execution.json`` before it asks the native Bot Chat door, and the door is
asked with one delivery id derived from the message id (``native_delivery_id``). A second claim
loses the create; a second ask of the door answers the first admission. An uncertain execution
keeps its claim and its payload and is never replayed.
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Optional

from utils import atomic_json_write, fsync_directory

SPOOL_DIR_REL = ("durable", "dm-spool")
STATE_SUBDIR = ".state"
SCHEMA_VERSION = 1
LOCAL_EXPIRY = datetime.timedelta(hours=24)
# Execution-claim statuses. ``admitted``: the door holds it and its receipt has not settled.
# ``uncertain``: an outcome nobody can prove (a death or a lost connection past the dial, a door
# that answered ``unknown_execution``/``ambiguous``): kept, never retried.
TERMINAL_EXECUTION = frozenset({"settled", "failed", "refused", "cancelled", "uncertain", "expired"})
_IMMUTABLE = ("message_id", "sender", "recipient", "body_sha256")


class SpoolConflictError(ValueError):
    """An envelope or registry doc with the same id holds different immutable fields. Both kept."""


def utc_iso(now: Optional[datetime.datetime] = None) -> str:
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def spool_dir(home: Path | str) -> Path:
    return Path(home).joinpath(*SPOOL_DIR_REL)


def native_delivery_id(message_id: str) -> str:
    """The ONE native Bot Chat delivery id of a fleet message (64 lowercase hex, the door's id
    shape). Local spool and cloud claim derive the same id, so the door's per-id idempotency
    fences every second attempt: it answers the existing receipt instead of a second turn."""
    from tools.fleet_message_enqueue import COLLECTION

    if not isinstance(message_id, str) or not message_id:
        raise ValueError("fleet message id is required")
    return hashlib.sha256(f"{COLLECTION}:{message_id}".encode()).hexdigest()


def _stem(message_id: str) -> str:
    """The spool file stem of a message id: the id itself when it is a safe fleet id, else a digest
    (a registry doc another producer named), so every doc still gets its one execution claim."""
    from tools.fleet_message_enqueue import valid_message_id

    if valid_message_id(message_id):
        return message_id
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("fleet message id is required")
    return "h" + hashlib.sha256(message_id.encode()).hexdigest()[:40]


def _ensure_dir(path: Path) -> Path:
    created = not path.is_dir()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if created:
        fsync_directory(path.parent)
    return path


def _create_no_clobber(target: Path, payload: dict) -> bool:
    """Durably create ``target`` with ``payload`` unless it exists. False when another writer
    holds it. The bytes are fsynced under a private name and linked in, so a reader never sees a
    partial file and a crash leaves either nothing or the whole record."""
    _ensure_dir(target.parent)
    temp = target.with_name(f".{target.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp, target)
        except FileExistsError:
            return False
        fsync_directory(target.parent)
        return True
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()


def _rewrite(target: Path, payload: dict) -> None:
    atomic_json_write(target, payload, indent=None, sort_keys=True, fsync_dir=True, mode=0o600)


def _read_json(path: Path) -> Optional[dict]:
    """Absent -> None. Unreadable or not an object raises: corruption is never absence, so it can
    never license a fresh execution or an overwrite."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return value


def _digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def envelope_path(home: Path | str, message_id: str) -> Path:
    return spool_dir(home) / f"{_stem(message_id)}.json"


def _state_path(home: Path | str, message_id: str, suffix: str) -> Path:
    return spool_dir(home) / STATE_SUBDIR / f"{_stem(message_id)}.{suffix}.json"


def persist_envelope(home: Path | str, *, message_id: str, sender: str, recipient: str, body: str,
                     now: Optional[datetime.datetime] = None) -> dict:
    """Make the spool envelope the message's first durable copy. Idempotent for the same payload;
    a different payload under an existing id raises ``SpoolConflictError`` and is retained beside
    the original (``.state/<id>.conflict-<digest>.json``) for review, never overwriting it."""
    if not isinstance(body, str) or not body.strip():
        raise ValueError("body must be a non-empty string")
    from tools.fleet_message_enqueue import valid_message_id

    if not valid_message_id(message_id):
        raise ValueError(f"unsafe fleet message id {message_id!r}")
    envelope = {"schema_version": SCHEMA_VERSION, "message_id": message_id, "sender": sender,
                "recipient": recipient, "body": body, "body_sha256": _digest(body), "created_at": utc_iso(now)}
    path = envelope_path(home, message_id)
    if _create_no_clobber(path, envelope):
        return envelope
    existing = read_envelope(home, message_id)
    if existing is not None and all(existing.get(k) == envelope[k] for k in _IMMUTABLE):
        return existing
    _create_no_clobber(_state_path(home, message_id, f"conflict-{envelope['body_sha256'][:16]}"), envelope)
    raise SpoolConflictError(f"spool envelope {message_id} already holds a different message")


def read_envelope(home: Path | str, message_id: str) -> Optional[dict]:
    envelope = _read_json(envelope_path(home, message_id))
    if envelope is None:
        return None
    body = envelope.get("body")
    if not isinstance(body, str) or _digest(body) != envelope.get("body_sha256"):
        raise ValueError(f"spool envelope {message_id} body does not match its digest")
    return envelope


def envelope_matches_doc(envelope: dict, fields: dict) -> bool:
    """Whether a registry doc carries the envelope's message (handles compared as the drain does)."""
    from tools.fleet_message_enqueue import fleet_handle

    try:
        return (fields.get("from") == fleet_handle(envelope.get("sender"), fallback="sender")
                and fields.get("to") == fleet_handle(envelope.get("recipient"), fallback="recipient")
                and fields.get("body") == envelope.get("body"))
    except Exception:
        return False


def published(home: Path | str, message_id: str) -> Optional[dict]:
    return _read_json(_state_path(home, message_id, "published"))


def publish(home: Path | str, message_id: str, *, writer: Optional[Callable[..., int]] = None,
            reader: Optional[Callable[[str], Optional[dict]]] = None) -> dict:
    """Create the envelope's registry doc (create-only) and record the publication receipt.

    An existing doc under the id is accepted only when it carries the same message: a cloud
    commit whose ACK was lost is then confirmed, never rewritten. A different doc is a conflict;
    both copies stay. Raises ``FleetEnqueueError`` when the registry is unreachable."""
    from tools.fleet_message_enqueue import enqueue_busy_dm, read_registry_doc

    receipt = published(home, message_id)
    if receipt is not None:
        return receipt
    envelope = read_envelope(home, message_id)
    if envelope is None:
        raise FileNotFoundError(f"no spool envelope {message_id}")
    existing = read_registry_doc(message_id, reader=reader)
    if existing is not None and not envelope_matches_doc(envelope, existing):
        raise SpoolConflictError(f"registry doc {message_id} holds a different message")
    if existing is None:
        enqueue_busy_dm(sender=envelope["sender"], recipient=envelope["recipient"], body=envelope["body"],
                        message_id=message_id, writer=writer, reader=lambda _mid: None)
    receipt = {"message_id": message_id, "published_at": utc_iso(), "existing": existing is not None}
    _create_no_clobber(_state_path(home, message_id, "published"), receipt)
    return published(home, message_id) or receipt


def unpublished(home: Path | str, *, limit: int = 3) -> list[str]:
    """Envelope ids no transport has published or executed yet, oldest first (bounded)."""
    found = []
    for message_id, _envelope in iter_envelopes(home):
        if published(home, message_id) is None and execution(home, message_id) is None:
            found.append(message_id)
            if len(found) >= limit:
                break
    return found


def iter_envelopes(home: Path | str):
    """``(message_id, envelope)`` oldest first; an unreadable envelope is skipped, never executed."""
    import logging

    root = spool_dir(home)
    if not root.is_dir():
        return
    entries = []
    for path in root.glob("*.json"):
        try:
            envelope = read_envelope(home, path.stem)
        except (OSError, ValueError) as exc:
            logging.getLogger(__name__).warning("fleet spool: skipping unreadable envelope %s (%s)", path.name, exc)
            continue
        if envelope is not None:
            entries.append((str(envelope.get("created_at") or ""), path.stem, envelope))
    for _created, message_id, envelope in sorted(entries):
        yield message_id, envelope


def execution_path(home: Path | str, message_id: str) -> Path:
    return _state_path(home, message_id, "execution")


def execution(home: Path | str, message_id: str) -> Optional[dict]:
    return _read_json(execution_path(home, message_id))


def claim_execution(home: Path | str, message_id: str, *, transport: str, fields: dict) -> bool:
    """Win the message's one execution claim (no-clobber). False when any claim exists."""
    record = {"message_id": message_id, "transport": transport, "status": "started",
              "delivery_id": native_delivery_id(message_id), "started_at": utc_iso(),
              **{k: fields.get(k) for k in ("from", "to") if fields.get(k) is not None}}
    if isinstance(fields.get("body"), str):
        record["body_sha256"] = _digest(fields["body"])
    return _create_no_clobber(execution_path(home, message_id), record)


def execution_matches(record: dict, fields: dict) -> bool:
    """Whether a registry doc is the message an execution claim was made for (its sender,
    recipient and body digest): a different doc under the same id is a conflict, never settled
    from someone else's execution."""
    if any(record.get(k) is not None and record.get(k) != fields.get(k) for k in ("from", "to")):
        return False
    digest = record.get("body_sha256")
    return digest is None or (isinstance(fields.get("body"), str) and _digest(fields["body"]) == digest)


def update_execution(home: Path | str, message_id: str, **updates: Any) -> dict:
    record = execution(home, message_id)
    if record is None:
        raise FileNotFoundError(f"no execution claim for {message_id}")
    record.update(updates, updated_at=utc_iso())
    _rewrite(execution_path(home, message_id), record)
    return record


def release_execution(home: Path | str, message_id: str) -> None:
    """Drop a claim whose door refused before anything ran (no authority, a pre-dial death, an
    owner-state refusal). A later claim asks the door with the same delivery id, so even a refusal
    that committed after all is answered from its existing receipt, never run twice."""
    with contextlib.suppress(FileNotFoundError):
        execution_path(home, message_id).unlink()
    fsync_directory(spool_dir(home) / STATE_SUBDIR)


def inflight(home: Path | str) -> list[dict]:
    """Execution claims the door holds (``admitted``), that may yet learn a receipt (``started``,
    ``uncertain``), or whose recorded outcome is not on its registry doc yet (``doc_synced`` False):
    the only records a drain re-reads. Oldest first."""
    state = spool_dir(home) / STATE_SUBDIR
    if not state.is_dir():
        return []
    records = []
    for path in state.glob("*.execution.json"):
        try:
            record = _read_json(path)
        except (OSError, ValueError):
            continue
        if record and (record.get("status") in ("admitted", "uncertain", "started")
                       or record.get("doc_synced") is False):
            records.append(record)
    return sorted(records, key=lambda r: (str(r.get("started_at") or ""), str(r.get("message_id"))))


def retire_envelope(home: Path | str, message_id: str) -> None:
    """Remove a settled message's payload; its small execution/publication records stay."""
    with contextlib.suppress(FileNotFoundError):
        envelope_path(home, message_id).unlink()


def local_expired(envelope: dict, now: Optional[datetime.datetime] = None) -> bool:
    from tools.fleet_message_drain import parse_ts

    created = parse_ts(envelope.get("created_at"))
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    return created is not None and moment - created >= LOCAL_EXPIRY


# The sender waits this long for the registry ACK; past it the envelope is still the durable copy
# (the recipient's local-first drain admits it) and the create-only publish may still land.
PUBLISH_WAIT_SECONDS = 8.0


def spool_busy_dm(recipient_home: Path | str, *, sender: str, recipient: str, body: str,
                  writer: Optional[Callable[..., int]] = None,
                  reader: Optional[Callable[[str], Optional[dict]]] = None,
                  wait: float = PUBLISH_WAIT_SECONDS) -> dict:
    """Queue one DM for a busy local recipient: mint its id, persist the envelope (the payload's
    first durable copy), then publish it create-only within a bounded wait. Raises only when the
    envelope itself could not be written (then nothing was queued)."""
    from agent.memory_provider import spawn_context_thread
    from tools.fleet_message_enqueue import fleet_handle, new_message_id

    message_id = new_message_id()
    persist_envelope(recipient_home, message_id=message_id, sender=fleet_handle(sender, fallback="sender"),
                     recipient=fleet_handle(recipient, fallback="recipient"), body=body)
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            publish(recipient_home, message_id, writer=writer, reader=reader)
            outcome["published"] = True
        except Exception as exc:
            outcome["error"] = type(exc).__name__
            import logging
            logging.getLogger(__name__).info("fleet spool: %s not published yet (%s); the local copy stands",
                                             message_id, type(exc).__name__)

    thread = spawn_context_thread(run, name=f"fleet-publish-{message_id}")
    thread.start()
    thread.join(wait)
    return {"message_id": message_id, "published": bool(outcome.get("published")),
            **({"publish_error": outcome["error"]} if "error" in outcome else {})}
