"""Wrapped artifact references must survive actual terminal workspace cleanup."""
import json

import pytest

from hermes_cli import kanban_bundle_preserve as bp
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as ws
from tests.hermes_cli.test_kanban_bundle_preserve import board, contents, task


WRAPPERS = ["`{path}`", '"{path}"', "'{path}'", "[report]({path})", "<{path}>"]


@pytest.mark.parametrize("wrapper", WRAPPERS)
@pytest.mark.parametrize("location", ["result", "summary", "metadata"])
@pytest.mark.parametrize("path_kind", ["relative", "absolute", "spaces"])
def test_wrapped_artifact_survives_real_cleanup(board, wrapper, location, path_kind):
    tid, root = task(board, marker=False)
    (root / "bundle").mkdir()
    name = "bundle/final report.md" if path_kind == "spaces" else "bundle/report.md"
    artifact = root / name
    artifact.write_text("accepted report")
    reference = wrapper.format(path=artifact if path_kind == "absolute" else name)
    if location == "result":
        board.execute("UPDATE tasks SET result=? WHERE id=?", (reference, tid))
    else:
        board.execute(
            "INSERT INTO task_runs(task_id,status,started_at,summary,metadata) VALUES(?,'completed',0,?,?)",
            (tid, reference if location == "summary" else None,
             json.dumps({"artifacts": [reference]}) if location == "metadata" else None),
        )
    board.commit()
    report = bp.preserve_bundle(board, tid, root, dry_run=True)
    assert [entry["path"] for entry in report["files"]] == [name]
    assert report["unresolved_refs"] == []
    ws._cleanup_workspace(board, tid)
    assert not root.exists()
    assert contents(board, tid) == [name]


@pytest.mark.parametrize("wrapper", WRAPPERS)
@pytest.mark.parametrize("kind", ["absolute_missing", "secret", "linked", "dangling"])
def test_wrapped_unsafe_reference_retains_workspace(board, wrapper, kind):
    tid, root = task(board, marker=False)
    if kind == "absolute_missing":
        path = root / "gone.patch"
    else:
        path = root / ("secrets.json" if kind == "secret" else "link.patch")
        if kind == "secret":
            path.write_text("excluded fixture")
        else:
            path.symlink_to(root / ("accepted.patch" if kind == "linked" else "gone.patch"))
    target = str(path) if kind == "absolute_missing" else path.name
    board.execute("UPDATE tasks SET result=? WHERE id=?", (wrapper.format(path=target), tid))
    board.commit()
    with pytest.raises(ValueError):
        bp.preserve_bundle(board, tid, root, dry_run=True)
    ws._cleanup_workspace(board, tid)
    assert root.is_dir()
    assert not kb.list_attachments(board, tid)


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_wrapped_urls_never_select_local_fragments(board, wrapper):
    tid, root = task(board, marker=False)
    board.execute(
        "UPDATE tasks SET result=? WHERE id=?",
        (wrapper.format(path="https://example.org/accepted.patch"), tid),
    )
    board.commit()
    report = bp.preserve_bundle(board, tid, root, dry_run=True)
    assert report["files"] == []
    assert report["unresolved_refs"] == []


def test_remote_markdown_label_is_not_a_local_artifact(board):
    tid, root = task(board, marker=False)
    board.execute(
        "UPDATE tasks SET result=? WHERE id=?",
        ("[accepted.patch](https://example.org/bundle/report.md)", tid),
    )
    board.commit()
    report = bp.preserve_bundle(board, tid, root, dry_run=True)
    assert report["files"] == []
    assert report["unresolved_refs"] == []
