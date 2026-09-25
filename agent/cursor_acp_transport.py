"""Owned Cursor process group; refusing client requests is not a native-tool sandbox."""
from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import select
import signal
import subprocess
import threading
import time

from tools.environments.local import hermes_subprocess_env

logger = logging.getLogger(__name__)


class CursorCleanupError(RuntimeError):
    """Cleanup is not a failed inference and must never cause a prompt replay."""
    code = "cursor_cleanup_failed"

    def __init__(self):
        super().__init__("Cursor ACP cleanup failed; automatic replay is disabled.")


class CursorSession:
    def __init__(self, command, args, cwd, timeout):
        if not hasattr(os, "killpg"):
            raise RuntimeError("Cursor ACP requires POSIX process-group cleanup on this platform.")
        self.deadline = time.monotonic() + timeout
        self._write_lock = threading.RLock()
        self._stop_writes = threading.Event()
        self._write_broken = False
        self._close_lock = threading.Lock()
        self._closed = False
        self._cancelled = False
        self._cancel_timer = None
        self.cleanup_error = None
        self.session_id = None
        self.prompt_delivered = False
        self.prompt_pending = False
        self._cancel_sent = False
        self._next_id = 0
        # Auth belongs to Cursor's existing login. Do not pass provider API credentials.
        self.process = subprocess.Popen(
            [command, *args], cwd=cwd, env=hermes_subprocess_env(inherit_credentials=False),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            start_new_session=True,
        )
        # Retain the group identity even after the leader has exited/reparented children.
        self._pgid = self.process.pid
        self._group_gone = False
        # TextIOWrapper is used only for stdout decoding; stdin is written with
        # os.write so neither flushing nor closing can wait on a full pipe.
        os.set_blocking(self.process.stdin.fileno(), False)
        self._inbox = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    self._inbox.put(value)
        finally:
            self.process.stdout.close()
            self._inbox.put(None)

    def send(self, **message):
        data = memoryview((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode("utf-8"))
        cancelling = message.get("method") == "session/cancel"
        deadline = time.monotonic() + .2 if cancelling else self.deadline
        with self._write_lock:
            if self._write_broken:
                raise BrokenPipeError("Cursor ACP stdin contains an incomplete frame.")
            sent = 0
            try:
                while sent < len(data):
                    if self._stop_writes.is_set() and not cancelling:
                        raise InterruptedError("Cursor ACP session was cancelled or closed.")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Cursor ACP timed out writing to stdin.")
                    fd = self.process.stdin.fileno()
                    if not select.select([], [fd], [], min(.05, remaining))[1]:
                        continue
                    try:
                        sent += os.write(fd, data[sent:sent + 65536])
                    except BlockingIOError:
                        continue
            finally:
                if sent == len(data) and message.get("method") == "session/prompt":
                    # Delivery is irreversible even if the peer exits before replying.
                    self.prompt_delivered = True
                if 0 < sent < len(data):
                    # A cancel frame must never be appended to a partial prompt.
                    self._write_broken = True

    def cancel(self):
        # Fence the writer before waiting on its lock. It checks this every 50ms.
        self._stop_writes.set()
        with self._write_lock:
            if self._cancelled:
                return
            self._cancelled = True
            self._send_cancel()
            # Setup has no ACP cancellation acknowledgment; wake its waiter now.
            if not self.prompt_pending:
                self._inbox.put(None)
            self._cancel_timer = threading.Timer(2 if self.prompt_pending else 0, self._close_after_cancel)
            self._cancel_timer.daemon = True
            self._cancel_timer.start()

    def _send_cancel(self):
        if self.session_id and self.prompt_pending and not self._cancel_sent and not self._write_broken:
            self._cancel_sent = True
            with contextlib.suppress(BrokenPipeError, OSError, ValueError):
                self.send(method="session/cancel", params={"sessionId": self.session_id})

    def _answer(self, message):
        if message["method"] == "session/request_permission":
            self.send(id=message["id"], result={"outcome": {"outcome": "cancelled"}})
        else:
            # Includes every cursor/* blocking extension, filesystem and terminal request.
            self.send(id=message["id"], error={"code": -32601, "message": "Request rejected by Hermes ask-only client."})

    def request_updates(self, method, params):
        self._next_id += 1
        request_id = self._next_id
        if method == "session/prompt":
            self.prompt_pending = True
        self.send(id=request_id, method=method, params=params)
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Cursor ACP timed out during {method}.")
            try:
                message = self._inbox.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(f"Cursor ACP timed out during {method}.") from exc
            if message is None:
                if self._cancelled:
                    raise InterruptedError("Cursor ACP session was cancelled or closed.")
                raise RuntimeError(f"Cursor ACP exited during {method}.")
            if "method" in message:
                if "id" in message:
                    self._answer(message)
                elif message["method"] == "session/update":
                    params = message.get("params") or {}
                    if params.get("sessionId") == self.session_id:
                        yield params.get("update") or {}
                continue
            if message.get("id") != request_id:
                continue
            if method == "session/prompt":
                self.prompt_pending = False
            if "error" in message:
                # Remote error strings may contain credentials or prompt content.
                raise RuntimeError(f"Cursor ACP {method} failed (JSON-RPC error).")
            result = message.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"Cursor ACP {method} returned an invalid result.")
            return result

    def request(self, method, params):
        updates = self.request_updates(method, params)
        while True:
            try:
                next(updates)
            except StopIteration as done:
                return done.value

    def close(self):
        self._stop_writes.set()
        with self._close_lock:
            if self._closed:
                return
            failures = []

            def attempt(action):
                try:
                    action()
                except Exception as exc:
                    # Continue through KILL/reap/join even if an earlier step failed.
                    # Exception text can contain request data; retain only the type.
                    failures.append(type(exc).__name__)

            attempt(self._close_input)
            # Also wake an RPC waiter if a descendant keeps the stdout pipe open.
            self._inbox.put(None)
            attempt(self._wait_for_eof_exit)
            attempt(lambda: self._signal_group(signal.SIGTERM))
            attempt(self._wait_for_group)
            attempt(lambda: self._signal_group(signal.SIGKILL))
            attempt(lambda: self.process.wait(timeout=2))
            attempt(lambda: self._reader.join(timeout=2))
            # Only the reader closes stdout: another thread could block on its buffer lock.
            if self._reader.is_alive():
                failures.append("stdout_open")
            attempt(self._verify_group_closed)
            if failures:
                self.cleanup_error = CursorCleanupError()
                logger.warning("Cursor ACP cleanup failed (%s); replay disabled.", ", ".join(failures))
                raise self.cleanup_error from None
            self.cleanup_error = None
            self._closed = True

    def _close_input(self):
        with self._write_lock:
            self._cancelled = True
            if self._cancel_timer:
                self._cancel_timer.cancel()
            self._send_cancel()
            with contextlib.suppress(BrokenPipeError, OSError):
                self.process.stdin.close()

    def _wait_for_eof_exit(self):
        if self.process.poll() is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=2)

    def _close_after_cancel(self):
        try:
            self.close()
        except CursorCleanupError:
            # close recorded the failure; the request owner reports it without an
            # unhandled timer exception replacing the cancellation outcome.
            pass

    def _wait_for_group(self):
        until = time.monotonic() + 2
        while self._group_exists() and time.monotonic() < until:
            time.sleep(.02)

    def _verify_group_closed(self):
        if self._group_exists():
            raise CursorCleanupError()

    def _signal_group(self, sig):
        if self._group_gone:
            return
        # Darwin may report EPERM for a group containing only unreaped descendants.
        self.process.poll()
        try:
            os.killpg(self._pgid, sig)
        except (PermissionError, ProcessLookupError):
            # The leader can exit between poll and killpg. Reap again and probe the
            # retained group, not just the leader: descendants may still own pipes.
            if self._group_exists():
                raise

    def _group_exists(self):
        if self._group_gone:
            return False
        until = time.monotonic() + 2
        while True:
            self.process.poll()
            try:
                os.killpg(self._pgid, 0)
                return True
            except ProcessLookupError:
                # Once gone, never signal this stored pgid again (it can be reused).
                self._group_gone = True
                return False
            except PermissionError:
                # On Darwin a zombie-only group can persist after our leader was
                # reaped. waitpid on that leader cannot wait for launchd's reaping.
                remaining = until - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(.02, remaining))
