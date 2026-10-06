"""Quiet ``hermes chat -Q`` helpers: bind this session's key and resume nested notifies.

Bot Mode delivers a local DM as ``hermes -p <bot> chat -Q --query-file``. Interactive
chat binds ``set_current_session_key(self.session_id)`` around the turn; the quiet
path did not, so a nested ``message_agent`` notify inherited the dispatcher's
``HERMES_SESSION_KEY`` and never woke the recipient. Quiet also printed and exited
after one turn, so a nested teammate reply that finished during the one-shot linger
was never injected as a follow-up.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import subprocess
import sys
import threading
import time
import traceback
from typing import Any, Callable, MutableMapping

# Nested A→B→C is one extra turn; this caps a runaway message_agent chain.
_MAX_QUIET_NOTIFY_ROUNDS = 8

# Last line a Kanban worker leaves in its own log: ``[kanban-worker-exit] rc=<code>``. A per-tick
# ``hermes kanban dispatch`` process never reaped the worker, so ``os.waitpid`` cannot tell it how
# the worker exited; the trailer is the process-independent witness the dead-worker sweep reads
# instead, so a clean exit without a terminal board call is booked as the same protocol violation
# (and a 75 as the same rate-limit requeue) whichever process notices the death.
KANBAN_WORKER_EXIT_TRAILER = "[kanban-worker-exit] rc="

# Line a Kanban worker writes to its own log, just before the exit trailer, when it never started
# its turn because its profile was at the active-session cap:
# ``[kanban-worker-busy] reason=MAX_CONCURRENT_SESSIONS run=<run id>``. Together with an
# EX_TEMPFAIL exit it tells the dead-worker sweep "profile busy" (requeue with backoff, no failure
# counted) apart from a provider quota wall. Bound to the run id so a marker left in the
# append-mode log by an earlier run can never classify a later one. Wire format: never change it
# without changing the dispatcher's reader on every node in the same carry.
KANBAN_WORKER_BUSY_MARKER = "[kanban-worker-busy] reason="

# Returned by a quiet notify follow-up when the session lease could not be reclaimed in time.
FOLLOW_UP_NOT_ACCEPTED = object()


def write_worker_busy_marker(reason: str) -> None:
    """Write the run-bound busy marker for a Kanban worker; a no-op outside one (or without a run id)."""
    run_id = os.environ.get("HERMES_KANBAN_RUN_ID", "").strip()
    if not (os.environ.get("HERMES_KANBAN_TASK") and run_id.isdigit()):
        return
    with contextlib.suppress(Exception):
        print(f"\n{KANBAN_WORKER_BUSY_MARKER}{reason} run={int(run_id)}", file=sys.stderr, flush=True)


def exit_single_query(code: int) -> None:
    """``sys.exit(code)`` for a one-shot turn; a Kanban worker first writes the exit trailer to its log.

    A one-shot's ``SystemExit`` is turned into a hard exit by ``_run_single_query_mode`` once its
    ``finally`` cleanup (session flush + active-session lease release) has run; see
    ``hard_exit_single_query``."""
    if os.environ.get("HERMES_KANBAN_TASK"):
        with contextlib.suppress(Exception):
            # stderr: stdout may be the ``--stream-json`` record stream, and the worker log
            # captures both streams.
            print(f"\n{KANBAN_WORKER_EXIT_TRAILER}{int(code)}", file=sys.stderr, flush=True)
    sys.exit(code)


def hard_exit_single_query(code: Any) -> None:
    """Exit a one-shot ``chat -q``/``-Q`` run without interpreter finalization.

    ``Py_FinalizeEx`` joins non-daemon threads and takes the import lock; a background thread
    parked mid-import left refused Kanban workers at 0% CPU for hours after printing
    ``[kanban-worker-exit] rc=75``, and a Bot Chat DM child sat 10h14m in
    ``Py_FinalizeEx -> gc -> import`` while its parent held the recipient's turn lock. Mirrors
    ``main._exit_after_oneshot`` (#30387, #43055): flush, shut down logging, ``os._exit``. The caller
    runs its cleanup first."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()
    with contextlib.suppress(Exception):
        logging.shutdown()  # module-level import: no import-lock wait at exit
    os._exit(code if isinstance(code, int) else (0 if code is None else 1))


def _failure_exit_code(exc: BaseException) -> int:
    """Exit code for an exception that escaped a one-shot run; never 0.

    A cleanup exception that replaced a pending ``SystemExit`` (raised inside the ``finally``)
    keeps that exit's non-zero int code, so a refusal's 75 still reaches the dispatcher."""
    if isinstance(exc, KeyboardInterrupt):
        return 130
    pending = exc.__context__
    while pending is not None and not isinstance(pending, SystemExit):
        pending = pending.__context__
    if pending is not None and isinstance(pending.code, int) and pending.code != 0:
        return pending.code
    return 1


@contextlib.contextmanager
def single_query_hard_exit():
    """Wrap a one-shot run: any way out of the block becomes ``hard_exit_single_query`` after every
    ``finally`` inside it (session flush, lease release, turn report) has already run.

    ``SystemExit`` keeps its code; an exception prints its traceback to stderr and exits 1
    (``KeyboardInterrupt``: 130), because ordinary interpreter teardown is what hangs."""
    try:
        yield
    except SystemExit as exc:
        # Module-global lookup keeps the test seam (tests/conftest.py neutralizes it).
        globals()["hard_exit_single_query"](exc.code)
        raise
    except (Exception, KeyboardInterrupt) as exc:
        with contextlib.suppress(Exception):
            traceback.print_exc(file=sys.stderr)
        globals()["hard_exit_single_query"](_failure_exit_code(exc))
        raise


# Old names, kept for external callers; in-tree code uses the ``single_query`` spellings.
hard_exit_kanban_worker = hard_exit_single_query
kanban_worker_hard_exit = single_query_hard_exit


# A spawner that bounds only the TURN (the cron Bot Chat lane) hands the quiet child a report
# path here. The child records the turn's outcome there the moment the turn ends, BEFORE the
# one-shot exit linger, so the spawner can book the delivery and stop waiting while the linger
# keeps protecting nested ``notify_on_complete`` replies. Popped before the turn runs (same
# contract as HERMES_TURN_AUTHOR): nothing the turn spawns inherits it, and a nested one-shot
# never writes over its host's report — the record also carries the writer's pid.
TURN_REPORT_FILE_ENV = "HERMES_QUIET_TURN_REPORT_FILE"
_report_callback = contextvars.ContextVar("quiet_turn_report_callback", default=None)


@contextlib.contextmanager
def release_lock_at_report(callback):
    token = _report_callback.set(callback)
    try:
        yield
    finally:
        _report_callback.reset(token)


def take_turn_report_path(environ: MutableMapping[str, str] = os.environ) -> str | None:
    """Read and remove the spawner's turn-report path so subprocesses started during the turn do not inherit it."""
    return environ.pop(TURN_REPORT_FILE_ENV, None) or None


def write_turn_report(path: str | None, *, exit_code: int, error: str = "", reply: str = "") -> None:
    """Atomically record ``{pid, exit_code, error, reply}`` at *path*; a no-op without a path. Never
    raises: the report is the spawner's convenience, the turn itself is already persisted. ``reply``
    is what the run will print — a spawner booking a lingering child from its report relays it."""
    if not path:
        return
    from utils import atomic_json_write

    record = {"pid": os.getpid(), "exit_code": int(exit_code), "error": str(error or ""), "reply": str(reply or "")}
    # 0600 from creation: the record now carries the turn's answer, like the 0600 query file beside it.
    with contextlib.suppress(Exception):
        atomic_json_write(path, record, indent=None, mode=0o600)


def read_turn_report(path: str, pid: int) -> dict | None:
    """The child's turn report, or None while absent, unreadable, or written by another process."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("pid") != pid:
        return None
    return record


# After the child reports its turn, a child with nothing to linger for exits at once; a spawner
# that needs only the outcome gives it that long so its real exit code and stream tails are
# booked instead of the report's summary.
REPORTED_TURN_EXIT_GRACE_SECONDS = 2.0

# A reported child may linger for nested ``notify_on_complete`` replies (the one-shot linger budget,
# ``quiet_notify_linger_seconds()``) plus this slack for its own finalization; never past the cap.
# A child still alive after that is hung (a 10h14m interpreter-exit import-lock deadlock held a
# recipient's turn lock) and is terminated: SIGTERM, then SIGKILL after ``CHILD_TERM_GRACE_SECONDS``.
POST_REPORT_EXIT_SLACK_SECONDS = 60.0
POST_REPORT_EXIT_CAP_SECONDS = 900.0
CHILD_TERM_GRACE_SECONDS = 10.0


def post_report_exit_wait_seconds() -> float:
    """How long after its turn report a child may stay alive before it is terminated."""
    return min(quiet_notify_linger_seconds() + POST_REPORT_EXIT_SLACK_SECONDS, POST_REPORT_EXIT_CAP_SECONDS)


def process_start_stamp(pid: int) -> str | None:
    """``ps`` start time of a live (non-zombie) process, or None; identifies a process across PID reuse."""
    try:
        out = subprocess.run(
            ["ps", "-o", "stat=,lstart=", "-p", str(int(pid))], capture_output=True, text=True, timeout=10,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"}).stdout.strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    state, _, started = out.partition(" ")
    if not started.strip() or state.startswith("Z"):
        return None
    return " ".join(started.split())


def reap_after(pid: int, stamp: str, kill_at: float, grace: float, *, now=None, sleep=None, probe=None, send=None) -> str:
    """Terminate exactly *pid* at wall-clock *kill_at* if it is still the process that had *stamp*.

    SIGTERM, then SIGKILL once *grace* seconds pass without it exiting. Returns ``"gone"`` (already
    exited, or the PID now belongs to another process: nothing is signalled), ``"terminated"`` or
    ``"killed"``. Self-contained on purpose: ``spawn_detached_reaper`` runs this function's source,
    with ``process_start_stamp``, in a fresh interpreter that outlives the spawner."""
    import os
    import signal
    import time

    now = now or time.time
    sleep = sleep or time.sleep
    probe = probe or process_start_stamp
    send = send or os.kill
    while now() < kill_at:
        sleep(min(1.0, max(kill_at - now(), 0.01)))
    if probe(pid) != stamp:
        return "gone"
    try:
        send(pid, signal.SIGTERM)
    except OSError:
        return "gone"
    end = now() + grace
    while now() < end:
        sleep(0.25)
        if probe(pid) != stamp:
            return "terminated"
    if probe(pid) != stamp:
        return "terminated"
    try:
        send(pid, signal.SIGKILL)
    except OSError:
        return "terminated"
    return "killed"


def spawn_detached_reaper(pid: int, kill_at_monotonic: float, grace: float) -> bool:
    """Arrange for a lingering child to be terminated even after this process exits.

    A Bot Chat delivery runner is detached and exits right after returning the reply, so an
    in-process timer would die with it and leave the hung child behind. The reaper is a tiny
    ``start_new_session`` interpreter that waits for the absolute deadline and then signals only that
    PID, after re-checking its start time. Returns False (nothing armed) when the child's identity
    cannot be pinned, e.g. no ``ps``."""
    import inspect

    if sys.platform == "win32":
        return False
    stamp = process_start_stamp(pid)
    if stamp is None:
        return False
    kill_at = time.time() + max(kill_at_monotonic - time.monotonic(), 0.0)
    source = "\n".join((
        "import os, subprocess",
        inspect.getsource(process_start_stamp),
        inspect.getsource(reap_after),
        f"reap_after({int(pid)}, {stamp!r}, {kill_at!r}, {float(grace)!r})",
    ))
    try:
        subprocess.Popen([sys.executable, "-I", "-c", source], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError:
        logging.getLogger(__name__).warning("could not spawn reaper for lingering quiet child %s", pid, exc_info=True)
        return False
    return True


def run_reported_turn(argv: list, *, env: MutableMapping[str, str], report_path: str, timeout: float | None,
                      exit_grace: float | None = REPORTED_TURN_EXIT_GRACE_SECONDS, cwd: str | None = None,
                      encoding: str | None = None, reported_linger: float | None = None,
                      exit_wait: float | None = None, term_grace: float | None = None) -> subprocess.CompletedProcess:
    """Run one ``hermes chat -Q`` delivery child; *timeout* bounds the TURN, not the process.

    The child records its turn at *report_path* (``write_turn_report``) the moment the turn ends,
    then runs the one-shot exit linger for nested ``notify_on_complete`` replies — bounded by
    ``terminal.oneshot_completion_wait_seconds``, whose default equals the delivery caps, so
    waiting for process exit booked every delivered turn that left a reply pending as a timeout
    and killed the linger (#113608, #114980). A child that exits is booked from its real exit
    code and streams. A child still lingering once its report exists is booked from the report:
    after *exit_grace* seconds for a spawner that needs only the outcome; after *reported_linger*
    seconds past the report for a spawner that relays the printed answer and must give the linger
    its whole budget (a teammate's reply during it may still become that answer; the turn cap no
    longer ends that wait); otherwise at the *timeout* cap. Whichever way it stops waiting, the
    child is never left to hang: it dies at ``report + exit_wait`` (default
    ``post_report_exit_wait_seconds()``) via ``spawn_detached_reaper``, which survives this process.
    A turn that never ends is killed at *timeout* — SIGTERM, then SIGKILL after *term_grace*
    (default ``CHILD_TERM_GRACE_SECONDS``) — and raised as ``subprocess.TimeoutExpired``.

    *cwd* pins the child's directory (a spawner sitting in a reaped scratch workspace must not
    hand its dead cwd on — the child dies at CLI startup, #102941). The pipes decode lossily
    everywhere: a stray non-UTF-8 byte (a grandchild sharing the pipe interleaving a partial
    multi-byte write) must not raise in the drain thread and take the reply and the failure tail
    with it (#105582). Without an explicit *encoding* they decode as UTF-8 only on win32, where
    the child is guaranteed UTF-8 (hermes_bootstrap reconfigures its streams even under
    PYTHONIOENCODING=cp1252) while the gateway parent is not started in UTF-8 mode, so the
    locale default mangled or lost accented replies (#115894); on POSIX the child keeps the
    locale codec, so the locale default stays correct there (#66566).
    """
    from hermes_cli._subprocess_compat import windows_hide_flags

    if encoding is None and sys.platform == "win32":
        encoding = "utf-8"
    if term_grace is None:
        term_grace = CHILD_TERM_GRACE_SECONDS
    proc = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding=encoding, errors="replace", env={**env, TURN_REPORT_FILE_ENV: report_path},
        cwd=cwd, creationflags=windows_hide_flags())
    streams: dict = {}

    def _drain() -> None:
        streams["out"], streams["err"] = proc.communicate()

    drain = threading.Thread(target=_drain, name=f"quiet-turn-drain-{proc.pid}", daemon=True)
    drain.start()
    deadline = time.monotonic() + timeout if timeout is not None else None
    report = None
    reported_at = 0.0
    killed = False
    while True:
        drain.join(timeout=exit_grace if report is not None and exit_grace is not None else 0.25)
        if not drain.is_alive():
            return subprocess.CompletedProcess(argv, proc.returncode, streams.get("out", ""), streams.get("err", ""))
        if report is not None and exit_grace is not None:
            break
        # Re-read while waiting for the cap: a follow-up turn rewrites the report with its answer.
        current = read_turn_report(report_path, proc.pid)
        if current is not None and report is None:
            reported_at = time.monotonic()
            if exit_wait is None:
                exit_wait = post_report_exit_wait_seconds()
            if current.get("exit_code") == 0:
                callback = _report_callback.get()
                if callback is not None:
                    callback()
        report = current or report
        if report is not None:
            now = time.monotonic()
            if now >= reported_at + exit_wait:
                break
            if reported_linger is not None:
                if now >= reported_at + reported_linger:
                    break
            elif deadline is not None and now >= deadline:
                break
        elif deadline is not None and time.monotonic() >= deadline:
            killed = True
            proc.terminate()
            drain.join(timeout=term_grace)
            if drain.is_alive():
                proc.kill()
                drain.join(timeout=5.0)
            # A killed child cannot run further, but the turn may have ENDED (and delivered)
            # in the window between the last report check and the kill landing. Re-read once:
            # a report that appeared means the turn completed — book it instead of
            # misreporting a delivered turn as a timeout (and never re-notifying).
            report = read_turn_report(report_path, proc.pid)
            if report is not None:
                break
            raise subprocess.TimeoutExpired(argv, timeout)
    # Turn over, child still lingering for a nested reply: not this spawner's wait, and not its job
    # to outlive its budget either.
    if not killed and proc.poll() is None:
        spawn_detached_reaper(proc.pid, reported_at + (exit_wait if exit_wait is not None else 0.0), term_grace)
    return subprocess.CompletedProcess(
        argv, int(report["exit_code"]), report.get("reply") or "", report.get("error") or "")


@contextlib.contextmanager
def bind_quiet_session_key(session_id: str):
    """Bind the approval/session key to *this* quiet session for the enclosing ``with`` block."""
    from tools.approval_context import reset_current_session_key, set_current_session_key

    token = set_current_session_key(session_id or "default")
    try:
        yield
    finally:
        reset_current_session_key(token)


def _diagnostic_only_wake_muted(events) -> bool:
    """True when every drained event is an automatic diagnostic AND the CLI policy suppresses them."""
    from agent.notification_presentation import diagnostic_process_event
    from gateway.warning_notifications import warning_notifications_enabled

    if not events or not all(diagnostic_process_event(e) for e in events if isinstance(e, dict)):
        return False
    return not warning_notifications_enabled("cli")


def quiet_notify_linger_seconds() -> float:
    """Total linger budget for one quiet run: the shared ``terminal.oneshot_completion_wait_seconds``.

    One budget covers the drain loop here AND the later ``_wait_for_oneshot_background_completions``
    pass, so a stuck ``notify_on_complete`` child cannot stack round-after-round waits on top of the
    finalize re-wait (pre-fix worst case: 8 rounds x 600s + 600s).
    """
    from tools.process_registry import ProcessRegistry

    return ProcessRegistry._oneshot_completion_wait_seconds()


def continue_quiet_notify_completions(
    session_id: str,
    run_turn: Callable[[str], Any],
    *,
    owns_event=None,
    max_rounds: int = _MAX_QUIET_NOTIFY_ROUNDS,
    linger_budget: float | None = None,
) -> Any:
    """Linger for ``notify_on_complete`` work, then run owned completion texts as follow-up turns.

    Returns the last ``run_turn`` result, or ``None`` when nothing owned completed. The whole
    loop shares ONE linger budget (default: ``terminal.oneshot_completion_wait_seconds``) — a
    process that times out is waited on no further this run: after the current round's drained
    texts run, the loop stops (the finalize linger still covers it once, bounded, via the
    budget handshake below).
    """
    from tools.process_registry import process_registry
    from tools.async_delegation import claim_event_delivery, complete_event_delivery, defer_event_delivery, release_event_delivery

    last: Any = None
    key = session_id or ""
    if linger_budget is None:
        linger_budget = quiet_notify_linger_seconds()
    deadline = time.monotonic() + max(float(linger_budget), 0.0)
    for _ in range(max_rounds):
        wait = process_registry.wait_for_pending_completions(None, timeout=max(deadline - time.monotonic(), 0.0))
        drained = []
        claimed: list[tuple[Any, str]] = []
        for event, text in process_registry.drain_notifications(session_key=key, owns_event=owns_event):
            # Durable async_delegation events carry a delivery ledger: without the
            # claim/complete handshake the row stays delivery_state='pending' and
            # restore_undelivered_completions re-queues it on the next process start,
            # injecting the same result twice. Same contract as every other drain consumer.
            claim = claim_event_delivery(event, "cli-quiet")
            if claim is None:
                continue
            drained.append((event, text))
            claimed.append((event, claim))
        # Every drained event type carries formatted text (completions, watch matches,
        # async_delegation results): drain_notifications POPS owned events off the queue,
        # so filtering by type here would consume-and-silently-drop owned
        # async_delegation results. Keep everything that rendered.
        texts = [text for _event, text in drained if text]
        if texts:
            try:
                follow = run_turn("\n\n".join(texts))
            except Exception:
                for event, claim in claimed:
                    release_event_delivery(event, claim)
                raise
            if follow is FOLLOW_UP_NOT_ACCEPTED:
                for event, claim in claimed:
                    defer_event_delivery(event, claim)
            else:
                for event, claim in claimed:
                    complete_event_delivery(event, claim)
                # Same admission rule as the interactive CLI turn: a wake made ONLY of automatic
                # diagnostics (early failure / watch notices) still runs, but under suppression its
                # reply never displaces the requested one-shot answer on stdout.
                if not _diagnostic_only_wake_muted([event for event, text in drained if text]):
                    last = follow
        else:
            for event, claim in claimed:
                complete_event_delivery(event, claim)
        if wait.get("timed_out"):
            break
        if not texts:
            return last
    return last


def adopt_unanswered_turn(cli: Any, query: Any, environ: MutableMapping[str, str] = os.environ) -> bool:
    """A dispatcher's re-run of a failed delivery turn resumes the DM its first attempt already
    persisted instead of appending it again. Returns True when the tail row was adopted.

    The failed attempt's turn-start persist left the DM as the transcript's unanswered tail row. A
    fresh process cannot know that by itself (``_DB_PERSISTED_MARKER`` is in-process only), and
    inferring it from an identical tail alone would swallow a person's deliberate re-send — so the
    dispatcher must say so with ``tools.bot_relay.RESUME_UNANSWERED_TURN_ENV``, consumed (popped) here
    before the turn so tool subprocesses never inherit it. Which row counts as the unanswered DM, and
    how it is re-staged as ``_pending_cli_user_message``, is shared with the in-process peer-DM lane
    (``agent.session_persistence.adopt_unanswered_turn``, #115325).
    """
    from tools.bot_relay import RESUME_UNANSWERED_TURN_ENV

    if environ.pop(RESUME_UNANSWERED_TURN_ENV, None) != "1":
        return False
    from agent.session_persistence import adopt_unanswered_turn as _adopt_tail

    return _adopt_tail(getattr(cli, "conversation_history", None) or [], query, cli.agent)
