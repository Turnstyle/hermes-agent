"""Claim, expiry and bookkeeping of a bot's own queued ``fleet_messages_v1`` docs.

A Bot Chat DM or notify-subscribe wake that hit ``target_busy`` stays in the durable registry with
``status='queued'`` (write-first sender, ``tools/fleet_message_spool.py``). The recipient's gateway
drains it (``gateway/fleet_message_bridge.py`` -> ``tools/fleet_message_admission.py``): exactly ONE
equality-only query per drain step,

    to == <this bot> AND status == 'queued'  LIMIT 50

sorted by created_at and document id locally. No composite index is needed. Execution is never
this module's: a claimed message is admitted through the recipient's native Bot Chat authority
(``bot_relay.deliver``), the only door that runs a Bot Chat turn, and the doc follows that
admission's receipt.

Status transitions this module performs (every write carries an ``updateTime`` precondition, so a
doc that anyone else touched in between is never overwritten):

    queued    -> delivered   claim (atomic: two racing drains, or a late sender success, cannot both
                             win because the precondition is the queued doc's own updateTime)
    delivered -> read        the message won its one execution claim and goes to the native door
    read      -> done        the native receipt settled (nothing else: not the claim, a reply, an exit)
    read      -> failed      the native receipt failed, or the door refused the delivery for good
    read      -> queued      the door refused before anything ran (no authority, owner busy): no attempt
    read      (kept)         uncertain execution: last_error ``execution_unverified``; never replayed
    delivered -> queued      the session got busy between claim and hand-off (no attempt counted)
    queued    -> rejected    the doc is malformed for this reader (unknown kind / missing body)
    queued    -> expired     its 24-hour expiry or queued delivery timeout passed (sender notified)

``replied`` is left to the reply path. The separate ``reclaim-stale`` maintenance command requeues
stale ``delivered`` claims only; a stale ``read`` claim may have run and is never requeued.

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

from hermes_platform.resolver import known_dirs, locate_command

logger = logging.getLogger(__name__)
_config_read_warned = False
_config_read_warning_lock = threading.Lock()

COLLECTION = "fleet_messages_v1"
DEFAULT_PROJECT = "mission-control-444444"
DEFAULT_DATABASE = "fleet-operations"
KINDS = frozenset({"dm", "notify_wake"})
TS_FIELDS = frozenset({"created_at", "updated_at", "expires_at", "delivered_at", "read_at", "replied_at",
                       "done_at", "failed_at", "rejected_at", "expired_at", "sender_notice_at", "requeued_at"})
# The only fields this reader may write. Message content (from/to/body/kind/created_at) is never written.
WRITABLE_FIELDS = frozenset({"status", "updated_at", "delivered_at", "read_at", "done_at", "failed_at",
                             "rejected_at", "expired_at", "attempts", "last_error", "sender_notice",
                             "sender_notice_at", "requeued_at"})
MAX_LIMIT = 50
SYSTEM_SENDER = "fleet-system"
EXPIRY_NOTICE_SUFFIX = "-expiry-notice"
LAST_ERROR_CHARS = 500
EXECUTION_UNVERIFIED = "execution_unverified"
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
    config when ``cfg`` is None, so the caller must already be in the profile's scope."""
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


@dataclasses.dataclass(frozen=True)
class QueueAdmission:
    """Config eligibility only; this does not prove a running consumer or backend access."""

    reason: str

    @property
    def enabled(self) -> bool:
        return self.reason == "enabled"


