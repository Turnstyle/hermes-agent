"""Per-request Cursor stdio session; no file, terminal, or permission grants."""
from __future__ import annotations

import contextlib
import json
import queue
import subprocess
import threading
import time

from hermes_cli._subprocess_compat import windows_hide_flags
from tools.environments.local import hermes_subprocess_env


class CursorSession:
    def __init__(self, command, args, cwd, timeout):
        self.deadline = time.monotonic() + timeout
        self._write_lock = threading.Lock()
        self.session_id = None
        self.prompt_pending = False
        self._cancel_sent = False
        self._next_id = 0
        # Auth belongs to Cursor's existing login. Do not pass provider API credentials.
        self.process = subprocess.Popen(
            [command, *args], cwd=cwd, env=hermes_subprocess_env(inherit_credentials=False),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=windows_hide_flags(),
        )
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
            self._inbox.put(None)

    def send(self, **message):
        with self._write_lock:
            self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
            self.process.stdin.flush()

    def cancel(self):
        if self.session_id and self.prompt_pending and not self._cancel_sent:
            self._cancel_sent = True
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
        if self.process.poll() is None:
            with contextlib.suppress(BrokenPipeError, OSError):
                self.cancel()
            # EOF lets the stdio server exit without signalling unrelated descendants.
            self.process.stdin.close()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        self._reader.join(timeout=2)
        self.process.stdin.close()
        self.process.stdout.close()
