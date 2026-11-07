"""Create-only write of a busy bot DM into Mission Control ``fleet_messages_v1``.

The recipient's drain (``tools/fleet_message_drain.py``: ``to == profile name`` and
``status == queued``) owns delivery, through the recipient's native Bot Chat authority.
This module only writes that document through the node's ``fleet-node-state/fleet_ops.py``
helper. The sender's local copy is ``tools/fleet_message_spool.py``; this keeps no queue.
"""

from __future__ import annotations

import contextlib
import datetime
import importlib.util
import json
import re
import sys
import tempfile
import urllib.error
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

COLLECTION = "fleet_messages_v1"
_HANDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+:-]{0,127}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class FleetEnqueueError(RuntimeError):
    """The durable registry did not accept the message. It was not queued."""


class MessageAlreadyExists(Exception):
    """The create-only write found the document. Return its message_id; do not PATCH."""

    already_exists = True


def _fleet_ops_path() -> Path:
    """The node-level registry writer, resolved at call time under the canonical Hermes root.

    Profiles share one Mission Control writer, so this is never a profile HERMES_HOME. HOME is not
    the anchor either: a served profile may run with HOME set to its own sandbox directory, where
    ``~/.hermes`` would name a helper that does not exist (or the wrong one)."""
    from hermes_constants import get_default_hermes_root, get_hermes_home

    return get_default_hermes_root(home=get_hermes_home()) / "fleet-node-state" / "fleet_ops.py"


def fleet_handle(value: Any, *, fallback: str) -> str:
    """A ``fleet_messages_v1`` from/to handle the drain's ``bot_identity`` can match."""
    text = str(value or "").strip()
    if text.startswith("bot:"):
        text = text.split("/")[-1]
        if text.startswith("bot:"):
            text = text[4:]
    text = text.replace("/", "-")
    if _HANDLE.fullmatch(text):
        return text
    if _HANDLE.fullmatch(fallback):
        return fallback
    raise FleetEnqueueError(f"no safe fleet handle in {value!r}")


def new_message_id() -> str:
    """Mint the shared id of a queued DM: its registry document id AND its local spool envelope id,
    so the sender can persist the spool before the registry write and both name one message."""
    return f"fm{uuid.uuid4().hex}"


def valid_message_id(message_id: Any) -> bool:
    return isinstance(message_id, str) and _ID.fullmatch(message_id) is not None


def build_queued_dm(*, sender: str, recipient: str, body: str, message_id: Optional[str] = None,
                    created: Optional[datetime.datetime] = None) -> dict:
    """A schema-v2 queued DM. ``expires_at`` is ``created_at`` plus 24 hours exactly."""
    if not isinstance(body, str) or not body.strip():
        raise FleetEnqueueError("body must be a non-empty string")
    created = (created or datetime.datetime.now(datetime.timezone.utc)).replace(microsecond=0)
    created_at = created.strftime("%Y-%m-%dT%H:%M:%SZ")
    expires_at = (created + datetime.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc_id = message_id or new_message_id()
    if not valid_message_id(doc_id):
        raise FleetEnqueueError(f"message_id {doc_id!r} is not a safe identifier")
    return {
        "message_id": doc_id,
        "from": fleet_handle(sender, fallback="sender"),
        "to": fleet_handle(recipient, fallback="recipient"),
        "kind": "dm",
        "body": body,
        "status": "queued",
        "attempts": 0,
        "created_at": created_at,
        "updated_at": created_at,
        "expires_at": expires_at,
        "last_error": "target_busy",
        "schema_version": 2,
    }


def _load_fleet_ops():
    path = _fleet_ops_path()
    if not path.is_file():
        raise FleetEnqueueError(f"fleet_ops missing at {path}")
    spec = importlib.util.spec_from_file_location("fleet_ops", path)
    if spec is None or spec.loader is None:
        raise FleetEnqueueError(f"fleet_ops is not loadable at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _default_writer(paths, collection: str, set_by: str) -> int:
    module = _load_fleet_ops()
    if len(paths) != 1:
        raise FleetEnqueueError("queued DM create writes one document")
    try:
        # fleet_ops prints an "OK <id> ..." progress line to stdout; keep stdout for callers.
        with contextlib.redirect_stdout(sys.stderr):
            return int(module.create_file_if_absent(paths[0], collection, set_by=set_by))
    except FleetEnqueueError:
        raise
    except Exception as exc:
        if getattr(exc, "already_exists", False):
            raise MessageAlreadyExists(getattr(exc, "doc_id", "")) from exc
        raise


def _default_reader(message_id: str) -> dict | None:
    module = _load_fleet_ops()
    try:
        with contextlib.redirect_stdout(sys.stderr):
            doc, _ = module.get_doc(COLLECTION, message_id)
        return doc
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise FleetEnqueueError(f"fleet_ops get_doc HTTP {exc.code}") from exc
    except SystemExit as exc:
        raise FleetEnqueueError(str(exc)) from exc


def read_registry_doc(message_id: str, *, reader: Optional[Callable[[str], dict | None]] = None) -> dict | None:
    """The registry document of ``message_id`` (None when absent); raises ``FleetEnqueueError``."""
    try:
        return (reader or _default_reader)(message_id)
    except FleetEnqueueError:
        raise
    except Exception as exc:
        raise FleetEnqueueError(str(exc)) from exc


def enqueue_busy_dm(
    *,
    sender: str,
    recipient: str,
    body: str,
    message_id: Optional[str] = None,
    writer: Optional[Callable[..., int]] = None,
    reader: Optional[Callable[[str], dict | None]] = None,
) -> str:
    """Write one queued doc. Returns ``message_id``. Raises when the write does not land.

    When ``message_id`` is supplied, an existing registry document is returned without
    rewriting it (an existing receipt is never reset to ``queued``). A concurrent create that
    finds the document already there also returns the existing ``message_id`` unchanged.
    """
    doc = build_queued_dm(sender=sender, recipient=recipient, body=body, message_id=message_id)
    doc_id = doc["message_id"]
    if message_id is not None and read_registry_doc(doc_id, reader=reader) is not None:
        return doc_id
    write = writer or _default_writer
    path: str | None = None
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(doc, handle)
        path = handle.name
    try:
        try:
            rc = write([path], COLLECTION, set_by=doc["from"])
        except MessageAlreadyExists:
            return doc_id
        except FleetEnqueueError:
            raise
        except Exception as exc:
            if getattr(exc, "already_exists", False):
                return doc_id
            raise FleetEnqueueError(str(exc)) from exc
        if rc != 0:
            raise FleetEnqueueError(f"fleet_ops create_file_if_absent returned {rc}")
        return doc_id
    finally:
        if path is not None:
            Path(path).unlink(missing_ok=True)


def queued_ack(message_id: str) -> str:
    """The sender-facing fast-ack. The id is the registry document id."""
    return f"queued ({message_id})"
