"""Review regressions for spaced artifacts, conflicting checkouts, and walk errors.

The publisher-identity case is a control: that token is already non-path and
must keep accepting the real local file.
"""
import errno
import hashlib
import json
import tarfile
from pathlib import Path

import pytest

from hermes_cli import kanban_bundle_preserve as bp
from hermes_cli import kanban_db as kb
from tests.hermes_cli.test_kanban_bundle_preserve import board, contents, task

_PREFIX = b"prefix-only\n"
_SPACED = b"spaced-final-bytes\n"
_PATCH = b"diff accepted\n"
_SLUG = "owner/repo"
_PUBLISHER = "gpt-6.1-sol/openai-codex"


def _run(conn, tid, metadata, result=None):
    if result is not None:
        conn.execute("UPDATE tasks SET result=? WHERE id=?", (result, tid))
    conn.execute(
        "INSERT INTO task_runs(task_id,status,started_at,metadata) VALUES(?,'completed',0,?)",
        (tid, json.dumps(metadata)),
    )
    conn.commit()


def _paths(report):
    return [entry["path"] for entry in report["files"]]


def _payloads(conn, tid):
    archived = contents(conn, tid)
    archive = next(
        Path(item.stored_path)
        for item in kb.list_attachments(conn, tid)
        if item.filename.endswith(".tgz")
    )
    with tarfile.open(archive) as bundle:
        return {name: bundle.extractfile(name).read() for name in archived}


def _fail_evidence_walk(monkeypatch):
    real = bp.os.scandir

    def scandir(path):
        if Path(path).name == "evidence":
            error = OSError(errno.EIO, "input/output error")
            error.filename = str(path)
            raise error
        return real(path)

    monkeypatch.setattr(bp.os, "scandir", scandir)


def test_spaced_absolute_artifact_archives_exact_bytes(board):
    tid, root = task(board, marker=False)
    (root / "report").write_bytes(_PREFIX)
    spaced = root / "report final.md"
    spaced.write_bytes(_SPACED)
    _run(board, tid, {"artifacts": [str(spaced)]}, result="accepted.patch")
    report = bp.preserve_bundle(board, tid, root)
    assert _paths(report) == ["accepted.patch", "report final.md"]
    assert _payloads(board, tid) == {
        "accepted.patch": _PATCH,
        "report final.md": _SPACED,
    }
    assert "report" not in _paths(report)
    assert (root / "report").read_bytes() == _PREFIX
    assert bp.preservation_gate(tid, root, conn=board) is True


def test_missing_spaced_absolute_artifact_does_not_select_prefix(board):
    tid, root = task(board, marker=False)
    (root / "report").write_bytes(_PREFIX)
    missing = root / "report final.md"
    assert not missing.exists()
    _run(board, tid, {"artifacts": [str(missing)]}, result="accepted.patch")
    with pytest.raises(ValueError):
        bp.plan_bundle(board, tid, root)
    assert bp.preservation_gate(tid, root, conn=board) is False
    assert root.exists()
    assert (root / "report").read_bytes() == _PREFIX
    assert not kb.list_attachments(board, tid)


def test_conflicting_checkouts_leave_extensionless_owner_repo_ambiguous(board):
    tid, root = task(board, marker=False)
    workspace_bytes = b"workspace-owner\n"
    checkout_a = b"checkout-a-owner\n"
    checkout_b = b"checkout-b-owner\n"
    (root / "checkout-a" / _SLUG).parent.mkdir(parents=True)
    (root / "checkout-b" / _SLUG).parent.mkdir(parents=True)
    (root / _SLUG).parent.mkdir(parents=True)
    (root / _SLUG).write_bytes(workspace_bytes)
    (root / "checkout-a" / _SLUG).write_bytes(checkout_a)
    (root / "checkout-b" / _SLUG).write_bytes(checkout_b)
    _run(board, tid, {"worktree": str(root / "checkout-a")})
    _run(board, tid, {"worktree": str(root / "checkout-b")}, result=_SLUG)
    plan = bp.plan_bundle(board, tid, root)
    assert _paths(plan) == []
    assert plan["ambiguous_refs"] == [_SLUG]
    assert _SLUG not in plan["unresolved_refs"]
    assert bp.preservation_gate(tid, root, conn=board) is False
    assert not kb.list_attachments(board, tid)
    assert (root / _SLUG).read_bytes() == workspace_bytes
    assert (root / "checkout-a" / _SLUG).read_bytes() == checkout_a
    assert (root / "checkout-b" / _SLUG).read_bytes() == checkout_b


