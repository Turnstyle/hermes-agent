"""Generic workspace-relative compatibility under the pending checkout ruling.

These tests state that ruling. They do not change production code or the
checkout-ref tests. Expected results against base 122b2829, from the code each
test calls:

- workspace regular files without a checkout: pass (today's workspace resolve)
- recorded checkout used only when the workspace file is absent: fail (worktree
  is not a source root on that base)
- both copies present: fail (the workspace copy is covered and removal is
  authorized)
- symlink component and fifo: pass (already rejected before read)
- parent escape: fail (the escape is skipped and the other local file is kept)

The current draft's file-like ambiguity policy is a separate collision. It is
not what these tests assert.

URL and owner/repo cases below are separate. A metadata URL or a missing slug
already produces no missing-member error. Assertions on
``unresolved_non_path_refs`` and ``bundle_non_path_refs`` are the new
reporting. An existing extensionless owner/repo file in the workspace stays a
file. A missing ``findings[].file`` under a recorded checkout still raises.
"""
import hashlib
import json
import os
import stat
import tarfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from hermes_cli import kanban_bundle_preserve as bp
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as ws
from tests.hermes_cli.test_kanban_bundle_preserve import board, contents, task


def _record(conn, tid, metadata, result=None):
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
    names = contents(conn, tid)
    archive = next(
        Path(item.stored_path)
        for item in kb.list_attachments(conn, tid)
        if item.filename.endswith(".tgz")
    )
    with tarfile.open(archive) as bundle:
        return {name: bundle.extractfile(name).read() for name in names}


def test_workspace_relative_files_preserve_without_checkout(board):
    """Regular workspace files stay covered when no checkout is recorded."""
    tid, root = task(board, marker=False)
    report_bytes = b"accepted report\n"
    (root / "bundle").mkdir()
    (root / "bundle" / "report.md").write_bytes(report_bytes)
    missing = "hermes_cli/missing_module.py"
    board.execute(
        "UPDATE tasks SET result=? WHERE id=?",
        (f"accepted.patch\nbundle/report.md\n{missing}", tid),
    )
    board.commit()
    plan = bp.plan_bundle(board, tid, root)
    assert _paths(plan) == ["accepted.patch", "bundle/report.md"]
    assert {entry["path"]: entry["sha256"] for entry in plan["files"]} == {
        "accepted.patch": hashlib.sha256(b"diff accepted\n").hexdigest(),
        "bundle/report.md": hashlib.sha256(report_bytes).hexdigest(),
    }
    assert plan["unresolved_refs"] == [missing]
    assert not plan.get("ambiguous_refs")
    assert bp.preservation_gate(tid, root, conn=board) is True
    ws._cleanup_workspace(board, tid)
    assert not root.exists()
    assert _payloads(board, tid) == {
        "accepted.patch": b"diff accepted\n",
        "bundle/report.md": report_bytes,
    }


def test_absent_workspace_file_uses_recorded_checkout(board):
    """A miss in the workspace falls through to the recorded checkout."""
    tid, root = task(board, marker=False)
    payload = b"checkout-only-report\n"
    relative = "bundle/report.md"
    (root / "head" / "bundle").mkdir(parents=True)
    (root / "head" / relative).write_bytes(payload)
    assert not (root / relative).exists()
    _record(board, tid, {"worktree": str(root / "head")}, result=relative)
    plan = bp.plan_bundle(board, tid, root)
    assert _paths(plan) == ["head/bundle/report.md"]
    assert plan["files"][0]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert not plan.get("ambiguous_refs")
    assert bp.preservation_gate(tid, root, conn=board) is True
    assert _payloads(board, tid) == {"head/bundle/report.md": payload}


@pytest.mark.parametrize("same_bytes", [False, True], ids=["bytes", "path"])
def test_workspace_and_checkout_copies_disagree_are_ambiguous(board, same_bytes):
    """Different bytes, or the same bytes at two path identities, retain."""
    tid, root = task(board, marker=False)
    relative = "bundle/report.md"
    workspace_bytes = b"same-copy\n" if same_bytes else b"workspace-copy\n"
    checkout_bytes = b"same-copy\n" if same_bytes else b"checkout-copy\n"
    (root / "bundle").mkdir()
    (root / "bundle" / "report.md").write_bytes(workspace_bytes)
    (root / "head" / "bundle").mkdir(parents=True)
    (root / "head" / relative).write_bytes(checkout_bytes)
    _record(board, tid, {"worktree": str(root / "head")}, result=relative)
    plan = bp.plan_bundle(board, tid, root)
    assert _paths(plan) == []
    assert plan["ambiguous_refs"] == [relative]
    assert relative not in plan["unresolved_refs"]
    assert bp.preservation_gate(tid, root, conn=board) is False
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert (root / relative).read_bytes() == workspace_bytes
    assert (root / "head" / relative).read_bytes() == checkout_bytes
    assert not kb.list_attachments(board, tid)


