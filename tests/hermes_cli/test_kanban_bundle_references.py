"""Regression for remote and ambiguous relative bundle references."""
import json

import pytest

from hermes_cli import kanban_bundle_preserve as bp
from hermes_cli import kanban_db as kb
from tests.hermes_cli.test_kanban_bundle_preserve import board, contents, task


@pytest.mark.parametrize("location", ["result", "summary", "metadata"])
def test_remote_and_repo_references_preserve_local_artifacts(board, location):
    tid, root = task(board, marker=False)
    (root / "bundle").mkdir()
    (root / "bundle" / "report.md").write_text("accepted report")
    references = [
        "https://github.com/Turnstyle/hermes-agent/pull/2",
        "Turnstyle/hermes-agent",
        "fix/kanban-startup-t_3d24ef28",
        "review-base/e06df9c2-t_3d24ef28",
        "work/t_3d24ef28",
        "hermes_cli/kanban_db_connect.py",
        "bundle/report.md",
        "accepted.patch",
    ]
    if location == "result":
        board.execute("UPDATE tasks SET result=? WHERE id=?", ("\n".join(references), tid))
    else:
        board.execute(
            "INSERT INTO task_runs(task_id,status,started_at,summary,metadata) VALUES(?,'completed',0,?,?)",
            (tid, "\n".join(references) if location == "summary" else None,
             json.dumps({"nested": {"references": references}}) if location == "metadata" else None),
        )
    board.commit()
    report = bp.preserve_bundle(board, tid, root)
    assert [entry["path"] for entry in report["files"]] == ["accepted.patch", "bundle/report.md"]
    assert report["unresolved_refs"] == sorted(references[1:6])
    assert contents(board, tid) == ["accepted.patch", "bundle/report.md"]
    events = board.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='bundle_unresolved_refs'", (tid,),
    ).fetchall()
    assert [json.loads(row["payload"]) for row in events] == [
        {"unresolved_refs": sorted(references[1:6])}
    ]


@pytest.mark.parametrize("url", [
    "https://github.com/owner/repo/blob/main/accepted.patch?raw=1#L4",
    "custom+git://host/bundle/report.md",
    "file:///bundle/report.md",
])
def test_urls_never_select_local_fragments(board, url):
    tid, root = task(board, marker=False)
    (root / "bundle").mkdir()
    (root / "bundle" / "report.md").write_text("not referenced locally")
    board.execute("UPDATE tasks SET result=? WHERE id=?", (f"See [remote]({url}).", tid))
    board.commit()
    report = bp.preserve_bundle(board, tid, root, dry_run=True)
    assert report["files"] == []
    assert report["unresolved_refs"] == []


@pytest.mark.parametrize("name", ["secrets.json", "dangling.patch", "linked.patch"])
def test_relative_metadata_references_keep_safety_checks(board, name):
    tid, root = task(board, marker=False)
    (root / "bundle").mkdir()
    path = root / "bundle" / name
    if name == "secrets.json":
        path.write_text("excluded fixture")
    elif name == "dangling.patch":
        path.symlink_to(root / "gone.patch")
    elif name == "linked.patch":
        path.symlink_to(root / "accepted.patch")
    board.execute(
        "INSERT INTO task_runs(task_id,status,started_at,metadata) VALUES(?,'completed',0,?)",
        (tid, json.dumps({"artifact": "bundle/" + name})),
    )
    board.commit()
    with pytest.raises(ValueError):
        bp.preserve_bundle(board, tid, root, dry_run=True)
    assert not kb.list_attachments(board, tid)


@pytest.mark.parametrize("reference", ["./gone.patch", "See ./gone.patch", "../{root}/gone.patch"])
def test_missing_relative_reference_is_unresolved(board, reference):
    tid, root = task(board, marker=False)
    reference = reference.format(root=root.name)
    board.execute("UPDATE tasks SET result=? WHERE id=?", (reference, tid))
    board.commit()
    report = bp.preserve_bundle(board, tid, root, dry_run=True)
    assert report["unresolved_refs"] == ["gone.patch"]
    assert report["files"] == []
    assert root.exists()
    assert not kb.list_attachments(board, tid)


def test_worktree_context_does_not_select_checkout(board):
    tid, root = task(board, marker=False)
    checkout = root / "source"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    (checkout / "secrets.json").write_text("excluded fixture")
    environment = root / "testenv"
    environment.mkdir()
    (environment / "package.py").write_text("not an artifact")
    board.execute(
        "INSERT INTO task_runs(task_id,status,started_at,metadata) VALUES(?,'completed',0,?)",
        (tid, json.dumps({"worktree": str(checkout), "artifacts": [str(root / "accepted.patch")]})),
    )
    board.commit()
    report = bp.preserve_bundle(board, tid, root, dry_run=True)
    assert [entry["path"] for entry in report["files"]] == ["accepted.patch"]
    assert report["unresolved_refs"] == []
    assert root.exists()
    assert not kb.list_attachments(board, tid)


def test_path_leading_prose_preserves_all_local_files(board):
    tid, root = task(board, marker=False)
    (root / "bundle").mkdir()
    (root / "bundle" / "report.md").write_text("accepted report")
    board.execute(
        "UPDATE tasks SET result=? WHERE id=?", ("./accepted.patch and bundle/report.md", tid),
    )
    board.commit()
    bp.preserve_bundle(board, tid, root)
    assert contents(board, tid) == ["accepted.patch", "bundle/report.md"]


def test_nested_worktree_key_still_selects_artifacts(board):
    tid, root = task(board, marker=False)
    board.execute(
        "INSERT INTO task_runs(task_id,status,started_at,metadata) VALUES(?,'completed',0,?)",
        (tid, json.dumps({"artifacts": {"worktree": str(root / "accepted.patch")}})),
    )
    board.commit()
    report = bp.preserve_bundle(board, tid, root, dry_run=True)
    assert [entry["path"] for entry in report["files"]] == ["accepted.patch"]