def inspect_recipient_queue(profile_name: str, *,
                            profile_home: Path | str | None = None) -> QueueAdmission:
    """A sanitized, typed recipient-scoped queue admission, read from the recipient's own
    ``config.yaml`` without changing env: ``enabled``, ``disabled``, ``missing_home``,
    ``missing_config``, ``missing_opt_in``, ``unreadable_config``, ``invalid_config``."""
    if profile_home is not None:
        home = Path(profile_home)
    else:
        from tools.bot_mode_probe import _default_home, _hermes_root, _roster
        home = dict(_roster(_hermes_root(Path(_default_home())))).get(profile_name)
    try:
        if home is None or not home.is_dir():
            return QueueAdmission("missing_home")
        path = home / "config.yaml"
        path.stat()
    except FileNotFoundError:
        return QueueAdmission("missing_config")
    except OSError:
        return QueueAdmission("unreadable_config")
    from utils import fast_safe_load
    try:
        with path.open(encoding="utf-8-sig") as stream:
            cfg = fast_safe_load(stream)
    except OSError:
        return QueueAdmission("unreadable_config")
    except Exception:
        return QueueAdmission("invalid_config")
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, dict):
        return QueueAdmission("invalid_config")
    if "fleet_messages" not in cfg:
        return QueueAdmission("missing_opt_in")
    section = cfg["fleet_messages"]
    if not isinstance(section, dict):
        return QueueAdmission("invalid_config")
    if "drain_on_turn_end" not in section:
        return QueueAdmission("missing_opt_in")
    opt_in = section["drain_on_turn_end"]
    if opt_in is False:
        return QueueAdmission("disabled")
    if opt_in is not True:
        return QueueAdmission("invalid_config")
    try:
        config = drain_config(cfg)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return QueueAdmission("invalid_config")
    return QueueAdmission("enabled" if config is not None else "invalid_config")


def recipient_drain_enabled(profile_name: str, *, profile_home: Path | str | None = None) -> bool:
    """Compatibility bool API; diagnostics use :func:`inspect_recipient_queue`."""
    return inspect_recipient_queue(profile_name, profile_home=profile_home).enabled


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

    def _rows(self, query: dict) -> list[Row]:
        self.reads += 1
        rows = []
        for item in self._req(self.query_url, "POST", {"structuredQuery": query}):
            doc = item.get("document")
            if doc:
                rows.append(Row(doc["name"].rsplit("/", 1)[1],
                                {k: dec(v) for k, v in doc.get("fields", {}).items()}, doc.get("updateTime", "")))
        return rows

    def query_queued(self, to: str, limit: int) -> list[Row]:
        if not to:
            raise Refused("recipient id is required")
        where = {"compositeFilter": {"op": "AND", "filters": [
            {"fieldFilter": {"field": {"fieldPath": "to"}, "op": "EQUAL", "value": enc(to)}},
            {"fieldFilter": {"field": {"fieldPath": "status"}, "op": "EQUAL", "value": enc("queued")}},
        ]}}
        rows = self._rows({"from": [{"collectionId": COLLECTION}], "where": where, "limit": MAX_LIMIT})

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
        return self._rows({"from": [{"collectionId": COLLECTION}],
                           "where": {"fieldFilter": {"field": {"fieldPath": "status"},
                                                     "op": "EQUAL", "value": enc(status)}},
                           "limit": max(1, int(limit))})

    def get(self, doc_id: str) -> Optional[Row]:
        """Single-doc read (receipt reconciliation and operator checks)."""
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
        raise Refused(f"field(s) not writable by the drain: {sorted(bad)}")
    if not update_time:
        raise Refused("every drain write needs an updateTime precondition")


_live_lock = threading.Lock()
_live_credentials: Any = None
_gcloud_token_cached = ""
_gcloud_token_until = 0.0
_credentials_warned = False
_FLEET_GOOGLE_ACCOUNT = "conductive-admin-sa@fifth-flame-482113-v8.iam.gserviceaccount.com"


class NoGoogleCredentials(RuntimeError):
    """Neither the named service-account source nor its gcloud login can authorize Firestore."""


def _named_key_credentials() -> Any:
    """Sign in with the named service-account key file. Never Application Default Credentials.

    Owner's firm rule (card A-L3): no ADC. The path comes from FLEET_MESSAGES_SA_KEY_FILE (read
    through the profile's secret scope), else the OS user's conductive-admin key (the account's
    real home, never a sandboxed HOME). A missing or unreadable file raises, and _live_token then
    uses the explicitly bound fleet service account in gcloud."""
    import pwd  # windows-footgun: ok — a Windows ImportError falls through to gcloud
    from google.oauth2 import service_account
    from agent.secret_scope import get_secret
    path = get_secret("FLEET_MESSAGES_SA_KEY_FILE") or os.path.join(
        pwd.getpwuid(os.getuid()).pw_dir, ".config", "conductive-admin", "conductive-admin-sa-key.json")
    return service_account.Credentials.from_service_account_file(path, scopes=[_SCOPE])


