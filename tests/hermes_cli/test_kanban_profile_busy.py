"""A worker refused its profile's only session slot is "profile busy", not a crash.

A singleton profile (``max_concurrent_sessions: 1``) that is deliberately holding its one
slot makes every Kanban worker spawned for it exit at once with "Hermes is at the active
session limit (1/1)". Booked as crashes, two such exits spent ``failure_limit`` and parked
the card ``gave_up`` (Sheldon t_85083148). The worker now leaves a run-bound busy marker in
its log next to the EX_TEMPFAIL exit; the dispatcher books that run ``profile_busy``,
requeues WITHOUT counting a failure, and backs off 60s doubling to a 15 min cap. The phrase
alone (printed by a real crash) still counts as a crash.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import quiet_single_query as qsq
from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER

# Wire format pinned here, not imported: an old worker and a new dispatcher (or the reverse)
# on the same node must agree on it byte for byte.
KANBAN_WORKER_BUSY_MARKER = "[kanban-worker-busy] reason="


def test_busy_marker_wire_format_is_stable():
    assert getattr(qsq, "KANBAN_WORKER_BUSY_MARKER", None) == KANBAN_WORKER_BUSY_MARKER

_REFUSAL = (
    "Hermes is at the active session limit (1/1). Held by: cli, oldest 2m ago. "
    "Try again when another session finishes."
)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.delenv("HERMES_KANBAN_PROFILE_BUSY_BACKOFF_SECONDS", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_PROFILE_BUSY_BACKOFF_MAX_SECONDS", raising=False)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kbd._recent_worker_exits.clear()
    kb.init_db()
    return home


def _claim_dead(conn, tid: str, pid: int) -> int:
    """Claim ``tid`` for a worker ``pid`` that is already dead; returns the open run id."""
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, tid, claimer=f"{host}:w{pid}")
    conn.execute(
        "UPDATE tasks SET worker_pid=?, worker_started_at=NULL, started_at=? WHERE id=?",
        (pid, int(time.time()) - 120, tid),
    )
    conn.commit()
    return int(kb.get_task(conn, tid).current_run_id)


def _append_log(tid: str, text: str) -> None:
    log = kb.worker_log_path(tid)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(text)


def _busy_exit_log(run_id: int) -> str:
    """What a refused worker leaves in its log (real layout: refusal, marker, trailer)."""
    return (
        "I0925 23:32:35.274664 ev_poll_posix.cc:593] FD from fork parent still in poll list\n"
        f"{_REFUSAL}\n"
        f"\n{KANBAN_WORKER_BUSY_MARKER}MAX_CONCURRENT_SESSIONS run={run_id}\n"
        f"\n{KANBAN_WORKER_EXIT_TRAILER}{kb.KANBAN_RATE_LIMIT_EXIT_CODE}\n"
    )


def _events(conn, tid: str) -> list[str]:
    return [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]


def _last_run(conn, tid: str):
    return conn.execute(
        "SELECT outcome, error, metadata FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (tid,)).fetchone()


# --------------------------------------------------------------------- the card's RED test


@pytest.mark.parametrize("via_registry", [False, True], ids=["log-trailer", "reap-registry"])
def test_two_session_limit_exits_stay_ready_with_no_failure_counted(kanban_home, via_registry):
    """The Sheldon t_85083148 sequence: 2 session-limit exits with the default failure limit (2).

    Before: 2 ``crashed`` runs -> ``consecutive_failures`` 2 -> ``gave_up``. Now: still ``ready``,
    counter 0, no ``gave_up``, each run booked ``profile_busy`` with a ``profile_busy`` event.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="merge PR", assignee="shld-merge-marshal")
        for i in range(2):
            pid = 80000 + i
            run_id = _claim_dead(conn, tid, pid)
            _append_log(tid, _busy_exit_log(run_id))
            if via_registry:
                kbd._record_worker_exit(pid, kb.KANBAN_RATE_LIMIT_EXIT_CODE << 8)

            crashed = kbd.detect_crashed_workers(conn)
            kb.recompute_ready(conn, failure_limit=kbd.DEFAULT_FAILURE_LIMIT)

            assert tid not in crashed
            assert tid in getattr(kbd.detect_crashed_workers, "_last_profile_busy", [])
            task = kb.get_task(conn, tid)
            assert task.status == "ready", f"exit {i + 1}: {task.status}"
            assert task.consecutive_failures == 0
            run = _last_run(conn, tid)
            assert run["outcome"] == "profile_busy"
            meta = kb._json_dict(run["metadata"])
            assert meta.get("busy_streak") == i + 1
            assert meta.get("reason") == "MAX_CONCURRENT_SESSIONS"

        kinds = _events(conn, tid)
        assert kinds.count("profile_busy") == 2
        assert "gave_up" not in kinds
        assert "crashed" not in kinds
        # Visible reason on the card, not a silent loop.
        assert "profile busy" in (kb.get_task(conn, tid).last_failure_error or "")


