"""Turn-end and idle drain of a bot's own queued ``fleet_messages_v1`` docs.

A Bot Chat DM or notify-subscribe wake that hit ``target_busy`` stays in the durable registry with
``status='queued'`` (write-first sender). The TUI, CLI and API Bot Chat turn-end paths,
plus the idle paths, call :func:`claim_next`, which runs exactly ONE equality-only query:

    to == <this bot> AND status == 'queued'  LIMIT 50

The returned page is sorted by created_at and document id locally before the caller's limit
is applied. No composite index is needed.

An idle TUI owner polls at a bounded interval; a profile's no-agent cron can run
``python -m tools.fleet_message_drain idle-tick`` for a Bot Chat without a live UI owner.
Both paths use the same atomic claim and hand-off. Expired docs cannot starve fresh docs.
API turn-end follow-ups use the canonical quiet Bot Chat CLI with the relay's 600-second
subprocess deadline; the child is killed and its claim requeued if that deadline expires.

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
    queued    -> expired     its 24-hour expiry or queued delivery timeout passed

``replied`` is left to the reply path: the injected input tells the bot to answer the sender with
``message_agent``; a sender-side write may then mark the doc ``replied``.

One message is handed over per query. The next turn or idle tick runs the next query, so ordering holds,
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
      queued_timeout_seconds: 1800 # queued without delivery -> expired with sender notice

Security: the query filters ``to == me``, every returned doc is re-checked for ``to == me`` before
any write, and writes are limited to the status bookkeeping fields below. Firestore IAM/rules stay
the real boundary; this module never deletes and never writes message content.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)
_config_read_warned = False
_config_read_warning_lock = threading.Lock()
_api_turn_locks_guard = threading.Lock()
_api_turn_locks: dict[tuple[str, str], threading.Lock] = {}
_api_drain_flights_guard = threading.Lock()
_api_drain_flights: set[str] = set()

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
SYSTEM_SENDER = "fleet-system"
EXPIRY_NOTICE_SUFFIX = "-expiry-notice"
LAST_ERROR_CHARS = 500
_SCOPE = "https://www.googleapis.com/auth/datastore"


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
    queued_timeout_seconds: int = 1800


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
        queued_timeout = max(1, int(section.get("queued_timeout_seconds", 1800)))
    except (TypeError, ValueError):
        logger.warning("fleet message drain: bad numeric setting in fleet_messages; skipping")
        return None
    return DrainConfig(target=target, emulator_host=host,
                       project=str(section.get("project") or DEFAULT_PROJECT),
                       database=str(section.get("database") or DEFAULT_DATABASE),
                       limit=limit, max_attempts=max_attempts, timeout_seconds=timeout,
                       queued_timeout_seconds=queued_timeout)


def drain_config_for_home(profile_home: Path | str) -> Optional[DrainConfig]:
    """Resolve a recipient's opt-in from its own profile, without changing process env."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(profile_home)
    try:
        return drain_config()
    finally:
        reset_hermes_home_override(token)


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


def _queued_timeout_sender_notice(doc_id: str, recipient: Any, seconds: int) -> str:
    label = str(recipient or "recipient")
    duration = f"{seconds // 60} minutes" if seconds % 60 == 0 else f"{seconds} seconds"
    return (f"Queued message {doc_id} to @{label} was NOT delivered: it remained queued "
            f"for {duration} without an available Bot Chat turn.")