def _gcloud_token() -> str:
    global _gcloud_token_cached, _gcloud_token_until
    if not _gcloud_token_cached or time.monotonic() >= _gcloud_token_until:
        # gcloud runs its own Python; the Hermes bootstrap's PYTHONPATH/PYTHONHOME would load
        # Hermes's crypto packages into it and crash it (pyOpenSSL: no attribute GEN_EMAIL).
        gcloud_env = {k: v for k, v in os.environ.items()
                      if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "GOOGLE_APPLICATION_CREDENTIALS")}
        gcloud = locate_command("gcloud", known_dirs=known_dirs.snap_bin_dirs()).command
        if not gcloud:
            raise FileNotFoundError("gcloud executable not found")
        proc = subprocess.run([*gcloud, "auth", "print-access-token", f"--account={_FLEET_GOOGLE_ACCOUNT}"],
                              capture_output=True, text=True, check=True, timeout=3, env=gcloud_env)
        token = proc.stdout.strip()
        if not token:
            raise ValueError("gcloud returned no token")
        _gcloud_token_cached = token
        _gcloud_token_until = time.monotonic() + 45 * 60
    return _gcloud_token_cached


def _live_token() -> str:
    """Prefer the named key source; fall back to the same explicitly named gcloud account."""
    global _live_credentials, _credentials_warned
    with _live_lock:
        try:
            import google.auth.transport.requests
            if _live_credentials is None:
                _live_credentials = _named_key_credentials()  # A-L3: named key file, never ADC
            if not _live_credentials.valid:
                _live_credentials.refresh(google.auth.transport.requests.Request())
            if not _live_credentials.token:
                raise ValueError("Named service-account source returned no token")
            _credentials_warned = False
            return _live_credentials.token
        except Exception:
            _live_credentials = None
        try:
            token = _gcloud_token()
            _credentials_warned = False
            return token
        except Exception as exc:
            if not _credentials_warned:
                logger.warning("fleet message drain: no Google credentials (named service account and gcloud "
                               "token failed); drain skipped, turns unaffected")
                _credentials_warned = True
            raise NoGoogleCredentials("named service account and gcloud token failed") from exc


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


def _reclaimed_sender_notice(doc_id: str, recipient: Any) -> str:
    return (f"Queued message {doc_id} to @{recipient or 'recipient'} expired before completion; "
            "a previously started turn may have received it.")


def _handed_over_sender_notice(doc_id: str, recipient: Any, read_at: Any) -> str:
    return (f"Queued message {doc_id} to @{recipient or 'recipient'} was handed to a Bot Chat turn at "
            f"{read_at}; no reply or completion was recorded before it timed out. It may have been "
            "answered (check for a reply), so do not treat it as undelivered.")


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
    body = (_reclaimed_sender_notice(doc_id, fields.get("to"))
            if fields.get("status") in ("delivered", "read") else
            _handed_over_sender_notice(doc_id, fields.get("to"), fields["read_at"])
            if fields.get("read_at") else
            f"to @{fields.get('to') or 'recipient'}, message {doc_id} was NOT delivered: "
            f"{reason}, queued {minutes} minutes")
    notice = {
        "from": SYSTEM_SENDER, "to": sender, "kind": "notify_wake", "status": "queued", "attempts": 0,
        "schema_version": 2, "created_at": now_s, "updated_at": now_s,
        "expires_at": rfc3339(now + datetime.timedelta(hours=24)),
        "body": body,
    }
    try:
        store.create(doc_id + EXPIRY_NOTICE_SUFFIX, notice)
    except urllib.error.HTTPError as exc:
        if exc.code != 409:
            logger.warning("fleet message drain: expiry notice for %s failed (HTTP %s)", doc_id, exc.code)
    except Exception:
        logger.warning("fleet message drain: could not write expiry notice for %s", doc_id, exc_info=True)


def queued_clock_start(fields: dict, receiver_up_since: Optional[datetime.datetime] = None) -> datetime.datetime:
    """When the queued-timeout clock of a doc started: the latest of created_at, requeued_at and
    the time the receiver came back up."""
    starts = [parse_ts(fields.get("created_at")), parse_ts(fields.get("requeued_at")), receiver_up_since]
    return max(start for start in starts if start is not None)