def test_workspace_relative_symlink_component_is_rejected(board):
    tid, root = task(board, marker=False)
    real = root / "real"
    real.mkdir()
    (real / "report.md").write_bytes(b"linked-report\n")
    (root / "bundle").symlink_to(real, target_is_directory=True)
    board.execute("UPDATE tasks SET result=? WHERE id=?", ("bundle/report.md", tid))
    board.commit()
    with pytest.raises(ValueError):
        bp.plan_bundle(board, tid, root)
    assert bp.preservation_gate(tid, root, conn=board) is False
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert (root / "bundle").is_symlink()
    assert not kb.list_attachments(board, tid)


def test_workspace_relative_parent_escape_is_rejected(board):
    tid, root = task(board, marker=False)
    outside = root.parent / "outside" / "payload.txt"
    outside.parent.mkdir()
    outside.write_bytes(b"escaped\n")
    board.execute(
        "UPDATE tasks SET result=? WHERE id=?",
        ("../outside/payload.txt\naccepted.patch", tid),
    )
    board.commit()
    plan = bp.plan_bundle(board, tid, root)
    assert [entry["path"] for entry in plan["files"]] == ["accepted.patch"]
    assert plan["files"][0]["sha256"] == hashlib.sha256(b"diff accepted\n").hexdigest()
    assert plan["ambiguous_refs"] == ["../outside/payload.txt"]
    assert plan["unresolved_refs"] == []
    assert "payload.txt" not in [entry["path"] for entry in plan["files"]]
    assert bp.preservation_gate(tid, root, conn=board) is False
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert (root / "accepted.patch").read_bytes() == b"diff accepted\n"
    assert outside.read_bytes() == b"escaped\n"
    assert not kb.list_attachments(board, tid)


def test_workspace_relative_fifo_is_rejected_without_open(board):
    tid, root = task(board, marker=False)
    pipe = root / "bundle" / "queue.pipe"
    pipe.parent.mkdir()
    os.mkfifo(pipe)
    board.execute("UPDATE tasks SET result=? WHERE id=?", ("bundle/queue.pipe", tid))
    board.commit()
    with pytest.raises(ValueError):
        bp.plan_bundle(board, tid, root)
    assert bp.preservation_gate(tid, root, conn=board) is False
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert stat.S_ISFIFO(os.lstat(pipe).st_mode)
    assert not kb.list_attachments(board, tid)


