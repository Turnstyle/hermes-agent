"""KEEP-BUNDLE probe failures must not partially archive and delete."""
import errno
import os

import pytest

from hermes_cli import kanban_bundle_preserve as bp
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as ws
from tests.hermes_cli import test_kanban_bundle_preserve as fixtures
from tests.hermes_cli.test_kanban_bundle_preserve import contents, task

board = fixtures.board


def _workspace(board, result="`accepted.patch`"):
    tid, root = task(board, marker=False, result=result)
    (root / "other.txt").write_text("other deliverable\n", encoding="utf-8")
    assert kb.list_attachments(board, tid) == []
    return tid, root


def _assert_retained(board, tid, root):
    ws._cleanup_workspace(board, tid)
    assert kb.list_attachments(board, tid) == []
    assert root.is_dir()
    assert (root / "other.txt").read_text(encoding="utf-8") == "other deliverable\n"
    assert (root / "accepted.patch").read_text(encoding="utf-8") == "diff accepted\n"
    with pytest.raises(ValueError, match="keep marker probe failed"):
        bp.plan_bundle(board, tid, root)
    assert bp.preservation_gate(tid, root, conn=board) is False


def _deny_keep(monkeypatch, kind):
    marker_name = bp.KEEP_MARKER
    if kind == "lstat":
        real = os.stat

        def denied(path, *args, **kwargs):
            if str(path).endswith(marker_name):
                raise PermissionError(13, "denied")
            return real(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", denied)
        return
    if kind == "open":
        real = os.open

        def denied(path, flags, *args, **kwargs):
            if not isinstance(path, int) and str(path).endswith(marker_name):
                raise PermissionError(13, "denied")
            return real(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", denied)
        return
    real_open = os.open
    opened = set()

    def tracking(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if not isinstance(path, int) and str(path).endswith(marker_name):
            opened.add(fd)
        return fd

    real_read = os.read

    def eio(fd, size, *args, **kwargs):
        if fd in opened:
            raise OSError(errno.EIO, "read error")
        return real_read(fd, size, *args, **kwargs)

    monkeypatch.setattr(os, "open", tracking)
    monkeypatch.setattr(os, "read", eio)


@pytest.mark.parametrize("kind", [
    "lstat",
    "open",
    "read",
    "symlink",
    "chmod",
    "directory",
    "oversize",
    "missing-ref",
])
def test_failed_keep_does_not_partially_archive(board, monkeypatch, kind):
    result = "`accepted.patch`"
    if kind == "missing-ref":
        # Combination only: a missing relative name does not by itself retain.
        # Paired with a failed KEEP probe, the valid reference must not be
        # registered alone.
        result = "`accepted.patch` `missing-deliverable.txt`"
    tid, root = _workspace(board, result=result)
    marker = root / bp.KEEP_MARKER
    if kind in {"lstat", "open", "read", "chmod", "oversize"}:
        if kind == "oversize":
            limit = getattr(bp, "KEEP_MARKER_MAX_BYTES", 4096)
            marker.write_bytes(b"k" * (limit + 1))
        else:
            marker.write_text("keep\n", encoding="utf-8")
        if kind == "chmod":
            os.chmod(marker, 0)
        elif kind in {"lstat", "open", "read"}:
            _deny_keep(monkeypatch, kind)
    elif kind == "symlink":
        marker.symlink_to(root / "accepted.patch")
    elif kind == "directory":
        marker.mkdir()
    else:
        marker.symlink_to(root / "accepted.patch")
    try:
        _assert_retained(board, tid, root)
    finally:
        if kind == "chmod" and marker.is_file() and not marker.is_symlink():
            os.chmod(marker, 0o644)


def test_readable_keep_archives_whole_tree(board):
    tid, root = _workspace(board, result="`accepted.patch`")
    (root / bp.KEEP_MARKER).write_text("keep\n", encoding="utf-8")
    bp.preserve_bundle(board, tid, root)
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert contents(board, tid) == ["KEEP-BUNDLE", "accepted.patch", "other.txt"]


def test_absent_keep_preserves_referenced_selection(board):
    tid, root = _workspace(board, result="`accepted.patch`")
    bp.preserve_bundle(board, tid, root)
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert contents(board, tid) == ["accepted.patch"]


def _retain_bytes(board, tid, root):
    ws._cleanup_workspace(board, tid)
    assert kb.list_attachments(board, tid) == []
    assert (root / "other.txt").read_text(encoding="utf-8") == "other deliverable\n"
    assert (root / "accepted.patch").read_text(encoding="utf-8") == "diff accepted\n"


def _missing_absolute_result(board, tid, root):
    missing = root / "missing-deliverable.txt"
    with kb.write_txn(board):
        board.execute(
            "UPDATE tasks SET result=? WHERE id=?",
            (f"`accepted.patch` `{missing}`", tid),
        )


def test_missing_absolute_with_absent_keep_retains(board):
    tid, root = _workspace(board)
    _missing_absolute_result(board, tid, root)
    _retain_bytes(board, tid, root)


def test_missing_absolute_with_readable_keep_retains(board):
    tid, root = _workspace(board)
    (root / bp.KEEP_MARKER).write_text("keep\n", encoding="utf-8")
    _missing_absolute_result(board, tid, root)
    _retain_bytes(board, tid, root)


def test_keep_at_exact_cap_archives_whole_tree(board):
    tid, root = _workspace(board, result="`accepted.patch`")
    limit = getattr(bp, "KEEP_MARKER_MAX_BYTES", 4096)
    marker = root / bp.KEEP_MARKER
    marker.write_bytes(b"k" * limit)
    os.chmod(marker, 0o644)
    bp.preserve_bundle(board, tid, root)
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert contents(board, tid) == ["KEEP-BUNDLE", "accepted.patch", "other.txt"]
