"""Authenticated, no-agent peer ingress receipts.

Receipts live in the URL-selected profile's Hermes home. They prove gateway
acceptance only; no session, queue, or agent is involved. Records expire after
seven days. A full store fails closed instead of evicting a live receipt.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import sqlite3
import time
from pathlib import Path

from aiohttp import web

MAX_BODY_BYTES = 2048
RETENTION_SECONDS = 7 * 86400
MAX_RECORDS = 100_000
PER_SENDER_PER_MINUTE = 60
GLOBAL_PER_MINUTE = 600
_KEY_INVALID = re.compile(r"[\r\n\x00]")
_NODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _valid_key(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 255 and not _KEY_INVALID.search(value)


def _error(message: str, status: int) -> web.Response:
    return web.json_response({"error": {"message": message}}, status=status)


def _db_path() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "peer_ping_receipts.db"


def _connect(path: Path) -> sqlite3.Connection:
    # The profile home already exists in normal operation. Fail closed if its
    # durable state directory is unavailable; never fall back to memory.
    conn = sqlite3.connect(str(path), timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS peer_ping (
        idempotency_key TEXT PRIMARY KEY, payload_sha256 TEXT NOT NULL,
        sender_node TEXT NOT NULL, nonce TEXT NOT NULL, sent_at TEXT NOT NULL,
        received_at TEXT NOT NULL, received_epoch REAL NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS peer_ping_rate (
        peer TEXT NOT NULL, minute INTEGER NOT NULL, count INTEGER NOT NULL,
        PRIMARY KEY (peer, minute))""")
    return conn


def _record(row: tuple) -> dict:
    return dict(zip(("idempotency_key", "payload_sha256", "sender_node",
                     "nonce", "sent_at", "received_at"), row))


def _post(path: Path, body: dict, peer_identity: str) -> tuple[int, dict | None]:
    now = time.time()
    minute = int(now // 60)
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM peer_ping WHERE received_epoch < ?", (now - RETENTION_SECONDS,))
        conn.execute("DELETE FROM peer_ping_rate WHERE minute < ?", (minute,))
        sender = body["sender_node"]
        row = conn.execute("""SELECT idempotency_key, payload_sha256, sender_node, nonce,
            sent_at, received_at FROM peer_ping WHERE idempotency_key=?""",
            (body["idempotency_key"],)).fetchone()
        if row:
            conn.commit()
            return (200, _record(row)) if row[1] == body["payload_sha256"] else (409, None)
        # Limit new records per network peer and globally. Replays preserve the
        # original receipt even after the creation budget has been used.
        for peer, limit in (("*", GLOBAL_PER_MINUTE), (peer_identity, PER_SENDER_PER_MINUTE)):
            count = conn.execute(
                "SELECT count FROM peer_ping_rate WHERE peer=? AND minute=?", (peer, minute)
            ).fetchone()
            if count and count[0] >= limit:
                conn.commit()
                return 429, None
        for peer in ("*", peer_identity):
            conn.execute("""INSERT INTO peer_ping_rate(peer, minute, count) VALUES (?, ?, 1)
                ON CONFLICT(peer, minute) DO UPDATE SET count=count+1""", (peer, minute))
        count = conn.execute("SELECT COUNT(*) FROM peer_ping").fetchone()[0]
        if count >= MAX_RECORDS:
            conn.commit()
            return 503, None
        received_at = dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat()
        conn.execute("""INSERT INTO peer_ping VALUES (?, ?, ?, ?, ?, ?, ?)""",
                     (body["idempotency_key"], body["payload_sha256"], sender,
                      body["nonce"], body["sent_at"], received_at, now))
        conn.commit()
        return 201, {**body, "received_at": received_at}
    finally:
        conn.close()


def _get(path: Path, key: str) -> dict | None:
    conn = _connect(path)
    try:
        row = conn.execute("""SELECT idempotency_key, payload_sha256, sender_node, nonce,
            sent_at, received_at FROM peer_ping
            WHERE idempotency_key=? AND received_epoch>=?""",
            (key, time.time() - RETENTION_SECONDS)).fetchone()
        return _record(row) if row else None
    finally:
        conn.close()


async def handle_post(adapter, request: web.Request) -> web.Response:
    # _check_auth has a legacy no-key test bypass. This new peer surface never does.
    if not adapter._expected_api_key():
        return adapter._auth_failed_response()
    auth_error = adapter._check_auth(request)
    if auth_error is not None:
        return auth_error
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        return _error("Ping body too large", 413)
    raw = await request.content.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        return _error("Ping body too large", 413)
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return _error("Invalid JSON", 400)
    if not isinstance(body, dict) or set(body) != {
            "idempotency_key", "nonce", "payload_sha256", "sender_node", "sent_at"}:
        return _error("Invalid ping fields", 400)
    if isinstance(body["idempotency_key"], str):
        body["idempotency_key"] = body["idempotency_key"].strip()
    if not _valid_key(body["idempotency_key"]):
        return _error("Invalid idempotency key", 400)
    if (not isinstance(body["nonce"], str) or not 1 <= len(body["nonce"]) <= 256
            or not isinstance(body["payload_sha256"], str)
            or not _HASH.fullmatch(body["payload_sha256"])
            or not isinstance(body["sender_node"], str)
            or not _NODE.fullmatch(body["sender_node"])
            or not isinstance(body["sent_at"], str)
            or not 1 <= len(body["sent_at"]) <= 64):
        return _error("Invalid ping value", 400)
    try:
        # API_SERVER_KEY is shared across registered peers. The transport address
        # is the available per-peer limiter identity; sender_node is only a label.
        peer_identity = f"remote:{request.remote or 'unknown'}"
        status, record = await asyncio.to_thread(_post, _db_path(), body, peer_identity)
    except (OSError, sqlite3.Error):
        return _error("Ping receipt storage unavailable", 503)
    if record is None:
        return _error({409: "Idempotency key has a different hash",
                       429: "Ping rate limit exceeded", 503: "Ping receipt store full"}[status], status)
    return web.json_response({"object": "hermes.peer.ping", **record}, status=status)


async def handle_get(adapter, request: web.Request) -> web.Response:
    if not adapter._expected_api_key():
        return adapter._auth_failed_response()
    auth_error = adapter._check_auth(request)
    if auth_error is not None:
        return auth_error
    key = request.match_info["key"].strip()
    if not _valid_key(key):
        return _error("Invalid idempotency key", 400)
    try:
        record = await asyncio.to_thread(_get, _db_path(), key)
    except (OSError, sqlite3.Error):
        return _error("Ping receipt storage unavailable", 503)
    if record is None:
        return _error("Ping receipt not found", 404)
    return web.json_response({"object": "hermes.peer.ping", **record})
