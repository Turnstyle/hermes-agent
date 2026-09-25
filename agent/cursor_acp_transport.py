"""Owned Cursor process group; refusing client requests is not a native-tool sandbox."""
from __future__ import annotations

import contextlib
import json
import os
import queue
import signal
import subprocess
import threading
import time

from tools.environments.local import hermes_subprocess_env


class CursorSession:
    def __init__(self, command, args, cwd, timeout):
        if not hasattr(os, "killpg"):
            raise RuntimeError("Cursor ACP requires POSIX process-group cleanup on this platform.")
        self.deadline = time.monotonic() + timeout
        self._write_lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._cancelled = False
        self._cancel_timer = None
        self.session_id = None
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
        with self._write_lock:
            if self._cancelled and message.get("method") != "session/cancel":
                raise InterruptedError("Cursor ACP session was cancelled or closed.")
            self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
            self.process.stdin.flush()

    def cancel(self):
        with self._write_lock:
            if self._cancelled:
                return
            self._cancelled = True
            self._send_cancel()
            # Setup has no ACP cancellation acknowledgment; wake its waiter now.
            if not self.prompt_pending:
                self._inbox.put(None)
            self._cancel_timer = threading.Timer(2 if self.prompt_pending else 0, self.close)
            self._cancel_timer.daemon = True
            self._cancel_timer.start()

    def _send_cancel(self):
        if self.session_id and self.prompt_pending and not self._cancel_sent:
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
        with self._close_lock:
            if self._closed:
                return
            with self._write_lock:
                self._cancelled = True
                if self._cancel_timer:
                    self._cancel_timer.cancel()
                self._send_cancel()
                with contextlib.suppress(BrokenPipeError, OSError):
                    self.process.stdin.close()
            # Also wake an RPC waiter if a descendant keeps the stdout pipe open.
            self._inbox.put(None)
            if self.process.poll() is None:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self.process.wait(timeout=2)
            self._signal_group(signal.SIGTERM)
            until = time.monotonic() + 2
            while self._group_exists() and time.monotonic() < until:
                self.process.poll()
                time.sleep(.02)
            self._signal_group(signal.SIGKILL)
            self.process.wait(timeout=2)
            self._reader.join(timeout=2)
            # Only the reader closes stdout: another thread could block on its buffer lock.
            if self._reader.is_alive():
                raise RuntimeError("Cursor ACP stdout remained open after process-group cleanup.")
            self._closed = True

    def _signal_group(self, sig):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self._pgid, sig)

    def _group_exists(self):
        try:
            os.killpg(self._pgid, 0)
            return True
        except ProcessLookupError:
            return False
