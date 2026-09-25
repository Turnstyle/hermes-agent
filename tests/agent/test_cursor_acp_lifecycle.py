"""Real subprocess contracts for cleanup and cancellation, including inherited pipes."""
import json
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
