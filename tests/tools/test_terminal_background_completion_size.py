"""A background process whose spawner asks for a larger completion (a bot DM's reply) gets that size even
when it exits the instant its notify flag flips.

``_publish_finished`` reads ``notify_on_complete`` and then sizes the output from
``completion_output_chars``. Setting the flag first left a window in which a fast exit published the
default tail, truncating a reply that the spawner had asked to receive whole.
"""

import json
from types import SimpleNamespace

from tools import terminal_tool, terminal_tool_background as background


class _RacingSession(SimpleNamespace):
    """Records what an exit observed at the moment the notify flag becomes true."""

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name == "notify_on_complete" and value:
            super().__setattr__("size_seen_by_exit", self.completion_output_chars)


def test_completion_size_is_set_before_the_notify_flag(monkeypatch, tmp_path):
    proc = _RacingSession(id="proc_reply", pid=1, watcher_platform="", completion_output_chars=0,
                          notify_on_complete=False, watch_patterns=[])
    monkeypatch.setattr(terminal_tool, "_resolve_command_cwd", lambda **kw: str(tmp_path))
    monkeypatch.setattr(background, "_spawn", lambda *a, **kw: proc)
    monkeypatch.setattr(background, "_apply_async_support", lambda s, r, notify, patterns: (notify, patterns))

    result = json.loads(background.spawn_background_process(
        command="runner", env=SimpleNamespace(), env_type="local", effective_task_id="parent", task_id="parent",
        session_key="", workdir=None, cwd=str(tmp_path), effective_pty=False, notify_on_complete=True,
        watch_patterns=None, approval_note=None, pty_disabled_reason=None, completion_output_chars=24_000))

    assert result["notify_on_complete"] is True
    assert proc.completion_output_chars == 24_000
    assert proc.size_seen_by_exit == 24_000