def _notify_sender_of_expiry(store: Any, doc_id: str, fields: dict, now: datetime.datetime,
                             reason: str) -> None:
    """Push the expiry to the original sender as one ``notify_wake`` doc from ``fleet-system`` (the
    schema's system-notification kind). Never raises: the expiry it reports is already committed.

    The doc id is deterministic, so a repeated sweep finds it already created. Our own notices
    (``fleet-system`` sender / ``-expiry-notice`` id) never get one, so an unread notice that
    expires cannot start a chain."""
    sender = fields.get("from")
    if (not isinstance(sender, str) or not sender.strip() or sender == SYSTEM_SENDER
            or doc_id.endswith(EXPIRY_NOTICE_SUFFIX)):
        return
    created = parse_ts(fields.get("created_at"))
    minutes = int((now - created).total_seconds() // 60) if created else 0
    now_s = rfc3339(now)
    notice = {
        "from": SYSTEM_SENDER, "to": sender, "kind": "notify_wake", "status": "queued", "attempts": 0,
        "schema_version": 2, "created_at": now_s, "updated_at": now_s,
        "expires_at": rfc3339(now + datetime.timedelta(hours=24)),
        "body": (f"to @{fields.get('to') or 'recipient'}, message {doc_id} was NOT delivered: "
                 f"{reason}, queued {minutes} minutes"),
    }
    try:
        store.create(doc_id + EXPIRY_NOTICE_SUFFIX, notice)
    except urllib.error.HTTPError as exc:
        if exc.code != 409:
            logger.warning("fleet message drain: expiry notice for %s failed (HTTP %s)", doc_id, exc.code)
    except Exception:
        logger.warning("fleet message drain: could not write expiry notice for %s", doc_id, exc_info=True)


def claim_next(store: Any, me: str, *, limit: int = 10, now: Optional[datetime.datetime] = None,
               queued_timeout_seconds: int = 1800, allow_claim: bool = True) -> Optional[Claimed]:
    """Run one recipient query and atomically claim the oldest claimable doc (queued -> delivered).

    Expires docs past ``expires_at`` or the queued timeout and skips foreign docs. A doc whose claim
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
        expired_at = parse_ts(fields["expires_at"])
        queued_timed_out = now - parse_ts(fields["created_at"]) >= datetime.timedelta(
            seconds=queued_timeout_seconds)
        if expired_at <= now or queued_timed_out:
            notice = (_expire_sender_notice(row.doc_id, fields.get("to")) if expired_at <= now else
                      _queued_timeout_sender_notice(row.doc_id, fields.get("to"), queued_timeout_seconds))
            try:
                store.update(row.doc_id, {"status": "expired", "expired_at": now_s, "updated_at": now_s,
                                          "sender_notice": notice, "sender_notice_at": now_s,
                                          "last_error": "queued_delivery_timeout" if queued_timed_out else "expired"},
                             row.update_time)
            except PreconditionFailed:
                pass
            except Exception:
                logger.warning("fleet message drain: could not expire doc %s", row.doc_id, exc_info=True)
            else:
                _notify_sender_of_expiry(store, row.doc_id, fields, now,
                                         "expired" if expired_at <= now else "timed out")
            continue
        if not allow_claim:
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
    """The turn-end Firestore query (expire + claim). Shared by TUI, CLI and API turns.

    Returns ``(config, store, claimed)`` when drain is enabled (``claimed`` may be None after the
    query). Returns ``None`` when :func:`drain_config` is disabled for this profile."""
    config = drain_config(cfg)
    if config is None:
        return None
    store = store_for(config)
    claimed = claim_next(store, bot_identity(profile_home), limit=config.limit,
                         queued_timeout_seconds=config.queued_timeout_seconds)
    return config, store, claimed


def drain_agent_turn(agent: Any, profile_home: Path | str, history: Optional[list] = None) -> bool:
    """Claim one API Bot Chat message and run its canonical CLI turn with a hard deadline."""
    # Strict Bot Chat gate (carry t_2e0ceb41): a Slack/Meet session that carries message_agent
    # via bot_mode.message_agent_platforms must never drain the Bot Chat mailbox.
    from tools.bot_mode_dm import is_canonical_bot_chat

    if not is_canonical_bot_chat(agent):
        return False
    triplet = turn_end_drain_query(profile_home)
    if triplet is None:
        return False
    config, store, claimed = triplet
    if claimed is None or not mark_read(store, claimed):
        return False
    try:
        text, author, _metadata = render_input(claimed)
        outcome = _idle_cli_turn(Path(profile_home), text, author)
        finish(store, claimed, outcome, max_attempts=config.max_attempts)
    except Exception as exc:
        record_error(store, claimed, str(exc), max_attempts=config.max_attempts)
        raise
    return True


@contextlib.contextmanager
def api_turn_lock(profile_home: Path | str, session_id: str | None, *, agent: Any = None):
    """Serialize one session locally; API requests never wait on the profile flock."""
    if agent is None or not _api_drain_enabled(agent):
        yield
        return
    key = (str(Path(profile_home).resolve()), str(session_id or ""))
    with _api_turn_locks_guard:
        lock = _api_turn_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _api_turn_locks[key] = lock
    # A running drain owns this session until its bounded CLI child exits. Return busy
    # promptly if a new API request arrives instead of occupying a gateway worker.
    if not lock.acquire(timeout=1):
        from tools.bot_relay import TurnBusyError
        raise TurnBusyError(bot_identity(profile_home), 1)
    try:
        yield
    finally:
        lock.release()


def _api_drain_enabled(agent: Any) -> bool:
    from tools.bot_mode_dm import is_canonical_bot_chat
    return is_canonical_bot_chat(agent) and drain_config() is not None


def schedule_drain_agent_turn(agent: Any, profile_home: Path | str, history: Optional[list] = None,
                              *, session_id: str | None = None) -> bool:
    """Start a follow-up after the API result is ready, without delaying that result."""
    if not _api_drain_enabled(agent):
        return False
    from agent.memory_provider import spawn_context_thread
    home = Path(profile_home)
    profile_key = str(home.resolve())
    with _api_drain_flights_guard:
        if profile_key in _api_drain_flights:
            return False
        _api_drain_flights.add(profile_key)
    session_id = session_id or getattr(agent, "session_id", None)

    def run_in_profile_scope() -> None:
        key = (profile_key, str(session_id or ""))
        with _api_turn_locks_guard:
            session_lock = _api_turn_locks.setdefault(key, threading.Lock())
        if not session_lock.acquire(blocking=False):
            return
        try:
            from tools.bot_mode_probe import _hermes_root
            from tools.bot_relay import TurnBusyError, acquire_turn_lock
            try:
                with acquire_turn_lock(_hermes_root(home), bot_identity(home), timeout_seconds=0):
                    drain_agent_turn(agent, home, history)
            except TurnBusyError:
                return  # The next turn end or idle tick will retry the queued doc.
        finally:
            session_lock.release()

    def run() -> None:
        try:
            run_in_profile_scope()
        except Exception:
            logger.warning("api_server fleet message drain failed", exc_info=True)
        finally:
            with _api_drain_flights_guard:
                _api_drain_flights.discard(profile_key)

    try:
        spawn_context_thread(run, name="fleet-message-api-drain").start()
    except Exception:
        with _api_drain_flights_guard:
            _api_drain_flights.discard(profile_key)
        raise
    return True


def idle_tick(profile_home: Path | str, *, config: Optional[DrainConfig] = None, store: Any = None,
              busy: Optional[Callable[[], bool]] = None,
              run_turn: Optional[Callable[[str, Optional[dict], dict], dict]] = None,
              now: Optional[datetime.datetime] = None) -> bool:
    """Expire queued timeouts and hand over at most one doc when this Bot Chat is idle.

    The no-agent cron command holds the profile's cross-process turn lock around this call.
    A live TUI owner uses its own admitted-session hook instead.
    """
    config = config or drain_config()
    if config is None:
        return False
    store = store if store is not None else store_for(config)
    home = Path(profile_home)
    busy = busy or (lambda: _idle_bot_chat_busy(home))
    occupied = busy()
    claimed = claim_next(store, bot_identity(home), limit=config.limit, now=now,
                         queued_timeout_seconds=config.queued_timeout_seconds, allow_claim=not occupied)
    if claimed is None:
        return False
    if busy():
        release(store, claimed, now=now)
        return False
    if not mark_read(store, claimed, now=now):
        return False
    try:
        text, author, metadata = render_input(claimed)
        outcome = (run_turn or (lambda t, a, m: _idle_cli_turn(home, t, a)))(text, author, metadata)
        finish(store, claimed, outcome, max_attempts=config.max_attempts, now=now)
    except Exception as exc:
        record_error(store, claimed, str(exc), max_attempts=config.max_attempts, now=now)
        raise
    return True


def _idle_bot_chat_busy(home: Path) -> bool:
    from tools.bot_live_delivery import find_canonical_live_owner
    return find_canonical_live_owner(home) is not None


def _idle_cli_turn(home: Path, text: str, author: Optional[dict]) -> dict:
    """Run the claimed input in the recipient's canonical Bot Chat through its CLI."""
    import tempfile
    from tools.bot_relay import BOT_CHAT_TURN_ARGS, TURN_ATTEMPT_TIMEOUT_SECONDS, _hermes_cli, delivery_env

    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="fleet-msg-", suffix=".txt",
                                     delete=False) as payload:
        payload.write(text)
        path = Path(payload.name)
    try:
        argv = [_hermes_cli(), "-p", bot_identity(home), *BOT_CHAT_TURN_ARGS, "--query-file", str(path)]
        proc = subprocess.run(argv, env=delivery_env(author, home), stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                              timeout=TURN_ATTEMPT_TIMEOUT_SECONDS)
        return {"status": "settled" if proc.returncode == 0 else "failed",
                "error": f"Bot Chat turn exited {proc.returncode}" if proc.returncode else ""}
    finally:
        path.unlink(missing_ok=True)


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
    """attempts+1 and last_error; requeue for the next drain, or ``failed`` at max_attempts.
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
                if outcome == "expired":
                    _notify_sender_of_expiry(store, row.doc_id, fields, now, "expired")
            counts[outcome] += 1
    return counts


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Fleet message maintenance")
    subparsers = parser.add_subparsers(dest="command", required=True)
    enqueue = subparsers.add_parser("enqueue", help="queue a busy target's message")
    enqueue.add_argument("--from", dest="from_handle", required=True)
    enqueue.add_argument("--to-home", type=Path, required=True)
    enqueue.add_argument("--body-file", type=Path, required=True)
    enqueue.add_argument("--message-id", default=None,
                         help="relay envelope id; create-only, an existing doc is not rewritten")
    reclaim = subparsers.add_parser("reclaim-stale", help="requeue stale delivered/read claims")
    reclaim.add_argument("--older-than", type=int, default=1800, metavar="SECONDS")
    reclaim.add_argument("--target", choices=("live", "emulator"))
    reclaim.add_argument("--dry-run", action="store_true")
    subparsers.add_parser("idle-tick", help="expire queued timeouts and deliver one idle Bot Chat message")
    args = parser.parse_args(argv)
    if args.command == "enqueue":
        try:
            recipient = bot_identity(args.to_home)
            if not recipient_drain_enabled(recipient, profile_home=args.to_home):
                print(json.dumps({"status": "disabled", "reason": "target_busy", "reply_relayed": False}))
                return 0
            body = args.body_file.read_text(encoding="utf-8-sig")
            from tools.fleet_message_enqueue import enqueue_busy_dm
            message_id = enqueue_busy_dm(
                sender=args.from_handle, recipient=recipient, body=body, message_id=args.message_id)
        except Exception as exc:
            reason = str(exc).splitlines()
            print(f"fleet message enqueue: {reason[0] if reason else type(exc).__name__}", file=sys.stderr)
            return 1
        print(json.dumps({"status": "queued", "message_id": message_id, "reply_relayed": False}))
        return 0
    if args.command == "idle-tick":
        from hermes_constants import get_hermes_home
        from tools.bot_mode_probe import _hermes_root
        from tools.bot_relay import TurnBusyError, acquire_turn_lock

        home = Path(get_hermes_home())
        config = drain_config()
        if config is None:
            print(json.dumps({"status": "disabled"}))
            return 0
        try:
            with acquire_turn_lock(_hermes_root(home), bot_identity(home), timeout_seconds=0):
                delivered = idle_tick(home, config=config)
        except TurnBusyError:
            # A busy recipient can still receive its 30-minute failure notice.
            idle_tick(home, config=config, busy=lambda: True)
            delivered = False
        except Exception as exc:
            reason = str(exc).splitlines()
            print(f"fleet message idle-tick: {reason[0] if reason else type(exc).__name__}", file=sys.stderr)
            return 1
        print(json.dumps({"status": "delivered" if delivered else "idle"}))
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
                  f"delivered as your next turn{attempt_note}.]")
        return f"{header}\n{body}", None, {"notification_category": "result"}
    header = (f"[Queued fleet message {claimed.doc_id} from @{sender}{task}, held while you were busy "
              f"(target_busy); delivered as your next turn{attempt_note}. The sender is not waiting on "
              f"this turn: if a reply is needed, send it with message_agent to @{sender}.]")
    text = body if body.lstrip().startswith("Message from ") else f"Message from 🤖 {sender} (@{sender}): {body}"
    author = {"id": f"bot:{sender}", "name": sender, "is_bot": True}
    return f"{header}\n{text}", author, {}


if __name__ == "__main__":
    raise SystemExit(main())