def receiver_up_since(home: Path | str) -> Optional[datetime.datetime]:
    """Best-effort time since which the gateway that serves this profile has been up and taking
    messages; None when unknown. A running gateway counts from its process start. A gateway that is
    starting, draining, stopped or gone counts from its last state write, so waiting out a restart
    does not use up the queued timeout. A profile is served by its own gateway_state.json or by the
    shared gateway one level above ``profiles/``; the latest time wins."""
    home = Path(home)
    candidates = [home]
    if home.parent.name == "profiles":
        candidates.append(home.parent.parent)
    latest: Optional[datetime.datetime] = None
    for root in candidates:
        try:
            record = json.loads((root / "gateway_state.json").read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                continue
            since = None
            pid, recorded_start = record.get("pid"), record.get("start_time")
            if record.get("gateway_state") == "running" and isinstance(pid, int) and pid > 0:
                from gateway.status import _pid_exists, get_process_start_time
                if _pid_exists(pid) and (recorded_start is None or get_process_start_time(pid) == recorded_start):
                    import psutil
                    since = datetime.datetime.fromtimestamp(psutil.Process(pid).create_time(),
                                                            tz=datetime.timezone.utc)
            if since is None:
                since = parse_ts(record.get("updated_at"))
        except Exception:
            continue
        if since is not None and (latest is None or since > latest):
            latest = since
    return latest


def _expire_row(store: Any, row: Row, now: datetime.datetime, queued_timeout_seconds: int,
                receiver_since: Optional[datetime.datetime]) -> bool:
    """Expire one queued row past ``expires_at`` or its queued timeout (sender notified). True when
    the row is out of the claimable set (expired now, or moved by someone else meanwhile)."""
    fields = row.fields
    expired_at = parse_ts(fields["expires_at"])
    queued_timed_out = now - queued_clock_start(fields, receiver_since) >= datetime.timedelta(
        seconds=queued_timeout_seconds)
    if expired_at > now and not queued_timed_out:
        return False
    notice = (_handed_over_sender_notice(row.doc_id, fields.get("to"), fields["read_at"])
              if fields.get("read_at") else
              _expire_sender_notice(row.doc_id, fields.get("to")) if expired_at <= now else
              _queued_timeout_sender_notice(row.doc_id, fields.get("to"), queued_timeout_seconds))
    now_s = rfc3339(now)
    try:
        store.update(row.doc_id, {"status": "expired", "expired_at": now_s, "updated_at": now_s,
                                  "sender_notice": notice, "sender_notice_at": now_s,
                                  "last_error": "queued_delivery_timeout" if queued_timed_out else "expired"},
                     row.update_time)
    except PreconditionFailed:
        return True
    except Exception:
        logger.warning("fleet message drain: could not expire doc %s", row.doc_id, exc_info=True)
        return True
    _notify_sender_of_expiry(store, row.doc_id, fields, now, "expired" if expired_at <= now else "timed out")
    return True


def claim_next(store: Any, me: str, *, limit: int = 10, now: Optional[datetime.datetime] = None,
               queued_timeout_seconds: int = 1800, allow_claim: bool = True,
               receiver_up_since: Optional[datetime.datetime] = None) -> Optional[Claimed]:
    """Run one recipient query and atomically claim the oldest claimable doc (queued -> delivered).

    Expires docs past ``expires_at`` or the queued timeout and skips foreign docs. A doc whose claim
    precondition fails was moved by someone else (late sender success, a racing drain): skipped.

    The queued timeout counts from the latest of ``created_at``, ``requeued_at`` (a requeue restarts
    the clock) and ``receiver_up_since`` (time the receiving gateway was draining or restarting does
    not count). The 24-hour ``expires_at`` limit is unchanged.
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
                                          "last_error": f"drain: {problem}"}, row.update_time)
            except PreconditionFailed:
                pass
            except Exception:
                logger.warning("fleet message drain: could not reject malformed doc %s", row.doc_id, exc_info=True)
            continue
        if _expire_row(store, row, now, queued_timeout_seconds, receiver_up_since) or not allow_claim:
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


def claimed_from_row(row: Optional[Row], me: str) -> Optional[Claimed]:
    """A claim handle for a doc this bot already holds (delivered/read), re-read for its next write."""
    if row is None or row.fields.get("to") != me or row.fields.get("status") not in ("delivered", "read"):
        return None
    return Claimed(row.doc_id, dict(row.fields), row.update_time, me)


def mark_read(store: Any, claimed: Claimed, now: Optional[datetime.datetime] = None,
              *, profile_home: Path | str | None = None) -> bool:
    """Win the message's ONE execution claim, then move the doc delivered -> read. True only when
    this caller may now hand the message to the native door.

    The claim lives under the recipient home (``tools.fleet_message_spool``) and is shared with the
    local-first spool drain, so a message the local transport already took — or a doc requeued by
    ``reclaim_stale`` after its turn may have run — is reconciled from that record and never run
    again: settled -> done; a final outcome -> failed; anything else stays held ``read``."""
    from hermes_constants import get_hermes_home
    from tools import fleet_message_spool as spool

    home = Path(profile_home or get_hermes_home())
    now_s = rfc3339(now or utcnow())
    envelope = spool.read_envelope(home, claimed.doc_id)
    if envelope is not None and not spool.envelope_matches_doc(envelope, claimed.fields):
        _write(store, claimed, {"status": "read", "last_error": "local_spool_conflict", "updated_at": now_s})
        return False
    record = spool.execution(home, claimed.doc_id)
    if record is None and spool.claim_execution(home, claimed.doc_id, transport="cloud", fields=claimed.fields):
        try:
            committed = _write(store, claimed, {"status": "read", "read_at": now_s, "updated_at": now_s})
        except BaseException:
            spool.release_execution(home, claimed.doc_id)  # nothing ran: the doc may be read; reclaim finds it
            raise
        if not committed:
            spool.release_execution(home, claimed.doc_id)
            return False
        return True
    record = record or spool.execution(home, claimed.doc_id) or {}
    if not spool.execution_matches(record, claimed.fields):
        _write(store, claimed, {"status": "read", "last_error": "local_spool_conflict", "updated_at": now_s})
        return False
    reconcile_from_execution(store, claimed, record, now=now)
    return False


def reconcile_from_execution(store: Any, claimed: Claimed, record: dict,
                             now: Optional[datetime.datetime] = None) -> None:
    """Project an existing execution claim onto the doc without running anything."""
    status = record.get("status")
    if status == "settled":
        finish(store, claimed, {"status": "settled"}, max_attempts=1, now=now)
    elif status in ("failed", "refused", "cancelled", "expired"):
        fail(store, claimed, f"native {status}: {record.get('reason') or record.get('error') or status}", now=now)
    else:
        _write(store, claimed, {"status": "read", "last_error": EXECUTION_UNVERIFIED,
                                "updated_at": rfc3339(now or utcnow())})


def release(store: Any, claimed: Claimed, now: Optional[datetime.datetime] = None,
            *, last_error: Optional[str] = None) -> bool:
    """Hand a claim back (-> queued) without counting an attempt: nothing ran."""
    with claimed.lock:
        if claimed.finished:
            return False
        now_s = rfc3339(now or utcnow())
        fields = claimed.pending_fields or {"status": "queued", "updated_at": now_s, "requeued_at": now_s,
                                            **({"last_error": last_error[:LAST_ERROR_CHARS]} if last_error else {})}
        committed = _write(store, claimed, fields)
        claimed.finished = True
        return committed


def record_error(store: Any, claimed: Claimed, error: str, *, max_attempts: int,
                 now: Optional[datetime.datetime] = None) -> bool:
    """attempts+1 and last_error; requeue for the next drain, or ``failed`` at max_attempts.
    The doc is never deleted either way. Only for a claim whose message never ran."""
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
                fields.update(status="queued", requeued_at=now_s)
        committed = _write(store, claimed, fields)
        claimed.finished = True
        return committed


def fail(store: Any, claimed: Claimed, error: str, *, now: Optional[datetime.datetime] = None) -> bool:
    """Terminal ``failed``: the native admission settled failed, or its door refused it for good.
    Never requeued: the authority already owned the one retry an admitted turn may get."""
    with claimed.lock:
        if claimed.finished:
            return False
        now_s = rfc3339(now or utcnow())
        committed = _write(store, claimed, {"status": "failed", "failed_at": now_s, "updated_at": now_s,
                                            "attempts": int(claimed.fields.get("attempts") or 0) + 1,
                                            "last_error": str(error or "failed")[:LAST_ERROR_CHARS]})
        claimed.finished = True
        return committed


def hold_uncertain(store: Any, claimed: Claimed, error: str, *, now: Optional[datetime.datetime] = None) -> bool:
    """Keep an uncertain execution ``read`` with ``execution_unverified``: never requeued, never run
    again; a later native receipt (or the operator) settles it."""
    with claimed.lock:
        if claimed.finished:
            return False
        committed = _write(store, claimed, {"status": "read", "updated_at": rfc3339(now or utcnow()),
                                            "last_error": f"{EXECUTION_UNVERIFIED}: {error}"[:LAST_ERROR_CHARS]})
        claimed.finished = True
        return committed


def finish(store: Any, claimed: Claimed, outcome: dict, *, max_attempts: int,
           now: Optional[datetime.datetime] = None) -> bool:
    """Terminal receipt for this message: settled -> done; anything else is a handling error
    (requeue or failed). Idempotent: only the first terminal outcome is written."""
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
    """Recover stale claims outside the drain path.

    A stale ``delivered`` claim was never handed over: it is requeued (or failed at the attempt
    limit). A stale ``read`` claim reached the native door and may have run: it is never requeued
    (``held``), only expired with a "may have received it" notice once its 24 hours pass. Even a
    requeued doc that already has an execution record is held by ``mark_read``, never run twice.
    Run this from a no-model maintenance job; it performs two status queries. In dry-run mode,
    transition counts describe what would be written.
    """
    if older_than_seconds < 0 or max_attempts < 1 or limit < 1:
        raise ValueError("older_than_seconds must be nonnegative; max_attempts and limit must be positive")
    now = now or utcnow()
    now_s = rfc3339(now)
    cutoff = now - datetime.timedelta(seconds=older_than_seconds)
    counts = {"scanned": 0, "fresh": 0, "requeued": 0, "failed": 0,
              "expired": 0, "held": 0, "conflicts": 0, "dry_run": dry_run}
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
            if status == "read" and (expiry is None or expiry > now):
                # Handed to the native door: its admission may have started, so a stale claim is
                # never permission to run it again. Its execution record or receipt settles it;
                # at expiry the sender hears it may have been received.
                counts["held"] += 1
                continue
            changes = {"attempts": attempts, "updated_at": now_s,
                       "last_error": (f"reclaimed stale {status} claim after {older_than_seconds}s "
                                      "(claimer died or store failed; a 'read' claim's turn may already have run)")}
            if expiry is not None and expiry <= now:
                outcome = "expired"
                changes.update(status=outcome, expired_at=now_s,
                               sender_notice=_reclaimed_sender_notice(row.doc_id, fields.get("to")),
                               sender_notice_at=now_s)
            elif attempts >= max_attempts:
                outcome = "failed"
                changes.update(status=outcome, failed_at=now_s)
            else:
                outcome = "requeued"
                changes.update(status="queued", requeued_at=now_s)
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
    reclaim = subparsers.add_parser("reclaim-stale", help="requeue stale delivered/read claims")
    reclaim.add_argument("--older-than", type=int, default=1800, metavar="SECONDS")
    reclaim.add_argument("--target", choices=("live", "emulator"))
    reclaim.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
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


# The native door caps an authored DM (gateway/session_bot.py ``_BOT_DM_MAX_CHARS``); the queue
# header is dropped rather than letting it push a full-size DM over that cap.
_DOOR_AUTHORED_MAX_CHARS = 16200


def render_input(claimed: Claimed) -> tuple[str, Optional[dict], dict]:
    """(text, turn_author, notification metadata) for the admitted turn.

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
    framed = f"{header}\n{text}"
    return (framed if len(framed) <= _DOOR_AUTHORED_MAX_CHARS else text), author, {}


if __name__ == "__main__":
    raise SystemExit(main())
