"""Real subprocess contracts for cleanup and cancellation, including inherited pipes."""
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from agent.cursor_acp_client import CursorACPClient

PEER = Path(__file__).parent / "fixtures" / "cursor_acp_lifecycle_peer.py"
ACK = {"cursor": {"allow_native_tools": True}}
# These tests signal only the peers they create, including reparented descendants.
pytestmark = pytest.mark.live_system_guard_bypass


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.02)
    return False


def live_pids(journal):
    alive = []
    for row in rows(journal):
        if "pid" in row:
            try:
                p = psutil.Process(row["pid"])
                if p.is_running() and p.status() != psutil.STATUS_ZOMBIE:
                    alive.append(p)
            except psutil.NoSuchProcess:
                pass
    return alive


@pytest.mark.parametrize("scenario", ["tree-exit", "tree-stall"])
def test_close_reaps_descendants_even_after_leader_exit(tmp_path, scenario):
    from agent.cursor_acp_transport import CursorSession

    journal = tmp_path / "pids.jsonl"
    session = CursorSession(sys.executable, [str(PEER), scenario, str(journal)], str(tmp_path), 30)
    session.session_id = "local"
    session.send(id=1, method="session/prompt", params={})
    closed = threading.Event()
    errors = []
    def close():
        try:
            session.close()
        except Exception as exc:
            errors.append(exc)
        finally:
            closed.set()
    try:
        assert wait_for(lambda: len([r for r in rows(journal) if "pid" in r]) == 3)
        worker = threading.Thread(target=close, daemon=True)
        worker.start()
        assert closed.wait(10), "close blocked on a descendant's inherited stdout"
        assert not errors
        assert wait_for(lambda: not live_pids(journal), 3), "descendants survived close"
        assert session.process.poll() is not None
        assert not session._reader.is_alive()
    finally:
        for proc in reversed(live_pids(journal)):
            proc.kill()
        if not closed.is_set():
            closed.wait(5)
        session.close()


@pytest.mark.parametrize("scenario,ready", [("stall-setup", "session/set_config_option"),
                                           ("stall-prompt", "session/prompt")])
def test_cancel_fences_setup_and_bounds_unacknowledged_prompt(tmp_path, scenario, ready):
    journal = tmp_path / "cancel.jsonl"
    client = CursorACPClient(command=sys.executable, args=[str(PEER), scenario, str(journal)], acp_cwd=str(tmp_path))
    done = threading.Event()
    errors = []
    def request():
        try:
            client.chat.completions.create(model="default[]", messages=[], timeout=60, extra_body=ACK)
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()
    worker = threading.Thread(target=request, daemon=True)
    worker.start()
    try:
        assert wait_for(lambda: any(r.get("method") == ready for r in rows(journal)))
        client.cancel()
        assert done.wait(10), "cancel left the request running until its original deadline"
        assert errors and isinstance(errors[0], InterruptedError)
        assert not live_pids(journal)
        if scenario == "stall-setup":
            assert not any(r.get("method") == "session/prompt" for r in rows(journal))
        with pytest.raises(InterruptedError):
            client.chat.completions.create(model="default[]", messages=[], extra_body=ACK)
    finally:
        for proc in reversed(live_pids(journal)):
            proc.kill()
        worker.join(5)
        client.close()


@pytest.mark.parametrize("operation", ["complete", "stream", "models", "invalid", "timeout"])
def test_eof_ignoring_peer_preserves_primary_outcome(tmp_path, operation):
    journal = tmp_path / "eof.jsonl"
    scenario = "timeout-ignore-eof" if operation == "timeout" else "success-ignore-eof"
    client = CursorACPClient(command=sys.executable, args=[str(PEER), scenario, str(journal)], acp_cwd=str(tmp_path))
    try:
        if operation == "models":
            assert client.list_models(timeout_seconds=5) == ["default[]"]
        elif operation in ("invalid", "timeout"):
            with pytest.raises(ValueError if operation == "invalid" else TimeoutError):
                client.chat.completions.create(model="invalid" if operation == "invalid" else "default[]",
                                               messages=[], timeout=2, extra_body=ACK)
        else:
            response = client.chat.completions.create(model="default[]", messages=[], timeout=5,
                                                      stream=operation == "stream", extra_body=ACK)
            if operation == "stream":
                assert "".join(c.choices[0].delta.content or "" for c in response) == "pong"
            else:
                assert response.choices[0].message.content == "pong"
        assert not live_pids(journal)
        assert not client._sessions
    finally:
        for proc in reversed(live_pids(journal)):
            proc.kill()
        client.close()


@pytest.mark.parametrize("action", ["timeout", "cancel", "close"])
def test_stopped_reader_cannot_block_deadline_or_shutdown(tmp_path, action):
    journal = tmp_path / "blocked.jsonl"
    client = CursorACPClient(command=sys.executable, args=[str(PEER), "stopped-reader", str(journal)], acp_cwd=str(tmp_path))
    done, stopped = threading.Event(), threading.Event()
    errors = []

    def request():
        try:
            client.chat.completions.create(model="default[]", messages=[{"role": "user", "content": "x" * 1000000}],
                                           timeout=2 if action == "timeout" else 60, extra_body=ACK)
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()

    def stop():
        try:
            getattr(client, action)()
        finally:
            stopped.set()

    worker = threading.Thread(target=request, daemon=True)
    worker.start()
    stopper = None
    try:
        assert wait_for(lambda: any(r.get("ready") for r in rows(journal)))
        if action != "timeout":
            stopper = threading.Thread(target=stop, daemon=True)
            stopper.start()
            assert stopped.wait(6), "shutdown blocked behind a full stdin pipe"
        assert done.wait(8), "request deadline blocked behind a full stdin pipe"
        assert len(errors) == 1 and isinstance(errors[0], TimeoutError if action == "timeout" else InterruptedError)
        assert not live_pids(journal)
    finally:
        for proc in reversed(live_pids(journal)):
            proc.kill()
        worker.join(5)
        if stopper:
            stopper.join(5)
        client.close()


@pytest.mark.parametrize("error_type", [PermissionError, ProcessLookupError])
def test_group_signal_error_requires_descendant_reprobe(tmp_path, monkeypatch, error_type):
    from agent.cursor_acp_transport import CursorCleanupError, CursorSession

    journal = tmp_path / "group.jsonl"
    session = CursorSession(sys.executable, [str(PEER), "tree-stall", str(journal)], str(tmp_path), 30)
    killpg = os.killpg
    probes = []

    def fail_term(pgid, sig):
        if pgid == session._pgid:
            probes.append(sig)
            if sig == signal.SIGTERM:
                raise error_type("fixture group signal failure")
        return killpg(pgid, sig)

    try:
        session.send(id=1, method="session/prompt", params={})
        assert wait_for(lambda: len([r for r in rows(journal) if "pid" in r]) == 3)
        monkeypatch.setattr(os, "killpg", fail_term)
        with pytest.raises(CursorCleanupError):
            session.close()
        assert 0 in probes[probes.index(signal.SIGTERM) + 1:]
        assert signal.SIGKILL in probes
        assert not live_pids(journal)
        assert session.process.poll() is not None
        assert not session._reader.is_alive()
    finally:
        monkeypatch.setattr(os, "killpg", killpg)
        for proc in reversed(live_pids(journal)):
            proc.kill()
        session.close()
