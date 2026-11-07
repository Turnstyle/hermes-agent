"""Restart-spooled API session chat (``POST /api/sessions/{id}/chat[/stream]`` while draining).

While the gateway drains for a restart that keeps queued input
(``GatewayRunner._queue_during_drain_enabled``: busy_input_mode queue/steer), a text session-chat
turn is written to the routed profile's ``pending_messages/`` spool and answered 202
``hermes.session.chat.queued`` instead of the retryable 503. Payload (unchanged from the fleet
carry): ``{"session_key": "agent:api:<sid>", "reason": "gateway_draining", "message_id", "ts",
"data": {"text", "session_id"}}``.

After the next start, once boot auto-resume turns and native session recovery have run, each spool
is admitted ONCE through the door the live route uses: the canonical Bot Chat's own FIFO
(``api_server_bot_chat.admit_peer_turn``), else the session authority's API admission
(``session_api_turn.admit_api_turn_async``), under an id derived from its message id. A crash
between admission and unlink therefore replays into the same admission, never a second turn. The
spool is deleted only after the admission committed and is never appended as a transcript row.
Invalid or unreadable payloads are retained for manual recovery and never scheduled; a refused
admission keeps its spool (and holds back that session's later spools) for bounded delayed passes.
"""

import asyncio
import hashlib
import json
import logging
import time
import uuid

try:
    from aiohttp import web
except ImportError:  # the adapter reports aiohttp missing; this module must still import
    web = None  # type: ignore[assignment]

from gateway.platforms.api_server_caller_keys import bind_verified_message

logger = logging.getLogger(__name__)

# Delays (seconds) between replay passes for spools whose admission was refused (a profile still
# parked, its authority not open yet); TB's served-profile retry ladder, applied to every home.
REPLAY_DELAYS = (60, 300, 900, 3600)
_SESSION_CHAT_SUFFIXES = ("/chat", "/chat/stream")


def _is_session_chat(request) -> bool:
    return bool(request.match_info.get("session_id")) and request.path.rstrip("/").endswith(_SESSION_CHAT_SUFFIXES)


async def spool_or_refuse(adapter, request, draining):
    """*draining* (the native 503) unless *request* is a text session chat this restart keeps: then
    spool it and answer 202. A spooled message reserves no turn slot, so drain never waits on it."""
    if not _is_session_chat(request):
        return draining
    from gateway.run import _gateway_runner_ref
    runner = _gateway_runner_ref()
    enabled = getattr(runner, "_queue_during_drain_enabled", None)
    if not callable(enabled) or not enabled():
        return draining
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return draining
    message = body.get("message") if isinstance(body, dict) else None
    session_id = str(request.match_info.get("session_id") or "")
    if not isinstance(message, str) or not message.strip():
        return draining
    message, _ = bind_verified_message(message)
    from gateway.shutdown_flush import GATEWAY_DRAINING_REASON, _flush_value, _get_flush_dir
    message_id = uuid.uuid4().hex
    try:
        flush_dir = _get_flush_dir()
    except (OSError, RuntimeError):  # no writable spool for this profile: the native 503 answers
        logger.warning("Gateway-draining session chat for %s not spooled", session_id, exc_info=True)
        return draining
    if not _flush_value(flush_dir, "drain", f"agent:api:{session_id}",
                        {"text": message, "session_id": session_id}, reason=GATEWAY_DRAINING_REASON,
                        message_id=message_id, ts=int(time.time())):
        return draining
    return web.json_response(
        {"object": "hermes.session.chat.queued", "session_id": session_id, "status": "queued",
         "message_id": message_id}, status=202, headers=adapter._session_headers(session_id, None))


def _spool_entry(path):
    """``(payload, problem)``: a replayable gateway-draining payload, or None with why it is kept
    (``""`` when another recovery path owns the file or it cannot be read at all)."""
    from gateway.shutdown_flush import GATEWAY_DRAINING_REASON
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, RecursionError):
        return None, ""  # recover_pending_spool reports unreadable files; never scheduled here
    if not isinstance(payload, dict) or payload.get("reason") != GATEWAY_DRAINING_REASON:
        return None, ""
    data = payload.get("data")
    if not isinstance(data, dict):
        return None, "structurally invalid"
    session_id, text, message_id = data.get("session_id"), data.get("text"), payload.get("message_id")
    if (not isinstance(session_id, str) or not session_id.strip() or not isinstance(text, str)
            or not text.strip() or (message_id is not None and not isinstance(message_id, str))):
        return None, "structurally invalid"
    return payload, None


def _replay_identity(payload, path) -> str:
    """64-hex id naming this spool's one admission (a Bot Chat delivery id and an API request id)."""
    seed = payload.get("message_id") or path.stem
    return hashlib.sha256(f"gateway-draining:{seed}".encode()).hexdigest()


def _replay_text(payload) -> str:
    """The admitted text: deterministic per payload, so a repeated replay matches its admission."""
    text = payload["data"]["text"]
    ts = payload.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts <= 0:
        return text
    sent_at = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(ts))
    return (f"Late delivery: this message was sent at {sent_at}. "
            "The gateway was restarting, so it can only be delivered now.\n\n" + text)


