"""Desktop /stop owns only the calling session's background processes."""

from types import SimpleNamespace

import pytest

from tools.process_registry import ProcessRegistry
from tui_gateway.methods_slash import _mirror_stop


@pytest.mark.platforms("posix")
def test_desktop_stop_only_kills_own_session(monkeypatch):
    import tools.process_registry as module

    registry = ProcessRegistry()
    monkeypatch.setattr(module, "process_registry", registry)
    mine = registry.spawn_local("sleep 20", session_key="session-a")
    other = registry.spawn_local("sleep 20", session_key="session-b")
    try:
        assert _mirror_stop("ui-a", {"session_key": "session-a"}, None, "") == ""
        assert registry.wait(mine.id, timeout=3)["status"] == "exited"
        assert registry.poll(other.id)["status"] == "running"
        assert "could not identify" in _mirror_stop("ui-a", {}, None, "")
        assert registry.poll(other.id)["status"] == "running"
        old_key = registry.spawn_local("sleep 20", session_key="before-compression", owner_task_id="turn-a")
        assert _mirror_stop("ui-a", {"session_key": "after-compression"},
                            SimpleNamespace(_process_owner_task_ids={"turn-a"}), "") == ""
        assert registry.wait(old_key.id, timeout=3)["status"] == "exited"
        assert registry.poll(other.id)["status"] == "running"
    finally:
        registry.kill_all()
