"""Governed, compare-and-swap recovery of a running task with no live claimant.

Operator path (``hermes kanban recover-ghost``): a read-only fingerprinted dry run, then an
apply that re-checks everything under the board write lock and writes only if the row still
matches. On an owner-managed board the dispatcher's worker pid is the owner-submit WAITER, not
the agent interpreter, so a dead pid and an absent claim prove nothing on their own: recovery
also needs the owner's own evidence that the attempt is over — no unresolved owner admission
receipt (``kanban_owner_recovery.owner_reclaim_blocked``, which already accepts
``owner_attempt_provably_over``) and no live interpreter bound to the run (``worker_bound``).
"""

from __future__ import annotations

import hashlib
from contextlib import closing
import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_owner_recovery as owner_recovery


class RecoveryRefused(RuntimeError):
    """A recovery precondition changed or cannot be verified."""


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _read_only(path: Path) -> sqlite3.Connection:
    conn = kbc.open_readonly_with_retry(path, connect_timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def _board_checks(conn: sqlite3.Connection, slug: str, path: Path) -> list[dict]:
    expected = kb.kanban_home() / "kanban.db" if slug == kb.DEFAULT_BOARD else kb.board_dir(slug) / "kanban.db"
    checks = [_check("board path", path.resolve() == expected.resolve(),
                     f"resolved {path}; expected {expected}")]
    metadata = kb.read_board_metadata(slug)
    meta_path = kb.board_metadata_path(slug)
    # read_board_metadata deliberately synthesizes a slug and masks a false raw
    # slug. The raw field must agree too; only the legacy default may lack a file.
    raw_slug = None
    if meta_path.is_file():
        try:
            raw_slug = json.loads(meta_path.read_text(encoding="utf-8-sig")).get("slug")
        except (OSError, ValueError, AttributeError):
            pass
    valid = metadata.get("slug") == slug and (
        raw_slug == slug or (slug == kb.DEFAULT_BOARD and not meta_path.exists())
    )
    checks.append(_check("board metadata", valid, f"{meta_path}: slug={raw_slug!r}"))
    if _table_exists(conn, "kanban_board_identity"):
        identity = conn.execute("SELECT board_slug FROM kanban_board_identity WHERE singleton=1").fetchone()
        recorded = identity[0] if identity else None
        checks.append(_check("board identity", recorded is None or recorded == slug,
                             f"recorded={recorded!r}; absent is allowed until first apply"))
    else:
        checks.append(_check("board identity", True, "absent; first apply will create it"))
    return checks


def _check(name: str, passed: bool, reason: str) -> dict:
    return {"check": name, "result": "PASS" if passed else "FAIL", "reason": reason}


def _process_marker_absent(task_id: str) -> tuple[bool, str]:
    if os.name != "posix":
        return False, "process environment probe unavailable on this host"
    try:
        probe = subprocess.run(
            ["ps", "eww", "-A", "-o", "pid=,command="],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"process environment probe failed: {exc}"
    if probe.returncode != 0:
        return False, f"process environment probe exited {probe.returncode}: {probe.stderr.strip()}"
    marker = re.compile(r"(?:^|\s)HERMES_KANBAN_TASK=" + re.escape(task_id) + r"(?:\s|$)")
    for line in probe.stdout.splitlines():
        match = re.match(r"\s*(\d+)\s+(.*)", line)
        if match and int(match.group(1)) != os.getpid() and marker.search(match.group(2)):
            return False, f"pid {match.group(1)} carries HERMES_KANBAN_TASK={task_id}"
    return True, "no other local process carries the task marker"


def _snapshot(conn: sqlite3.Connection, task_id: str, run_id: int) -> dict:
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    run = conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
    mapping = None
    lease = None
    if _table_exists(conn, "fleet_kanban_issue_map"):
        mapping = conn.execute(
            "SELECT * FROM fleet_kanban_issue_map WHERE local_task_id=?", (task_id,)
        ).fetchone()
        if mapping is not None and ("issue_id" not in mapping.keys() or not mapping["issue_id"]):
            raise RecoveryRefused("Fleet issue mapping has no issue_id")
    if mapping is not None and _table_exists(conn, "fleet_kanban_verified_leases"):
        cols = _columns(conn, "fleet_kanban_verified_leases")
        if not {"issue_id", "expires_at"} <= cols:
            raise RecoveryRefused("Fleet verified-leases schema lacks issue_id or expires_at")
        lease = conn.execute(
            "SELECT * FROM fleet_kanban_verified_leases WHERE issue_id=?",
            (mapping["issue_id"],),
        ).fetchone()
    event_id = conn.execute(
        "SELECT MAX(id) FROM task_events WHERE task_id=?", (task_id,)
    ).fetchone()[0]
    identity = None
    if _table_exists(conn, "kanban_board_identity"):
        row = conn.execute("SELECT * FROM kanban_board_identity WHERE singleton=1").fetchone()
        identity = dict(row) if row else None
    data = {
        "task": dict(task) if task else None,
        "run": dict(run) if run else None,
        "mapping": dict(mapping) if mapping else None,
        "lease": dict(lease) if lease else None,
        "last_event_id": event_id,
        "identity": identity,
    }
    # A full row is stricter than the required claim/status/run projection: it
    # also detects concurrent metadata edits (and any new owner event, via
    # last_event_id) that might affect operator intent.
    data["fingerprint"] = hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    return data


def _already_recovered(conn: sqlite3.Connection, snapshot: dict, task_id: str, run_id: int) -> bool:
    run = snapshot["run"]
    if snapshot["task"] is None or run is None or run["task_id"] != task_id:
        return False
    if run["outcome"] == "reclaimed_ghost":
        return True
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND run_id=? AND kind='ghost_recovered' LIMIT 1",
        (task_id, run_id),
    ).fetchone() is not None


def _readback(snapshot: dict) -> dict:
    task, mapping = snapshot["task"], snapshot["mapping"]
    return {
        "task_status": task["status"] if task else None,
        "run_status": snapshot["run"]["status"] if snapshot["run"] else None,
        "canonical_status": mapping.get("canonical_status") if mapping else None,
        "canonical_revision": mapping.get("canonical_revision") if mapping else None,
        "sync_state": mapping.get("sync_state") if mapping else None,
        "last_error": mapping.get("last_error") if mapping else None,
    }


def _claim_checks(task: dict, run: dict, now: int) -> list[dict]:
    """Waiter/claim evidence on the task and run rows: necessary, never sufficient."""
    checks = []
    prefix = kb._host_prefix()
    for label, row in (("task", task), ("run", run)):
        lock, expiry, pid = row["claim_lock"], row["claim_expires"], row["worker_pid"]
        local = lock is None or str(lock).startswith(prefix)
        # Strict on purpose: any live process holding the number (even a recycled one) refuses.
        dead = pid is None or not kbd._pid_alive(pid)
        expired = isinstance(expiry, (int, float)) and expiry < now
        claim_ok = local and (lock is None or (expired and dead))
        checks.append(_check(f"{label} claim", claim_ok,
                             f"lock={lock!r}, expiry={expiry!r}, pid={pid!r}; foreign/live claims refuse"))
        checks.append(_check(f"{label} pid", local and dead,
                             f"pid={pid!r}; must be verifiably dead on this host"))
        checks.append(_check(f"{label} expiry", expiry is None or expired,
                             f"expires={expiry!r}, now={now}"))
    return checks


def _bound_interpreter_check(conn: sqlite3.Connection, task_id: str) -> dict:
    """The interpreter the owner bound to the current run must be verifiably gone on this host."""
    row = conn.execute(
        "SELECT e.payload FROM task_events e JOIN tasks t ON t.current_run_id=e.run_id "
        "WHERE t.id=? AND e.task_id=t.id AND e.kind='worker_bound' ORDER BY e.id DESC LIMIT 1",
        (task_id,)).fetchone()
    if row is None:
        return _check("bound interpreter", True, "no interpreter was bound to this run")
    try:
        bound = json.loads(row["payload"])
        pid, started_at, lock = int(bound["pid"]), bound.get("started_at"), bound.get("claim_lock")
    except (TypeError, ValueError, KeyError):
        return _check("bound interpreter", False, "worker_bound payload is unreadable; liveness unprovable")
    if lock and not str(lock).startswith(kb._host_prefix()):
        return _check("bound interpreter", False, f"pid {pid} was bound under foreign claim {lock!r}")
    if kbd._worker_alive(pid, started_at):
        return _check("bound interpreter", False, f"bound interpreter pid {pid} is alive")
    if kbd._pid_alive(pid) and kbd._process_fingerprint(pid) is None:
        return _check("bound interpreter", False, f"pid {pid} is live and its identity is unreadable")
    return _check("bound interpreter", True, f"bound interpreter pid {pid} is gone")


def _owner_checks(conn: sqlite3.Connection, task: dict, task_id: str) -> list[dict]:
    """Owner-runtime proof that nothing can still execute the attempt."""
    paused = owner_recovery.owner_reclaim_paused(conn, task_id)
    blocked = owner_recovery.owner_reclaim_blocked(conn, task_id)
    if not paused:
        receipt = "no unresolved owner admission receipt for this run"
    elif blocked:
        receipt = ("owner admission receipt is unresolved and the attempt is not provably over; "
                   "a dead waiter or missing claim is not proof — resolve it at the owner first")
    else:
        receipt = "owner receipt unresolved but owner_attempt_provably_over holds"
    live = [pid for pid, fingerprint in owner_recovery._run_worker_pids(conn, task, task_id)
            if kbd._worker_alive(pid, fingerprint)
            or (kbd._pid_alive(pid) and kbd._process_fingerprint(pid) is None)]
    return [
        _check("owner receipt", not blocked, receipt),
        _bound_interpreter_check(conn, task_id),
        _check("recorded worker pids", not live,
               f"live recorded worker pid(s) {live}" if live else
               "every pid recorded for the run (worker, spawned, worker_bound) is gone"),
    ]


def _fleet_checks(conn: sqlite3.Connection, task: dict, task_id: str, mapping: dict,
                  lease, now: int) -> list[dict]:
    checks = [_check("fleet handoff shape", task["claim_lock"] is None,
                     "adapter handoff requires a claimless task row")]
    installed = kb._fleet_adapter_installed_node_id(conn)
    owner, detail = kb._fleet_row_ownership(conn, task_id, installed)
    # A mapped card on a board without the adapter trigger cannot be attributed to this node.
    checks.append(_check("fleet owner", installed is not None and owner == kb.FLEET_OWNER_LOCAL,
                         f"canonical ownership={owner!r} {detail}".strip() if installed is not None
                         else "issue is mapped but no installed fleet adapter node id"))
    if _table_exists(conn, "fleet_kanban_verified_leases"):
        lease_expiry = lease["expires_at"] if lease else None
        checks.append(_check("fleet verified lease", lease is None or (
            isinstance(lease_expiry, (int, float)) and lease_expiry < now),
                             f"expires={lease_expiry!r}, now={now}"))
    else:
        checks.append(_check("fleet verified lease", True, "no verified-leases table or row"))
    return checks


def _checks(conn: sqlite3.Connection, snapshot: dict, task_id: str, run_id: int,
            slug: str, path: Path, now: int) -> list[dict]:
    checks = _board_checks(conn, slug, path)
    task, run, mapping, lease = (snapshot[key] for key in ("task", "run", "mapping", "lease"))
    identity_ok = bool(task and run and run["task_id"] == task_id
                       and task["current_run_id"] == run_id
                       and task["status"] == run["status"] == "running")
    checks.append(_check("task/run identity", identity_ok,
                         "task and requested run must be the same active running attempt"))
    if task is None or run is None:
        return checks
    checks.extend(_claim_checks(task, run, now))
    if identity_ok:
        checks.extend(_owner_checks(conn, task, task_id))
    marker_ok, marker_reason = _process_marker_absent(task_id)
    checks.append(_check("task process marker", marker_ok, marker_reason))
    heartbeats = [row["last_heartbeat_at"] for row in (task, run) if row["last_heartbeat_at"] is not None]
    if any(not isinstance(heartbeat, (int, float)) for heartbeat in heartbeats):
        checks.append(_check("heartbeat", False, "heartbeat timestamp is not numeric"))
        return checks
    latest = max(heartbeats) if heartbeats else None
    checks.append(_check("heartbeat", latest is None or latest < now - kb.DEFAULT_CLAIM_TTL_SECONDS,
                         f"latest={latest!r}; must be older than {kb.DEFAULT_CLAIM_TTL_SECONDS}s"))
    if mapping is not None:
        checks.extend(_fleet_checks(conn, task, task_id, mapping, lease, now))
    return checks


def _emit(args, result: dict) -> None:
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return
    print(f"board={result['board']} db={result['db_path']}")
    if result.get("already_recovered"):
        print(f"already recovered: {result['readback']}")
        return
    for check in result.get("checks", []):
        print(f"{check['result']}: {check['check']}: {check['reason']}")
    if result.get("fingerprint"):
        print(f"fingerprint: {result['fingerprint']}")
    if result.get("planned_writes"):
        print("planned writes: " + "; ".join(result["planned_writes"]))
    if result.get("message"):
        print(result["message"])
    if result.get("readback"):
        print(f"readback: {result['readback']}")
    if result.get("follow_up"):
        print("governed follow-up: " + result["follow_up"])


def _apply(conn: sqlite3.Connection, task_id: str, run_id: int, snapshot: dict,
           actor: str, evidence: str | None, now: int) -> tuple[str, str | None]:
    mapping = snapshot["mapping"]
    fleet = mapping is not None
    mode = "fleet_handoff" if fleet else ("done" if evidence is not None else "retry")
    target = "done" if evidence is not None else kb._retry_status_for_run(conn, task_id, run_id)
    status = "done" if evidence is not None and not fleet else "reclaimed"
    # Run-id compare-and-swap: only the exact running attempt the checks judged is closed.
    updated = conn.execute(
        "UPDATE task_runs SET status=?, outcome='reclaimed_ghost', ended_at=?, "
        "summary=COALESCE(summary, ?) WHERE id=? AND task_id=? AND status='running'",
        (status, now, f"claimless ghost recovered by {actor}", run_id, task_id),
    )
    if updated.rowcount != 1:
        raise RecoveryRefused("run changed before recovery write")
    if fleet:
        updated = conn.execute(
            "UPDATE tasks SET claim_lock=?, claim_expires=1, worker_pid=NULL, worker_started_at=NULL "
            "WHERE id=? AND status='running' AND claim_lock IS NULL AND current_run_id=?",
            (f"{kb._host_prefix()}dead-worker-pending", task_id, run_id),
        )
    else:
        updated = conn.execute(
            "UPDATE tasks SET status=?, completed_at=CASE WHEN ?='done' THEN ? ELSE completed_at END, "
            "result=CASE WHEN ?='done' THEN ? ELSE result END, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, worker_started_at=NULL, current_run_id=NULL "
            "WHERE id=? AND status='running' AND current_run_id=?",
            (target, target, now, target, evidence, task_id, run_id),
        )
    if updated.rowcount != 1:
        raise RecoveryRefused("task changed before recovery write")
    payload = {"actor": actor, "mode": mode, "fingerprint": snapshot["fingerprint"],
               "target_status": target}
    if evidence is not None:
        payload["evidence"] = evidence
    conn.execute(
        "INSERT INTO task_events(task_id, run_id, kind, payload, created_at) VALUES (?,?,'ghost_recovered',?,?)",
        (task_id, run_id, json.dumps(payload, sort_keys=True), now),
    )
    follow_up = None
    if fleet and evidence is not None:
        import shlex
        follow_up = ("fleet_kanban_sync.py --complete " + shlex.quote(str(mapping["issue_id"]))
                     + " --dead-worker --evidence " + shlex.quote(evidence))
    return mode, follow_up


def _evidence_check(args) -> tuple[str | None, dict]:
    evidence = args.evidence.strip() if args.evidence else None
    if args.done and not evidence:
        return evidence, _check("completion evidence", False, "--done requires non-empty --evidence")
    if evidence and not args.done:
        return evidence, _check("completion evidence", False, "--evidence requires --done")
    return evidence, _check("completion evidence", True, "valid")


def _ensure_board_identity(conn: sqlite3.Connection, slug: str) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS kanban_board_identity ("
        "singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
        "board_slug TEXT NOT NULL, created_at INTEGER NOT NULL)"
    )
    identity = conn.execute("SELECT board_slug FROM kanban_board_identity WHERE singleton=1").fetchone()
    if identity and identity[0] != slug:
        raise RecoveryRefused(f"board identity mismatch: {identity[0]!r} != {slug!r}")
    if identity is None:
        conn.execute(
            "INSERT INTO kanban_board_identity(singleton, board_slug, created_at) VALUES(1,?,?)",
            (slug, int(time.time())),
        )


def _apply_locked(args, path: Path, slug: str, initial: dict, evidence: str | None, result: dict):
    """Re-check under the board write lock, then write; ``None`` when already recovered."""
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True,
                                 timeout=10, isolation_level=None)) as conn:
        conn.row_factory = sqlite3.Row
        with kbc.write_txn(conn):
            current = _snapshot(conn, args.task_id, args.run_id)
            if _already_recovered(conn, current, args.task_id, args.run_id):
                result.update(already_recovered=True, readback=_readback(current))
                return None
            current_checks = _checks(conn, current, args.task_id, args.run_id, slug, path, int(time.time()))
            if any(c["result"] == "FAIL" for c in current_checks):
                raise RecoveryRefused("recovery checks changed under write lock")
            if current["fingerprint"] != initial["fingerprint"]:
                raise RecoveryRefused("fingerprint changed since initial read")
            if args.expect and current["fingerprint"] != args.expect:
                raise RecoveryRefused("fingerprint differs from --expect")
            _ensure_board_identity(conn, slug)
            actor = os.environ.get("HERMES_PROFILE") or os.environ.get("USER") or "operator"
            return _apply(conn, args.task_id, args.run_id, current, actor, evidence, int(time.time()))


