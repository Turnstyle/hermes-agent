"""Turn-end drain of a bot's own queued ``fleet_messages_v1`` docs (Mission Control Firestore).

A Bot Chat DM or notify-subscribe wake that hit ``target_busy`` stays in the durable registry with
``status='queued'`` (write-first sender). When this bot's canonical Bot Chat finishes a turn, the
TUI gateway's post-turn hook (``tui_gateway.session_notifications._drain_fleet_messages_once``)
calls :func:`claim_next`, which runs exactly ONE equality-only query:

    to == <this bot> AND status == 'queued'  LIMIT 50

The returned page is sorted by created_at and document id locally before the caller's limit
is applied. No composite index is needed.

It never runs while the bot is idle: the post-turn hook is its only caller, so no turn end means no
Firestore read. Expired docs seen in the page are marked expired so they cannot starve fresh docs.

Status transitions this module performs (every write carries an ``updateTime`` precondition, so a
doc that anyone else touched in between is never overwritten):

    queued    -> delivered   claim (the atomic step: two racing turn ends, or a late sender
                             success, cannot both win because the precondition is the queued
                             doc's own updateTime)
    delivered -> read        the message is handed to the bot as its next input
    read      -> done        the bot's turn settled
    read      -> queued      the turn failed or was interrupted: attempts+1, last_error set;
                             retried at the next turn end (explicit requeue)
    read      -> failed      the same, once attempts reaches max_attempts (doc kept, never deleted)
    delivered -> queued      the session got busy between claim and hand-off (no attempt counted)
    queued    -> rejected    the doc is malformed for this reader (unknown kind / missing body)
    queued    -> expired     its expiry passed before the drain saw it

``replied`` is left to the reply path: the injected input tells the bot to answer the sender with
``message_agent``; a sender-side write may then mark the doc ``replied``.

One message is handed over per turn end. Its own turn end runs the next query, so ordering holds,
nothing is claimed that is not about to run, and a crash leaves at most one doc in delivered/read.
The separate ``reclaim-stale`` maintenance command recovers those claims after the age threshold;
it does not add a read to the turn-end path.

The feature is dark by default. Config (profile ``config.yaml``)::

    fleet_messages:
      drain_on_turn_end: false     # true to enable
      target: emulator             # "emulator" (needs emulator_host) or "live"
      emulator_host: ""            # e.g. 127.0.0.1:8794
      project: mission-control-444444
      database: fleet-operations
      limit: 10                    # locally selected page size (1..50)
      max_attempts: 5
      timeout_seconds: 5

Security: the query filters ``to == me``, every returned doc is re-checked for ``to == me`` before
any write, and writes are limited to the status bookkeeping fields below. Firestore IAM/rules stay
the real boundary; this module never deletes and never writes message content.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)
_config_read_warned = False
_config_read_warning_lock = threading.Lock()

COLLECTION = "fleet_messages_v1"
DEFAULT_PROJECT = "mission-control-444444"
DEFAULT_DATABASE = "fleet-operations"
KINDS = frozenset({"dm", "notify_wake"})
TS_FIELDS = frozenset({"created_at", "updated_at", "expires_at", "delivered_at", "read_at", "replied_at",
                       "done_at", "failed_at", "rejected_at", "expired_at", "sender_notice_at"})
# The only fields this reader may write. Message content (from/to/body/kind/created_at) is never written.
WRITABLE_FIELDS = frozenset({"status", "updated_at", "delivered_at", "read_at", "done_at", "failed_at",
                             "rejected_at", "expired_at", "attempts", "last_error", "sender_notice",
                             "sender_notice_at"})
MAX_LIMIT = 50
LAST_ERROR_CHARS = 500
_SCOPE = "https://www.googleapis.com/auth/datastore"
SAFE_HANDLE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+:-]{0,127}\Z")


class PreconditionFailed(RuntimeError):
    """The doc changed since it was read: someone else moved it. Not ours to write."""


class Refused(RuntimeError):
    """A write this reader must never make (foreign doc, disallowed field, no precondition)."""


# ---------- config ----------
@dataclasses.dataclass(frozen=True)
class DrainConfig:
    target: str
    emulator_host: str = ""
    project: str = DEFAULT_PROJECT
    database: str = DEFAULT_DATABASE
    limit: int = 10
    max_attempts: int = 5
    timeout_seconds: float = 5.0


def drain_config(cfg: Optional[dict] = None) -> Optional[DrainConfig]:
    """The enabled drain config, or None (disabled / misconfigured). Reads the active profile's
    config when ``cfg`` is None, so the caller must already be in the session's profile scope."""
    if cfg is None:
        try:
            from hermes_cli.config import load_config
            cfg = load_config() or {}
        except Exception as exc:
            global _config_read_warned
            with _config_read_warning_lock:
                if not _config_read_warned:
                    _config_read_warned = True
                    logger.warning("fleet message drain: config read failed (%s)", type(exc).__name__)
            return None
    section = (cfg or {}).get("fleet_messages") or {}
    if not isinstance(section, dict) or section.get("drain_on_turn_end") is not True:
        return None
    target = str(section.get("target") or "emulator").strip().lower()
    host = str(section.get("emulator_host") or "").strip()
    if target not in ("emulator", "live") or (target == "emulator" and not host):
        logger.warning("fleet message drain: enabled but target=%r emulator_host=%r is unusable; skipping",
                       target, host)
        return None
    try:
        limit = max(1, min(MAX_LIMIT, int(section.get("limit", 10))))
        max_attempts = max(1, int(section.get("max_attempts", 5)))
        timeout = max(0.5, float(section.get("timeout_seconds", 5)))
    except (TypeError, ValueError):
        logger.warning("fleet message drain: bad numeric setting in fleet_messages; skipping")
        return None
    return DrainConfig(target=target, emulator_host=host,
                       project=str(section.get("project") or DEFAULT_PROJECT),
                       database=str(section.get("database") or DEFAULT_DATABASE),
                       limit=limit, max_attempts=max_attempts, timeout_seconds=timeout)