def test_conflicting_checkout_only_owner_repo_is_not_a_missing_slug(board):
    tid, root = task(board, marker=False)
    checkout_a = b"checkout-a-only\n"
    checkout_b = b"checkout-b-only\n"
    (root / "checkout-a" / _SLUG).parent.mkdir(parents=True)
    (root / "checkout-b" / _SLUG).parent.mkdir(parents=True)
    (root / "checkout-a" / _SLUG).write_bytes(checkout_a)
    (root / "checkout-b" / _SLUG).write_bytes(checkout_b)
    assert not (root / _SLUG).exists()
    _run(board, tid, {"worktree": str(root / "checkout-a")})
    _run(board, tid, {"worktree": str(root / "checkout-b")}, result=_SLUG)
    plan = bp.plan_bundle(board, tid, root)
    assert _paths(plan) == []
    assert plan["ambiguous_refs"] == [_SLUG]
    assert _SLUG not in plan["unresolved_refs"]
    assert _SLUG not in plan["unresolved_non_path_refs"]
    assert bp.preservation_gate(tid, root, conn=board) is False
    assert not kb.list_attachments(board, tid)
    assert (root / "checkout-a" / _SLUG).read_bytes() == checkout_a
    assert (root / "checkout-b" / _SLUG).read_bytes() == checkout_b


def test_keep_bundle_walk_eio_fails_closed(board, monkeypatch):
    tid, root = task(board, marker=True)
    required = root / "evidence" / "required.md"
    required.parent.mkdir()
    required.write_bytes(b"required-evidence\n")
    _fail_evidence_walk(monkeypatch)
    with pytest.raises(ValueError):
        bp.plan_bundle(board, tid, root)
    assert bp.preservation_gate(tid, root, conn=board) is False
    assert root.exists()
    assert required.read_bytes() == b"required-evidence\n"
    assert not kb.list_attachments(board, tid)


def test_explicit_directory_walk_eio_fails_closed(board, monkeypatch):
    tid, root = task(board, marker=False)
    evidence = root / "evidence"
    required = evidence / "required.md"
    evidence.mkdir()
    required.write_bytes(b"required-evidence\n")
    board.execute(
        "UPDATE tasks SET result=? WHERE id=?",
        (f"{evidence}\naccepted.patch", tid),
    )
    board.commit()
    _fail_evidence_walk(monkeypatch)
    with pytest.raises(ValueError):
        bp.plan_bundle(board, tid, root)
    assert bp.preservation_gate(tid, root, conn=board) is False
    assert root.exists()
    assert required.read_bytes() == b"required-evidence\n"
    assert (root / "accepted.patch").read_bytes() == _PATCH
    assert not kb.list_attachments(board, tid)


def test_publisher_identity_keeps_accepted_patch(board):
    tid, root = task(board, marker=False, result=f"accepted.patch\n{_PUBLISHER}")
    _run(board, tid, {"note": _PUBLISHER})
    plan = bp.plan_bundle(board, tid, root)
    assert _paths(plan) == ["accepted.patch"]
    assert plan["files"][0]["sha256"] == hashlib.sha256(_PATCH).hexdigest()
    assert _PUBLISHER not in _paths(plan)
    assert bp.preservation_gate(tid, root, conn=board) is True
    assert _payloads(board, tid) == {"accepted.patch": _PATCH}
