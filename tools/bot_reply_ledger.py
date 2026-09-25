"""Durable ledger of ``message_agent`` reply waiters (``bot_mode_dm.py --wait-reply``).

A reply waiter is tracked only in the spawning process's in-memory registry. When that
process dies (app quit, crash) the waiter keeps running with nobody to wake, and nothing
on disk says which session was waiting on which teammate. This module closes that gap:

* the waiter writes ``<root>/bot_relay/pending/<delivery_id>.json`` at start and removes
  it on every exit path (reply, timeout, error, SIGTERM) — :func:`pending_waiter`;
* gateway / ``hermes serve`` startup runs :func:`sweep_pending_waiters`: adopt live
  orphans into the process registry, kill identity-verified orphans past their budget,
  drop stale entries, and post one plain notice per owning session (no model call);
* after an interrupted ``process wait`` or a compaction, :func:`consume_turn_note`
  gives the next turn one line naming the waiters still pending for its session lineage.

Top-level imports stay stdlib-only: the waiter itself imports this module.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import signal
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

PENDING_DIR = "pending"
UNDELIVERABLE_LOG = "_undeliverable.log"
_SWEEP_LOCK = ".sweep.lock"
WAITER_MARKER = "bot_mode_dm.py --wait-reply"
DEFAULT_ORPHAN_GRACE_SECONDS = 300
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def pending_dir_for_reply(reply_path: "str | os.PathLike") -> Path:
    """``<root>/bot_relay/pending`` — sibling of the ``replies/`` dir the waiter watches."""
    return Path(reply_path).resolve().parent.parent / PENDING_DIR


def pending_dir(root: "str | os.PathLike") -> Path:
    from tools.bot_relay import relay_root

    return relay_root(Path(root)) / PENDING_DIR


def process_start_time(pid: Any) -> Optional[int]:
    """Same fingerprint as ``gateway.status.get_process_start_time`` (kept local: the waiter
    must not import the gateway package)."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    with contextlib.suppress(IndexError, ValueError, OSError):
        return int(Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[21])
    try:
        import psutil  # type: ignore

        return int(round(psutil.Process(pid).create_time() * 100))
    except Exception:
        return None


def _spawner_pid(pid: int) -> Optional[int]:
    """Nearest ancestor OUTSIDE ``pid``'s own process session — the process that launched the
    waiter (the registry spawns with ``start_new_session``, so the login shell wrapper shares
    the waiter's session). 1 when the chain was reparented to init; None when unknown."""
    try:
        import psutil  # type: ignore

        getsid = getattr(os, "getsid", None)
        if getsid is None:
            return None
        sid, proc = getsid(pid), psutil.Process(pid)
        for _ in range(8):
            ppid = proc.ppid()
            if ppid <= 1:
                return 1
            try:
                if getsid(ppid) != sid:
                    return ppid
            except OSError:
                return 1
            proc = psutil.Process(ppid)
    except Exception:
        return None
    return None


