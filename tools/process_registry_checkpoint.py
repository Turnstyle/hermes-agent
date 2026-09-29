"""Running-process checkpoint persistence and PID-safe recovery."""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.redact import redact_sensitive_text

logger = logging.getLogger("tools.process_registry")


class ProcessCheckpointMixin:
    # ----- Checkpoint (crash recovery) -----

    def _write_checkpoint(self, extra_entries: Optional[List[Dict[str, Any]]] = None,
                          retire_ids: frozenset = frozenset()):
        """Write each profile's running entries without erasing another producer's."""
        from tools.process_registry import _checkpoint_path, _CHECKPOINT_FIELDS
        from hermes_constants import get_hermes_home

        try:
            with self._checkpoint_lock:
                current_home = get_hermes_home()
                entries_by_home: Dict[Path, List[Dict[str, Any]]] = {current_home: []}
                known_ids = set(retire_ids)
                with self._lock:
                    for s in (*self._running.values(), *self._finished.values()):
                        known_ids.add(s.id)
                        home = Path(s.output_log_path).parent.parent.parent if s.output_log_path else current_home
                        entries_by_home.setdefault(home, [])
                        if s.exited:
                            continue
                        # Backfill the start time so recovery can detect PID recycling.
                        if s.host_start_time is None and s.pid_scope == "host" and s.pid:
                            s.host_start_time = self._safe_host_start_time(s.pid)
                        entry = {"session_id": s.id, **{f: getattr(s, f) for f in _CHECKPOINT_FIELDS}}
                        # Recovery uses command only for display, never re-runs it.
                        entry["command"] = redact_sensitive_text(s.command, code_file=True)
                        entry["owner_task_id"] = s.owner_task_id or s.task_id
                        entry["_writer_id"] = self._checkpoint_writer_id
                        entries_by_home[home].append(entry)
                    if extra_entries:
                        for item in extra_entries:
                            log_path = item.get("output_log_path")
                            home = Path(log_path).parent.parent.parent if log_path else current_home
                            entries = entries_by_home.setdefault(home, [])
                            if not any(row["session_id"] == item.get("session_id") for row in entries):
                                entries.append(item)
                from utils import atomic_json_write
                for home, entries in entries_by_home.items():
                    path = _checkpoint_path() if home == current_home else home / "processes.json"
                    if os.name != "posix":
                        atomic_json_write(path, entries)
                        continue
                    import fcntl
                    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
                    with os.fdopen(fd, "rb+") as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX)
                        try:
                            previous = json.loads(path.read_text(encoding="utf-8-sig"))
                        except (FileNotFoundError, ValueError, TypeError):
                            previous = []
                        if not isinstance(previous, list):
                            previous = []
                        own_ids = known_ids | {entry.get("session_id") for entry in entries}
                        preserved = [entry for entry in previous if isinstance(entry, dict)
                                     and entry.get("_writer_id") != self._checkpoint_writer_id
                                     and entry.get("session_id") not in own_ids]
                        atomic_json_write(path, preserved + entries)
        except Exception as e:
            logger.warning("Failed to write process checkpoint: %s", e, exc_info=True)

    def recover_from_checkpoint(self) -> int:
        """On gateway startup, probe PIDs from the checkpoint file; returns how many
        were recovered as detached sessions."""
        from tools.process_registry import (
            ProcessSession, _CHECKPOINT_FIELDS, _checkpoint_path,
            _CHECKPOINT_DEFAULTS, _WATCHER_ROUTE_KEYS, _stop_systemd_unit,
        )

        checkpoint_path = _checkpoint_path()
        if not checkpoint_path.exists():
            return 0
        try:
            entries = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except Exception:
            return 0
        recovered = 0
        unresolved_scope_entries: List[Dict[str, Any]] = []
        discard_ids = set()
        for entry in entries:
            pid, pid_scope = entry.get("pid"), entry.get("pid_scope", "host")
            if not pid:
                discard_ids.add(entry.get("session_id"))
                continue
            # The registry is process-global, so every profile's checkpoint carries every live
            # process; a multiplexer recovering several homes must adopt each session once.
            with self._lock:
                already_tracked = entry.get("session_id") in self._running
            if already_tracked:
                continue
            if pid_scope != "host":  # in-sandbox PIDs mean nothing once the env handle is gone
                discard_ids.add(entry.get("session_id"))
                logger.info(
                    "Skipping recovery for non-host process: %s (pid=%s, scope=%s)",
                    entry.get("command", "unknown")[:60], pid, pid_scope)
                continue
            # Alive AND the same process: across a restart the kernel may have
            # recycled the PID onto a stranger, and adopting it would let a later
            # kill tree-kill e.g. a browser.
            has_exit_file = bool(entry.get("exit_file_path") and
                                 Path(entry["exit_file_path"]).is_file())
            if not has_exit_file and not self._host_pid_is_ours(pid, entry.get("host_start_time")):
                if self._is_host_pid_alive(pid):
                    logger.info(
                        "Not recovering session %s: pid %d is alive but its "
                        "start time no longer matches — PID was recycled onto "
                        "an unrelated process; refusing to adopt it.",
                        entry.get("session_id", "?"), pid)
                systemd_unit = entry.get("systemd_unit", "")
                if systemd_unit and not _stop_systemd_unit(systemd_unit):
                    logger.warning(
                        "Could not reap persisted scope %s for dead wrapper pid %s; "
                        "retaining checkpoint entry for the next startup",
                        systemd_unit, pid)
                    unresolved_scope_entries.append(entry)
                else:
                    discard_ids.add(entry.get("session_id"))
                continue
            fields = {f: entry.get(f, _CHECKPOINT_DEFAULTS[f]) for f in _CHECKPOINT_FIELDS}
            fields.update(
                command=entry.get("command", "unknown"),
                owner_task_id=entry.get("owner_task_id", "") or entry.get("task_id", ""),
                started_at=entry.get("started_at", time.time()))
            # File-backed sessions can read missed output and the real exit
            # status even when the wrapper finished while no gateway ran.
            session = ProcessSession(id=entry["session_id"], detached=True, **fields)
            if session.output_log_path and session.log_offset_bytes:
                try:
                    with Path(session.output_log_path).open("rb") as stream:
                        start = max(session.log_offset_bytes - session.max_output_chars * 4, 0)
                        stream.seek(start)
                        session.output_buffer = stream.read(session.log_offset_bytes - start)[-session.max_output_chars:].decode(
                            "utf-8", errors="replace")
                    session.total_output_chars = len(session.output_buffer)
                except OSError:
                    logger.warning("Could not restore output history for %s", session.id)
            with self._lock:
                self._running[session.id] = session
            recovered += 1
            logger.info("Recovered detached process: %s (pid=%d)", session.command[:60], pid)
            if session.output_log_path:
                self._poll_file_session(session)
                if not session.exited:
                    self._track_recovered_file_session(session)
            # Re-enqueue watcher so gateway can resume notifications
            if session.watcher_interval > 0:
                self.pending_watchers.append({
                    "session_id": session.id,
                    "check_interval": session.watcher_interval,
                    "session_key": session.session_key,
                    **{key: getattr(session, f"watcher_{key}") for key in _WATCHER_ROUTE_KEYS},
                    "notify_on_complete": session.notify_on_complete,
                    "parent_session_id": session.parent_session_id,
                })
        self._write_checkpoint(extra_entries=unresolved_scope_entries, retire_ids=frozenset(discard_ids))
        return recovered