def test_real_crash_that_prints_the_refusal_phrase_still_counts(kanban_home):
    """No marker, exit 1: the phrase in the output is context, not a classification."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        for i in range(2):
            _claim_dead(conn, tid, 81000 + i)
            _append_log(tid, f"traceback ...\n{_REFUSAL}\n\n{KANBAN_WORKER_EXIT_TRAILER}1\n")
            kbd.detect_crashed_workers(conn)
            if i == 0:
                task = kb.get_task(conn, tid)
                assert task.consecutive_failures == 1
                assert _last_run(conn, tid)["outcome"] == "crashed"
        assert kb.get_task(conn, tid).status == "blocked"
        assert "gave_up" in _events(conn, tid)
        assert "profile_busy" not in _events(conn, tid)


def test_marker_with_a_crash_exit_code_still_counts(kanban_home):
    """The marker only classifies together with the EX_TEMPFAIL exit: marker + rc 1 is a crash."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        run_id = _claim_dead(conn, tid, 82000)
        _append_log(tid, f"\n{KANBAN_WORKER_BUSY_MARKER}MAX_CONCURRENT_SESSIONS run={run_id}\n"
                         f"\n{KANBAN_WORKER_EXIT_TRAILER}1\n")
        kbd.detect_crashed_workers(conn)
        assert _last_run(conn, tid)["outcome"] == "crashed"
        assert kb.get_task(conn, tid).consecutive_failures == 1


def test_stale_marker_from_an_earlier_run_does_not_classify(kanban_home):
    """The marker is bound to the run it was written for: an old run's marker left in the
    append-mode log cannot turn a later run's exit into ``profile_busy``."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        run_id = _claim_dead(conn, tid, 83000)
        _append_log(tid, _busy_exit_log(run_id))
        kbd.detect_crashed_workers(conn)
        assert _last_run(conn, tid)["outcome"] == "profile_busy"

        # Next run: a provider quota wall (75, no marker for THIS run) stays rate_limited.
        _claim_dead(conn, tid, 83001)
        _append_log(tid, f"\n{KANBAN_WORKER_EXIT_TRAILER}{kb.KANBAN_RATE_LIMIT_EXIT_CODE}\n")
        kbd.detect_crashed_workers(conn)
        assert _last_run(conn, tid)["outcome"] == "rate_limited"


def test_marker_must_be_a_whole_line(kanban_home):
    """A marker-looking string embedded mid-line (e.g. echoed by a tool) does not classify."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        run_id = _claim_dead(conn, tid, 84000)
        _append_log(tid, f"echo '{KANBAN_WORKER_BUSY_MARKER}MAX_CONCURRENT_SESSIONS run={run_id}'\n"
                         f"\n{KANBAN_WORKER_EXIT_TRAILER}{kb.KANBAN_RATE_LIMIT_EXIT_CODE}\n")
        kbd.detect_crashed_workers(conn)
        assert _last_run(conn, tid)["outcome"] == "rate_limited"


def test_truncated_log_tail_cannot_turn_a_midline_marker_into_a_whole_line(kanban_home):
    """Checker round 2: cutting a long, newline-free line at the 4,000-byte tail boundary
    must not make its embedded marker appear at ``^`` and classify as profile busy."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        pid = 84001
        run_id = _claim_dead(conn, tid, pid)
        marker = f"{KANBAN_WORKER_BUSY_MARKER}MAX_CONCURRENT_SESSIONS run={run_id}"
        # Seven-byte prefix is cut away by read_worker_log(tail_bytes=4000).
        _append_log(tid, "prefix " + marker + " " * (4000 - len(marker)))
        kbd._record_worker_exit(pid, kb.KANBAN_RATE_LIMIT_EXIT_CODE << 8)
        kbd.detect_crashed_workers(conn)
        assert _last_run(conn, tid)["outcome"] == "rate_limited"


def test_expired_claim_busy_exit_is_classified_before_ttl_reclaim(kanban_home):
    """Checker round 2: an expired claim must not charge a genuine busy exit as reclaimed.
    Two expired busy attempts stay ready, do not increment failures, and never give up."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        for i in range(2):
            pid = 84100 + i
            run_id = _claim_dead(conn, tid, pid)
            _append_log(tid, _busy_exit_log(run_id))
            kbd._record_worker_exit(pid, kb.KANBAN_RATE_LIMIT_EXIT_CODE << 8)
            conn.execute("UPDATE tasks SET claim_expires=? WHERE id=?", (int(time.time()) - 1, tid))
            conn.commit()
            result = kbd.DispatchResult()
            kbd._run_reclaim_phase(
                conn, result, stale_timeout_seconds=86400,
                failure_limit=kbd.DEFAULT_FAILURE_LIMIT, reconcile_orphans=False,
            )
            task = kb.get_task(conn, tid)
            assert tid in result.profile_busy
            assert result.reclaimed == 0
            assert task is not None
            assert task.status == "ready"
            assert task.consecutive_failures == 0
            assert _last_run(conn, tid)["outcome"] == "profile_busy"
        assert "gave_up" not in _events(conn, tid)


