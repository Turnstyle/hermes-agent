"""Point the interrupted-update marker lookup at the test's tmp dir.

``import hermes_bootstrap`` / ``hermes_cli.main`` run ``restore_interrupted_pull`` at import, which
stats ``<git dir>/hermes-interrupted-pull``. In a linked worktree whose common git dir lives under
the operator's real Hermes home, that stat trips the real-home I/O guard the first time a test
lazily imports an entry-point module — a topology failure, not a behaviour one. Only that one
metadata lookup moves; the guard and the recovery logic stay as they are.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def isolated_update_recovery_metadata(tmp_path, monkeypatch):
    from hermes_cli import _early_recovery

    git_dir = tmp_path / "update-recovery-git-dir"
    git_dir.mkdir()
    monkeypatch.setattr(
        _early_recovery, "interrupted_pull_marker",
        lambda root: git_dir / _early_recovery.INTERRUPTED_PULL_MARKER,
    )
    return git_dir