def _profile_from_env() -> str:
    explicit = os.environ.get("HERMES_SESSION_PROFILE", "").strip()
    if explicit:
        return explicit
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home.name if home.parent.name == "profiles" else "default"


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def pending_waiter(reply_path: str, label: str, timeout_s: float) -> Iterator[Optional[Path]]:
    """Hold a ledger entry for the life of the ``with`` body. SIGTERM/SIGHUP become
    ``SystemExit`` so the ``finally`` runs; a ledger write failure never blocks the wait."""
    delivery_id = Path(reply_path).stem
    if not _SAFE_ID_RE.match(delivery_id):
        delivery_id = str(abs(hash(reply_path)))
    entry_path: Optional[Path] = None
    try:
        pid = os.getpid()
        owner = _spawner_pid(pid)
        entry_path = pending_dir_for_reply(reply_path) / f"{delivery_id}.json"
        _atomic_write(entry_path, {
            "delivery_id": delivery_id, "reply_file": str(reply_path), "target": label,
            "profile": _profile_from_env(),
            "parent_session_id": os.environ.get("HERMES_SESSION_ID", ""),
            "session_key": os.environ.get("HERMES_SESSION_KEY", ""),
            "pid": pid, "pid_start_time": process_start_time(pid),
            "spawned_at": time.time(), "timeout_s": float(timeout_s),
            # Who launched us: a later sweep treats the waiter as orphaned once this is gone
            # (covers init-reparenting AND subreapers, where PPID is not 1).
            "owner_pid": owner, "owner_start_time": process_start_time(owner) if owner and owner > 1 else None,
        })
    except Exception:
        logger.debug("reply-waiter ledger write failed", exc_info=True)
        entry_path = None
    previous: dict[int, Any] = {}

    def _exit_on_signal(signum, _frame):
        raise SystemExit(128 + signum)

    if threading.current_thread() is threading.main_thread():
        for name in ("SIGTERM", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is not None:
                with contextlib.suppress(ValueError, OSError):
                    previous[signum] = signal.signal(signum, _exit_on_signal)
    try:
        yield entry_path
    finally:
        if entry_path is not None:
            with contextlib.suppress(OSError):
                entry_path.unlink()
        for signum, handler in previous.items():
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(signum, handler)


# ---- identity + orphan checks (gateway side) ----


def _read_cmdline(pid: int) -> str:
    try:
        from gateway.status import _read_process_cmdline

        return _read_process_cmdline(pid) or ""
    except Exception:
        return ""


def _identity_matches(entry: dict) -> bool:
    """Alive AND same start time AND a ``--wait-reply`` command line (PID-reuse guard)."""
    pid, recorded = entry.get("pid"), entry.get("pid_start_time")
    if not isinstance(pid, int) or pid <= 0 or recorded is None:
        return False
    try:
        from gateway.status import _pid_exists, get_process_start_time, start_time_fingerprints_match

        if not _pid_exists(pid):
            return False
        current = get_process_start_time(pid)
        if current is None or not start_time_fingerprints_match(recorded, current):
            return False
    except Exception:
        return False
    return WAITER_MARKER in _read_cmdline(pid)


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        from gateway.status import _pid_exists

        return bool(_pid_exists(pid))
    except Exception:
        return False


def _same_process(pid: Any, start: Any) -> bool:
    if not _pid_alive(pid) or start is None:
        return False
    current = process_start_time(pid)
    try:
        return current is not None and abs(int(current) - int(start)) <= 200
    except (TypeError, ValueError):
        return False


def _is_orphaned(entry: dict) -> bool:
    """True when the waiter lost its launcher: reparented to init (PPID 1) or — under a
    subreaper — no longer a child of the recorded launcher. A live launcher means a running
    owner, so the sweep leaves the waiter alone."""
    pid = entry.get("pid")
    if not isinstance(pid, int):
        return False
    spawner = _spawner_pid(pid)
    if spawner is None:
        return False
    if spawner == 1:
        return True
    owner = entry.get("owner_pid")
    if isinstance(owner, int) and owner > 1:
        return spawner != owner or not _same_process(owner, entry.get("owner_start_time"))
    return False


def _adopter_alive(entry: dict) -> bool:
    adopter = entry.get("adopted_by")
    if not isinstance(adopter, dict):
        return False
    return _same_process(adopter.get("pid"), adopter.get("start_time"))


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _as_seconds(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if value == value and value not in (float("inf"), float("-inf")) and value >= 0 else None


def normalize_entry(raw: dict) -> tuple[dict, list[str]]:
    """Type-checked copy of a ledger row plus the names of fields that were malformed.

    A malformed field is replaced by a value that can only make the sweep MORE careful:
    no pid/start time means "not our waiter" (never signalled), no age means "never killed
    for age", a malformed ``adopted_by`` means "not adopted"."""
    entry = dict(raw)
    bad: list[str] = []
    pid = _as_int(raw.get("pid"))
    if pid is None or pid <= 0:
        bad.append("pid")
        pid = None
    entry["pid"] = pid
    for key in ("pid_start_time", "owner_pid", "owner_start_time"):
        if raw.get(key) is not None and _as_int(raw.get(key)) is None:
            bad.append(key)
        entry[key] = _as_int(raw.get(key))
    for key in ("spawned_at", "timeout_s"):
        entry[key] = _as_seconds(raw.get(key))
        if entry[key] is None:
            bad.append(key)
    adopter = raw.get("adopted_by")
    if adopter is not None and not (isinstance(adopter, dict) and _as_int(adopter.get("pid"))
                                    and _as_int(adopter.get("start_time")) is not None):
        bad.append("adopted_by")
        adopter = None
    entry["adopted_by"] = adopter
    for key, default in (("profile", "default"), ("parent_session_id", ""), ("session_key", ""),
                         ("target", "?"), ("reply_file", "")):
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            bad.append(key)
            value = None
        entry[key] = value or default
    return entry, bad


def _grace_seconds() -> float:
    try:
        from tools.bot_relay import _bot_mode_cfg

        val = _bot_mode_cfg("waiter_orphan_grace_seconds", loader="load_config_readonly")
        return max(float(DEFAULT_ORPHAN_GRACE_SECONDS if val is None else val), 0.0)
    except Exception:
        return float(DEFAULT_ORPHAN_GRACE_SECONDS)


def read_entries(directory: Path) -> list[tuple[Path, Optional[dict]]]:
    """(path, entry) for every ledger file; ``entry`` is None when unreadable."""
    out: list[tuple[Path, Optional[dict]]] = []
    with contextlib.suppress(OSError):
        for path in sorted(directory.glob("*.json")):
            if path.name.startswith("."):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
            out.append((path, data if isinstance(data, dict) else None))
    return out


# ---- startup sweep ----


def _track_in_registry(registry: Any, entry: dict, cmdline: str) -> None:
    from tools.process_registry import ProcessSession

    pid = entry["pid"]
    with registry._lock:
        if any(s.pid == pid and not s.exited for s in registry._running.values()):
            return
    import uuid

    # The ledger's recorded start time is the identity the registry re-validates on every later
    # kill/refresh; a fresh read here could describe a process that reused the PID.
    session = ProcessSession(
        id=f"proc_{uuid.uuid4().hex[:12]}", command=cmdline or f"--wait-reply {entry['reply_file']}",
        pid=pid, pid_scope="host", host_start_time=entry["pid_start_time"], detached=True,
        started_at=entry["spawned_at"] if entry["spawned_at"] is not None else time.time(),
        parent_session_id=entry["parent_session_id"], session_key=entry["session_key"])
    with registry._lock:
        registry._running[session.id] = session
    registry._write_checkpoint()


def sweep_pending_waiters(root: "str | os.PathLike | None" = None, *, registry: Any = None,
                          now: Optional[float] = None, grace: Optional[float] = None) -> dict:
    """Reconcile the ledger after a restart and post the per-session notices. Never raises.
    Returns ``{"adopted", "killed", "removed", "skipped", "notices"}`` counts."""
    summary = {"adopted": 0, "killed": 0, "removed": 0, "skipped": 0, "notices": 0}
    try:
        if root is None:
            from tools.bot_mode_probe import _default_home, _hermes_root

            root = _hermes_root(Path(_default_home()))
        directory = pending_dir(root)
        if not directory.is_dir():
            return summary
        with _sweep_lock(directory) as held:
            if not held:
                logger.info("reply-waiter sweep skipped: another process is sweeping %s", directory)
                return summary
            _sweep_locked(Path(root), directory, registry, summary,
                          time.time() if now is None else now, _grace_seconds() if grace is None else grace)
    except Exception:
        logger.warning("reply-waiter ledger sweep failed", exc_info=True)
    return summary


@contextlib.contextmanager
def _sweep_lock(directory: Path) -> Iterator[bool]:
    try:
        import fcntl
    except ImportError:  # pragma: no cover — Windows
        yield True
        return
    fd = os.open(directory / _SWEEP_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _sweep_locked(root: Path, directory: Path, registry: Any, summary: dict, now: float, grace: float) -> None:
    if registry is None:
        from tools.process_registry import process_registry as registry
    outcomes: list[tuple[dict, str]] = []
    for path, raw in read_entries(directory):
        if raw is None:
            logger.warning("reply-waiter ledger: unreadable entry %s removed", path.name)
            with contextlib.suppress(OSError):
                path.unlink()
            summary["removed"] += 1
            continue
        # One bad row must never abandon the rows after it or the notices still to be posted.
        try:
            outcome = _sweep_entry(path, raw, registry, summary, now, grace)
        except Exception:
            logger.warning("reply-waiter ledger: entry %s skipped after an error", path.name, exc_info=True)
            summary["skipped"] += 1
            continue
        if outcome is not None:
            outcomes.append(outcome)
    for (profile, session_id, tip), group in _group_by_destination(root, outcomes).items():
        total = group["waiting"] + group["stopped"]
        line = (f"{total} replies were pending when the app restarted: {', '.join(group['targets'])}; "
                f"{group['waiting']} still waiting, {group['stopped']} stopped.")
        if tip and _post_notice(root, profile, tip, line):
            summary["notices"] += 1
        else:
            _append_undeliverable(directory, profile, session_id, line)


def _sweep_entry(path: Path, raw: dict, registry: Any, summary: dict, now: float,
                 grace: float) -> Optional[tuple[dict, str]]:
    """Adopt, kill or drop one ledger row. Returns ``(entry, "waiting"|"stopped")`` for the
    restart notice, or None when the row is left alone (spawner or adopter still running)."""
    entry, bad = normalize_entry(raw)
    if bad:
        logger.warning("reply-waiter ledger: entry %s has malformed field(s) %s", path.name, ", ".join(bad))
    pid = entry["pid"]
    # Waiter liveness first: an adopter being alive says nothing about the waiter it adopted.
    if pid is None or not _identity_matches(entry):
        if _pid_alive(pid):
            logger.info("reply-waiter ledger: pid %s no longer a reply waiter (PID reused); not killing", pid)
        with contextlib.suppress(OSError):
            path.unlink()
        summary["removed"] += 1
        return entry, "stopped"
    if _adopter_alive(entry) or not _is_orphaned(entry):
        summary["skipped"] += 1  # a live gateway / serve (maybe this one) or its own spawner owns it
        return None
    age = None if entry["spawned_at"] is None else now - entry["spawned_at"]
    # Within budget: adopt. Past budget but inside the grace window: its own deadline exit is due,
    # so it is adopted too rather than killed mid-write of its last line. An unreadable spawn time
    # cannot prove the budget is spent, so that waiter is adopted, never killed for age.
    if age is None or age < (entry["timeout_s"] or 0.0) + grace:
        _track_in_registry(registry, entry, _read_cmdline(pid))
        stamped = dict(raw, adopted_by={"pid": os.getpid(), "start_time": process_start_time(os.getpid())})
        with contextlib.suppress(OSError):
            _atomic_write(path, stamped)
        summary["adopted"] += 1
        return entry, "waiting"
    # The recorded identity, never a fresh read: the PID may have changed owners since the check.
    registry._terminate_host_pid(pid, entry["pid_start_time"])
    if _identity_matches(entry):
        # The registry refused (its identity check is exact) or the waiter outlived the signal:
        # keep the row so the waiter stays visible and the next sweep tries again.
        logger.warning("reply-waiter ledger: orphaned waiter pid %s survived termination; row kept", pid)
        summary["skipped"] += 1
        return None
    logger.info("reply-waiter ledger: killed orphaned waiter pid %s (age %ds)", pid, int(age))
    summary["killed"] += 1
    with contextlib.suppress(OSError):
        path.unlink()
    summary["removed"] += 1
    return entry, "stopped"


def _group_by_destination(root: Path, outcomes: list[tuple[dict, str]]) -> dict[tuple[str, str, Optional[str]], dict]:
    """Group notice rows by the conversation that will RECEIVE them: waiters recorded under
    successive compaction ids of one conversation share one live tip and so one notice.
    Key: ``(profile, session_id, tip)``; ``tip`` None (unresolvable) keeps the recorded id."""
    tips: dict[tuple[str, str], Optional[str]] = {}
    for entry, _state in outcomes:
        tips.setdefault((entry["profile"], entry["parent_session_id"]), None)
    for profile in {profile for profile, _sid in tips}:
        tips.update(_resolve_tips(root, profile, [sid for p, sid in tips if p == profile]))
    groups: dict[tuple[str, str, Optional[str]], dict] = {}
    for entry, state in outcomes:
        profile, session_id = entry["profile"], entry["parent_session_id"]
        tip = tips.get((profile, session_id))
        key = (profile, tip, tip) if tip else (profile, session_id, None)
        group = groups.setdefault(key, {"targets": [], "waiting": 0, "stopped": 0})
        if entry["target"] not in group["targets"]:
            group["targets"].append(entry["target"])
        group[state] += 1
    return groups


def _profile_db_path(root: Path, profile: str) -> Path:
    home = root if profile in ("", "default") else root / "profiles" / profile
    return home / "state.db"


def _resolve_tips(root: Path, profile: str, session_ids: list[str]) -> dict[tuple[str, str], Optional[str]]:
    """``(profile, session_id) -> live compression tip`` (None when no such session exists)."""
    out: dict[tuple[str, str], Optional[str]] = {(profile, sid): None for sid in session_ids}
    db_path = _profile_db_path(root, profile)
    wanted = [sid for sid in session_ids if sid]
    if not wanted or not db_path.exists():
        return out
    db = None
    try:
        from hermes_state import SessionDB

        db = SessionDB(db_path=db_path)
        for sid in wanted:
            try:
                tip = db.get_compression_tip(sid) or sid
                out[(profile, sid)] = tip if db.get_session(tip) else None
            except Exception:
                logger.info("reply-waiter notice: session %s did not resolve", sid, exc_info=True)
    except Exception:
        logger.info("reply-waiter notice: session store for profile %s unavailable", profile, exc_info=True)
    finally:
        if db is not None:
            with contextlib.suppress(Exception):
                db.close()
    return out


def _post_notice(root: Path, profile: str, tip: str, line: str) -> bool:
    """Append ``line`` to the already-resolved live session ``tip`` as a transcript row. No model call."""
    db = None
    try:
        from hermes_state import SessionDB

        db = SessionDB(db_path=_profile_db_path(root, profile))
        db.append_message(tip, "user", content=f"[System notice] {line}", display_kind="process_complete",
                          display_metadata={"display_text": line})
        return True
    except Exception:
        logger.info("reply-waiter restart notice not delivered to %s", tip, exc_info=True)
        return False
    finally:
        if db is not None:
            with contextlib.suppress(Exception):
                db.close()


def _append_undeliverable(directory: Path, profile: str, session_id: str, line: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with contextlib.suppress(OSError):
        with open(directory / UNDELIVERABLE_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} profile={profile or 'default'} session={session_id or '-'} {line}\n")


# ---- next-turn note ----

_armed_lock = threading.Lock()
_armed_sessions: set[str] = set()


def arm_turn_note(session_id: Optional[str]) -> None:
    """Ask the next turn of ``session_id`` to report its pending reply waiters."""
    if session_id:
        with _armed_lock:
            _armed_sessions.add(str(session_id))


def consume_turn_note(session_id: Optional[str], session_db: Any = None,
                      root: "str | os.PathLike | None" = None) -> str:
    """One line naming this session lineage's live waiters, or "" (also when not armed).
    The arm may have been set on a pre-compaction id, so it is matched across the
    compression lineage. Reads the ledger only; never calls a model."""
    if not session_id:
        return ""
    with _armed_lock:
        if not _armed_sessions:
            return ""
    try:
        lineage = {str(session_id)}
        lineage_fn = getattr(session_db, "get_compression_lineage", None)
        if callable(lineage_fn):
            with contextlib.suppress(Exception):
                lineage.update(str(s) for s in (lineage_fn(session_id) or ()))
        with _armed_lock:
            if not lineage & _armed_sessions:
                return ""
            _armed_sessions.difference_update(lineage)
        if root is None:
            from tools.bot_mode_probe import _default_home, _hermes_root

            root = _hermes_root(Path(_default_home()))
        rows = [normalize_entry(raw)[0] for _path, raw in read_entries(pending_dir(root)) if raw]
        targets = [row["target"] for row in rows
                   if row["parent_session_id"] in lineage and row["pid"] is not None and _identity_matches(row)]
    except Exception:
        logger.debug("pending reply-waiter note failed", exc_info=True)
        return ""
    if not targets:
        return ""
    return f"{len(targets)} background reply waiters still pending: {', '.join(targets)}"