def test_expired_busy_exit_inside_launch_grace_is_still_profile_busy(kanban_home, monkeypatch):
    """Checker round 3: explicit exit-75 + current-run marker proves the worker ended, so
    launch grace must not let generic TTL reclaim charge it as a failure."""
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "30")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        pid = 84200
        run_id = _claim_dead(conn, tid, pid)
        _append_log(tid, _busy_exit_log(run_id))
        kbd._record_worker_exit(pid, kb.KANBAN_RATE_LIMIT_EXIT_CODE << 8)
        conn.execute(
            "UPDATE tasks SET started_at=?, claim_expires=? WHERE id=?",
            (int(time.time()), int(time.time()) - 1, tid),
        )
        conn.commit()
        result = kbd.DispatchResult()
        kbd._run_reclaim_phase(
            conn, result, stale_timeout_seconds=86400,
            failure_limit=1, reconcile_orphans=False,
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert tid in result.profile_busy
        assert result.reclaimed == 0
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        assert _last_run(conn, tid)["outcome"] == "profile_busy"


def test_expired_real_crash_uses_dispatchers_failure_limit(kanban_home):
    """Checker round 3: classifying an expired crash before TTL reclaim must not silently
    replace the dispatcher's supplied failure limit with the module default."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        pid = 84300
        _claim_dead(conn, tid, pid)
        _append_log(tid, f"\n{KANBAN_WORKER_EXIT_TRAILER}1\n")
        kbd._record_worker_exit(pid, 1 << 8)
        conn.execute("UPDATE tasks SET claim_expires=? WHERE id=?", (int(time.time()) - 1, tid))
        conn.commit()
        result = kbd.DispatchResult()
        kbd._run_reclaim_phase(
            conn, result, stale_timeout_seconds=86400,
            failure_limit=1, reconcile_orphans=False,
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"
        assert task.consecutive_failures == 1
        assert "gave_up" in _events(conn, tid)


# --------------------------------------------------------------------- bounded backoff


def _busy_runs(conn, tid: str, n: int) -> None:
    for i in range(n):
        run_id = _claim_dead(conn, tid, 85000 + i)
        _append_log(tid, _busy_exit_log(run_id))
        kbd.detect_crashed_workers(conn)


def _age_last_run(conn, tid: str, seconds: int) -> None:
    """Move every closed run back in time, keeping their order, so the newest ended ``seconds`` ago."""
    conn.execute(
        "UPDATE task_runs SET ended_at = ? - ((SELECT MAX(id) FROM task_runs WHERE task_id = ?) - id) "
        "WHERE task_id = ?",
        (int(time.time()) - seconds, tid, tid),
    )
    conn.commit()


@pytest.mark.parametrize(
    "streak, wait", [(1, 60), (2, 120), (3, 240), (4, 480), (5, 900), (6, 900), (9, 900)],
)
def test_backoff_doubles_from_60s_and_caps_at_15_min(kanban_home, streak, wait):
    assert kbd._profile_busy_backoff_seconds(streak) == wait
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        _busy_runs(conn, tid, streak)
        assert kbd._profile_busy_streak(conn, tid) == streak
        _age_last_run(conn, tid, wait - 5)
        assert kbd.check_respawn_guard(conn, tid) == "profile_busy_backoff"
        _age_last_run(conn, tid, wait + 1)
        assert kbd.check_respawn_guard(conn, tid) is None


def test_a_real_run_resets_the_busy_streak(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        _busy_runs(conn, tid, 3)
        _claim_dead(conn, tid, 86000)
        _append_log(tid, f"\n{KANBAN_WORKER_EXIT_TRAILER}1\n")
        kbd.detect_crashed_workers(conn)
        assert kbd._profile_busy_streak(conn, tid) == 0
        _busy_runs(conn, tid, 1)
        assert kbd._profile_busy_streak(conn, tid) == 1


def test_long_busy_stays_visible(kanban_home):
    """A card busy for a long stretch keeps saying so: each run's event carries the streak and
    the next retry, and from the long-busy threshold on it is flagged ``long_busy``."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        _busy_runs(conn, tid, kbd.PROFILE_BUSY_LONG_STREAK)
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='profile_busy' "
            "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        payload = kb._json_dict(row["payload"])
        assert payload["busy_streak"] == kbd.PROFILE_BUSY_LONG_STREAK
        assert payload["next_retry_after_seconds"] == 900
        assert payload["long_busy"] is True
        assert "Held by: cli" in payload.get("worker_output", "")
        err = kb.get_task(conn, tid).last_failure_error or ""
        assert f"{kbd.PROFILE_BUSY_LONG_STREAK} times in a row" in err
        assert kb.get_task(conn, tid).consecutive_failures == 0


def test_busy_runs_do_not_break_or_extend_a_protocol_violation_streak(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        _claim_dead(conn, tid, 87000)
        _append_log(tid, f"\n{KANBAN_WORKER_EXIT_TRAILER}0\n")
        kbd.detect_crashed_workers(conn)
        _busy_runs(conn, tid, 2)
        assert kbd._protocol_violation_streak(conn, tid) == 1


def _closed_runs(conn, tid: str, outcomes: list[str], *, ended_at: int | None = None) -> None:
    """Insert closed runs directly (fast; the sweep path is covered above)."""
    now = int(time.time()) if ended_at is None else ended_at
    for outcome in outcomes:
        meta = '{"protocol_violation": true}' if outcome == "violation" else None
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at, metadata) "
            "VALUES (?, 'a', ?, ?, ?, ?, ?)",
            (tid, "crashed" if outcome == "violation" else outcome,
             "crashed" if outcome == "violation" else outcome, now - 1, now, meta),
        )
    conn.commit()