async def _admit_spooled(adapter, payload, path) -> bool:
    """True once the canonical door committed this spool's admission; False keeps the spool."""
    runner = adapter.gateway_runner
    if getattr(runner, "session_authority", None) is None:
        logger.warning("Gateway-draining spool %s kept: no session authority to admit it", path)
        return False
    session_id = payload["data"]["session_id"]
    session, err = await adapter._get_existing_session_or_404(session_id)
    if err is None:
        ctx, err = await adapter._build_session_chat_ctx(session_id, session, {"message": _replay_text(payload)}, None)
    if err is not None:
        logger.warning("Gateway-draining spool %s refused for session %s (HTTP %s); preserved",
                       path, session_id, getattr(err, "status", "?"))
        return False
    key = _replay_identity(payload, path)
    from gateway.platforms.api_server_bot_chat import admit_peer_turn
    delivered = await admit_peer_turn(runner, session_id, ctx["user_message"], None, delivery_id=key)
    if delivered is not None:
        record = delivered[1]
        if record.get("status") == "failed":
            logger.warning("Gateway-draining spool %s refused by the Bot Chat (%s); preserved",
                           path, record.get("reason"))
            return False
        return True
    from gateway.session_api_turn import admit_api_turn_async, schedule_orphan
    admitted = await admit_api_turn_async(
        adapter, **{**ctx["run_kwargs"], "conversation_history": [], "history_from_session": True,
                    "request_id": key})
    schedule_orphan(admitted)  # nobody observes a replayed turn: its FIFO runs it anyway
    return True


async def _replay_home(adapter, name, flush_dir) -> tuple[int, bool]:
    """Replay one profile home's spools in drop order -> ``(admitted, refused_left)``."""
    if not flush_dir.is_dir():
        return 0, False
    from gateway.platforms.api_server import _api_request_profile
    from gateway.shutdown_flush import _order_flush_files
    replayed, blocked = 0, set()
    token = _api_request_profile.set(name)
    try:
        with adapter._profile_scope(name):
            for path in _order_flush_files(flush_dir.glob("*.json")):
                payload, problem = _spool_entry(path)
                if payload is None:
                    if problem:
                        logger.warning("Gateway-draining spool %s is %s; preserved, not replayed", path, problem)
                    continue
                session_id = payload["data"]["session_id"]
                if session_id in blocked:
                    continue  # an older spool for this session is still refused: keep FIFO order
                try:
                    admitted = await _admit_spooled(adapter, payload, path)
                except Exception:  # health: allow BLE001 -- a refused admission keeps its spool for the next pass
                    logger.warning("Gateway-draining spool %s admission failed; preserved", path, exc_info=True)
                    admitted = False
                if not admitted:
                    blocked.add(session_id)
                    continue
                path.unlink(missing_ok=True)
                replayed += 1
    finally:
        _api_request_profile.reset(token)
    return replayed, bool(blocked)


def _spool_homes(runner) -> list:
    """``(profile name or None, pending_messages dir)``: the launch home, then served profiles."""
    from pathlib import Path
    from hermes_constants import get_hermes_home
    homes = [(None, Path(get_hermes_home()) / "pending_messages")]
    if getattr(runner.config, "multiplex_profiles", False):
        from gateway.run import _multiplex_profile_homes
        launch = Path(get_hermes_home()).resolve()
        homes.extend((name, Path(home) / "pending_messages")
                     for name, home in _multiplex_profile_homes(runner.config) if Path(home).resolve() != launch)
    return homes


async def replay_draining_session_chats(runner) -> int:
    """Admit every served home's restart-spooled session chats; refused ones retry on REPLAY_DELAYS."""
    from gateway.config import Platform
    adapter = (getattr(runner, "adapters", None) or {}).get(Platform.API_SERVER)
    if adapter is None:
        return 0  # spools stay for a start that serves the API
    homes, replayed = _spool_homes(runner), 0
    for delay in (0, *REPLAY_DELAYS):
        if delay:
            await asyncio.sleep(delay)
        if runner._startup_should_abort():
            break  # draining again: the spools stay for the next start
        pending = []
        for name, flush_dir in homes:
            count, left = await _replay_home(adapter, name, flush_dir)
            replayed += count
            if left:
                pending.append((name, flush_dir))
        homes = pending
        if not homes:
            break
    if replayed:
        logger.info("Admitted %d gateway-draining session chat turn(s) after restart", replayed)
    if homes:
        logger.warning("Gateway-draining session chats remain in %s; the text is still in those files "
                       "and replays on a later start", ", ".join(str(d) for _n, d in homes))
    return replayed


def start_draining_session_chat_replay(runner, resume_tasks=()) -> None:
    """Replay in one retained background task; startup never waits on it. Boot auto-resume turns
    (*resume_tasks*) go first: a bounded wait (``_startup_restore_drain_timeout_secs``), then any
    still running defer the replay to their completion instead of being overtaken or cancelled."""
    waits = [t for t in resume_tasks if isinstance(t, asyncio.Future) and not t.done()]

    async def _after_resumes() -> None:
        if waits:
            from gateway.run import _startup_restore_drain_timeout_secs
            _done, pending = await asyncio.wait(waits, timeout=max(0.0, _startup_restore_drain_timeout_secs()))
            if pending:
                logger.warning("Drain-spool replay deferred behind %d boot resume turn(s); spools preserved",
                               len(pending))

                def _resume_finished(task) -> None:
                    pending.discard(task)
                    if not pending and not runner._startup_should_abort():
                        runner._retain_background_task(asyncio.create_task(replay_draining_session_chats(runner)))

                for task in list(pending):
                    task.add_done_callback(_resume_finished)
                return
        await replay_draining_session_chats(runner)

    runner._retain_background_task(asyncio.create_task(_after_resumes()))