def _config_for_profile_home(profile_home: Path) -> Optional[dict]:
    path = profile_home / "config.yaml"
    if not path.is_file():
        return None
    try:
        from hermes_cli.config import read_user_config_raw
        return read_user_config_raw(path)
    except Exception:
        logger.debug("fleet message drain: could not read %s", path, exc_info=True)
        return None


def recipient_drain_enabled(profile_name: str, *, profile_home: Path | str | None = None) -> bool:
    """True when the named recipient profile has a usable turn-end drain (not the sender process config)."""
    home: Path | None
    if profile_home is not None:
        home = Path(profile_home)
    else:
        from tools.bot_mode_probe import _default_home, _hermes_root, _roster
        home = dict(_roster(_hermes_root(Path(_default_home())))).get(profile_name)
    if home is None or not home.is_dir():
        return False
    cfg = _config_for_profile_home(home)
    if cfg is None:
        return False
    return drain_config(cfg) is not None


def bot_identity(profile_home: Path | str) -> str:
    """This bot's fleet id: its profile folder name ('default' for the root home)."""
    home = Path(profile_home)
    return home.name if home.parent.name == "profiles" else "default"


# ---------- time + codec ----------
def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def rfc3339(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(value: Any) -> Optional[datetime.datetime]:
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    # Firestore may return nanoseconds; datetime takes at most microseconds.
    if "." in text:
        head, _, rest = text.partition(".")
        digits = "".join(ch for ch in rest if ch.isdigit())
        tz = rest[len(digits):]
        text = f"{head}.{digits[:6].ljust(6, '0')}{tz}"
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def enc(value: Any, field: str = "") -> dict:
    if field in TS_FIELDS and isinstance(value, str):
        return {"timestampValue": value}
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    raise Refused(f"unsupported value type {type(value).__name__} for {field!r}")


def dec(value: dict) -> Any:
    (kind, raw), = value.items()
    if kind == "integerValue":
        return int(raw)
    if kind == "doubleValue":
        return float(raw)
    if kind == "nullValue":
        return None
    if kind == "arrayValue":
        return [dec(v) for v in raw.get("values", [])]
    if kind == "mapValue":
        return {k: dec(v) for k, v in raw.get("fields", {}).items()}
    return raw  # string, boolean, timestampValue (RFC 3339 string)


# ---------- transport ----------
@dataclasses.dataclass
class Row:
    doc_id: str
    fields: dict
    update_time: str


class FirestoreStore:
    """Firestore REST transport locked to ``fleet_messages_v1`` with guarded field updates.
    ``reads``/``commits`` count calls."""

    def __init__(self, *, base_url: str, project: str, database: str,
                 token_fn: Callable[[], str], timeout: float = 5.0):
        self.dbpath = f"projects/{project}/databases/{database}"
        self.prefix = f"{self.dbpath}/documents/{COLLECTION}/"
        root = base_url.rstrip("/")
        self.query_url = f"{root}/v1/{self.dbpath}/documents:runQuery"
        self.commit_url = f"{root}/v1/{self.dbpath}/documents:commit"
        self.doc_url = f"{root}/v1/{self.dbpath}/documents/{COLLECTION}/"
        self._token_fn, self.timeout = token_fn, timeout
        self.reads = 0
        self.commits = 0

    def _req(self, url: str, method: str = "GET", body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "Bearer " + self._token_fn(), "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.load(resp)

    def query_queued(self, to: str, limit: int) -> list[Row]:
        if not to:
            raise Refused("recipient id is required")
        where = {"compositeFilter": {"op": "AND", "filters": [
            {"fieldFilter": {"field": {"fieldPath": "to"}, "op": "EQUAL", "value": enc(to)}},
            {"fieldFilter": {"field": {"fieldPath": "status"}, "op": "EQUAL", "value": enc("queued")}},
        ]}}
        query = {"from": [{"collectionId": COLLECTION}], "where": where, "limit": MAX_LIMIT}
        self.reads += 1
        rows = []
        for item in self._req(self.query_url, "POST", {"structuredQuery": query}):
            doc = item.get("document")
            if doc:
                rows.append(Row(doc["name"].rsplit("/", 1)[1],
                                {k: dec(v) for k, v in doc.get("fields", {}).items()}, doc.get("updateTime", "")))

        def sort_key(row: Row) -> tuple:
            created = parse_ts(row.fields.get("created_at"))
            return (created is None, created or datetime.datetime.max.replace(tzinfo=datetime.timezone.utc),
                    row.doc_id)

        rows.sort(key=sort_key)
        return rows[:max(1, min(MAX_LIMIT, int(limit)))]

    def query_status(self, status: str, limit: int) -> list[Row]:
        """Maintenance query by status alone; no composite index or recipient scope."""
        if status not in ("delivered", "read"):
            raise Refused(f"unsupported claim status {status!r}")
        query = {"from": [{"collectionId": COLLECTION}],
                 "where": {"fieldFilter": {"field": {"fieldPath": "status"},
                                           "op": "EQUAL", "value": enc(status)}},
                 "limit": max(1, int(limit))}
        self.reads += 1
        rows = []
        for item in self._req(self.query_url, "POST", {"structuredQuery": query}):
            doc = item.get("document")
            if doc:
                rows.append(Row(doc["name"].rsplit("/", 1)[1],
                                {k: dec(v) for k, v in doc.get("fields", {}).items()}, doc.get("updateTime", "")))
        return rows

    def get(self, doc_id: str) -> Optional[Row]:
        """Single-doc read (tests and operator checks; the drain path never calls it)."""
        _check_id(doc_id)
        self.reads += 1
        try:
            doc = self._req(self.doc_url + urllib.parse.quote(doc_id, safe=""))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        return Row(doc_id, {k: dec(v) for k, v in doc.get("fields", {}).items()}, doc.get("updateTime", ""))

    def create(self, doc_id: str, fields: dict) -> None:
        """Create exactly one named document; Firestore refuses an existing document id."""
        _check_id(doc_id)
        url = self.doc_url[:-1] + "?" + urllib.parse.urlencode({"documentId": doc_id})
        self.commits += 1
        self._req(url, "POST", {"fields": {key: enc(value, key) for key, value in fields.items()}})

    def update(self, doc_id: str, fields: dict, update_time: str) -> str:
        """Atomic guarded update of listed fields; returns the new updateTime. Raises
        :class:`PreconditionFailed` when the doc changed since ``update_time``."""
        _check_write(doc_id, fields, update_time)
        write = {"update": {"name": self.prefix + doc_id, "fields": {k: enc(v, k) for k, v in fields.items()}},
                 "updateMask": {"fieldPaths": sorted(fields)},
                 "currentDocument": {"updateTime": update_time}}
        self.commits += 1
        try:
            result = self._req(self.commit_url, "POST", {"writes": [write]})
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode(errors="replace")
            if exc.code in (400, 409) and ("FAILED_PRECONDITION" in detail or "ABORTED" in detail
                                           or "precondition" in detail.lower()):
                raise PreconditionFailed(detail) from exc
            raise
        return result["writeResults"][0]["updateTime"]


def _check_id(doc_id: Any) -> None:
    if not isinstance(doc_id, str) or not doc_id or "/" in doc_id or len(doc_id) > 400:
        raise Refused(f"bad doc id {doc_id!r}")


def _check_write(doc_id: str, fields: dict, update_time: str) -> None:
    _check_id(doc_id)
    bad = set(fields) - WRITABLE_FIELDS
    if bad:
        raise Refused(f"field(s) not writable by the turn-end drain: {sorted(bad)}")
    if not update_time:
        raise Refused("every drain write needs an updateTime precondition")


_live_lock = threading.Lock()
_live_credentials: Any = None
_gcloud_token_cached = ""
_gcloud_token_until = 0.0
_credentials_warned = False


class NoGoogleCredentials(RuntimeError):
    """Neither host ADC nor the operator's gcloud login can authorize Firestore."""


def _live_token() -> str:
    """Prefer host ADC; use a short, cached gcloud token when ADC is unavailable."""
    global _live_credentials, _gcloud_token_cached, _gcloud_token_until, _credentials_warned
    with _live_lock:
        try:
            import google.auth
            import google.auth.transport.requests
            if _live_credentials is None:
                _live_credentials, _project = google.auth.default(scopes=[_SCOPE])
            if not _live_credentials.valid:
                _live_credentials.refresh(google.auth.transport.requests.Request())
            if not _live_credentials.token:
                raise ValueError("ADC returned no token")
            _credentials_warned = False
            return _live_credentials.token
        except Exception:
            _live_credentials = None
        try:
            if not _gcloud_token_cached or time.monotonic() >= _gcloud_token_until:
                # gcloud runs its own Python; the Hermes bootstrap's PYTHONPATH/PYTHONHOME would load
                # Hermes's crypto packages into it and crash it (pyOpenSSL: no attribute GEN_EMAIL).
                gcloud_env = {k: v for k, v in os.environ.items()
                              if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
                proc = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True,
                                      text=True, check=True, timeout=3, env=gcloud_env)
                token = proc.stdout.strip()
                if not token:
                    raise ValueError("gcloud returned no token")
                _gcloud_token_cached = token
                _gcloud_token_until = time.monotonic() + 45 * 60
            _credentials_warned = False
            return _gcloud_token_cached
        except Exception as exc:
            if not _credentials_warned:
                logger.warning("fleet message drain: no Google credentials (ADC expired and gcloud token failed); "
                               "drain skipped, turns unaffected")
                _credentials_warned = True
            raise NoGoogleCredentials("ADC expired and gcloud token failed") from exc


def store_for(config: DrainConfig) -> FirestoreStore:
    if config.target == "emulator":
        return FirestoreStore(base_url=f"http://{config.emulator_host}", project=config.project,
                              database=config.database, token_fn=lambda: "owner", timeout=config.timeout_seconds)
    return FirestoreStore(base_url="https://firestore.googleapis.com", project=config.project,
                          database=config.database, token_fn=_live_token, timeout=config.timeout_seconds)


def enqueue_message(from_handle: str, to_handle: str, body: str, kind: str = "dm",
                    *, config: Optional[DrainConfig] = None) -> str:
    """Write a busy target's message to Mission Control before acknowledging its sender."""
    if not isinstance(from_handle, str) or not SAFE_HANDLE_RE.fullmatch(from_handle):
        raise Refused("invalid sender handle")
    if not isinstance(to_handle, str) or not SAFE_HANDLE_RE.fullmatch(to_handle):
        raise Refused("invalid recipient handle")
    if kind not in KINDS or not isinstance(body, str) or not body.strip():
        raise Refused("invalid message kind or empty body")
    config = config or drain_config()
    if config is None:
        raise Refused("fleet message drain is disabled")
    message_id = uuid.uuid4().hex
    now = utcnow()
    stamp = rfc3339(now)
    fields = {"message_id": message_id, "from": from_handle, "to": to_handle, "kind": kind,
              "body": body, "status": "queued", "attempts": 0, "created_at": stamp,
              "updated_at": stamp, "expires_at": rfc3339(now + datetime.timedelta(hours=24))}
    store_for(dataclasses.replace(config, timeout_seconds=min(config.timeout_seconds, 1.5))).create(message_id, fields)
    return message_id


# ---------- drain logic ----------
@dataclasses.dataclass
class Claimed:
    """A doc this bot claimed. ``update_time`` tracks our own latest write (next precondition)."""
    doc_id: str
    fields: dict
    update_time: str
    me: str
    finished: bool = False
    pending_fields: Optional[dict] = None
    lock: threading.Lock = dataclasses.field(default_factory=threading.Lock, repr=False)

    @property
    def kind(self) -> str:
        return str(self.fields.get("kind") or "")


def _malformed(fields: dict) -> str:
    if fields.get("kind") not in KINDS:
        return f"unknown kind {fields.get('kind')!r}"
    if not isinstance(fields.get("body"), str) or not fields["body"].strip():
        return "missing body"
    if not isinstance(fields.get("from"), str) or not fields["from"].strip():
        return "missing from"
    if parse_ts(fields.get("created_at")) is None:
        return "missing or invalid created_at"
    if parse_ts(fields.get("expires_at")) is None:
        return "missing or invalid expires_at"
    attempts = fields.get("attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        return "invalid attempts"
    return ""


def _expire_sender_notice(doc_id: str, recipient: Any) -> str:
    label = str(recipient or "recipient")
    return (f"Queued message {doc_id} to @{label} expired after 24 hours without delivery — "
            "it was NOT delivered.")


def claim_next(store: Any, me: str, *, limit: int = 10, now: Optional[datetime.datetime] = None) -> Optional[Claimed]:
    """Run the ONE turn-end query and atomically claim the oldest claimable doc (queued -> delivered).

    Expires docs past ``expires_at`` and skips docs addressed to anyone else. A doc whose claim
    precondition fails was moved by someone else (late sender success, a racing turn end): skipped.
    """
    now = now or utcnow()
    now_s = rfc3339(now)
    for row in store.query_queued(me, limit):
        fields = row.fields
        if fields.get("to") != me or fields.get("status") != "queued":
            continue  # defense in depth: the query already filters both
        problem = _malformed(fields)
        if problem:
            try:
                store.update(row.doc_id, {"status": "rejected", "rejected_at": now_s, "updated_at": now_s,
                                          "last_error": f"turn-end drain: {problem}"}, row.update_time)
            except PreconditionFailed:
                pass
            except Exception:
                logger.warning("fleet message drain: could not reject malformed doc %s", row.doc_id, exc_info=True)
            continue
        if parse_ts(fields["expires_at"]) <= now:
            notice = _expire_sender_notice(row.doc_id, fields.get("to"))
            try:
                store.update(row.doc_id, {"status": "expired", "expired_at": now_s, "updated_at": now_s,
                                          "sender_notice": notice, "sender_notice_at": now_s},
                             row.update_time)
            except PreconditionFailed:
                pass
            except Exception:
                logger.warning("fleet message drain: could not expire doc %s", row.doc_id, exc_info=True)
            continue
        try:
            new_ut = store.update(row.doc_id, {"status": "delivered", "delivered_at": now_s, "updated_at": now_s},
                                  row.update_time)
        except PreconditionFailed:
            logger.info("fleet message drain: %s changed before claim; skipped", row.doc_id)
            continue
        claimed = Claimed(row.doc_id, dict(fields), new_ut, me)
        claimed.fields.update(status="delivered", delivered_at=now_s)
        return claimed
    return None


def turn_end_drain_query(
    profile_home: Path | str,
    cfg: Optional[dict] = None,
) -> Optional[tuple[DrainConfig, Any, Optional[Claimed]]]:
    """The turn-end Firestore query (expire + claim). Shared by the TUI hook and ``-Q`` exit.

    Returns ``(config, store, claimed)`` when drain is enabled (``claimed`` may be None after the
    query). Returns ``None`` when :func:`drain_config` is disabled for this profile."""
    config = drain_config(cfg)
    if config is None:
        return None
    store = store_for(config)
    claimed = claim_next(store, bot_identity(profile_home), limit=config.limit)
    return config, store, claimed


def sender_delivery_status(store: Any, sender: str, message_id: str) -> Optional[dict]:
    """Status the original ``from`` handle may read for a message it queued (including expiry notices)."""
    row = store.get(message_id)
    if row is None or row.fields.get("from") != sender:
        return None
    fields = row.fields
    out: dict[str, Any] = {"message_id": message_id, "status": fields.get("status")}
    notice = fields.get("sender_notice")
    if isinstance(notice, str) and notice.strip():
        out["notice"] = notice
    if fields.get("sender_notice_at"):
        out["notice_at"] = fields["sender_notice_at"]
    return out


def _write(store: Any, claimed: Claimed, fields: dict) -> bool:
    """Guarded write from our last known updateTime; False when someone else moved the doc."""
    try:
        claimed.update_time = store.update(claimed.doc_id, fields, claimed.update_time)
    except PreconditionFailed:
        row = store.get(claimed.doc_id)
        if row is not None and all(row.fields.get(key) == value for key, value in fields.items()):
            claimed.update_time = row.update_time
            claimed.fields.update(fields)
            claimed.pending_fields = None
            return True
        logger.warning("fleet message drain: %s was changed by another writer; leaving it as is", claimed.doc_id)
        return False
    except Exception:
        claimed.pending_fields = dict(fields)
        raise
    claimed.fields.update(fields)
    claimed.pending_fields = None
    return True


def reconcile_claim(store: Any, claimed: Claimed) -> bool:
    """Resolve an uncertain pre-dispatch write before returning an unstarted claim to the queue."""
    row = store.get(claimed.doc_id)
    if row is None or row.fields.get("to") != claimed.me or row.fields.get("status") not in ("delivered", "read"):
        return False
    if claimed.pending_fields and row.update_time != claimed.update_time and not all(
            row.fields.get(key) == value for key, value in claimed.pending_fields.items()):
        return False
    claimed.update_time = row.update_time
    claimed.fields.update(row.fields)
    claimed.pending_fields = None
    return True


def mark_read(store: Any, claimed: Claimed, now: Optional[datetime.datetime] = None) -> bool:
    now_s = rfc3339(now or utcnow())
    return _write(store, claimed, {"status": "read", "read_at": now_s, "updated_at": now_s})


def release(store: Any, claimed: Claimed, now: Optional[datetime.datetime] = None) -> bool:
    """Hand an unstarted claim back (delivered -> queued) without counting an attempt."""
    with claimed.lock:
        if claimed.finished:
            return False
        fields = claimed.pending_fields or {"status": "queued", "updated_at": rfc3339(now or utcnow())}
        committed = _write(store, claimed, fields)
        claimed.finished = True
        return committed


def record_error(store: Any, claimed: Claimed, error: str, *, max_attempts: int,
                 now: Optional[datetime.datetime] = None) -> bool:
    """attempts+1 and last_error; requeue for the next turn end, or ``failed`` at max_attempts.
    The doc is never deleted either way."""
    with claimed.lock:
        if claimed.finished:
            return False
        fields = claimed.pending_fields
        if fields is None:
            now_s = rfc3339(now or utcnow())
            attempts = claimed.fields["attempts"] + 1
            fields = {"attempts": attempts, "last_error": str(error or "unknown error")[:LAST_ERROR_CHARS],
                      "updated_at": now_s}
            if attempts >= max_attempts:
                fields.update(status="failed", failed_at=now_s)
            else:
                fields["status"] = "queued"
        committed = _write(store, claimed, fields)
        claimed.finished = True
        return committed


def finish(store: Any, claimed: Claimed, outcome: dict, *, max_attempts: int,
           now: Optional[datetime.datetime] = None) -> bool:
    """Terminal receipt of the bot's turn for this message: settled -> done; anything else is a
    handling error (requeue or failed). Idempotent: only the first terminal outcome is written."""
    status = str((outcome or {}).get("status") or "failed")
    if status != "settled":
        reason = str((outcome or {}).get("error") or status)
        return record_error(store, claimed, f"turn {status}: {reason}", max_attempts=max_attempts, now=now)
    with claimed.lock:
        if claimed.finished:
            return False
        now_s = rfc3339(now or utcnow())
        fields = claimed.pending_fields or {"status": "done", "done_at": now_s, "updated_at": now_s}
        committed = _write(store, claimed, fields)
        claimed.finished = True
        return committed


def reclaim_stale(store: Any, *, older_than_seconds: int = 1800, max_attempts: int = 5,
                  now: Optional[datetime.datetime] = None, limit: int = 200,
                  dry_run: bool = False) -> dict:
    """Recover stale delivered/read claims outside the turn-end path.

    Delivery is at least once: a reclaimed ``read`` claim may run its turn twice. Run this
    from a no-model maintenance job; it performs two status queries and no turn-end reads.
    In dry-run mode, transition counts describe what would be written.
    """
    if older_than_seconds < 0 or max_attempts < 1 or limit < 1:
        raise ValueError("older_than_seconds must be nonnegative; max_attempts and limit must be positive")
    now = now or utcnow()
    now_s = rfc3339(now)
    cutoff = now - datetime.timedelta(seconds=older_than_seconds)
    counts = {"scanned": 0, "fresh": 0, "requeued": 0, "failed": 0,
              "expired": 0, "conflicts": 0, "dry_run": dry_run}
    for status in ("delivered", "read"):
        for row in store.query_status(status, limit):
            counts["scanned"] += 1
            fields = row.fields
            updated_at = parse_ts(fields.get("updated_at"))
            if fields.get("status") != status or updated_at is None or updated_at >= cutoff:
                counts["fresh"] += 1
                continue
            attempts = fields["attempts"] + 1
            expiry = parse_ts(fields.get("expires_at"))
            changes = {"attempts": attempts, "updated_at": now_s,
                       "last_error": (f"reclaimed stale {status} claim after {older_than_seconds}s "
                                      "(claimer died or store failed; a 'read' claim's turn may already have run)")}
            if expiry is not None and expiry <= now:
                outcome = "expired"
                changes.update(status=outcome, expired_at=now_s)
            elif attempts >= max_attempts:
                outcome = "failed"
                changes.update(status=outcome, failed_at=now_s)
            else:
                outcome = "requeued"
                changes["status"] = "queued"
            if not dry_run:
                try:
                    store.update(row.doc_id, changes, row.update_time)
                except PreconditionFailed:
                    counts["conflicts"] += 1
                    continue
            counts[outcome] += 1
    return counts


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Fleet message maintenance")
    subparsers = parser.add_subparsers(dest="command", required=True)
    enqueue = subparsers.add_parser("enqueue", help="queue a busy target's message")
    enqueue.add_argument("--from", dest="from_handle", required=True)
    enqueue.add_argument("--to-home", type=Path, required=True)
    enqueue.add_argument("--body-file", type=Path, required=True)
    reclaim = subparsers.add_parser("reclaim-stale", help="requeue stale delivered/read claims")
    reclaim.add_argument("--older-than", type=int, default=1800, metavar="SECONDS")
    reclaim.add_argument("--target", choices=("live", "emulator"))
    reclaim.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "enqueue":
        try:
            config = drain_config_for_home(args.to_home)
            if config is None:
                print(json.dumps({"status": "disabled"}))
                return 0
            body = args.body_file.read_text(encoding="utf-8-sig")
            message_id = enqueue_message(args.from_handle, bot_identity(args.to_home), body, config=config)
        except Exception as exc:
            reason = str(exc).splitlines()
            print(f"fleet message enqueue: {reason[0] if reason else type(exc).__name__}", file=sys.stderr)
            return 1
        print(json.dumps({"status": "queued", "message_id": message_id}))
        return 0
    # This standalone cron command owns its stderr contract even if the credential
    # fallback or config loader logs a warning before raising.
    prior_log_level = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        from hermes_cli.config import load_config
        section = (load_config() or {}).get("fleet_messages") or {}
        target = args.target or section.get("target") or "live"
        if target not in ("live", "emulator"):
            raise ValueError(f"unsupported target {target!r}")
        host = str(section.get("emulator_host") or "").strip()
        if target == "emulator" and not host:
            raise ValueError("emulator target requires fleet_messages.emulator_host")
        config = DrainConfig(target=target, emulator_host=host,
                             project=str(section.get("project") or DEFAULT_PROJECT),
                             database=str(section.get("database") or DEFAULT_DATABASE),
                             max_attempts=int(section.get("max_attempts", 5)),
                             timeout_seconds=float(section.get("timeout_seconds", 5)))
        counts = reclaim_stale(store_for(config), older_than_seconds=args.older_than,
                               max_attempts=config.max_attempts, dry_run=args.dry_run)
    except Exception as exc:
        reason = str(exc).splitlines()
        print(f"fleet message reclaim-stale: {reason[0] if reason else type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        logging.disable(prior_log_level)
    print(json.dumps(counts, sort_keys=True))
    return 0


def render_input(claimed: Claimed) -> tuple[str, Optional[dict], dict]:
    """(text, turn_author, display_metadata) for the injected turn.

    DM -> a Bot Chat message attributed to the sending bot (same author shape as ``message_agent``).
    notify_wake -> the wake text as a system notification turn (no bot author), like a kanban wake.
    """
    f = claimed.fields
    sender = str(f.get("from") or "unknown")
    body = str(f.get("body") or "")
    attempt_note = f" (redelivery; earlier attempts: {int(f.get('attempts') or 0)})" if f.get("last_error") not in (
        None, "", "target_busy") else ""
    task = f" for task {f['task_id']}" if f.get("task_id") else ""
    if claimed.kind == "notify_wake":
        header = (f"[Queued fleet notification {claimed.doc_id}{task}, held while you were busy; "
                  f"delivered at your turn end{attempt_note}.]")
        return f"{header}\n{body}", None, {"notification_category": "result"}
    header = (f"[Queued fleet message {claimed.doc_id} from @{sender}{task}, held while you were busy "
              f"(target_busy); delivered at your turn end{attempt_note}. The sender is not waiting on "
              f"this turn: if a reply is needed, send it with message_agent to @{sender}.]")
    text = body if body.lstrip().startswith("Message from ") else f"Message from 🤖 {sender} (@{sender}): {body}"
    author = {"id": f"bot:{sender}", "name": sender, "is_bot": True}
    return f"{header}\n{text}", author, {}


if __name__ == "__main__":
    raise SystemExit(main())