def test_a_long_busy_hold_does_not_erase_the_protocol_violation_streak(kanban_home):
    """Checker round 1, finding 4: busy runs must stay neutral however many there are, not
    only inside a fixed scan window."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        _closed_runs(conn, tid, ["violation"] + ["profile_busy"] * 60)
        assert kbd._protocol_violation_streak(conn, tid) == 1


def test_busy_streak_is_exact_past_any_scan_window(kanban_home):
    """Checker round 1, finding 5: the streak (event, error line) stays exact on a long hold."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        _closed_runs(conn, tid, ["crashed"] + ["profile_busy"] * 60)
        assert kbd._profile_busy_streak(conn, tid) == 60


def test_same_second_crash_then_busy_still_backs_off(kanban_home):
    """Checker round 1, finding 2: runs ending in the same second are ordered by run id, so the
    newest (busy) run decides the guard, not an older crash that tied on ``ended_at``."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        _closed_runs(conn, tid, ["crashed", "profile_busy"], ended_at=int(time.time()))
        assert kbd.check_respawn_guard(conn, tid) == "profile_busy_backoff"


@pytest.mark.parametrize(
    "base, cap, streak, wait",
    [
        ("0", None, 1, 60),        # contract floor: never below 60s
        ("-5", None, 1, 60),       # invalid -> default
        ("1000", "900", 1, 900),  # base above the cap is held to the cap
        (None, "0", 3, 60),        # cap is also held to the contract floor
        (None, "100000", 20, 900), # contract ceiling: never above 900s
    ],
)
def test_backoff_overrides_stay_bounded(monkeypatch, base, cap, streak, wait):
    """Checker round 1, finding 3: env overrides cannot remove the wait or exceed the cap."""
    for name, value in (("HERMES_KANBAN_PROFILE_BUSY_BACKOFF_SECONDS", base),
                        ("HERMES_KANBAN_PROFILE_BUSY_BACKOFF_MAX_SECONDS", cap)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    got = kbd._profile_busy_backoff_seconds(streak)
    assert got == wait
    assert got >= 1


# --------------------------------------------------------------------- worker side


@pytest.mark.parametrize(
    ("in_kanban", "reason", "marker"),
    [
        (True, "MAX_CONCURRENT_SESSIONS", True),
        (True, "SESSION_NOT_OWNED", False),
        (True, "SESSION_COORDINATION_UNAVAILABLE", False),
        (False, "MAX_CONCURRENT_SESSIONS", False),
    ],
)
def test_refused_worker_writes_the_run_bound_busy_marker(monkeypatch, capsys, in_kanban, reason, marker):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "1371")
    if in_kanban:
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_85083148")
    monkeypatch.setattr(cli, "_should_seed_interactive", lambda *a, **k: False)
    stub = SimpleNamespace(_active_session_refusal_reason=reason)
    stub._claim_active_session = lambda *a, **k: False
    with pytest.raises(SystemExit) as exc:
        cli._run_single_query_mode(stub, "do the thing", None, True, True)
    err = capsys.readouterr().err
    line = f"{KANBAN_WORKER_BUSY_MARKER}MAX_CONCURRENT_SESSIONS run=1371"
    assert (line in err.splitlines()) is marker
    if marker:
        assert exc.value.code == kb.KANBAN_RATE_LIMIT_EXIT_CODE
        # The marker precedes the exit trailer so the dispatcher reads both from one tail.
        assert err.index(line) < err.index(KANBAN_WORKER_EXIT_TRAILER)