def _sanitized_uri(url: str) -> str:
    """Drop userinfo, query, and fragment. The scheme, host, and path remain."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


_METADATA_URLS = [
    "https://github.com/Turnstyle/conductive-admin/pull/6360",
    "custom+git://host/bundle/report.md",
    "file:///bundle/report.md",
    "https://fixture-user:fixture-pass@github.com/example/repo/blob/main/README.md?token=fixture-query-token#L4",
]
_URL_IDS = ["https", "custom-git", "file", "redacted"]
_OWNER_REPO = "Turnstyle/conductive-admin"


def _url_metadata(url: str) -> dict:
    return {"ci": {"url": url}, "comment_url": url}


def _plant_local_fragments(root):
    (root / "bundle").mkdir()
    (root / "bundle" / "report.md").write_bytes(b"local-report\n")
    (root / "README.md").write_bytes(b"local-readme\n")


def _non_path_events(conn, tid):
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='bundle_non_path_refs'",
        (tid,),
    ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


@pytest.mark.parametrize("url", _METADATA_URLS, ids=_URL_IDS)
def test_metadata_url_does_not_select_local_fragment_or_raise(board, url):
    """A run-metadata URL is already non-mandatory and selects no local file."""
    tid, root = task(board, marker=False)
    _plant_local_fragments(root)
    _record(board, tid, _url_metadata(url))
    plan = bp.plan_bundle(board, tid, root)
    assert plan["files"] == []
    assert plan["unresolved_refs"] == []
    assert url not in plan["unresolved_refs"]
    assert bp.preservation_gate(tid, root, conn=board) is False
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert (root / "bundle" / "report.md").read_bytes() == b"local-report\n"
    assert (root / "README.md").read_bytes() == b"local-readme\n"
    assert not kb.list_attachments(board, tid)


@pytest.mark.parametrize("url", _METADATA_URLS, ids=_URL_IDS)
def test_metadata_url_is_recorded_as_sanitized_non_path(board, url):
    """The sanitized URI is a new report field and its own event."""
    tid, root = task(board, marker=False)
    _plant_local_fragments(root)
    _record(board, tid, _url_metadata(url))
    stored = _sanitized_uri(url)
    report = bp.preserve_bundle(board, tid, root)
    assert report["files"] == []
    assert report["unresolved_refs"] == []
    assert report["unresolved_non_path_refs"] == [stored]
    assert _non_path_events(board, tid) == [{"unresolved_non_path_refs": [stored]}]
    blob = json.dumps(report) + json.dumps(_non_path_events(board, tid))
    for secret in ("fixture-user", "fixture-pass", "fixture-query-token"):
        if secret in url:
            assert secret not in blob


def test_missing_owner_repo_slug_is_not_a_missing_member(board):
    """A missing owner/repo slug does not raise. It stays an ordinary unresolved ref."""
    tid, root = task(board, marker=False)
    _record(board, tid, {"repo": _OWNER_REPO})
    plan = bp.plan_bundle(board, tid, root)
    assert plan["files"] == []
    assert _OWNER_REPO in plan["unresolved_refs"]
    assert bp.preservation_gate(tid, root, conn=board) is False
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert not kb.list_attachments(board, tid)


def test_missing_owner_repo_slug_is_recorded_as_non_path(board):
    """The slug remains in unresolved_refs and is also recorded as non-path."""
    tid, root = task(board, marker=False)
    _record(board, tid, {"repo": _OWNER_REPO})
    report = bp.preserve_bundle(board, tid, root)
    assert _OWNER_REPO in report["unresolved_refs"]
    assert report["unresolved_non_path_refs"] == [_OWNER_REPO]
    assert _non_path_events(board, tid) == [{"unresolved_non_path_refs": [_OWNER_REPO]}]


@pytest.mark.parametrize("location", ["workspace", "checkout"])
def test_existing_extensionless_owner_repo_file_is_preserved(board, location):
    """A real extensionless owner/repo file is archived, not dropped as a slug."""
    tid, root = task(board, marker=False)
    payload = b"owner-repo-file\n"
    base = root / "head" if location == "checkout" else root
    path = base / _OWNER_REPO
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    metadata = {"repo": _OWNER_REPO}
    if location == "checkout":
        metadata["worktree"] = str(root / "head")
        assert not (root / _OWNER_REPO).exists()
    _record(board, tid, metadata)
    member = f"head/{_OWNER_REPO}" if location == "checkout" else _OWNER_REPO
    plan = bp.plan_bundle(board, tid, root)
    assert [entry["path"] for entry in plan["files"]] == [member]
    assert plan["files"][0]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert _OWNER_REPO not in plan["unresolved_refs"]
    assert _OWNER_REPO not in plan.get("unresolved_non_path_refs", [])
    assert bp.preservation_gate(tid, root, conn=board) is True
    assert _payloads(board, tid) == {member: payload}


def test_owner_repo_symlink_is_not_treated_as_non_path(board):
    """A repo value that lands on a symlink is still refused."""
    tid, root = task(board, marker=False)
    real = root / "real-owner"
    real.write_bytes(b"linked-owner\n")
    link = root / _OWNER_REPO
    link.parent.mkdir()
    link.symlink_to(real)
    _record(board, tid, {"repo": _OWNER_REPO})
    with pytest.raises(ValueError):
        bp.plan_bundle(board, tid, root)
    assert bp.preservation_gate(tid, root, conn=board) is False
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert link.is_symlink()
    assert not kb.list_attachments(board, tid)


def test_missing_typed_finding_file_fails_closed(board):
    """A missing findings[].file under the recorded checkout is not optional."""
    tid, root = task(board, marker=False)
    head = root / "head"
    head.mkdir()
    assert not (head / "bundle" / "missing.patch").exists()
    assert not (root / "bundle" / "missing.patch").exists()
    _record(
        board,
        tid,
        {"worktree": str(head), "findings": [{"file": "bundle/missing.patch"}]},
        result=str(root / "accepted.patch"),
    )
    with pytest.raises(ValueError):
        bp.plan_bundle(board, tid, root)
    assert bp.preservation_gate(tid, root, conn=board) is False
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert (root / "accepted.patch").read_bytes() == b"diff accepted\n"
    assert not kb.list_attachments(board, tid)
