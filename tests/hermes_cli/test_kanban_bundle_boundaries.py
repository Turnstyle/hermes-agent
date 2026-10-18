"""Complete decorated path identity survives preservation and isolated cleanup."""
import hashlib
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_bundle_preserve as bp
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as ws
from tests.hermes_cli.test_kanban_bundle_preserve import board, contents, task


DECORATIONS = [
    "({path})", "**{path}**", "`{path}`.", "`{path}`,",
    "artifact={path}", "path:{path}", "[{path}]",
    "<{path}>.", '"{path}";',
]


def set_reference(board, tid, reference, location):
    if location == "result":
        board.execute("UPDATE tasks SET result=? WHERE id=?", (reference, tid))
    else:
        board.execute(
            "INSERT INTO task_runs(task_id,status,started_at,summary,metadata) VALUES(?,'completed',0,?,?)",
            (tid, reference if location == "summary" else None,
             json.dumps({"artifacts": [reference]}) if location == "metadata" else None),
        )
    board.commit()


def assert_cleanup_preserves(board, tid, root, expected):
    report = bp.preserve_bundle(board, tid, root, dry_run=True)
    assert report["unresolved_refs"] == []
    expected_hashes = {name: hashlib.sha256(payload.encode()).hexdigest()
                       for name, payload in expected.items()}
    assert {entry["path"]: entry["sha256"] for entry in report["files"]} == expected_hashes
    ws._cleanup_workspace(board, tid)
    assert not root.exists()
    # contents checks each archive member's bytes/hash against its manifest.
    assert contents(board, tid) == sorted(expected)
    manifest = next(Path(a.stored_path) for a in kb.list_attachments(board, tid)
                    if a.filename.endswith(".json"))
    assert {entry["path"]: entry["sha256"] for entry in json.loads(manifest.read_text())} == expected_hashes


@pytest.mark.parametrize("decoration", DECORATIONS)
@pytest.mark.parametrize("location", ["result", "summary", "metadata"])
@pytest.mark.parametrize("path_kind", ["relative", "absolute", "extensionless"])
def test_decorated_artifact_survives_cleanup(board, decoration, location, path_kind):
    tid, root = task(board, marker=False)
    (root / "bundle").mkdir()
    name = "bundle/README" if path_kind == "extensionless" else "bundle/report.md"
    (root / name).write_text("accepted report")
    target = str(root / name) if path_kind == "absolute" else name
    set_reference(board, tid, decoration.format(path=target), location)
    assert_cleanup_preserves(board, tid, root, {name: "accepted report"})


@pytest.mark.parametrize("decoration", DECORATIONS)
@pytest.mark.parametrize("kind", ["absolute_missing", "secret", "linked", "dangling"])
def test_decorated_unsafe_reference_retains_workspace(board, decoration, kind):
    tid, root = task(board, marker=False)
    (root / "bundle").mkdir()
    name = "bundle/secrets.json" if kind == "secret" else "bundle/missing.md"
    path = root / name
    if kind == "secret":
        path.write_text("excluded fixture")
    elif kind in {"linked", "dangling"}:
        path.symlink_to(root / ("accepted.patch" if kind == "linked" else "gone.patch"))
    target = str(path) if kind == "absolute_missing" else name
    set_reference(board, tid, decoration.format(path=target), "result")
    with pytest.raises(ValueError):
        bp.preserve_bundle(board, tid, root, dry_run=True)
    ws._cleanup_workspace(board, tid)
    assert root.is_dir()
    assert not kb.list_attachments(board, tid)


@pytest.mark.parametrize("decoration", DECORATIONS)
@pytest.mark.parametrize("existing", [True, False])
def test_whole_path_never_selects_directory_prefix_or_siblings(board, decoration, existing):
    tid, root = task(board, marker=False)
    (root / "bundle/v1.0").mkdir(parents=True)
    name = "bundle/v1.0/README"
    (root / "bundle/v1.0/unrelated.md").write_text("not accepted")
    if existing:
        (root / name).write_text("accepted report")
    set_reference(board, tid, decoration.format(path=name), "result")
    if existing:
        assert_cleanup_preserves(board, tid, root, {name: "accepted report"})
    else:
        report = bp.preserve_bundle(board, tid, root, dry_run=True)
        assert report["files"] == []
        assert report["unresolved_refs"] == [name]


@pytest.mark.parametrize("name,decoration", [
    (name, decoration)
    for name in ["bundle/final report", "bundle/report(old).md", "bundle/report.md.", "bundle/a:b.md"]
    for decoration in ["`{path}`.", 'artifact="{path}";', "[report]({path})"]
    # Markdown's unescaped nested parentheses are outside the supported grammar.
    if not (decoration == "[report]({path})" and "(" in name)
])
def test_explicit_spaced_and_punctuation_identity(board, name, decoration):
    tid, root = task(board, marker=False)
    (root / "bundle").mkdir()
    (root / name).write_text("accepted report")
    set_reference(board, tid, decoration.format(path=name), "result")
    assert_cleanup_preserves(board, tid, root, {name: "accepted report"})


@pytest.mark.parametrize("decoration", DECORATIONS)
def test_decorated_urls_ignore_all_local_fragments(board, decoration):
    tid, root = task(board, marker=False)
    set_reference(board, tid, decoration.format(path="https://example.org/accepted.patch?path=bundle/report.md"), "result")
    report = bp.preserve_bundle(board, tid, root, dry_run=True)
    assert report["files"] == []
    assert report["unresolved_refs"] == []


def test_multiple_mixed_references_preserve_exact_files(board):
    tid, root = task(board, marker=False)
    (root / "bundle").mkdir()
    expected = {"bundle/final report.md": "spaced", "bundle/README": "extensionless",
                "bundle/owner's-report.md": "apostrophe"}
    for name, payload in expected.items():
        (root / name).write_text(payload)
    set_reference(board, tid, "Wrote `bundle/final report.md`, path:bundle/README; bundle/owner's-report.md.", "summary")
    assert_cleanup_preserves(board, tid, root, expected)


def test_outside_relative_reference_does_not_select_same_named_local_file(board):
    tid, root = task(board, marker=False)
    set_reference(board, tid, "python3 ../accepted.patch", "result")
    report = bp.preserve_bundle(board, tid, root, dry_run=True)
    assert report["files"] == []
    assert report["unresolved_refs"] == []