def _await_fleet(args, path: Path, fleet: bool, result: dict) -> int:
    wait = args.wait if args.wait is not None else (60 if fleet else 0)
    deadline = time.monotonic() + wait
    while True:
        with closing(_read_only(path)) as conn:
            readback = _readback(_snapshot(conn, args.task_id, args.run_id))
        result["readback"] = readback
        if not fleet or readback["task_status"] != "running":
            result["message"] = "recovery applied" if not fleet else "fleet sync completed handoff"
            return 0
        if time.monotonic() >= deadline:
            result["message"] = f"handed off to fleet sync; still running after {wait} s"
            return 3
        time.sleep(min(5, max(0, deadline - time.monotonic())))


def recover_ghost(args) -> int:
    slug = kb._normalize_board_slug(args.board) if args.board else kb.get_current_board()
    path = kb.kanban_db_path(slug)
    result = {"board": slug, "db_path": str(path), "task_id": args.task_id,
              "run_id": args.run_id}
    if not path.is_file():
        result["message"] = "refused: board DB does not exist"
        _emit(args, result)
        return 1
    committed = False
    try:
        with closing(_read_only(path)) as conn:
            initial = _snapshot(conn, args.task_id, args.run_id)
            board_checks = _board_checks(conn, slug, path)
            if all(c["result"] == "PASS" for c in board_checks) and _already_recovered(
                conn, initial, args.task_id, args.run_id
            ):
                result.update(already_recovered=True, readback=_readback(initial))
                _emit(args, result)
                return 0
            checks = _checks(conn, initial, args.task_id, args.run_id, slug, path, int(time.time()))
        evidence, evidence_check = _evidence_check(args)
        checks.append(evidence_check)
        result.update(checks=checks, fingerprint=initial["fingerprint"])
        fleet = initial["mapping"] is not None
        result["planned_writes"] = (["close run", "mark dead-worker-pending", "append ghost_recovered event",
                                     "create board identity if absent"] if fleet else
                                    ["close run", "release task to done/retry", "append ghost_recovered event",
                                     "create board identity if absent"])
        if any(c["result"] == "FAIL" for c in checks):
            result["message"] = "refused: recovery checks failed; no writes"
            _emit(args, result)
            return 1
        if not args.apply:
            result["message"] = "DRY RUN: no writes"
            _emit(args, result)
            return 0
        applied = _apply_locked(args, path, slug, initial, evidence, result)
        if applied is None:
            _emit(args, result)
            return 0
        committed = True
        result["mode"], result["follow_up"] = applied
        rc = _await_fleet(args, path, fleet, result)
        _emit(args, result)
        return rc
    except (sqlite3.Error, OSError, RecoveryRefused) as exc:
        result["message"] = (f"recovery committed; readback failed: {exc}" if committed else
                             f"refused: {exc}; no recovery writes committed")
        _emit(args, result)
        return 3 if committed else 1
