"""The legacy stdio entry scrubs inherited session markers only for a Desktop-owned backend
(fleet carry G20), and never the delegated-child board fence."""
from __future__ import annotations

import pytest

import hermes_cli.process_identity as process_identity
from tui_gateway import entry

_MARKERS = {"HERMES_SESSION_ID": "s-parent", "HERMES_CRON_SESSION": "1", "HERMES_SUPERVISED_CHILD": "1"}
_FENCE = "HERMES_DELEGATED_CHILD_CONTEXT"


def _run_main(monkeypatch, *, desktop_owned):
    for key, value in _MARKERS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(_FENCE, "fence-token")
    monkeypatch.setattr(process_identity, "is_desktop_owned_backend", lambda argv=None: desktop_owned)
    monkeypatch.setattr(entry, "_claim_launch_home", lambda: None)
    monkeypatch.setattr(entry, "_log_exit", lambda reason: None)
    with pytest.raises(SystemExit):
        entry.main()


def test_desktop_owned_entry_drops_inherited_markers_and_keeps_board_fence(monkeypatch):
    import os

    _run_main(monkeypatch, desktop_owned=True)
    assert not any(key in os.environ for key in _MARKERS)
    assert os.environ[_FENCE] == "fence-token"


def test_non_desktop_entry_keeps_its_session_identity(monkeypatch):
    import os

    _run_main(monkeypatch, desktop_owned=False)
    assert all(os.environ.get(key) == value for key, value in _MARKERS.items())
    assert os.environ[_FENCE] == "fence-token"
