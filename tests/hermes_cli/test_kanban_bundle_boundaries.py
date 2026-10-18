"""Complete decorated path identity survives preservation and isolated cleanup."""
import hashlib
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_bundle_preserve as bp
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
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


SOURCE_SUFFIXES = [":40", ":40-42", ":40:5", "#L5", "#section"]


@pytest.mark.parametrize("suffix", SOURCE_SUFFIXES)
@pytest.mark.parametrize("location", ["result", "summary", "metadata"])
@pytest.mark.parametrize("path_kind", ["basename", "nested", "absolute"])
def test_source_location_preserves_file_through_cleanup(board, suffix, location, path_kind):
    tid, root = task(board, marker=False)
    (root / "bundle").mkdir()
    name = "report.md" if path_kind == "basename" else "bundle/report.md"
    (root / name).write_text("accepted source location")
    target = str(root / name) if path_kind == "absolute" else name
    set_reference(board, tid, f"Changed {target}{suffix} for the fix.", location)
    assert_cleanup_preserves(board, tid, root, {name: "accepted source location"})


@pytest.mark.parametrize("suffix", SOURCE_SUFFIXES)
@pytest.mark.parametrize("decoration", ["`{path}`", '"{path}"', "[report]({path})"])
def test_wrapped_source_location_preserves_file(board, suffix, decoration):
    tid, root = task(board, marker=False)
    (root / "bundle").mkdir()
    (root / "bundle/report.md").write_text("accepted source location")
    set_reference(board, tid, decoration.format(path="bundle/report.md" + suffix), "result")
    assert_cleanup_preserves(board, tid, root, {"bundle/report.md": "accepted source location"})


@pytest.mark.parametrize("suffix", SOURCE_SUFFIXES)
def test_existing_literal_source_suffix_wins(board, suffix):
    tid, root = task(board, marker=False)
    (root / "bundle").mkdir()
    name = "bundle/report.md" + suffix
    (root / name).write_text("accepted literal suffix")
    (root / "bundle/report.md").write_text("not accepted")
    set_reference(board, tid, name, "result")
    assert_cleanup_preserves(board, tid, root, {name: "accepted literal suffix"})


@pytest.mark.parametrize("suffix", SOURCE_SUFFIXES)
@pytest.mark.parametrize("kind", ["secret", "env", "linked", "dangling", "linked_parent", "absolute_missing"])
def test_source_location_keeps_safety_checks(board, suffix, kind):
    tid, root = task(board, marker=False)
    (root / "bundle").mkdir()
    name = {"secret": "bundle/secrets.json", "env": "bundle/.env"}.get(kind, "bundle/report.md")
    if kind in {"secret", "env"}:
        (root / name).write_text("excluded fixture")
    elif kind in {"linked", "dangling"}:
        (root / name).symlink_to(root / ("accepted.patch" if kind == "linked" else "gone.patch"))
    elif kind == "linked_parent":
        (root / "alias").symlink_to(root / "bundle", target_is_directory=True)
        name = "alias/report.md"
    target = str(root / name) if kind == "absolute_missing" else name
    set_reference(board, tid, target + suffix, "result")
    with pytest.raises(ValueError):
        bp.preserve_bundle(board, tid, root, dry_run=True)
    ws._cleanup_workspace(board, tid)
    assert root.is_dir()
    assert not kb.list_attachments(board, tid)


@pytest.mark.parametrize("suffix", SOURCE_SUFFIXES)
@pytest.mark.parametrize("kind", ["secret", "linked", "dangling"])
def test_unsafe_literal_source_suffix_never_falls_back(board, suffix, kind):
    tid, root = task(board, marker=False)
    name = ("secrets.json" if kind == "secret" else "report.md") + suffix
    if kind == "secret":
        (root / name).write_text("excluded fixture")
    else:
        (root / name).symlink_to(root / ("accepted.patch" if kind == "linked" else "gone.patch"))
    (root / "report.md").write_text("not accepted")
    set_reference(board, tid, name, "result")
    with pytest.raises(ValueError):
        bp.preserve_bundle(board, tid, root, dry_run=True)
    ws._cleanup_workspace(board, tid)
    assert root.is_dir()
    assert not kb.list_attachments(board, tid)


@pytest.mark.parametrize("prose,expected", [
    ("No credential/config/model/allowlist change.", "credential/config/model/allowlist"),
    ("Fixed `SECRET_NAME` regex", "SECRET_NAME"),
    ("no token printed: 'token' redacted", "token"),
    ("See docs/secret-policy.md", "docs/secret-policy.md"),
    ("See docs/secret-policy.md:40", "docs/secret-policy.md"),
    ("See `bundle/.env`", "bundle/.env"),
])
@pytest.mark.parametrize("artifact", [True, False])
def test_missing_relative_prohibited_hint_is_durable_unresolved(board, prose, expected, artifact):
    tid, root = task(board, marker=False)
    set_reference(board, tid, prose + (" accepted.patch" if artifact else ""), "summary")
    changes = board.total_changes
    report = bp.preserve_bundle(board, tid, root, dry_run=True)
    assert report["unresolved_refs"] == [expected]
    assert board.total_changes == changes
    assert root.is_dir()
    assert not kb.list_attachments(board, tid)
    ws._cleanup_workspace(board, tid)
    assert not root.exists()
    if artifact:
        assert contents(board, tid) == ["accepted.patch"]
    else:
        assert not kb.list_attachments(board, tid)
    with kbc.connect_closing() as reopened:
        events = reopened.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='bundle_unresolved_refs'", (tid,),
        ).fetchall()
    assert [json.loads(row["payload"]) for row in events] == [{"unresolved_refs": [expected]}]


@pytest.mark.parametrize("suffix", SOURCE_SUFFIXES)
def test_source_suffix_does_not_expand_directory_or_escape_workspace(board, suffix):
    tid, root = task(board, marker=False)
    (root / "bundle/v1.0").mkdir(parents=True)
    (root / "bundle/v1.0/unrelated.md").write_text("not accepted")
    set_reference(board, tid, "bundle/v1.0" + suffix + " ../accepted.patch" + suffix, "result")
    report = bp.preserve_bundle(board, tid, root, dry_run=True)
    assert report["files"] == []
    assert report["unresolved_refs"] == ["bundle/v1.0" + suffix]
