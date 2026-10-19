"""Regression: structured findings resolve under the recorded checkout root.

Top-level ``task_runs.metadata.worktree`` is the absolute checkout
(``{workspace}/head``). ``findings[].file`` is a repo-relative source.
A workspace namesake is not that source. Rootless structured findings stay
ambiguous and block removal.
"""
import hashlib
import json
import tarfile
from pathlib import Path

import pytest

from hermes_cli import kanban_bundle_preserve as bp
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as ws
from tests.hermes_cli.test_kanban_bundle_preserve import board, contents, task

SOURCE_REL = "app_services/services/gp_fund_setup.py"
HEAD_MEMBER = "head/" + SOURCE_REL
SOURCE = b"recorded-checkout-gp-fund-setup\n"
NAMESAKE = b"workspace-namesake\n"


def _record(conn, tid, metadata, result=None):
    if result is not None:
        conn.execute("UPDATE tasks SET result=? WHERE id=?", (result, tid))
    conn.execute(
        "INSERT INTO task_runs(task_id,status,started_at,metadata) VALUES(?,'completed',0,?)",
        (tid, json.dumps(metadata)),
    )
    conn.commit()


def _write(path: Path, payload: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _payloads(conn, tid):
    archived = contents(conn, tid)
    archive = next(
        Path(item.stored_path)
        for item in kb.list_attachments(conn, tid)
        if item.filename.endswith(".tgz")
    )
    with tarfile.open(archive) as bundle:
        return {name: bundle.extractfile(name).read() for name in archived}


def _metadata(head: Path | None, file: str):
    findings = {"findings": [{"file": file}]}
    if head is None:
        return findings
    return {"worktree": str(head), **findings}


def test_structured_finding_preserves_recorded_checkout_bytes(board):
    tid, root = task(board, marker=False)
    head = root / "head"
    _write(head / SOURCE_REL, SOURCE)
    _write(root / SOURCE_REL, NAMESAKE)
    recorded = head.resolve()
    assert (recorded / SOURCE_REL).is_file()
    assert not (root / SOURCE_REL).resolve().is_relative_to(recorded)
    _record(board, tid, _metadata(head, SOURCE_REL))

    plan = bp.plan_bundle(board, tid, root)
    assert [entry["path"] for entry in plan["files"]] == [HEAD_MEMBER]
    assert plan["files"][0]["bytes"] == len(SOURCE)
    assert plan["files"][0]["sha256"] == hashlib.sha256(SOURCE).hexdigest()
    assert plan["bytes"] == len(SOURCE)
    assert Path(plan["workspace"]) == root
    assert SOURCE_REL not in plan["unresolved_refs"]

    preserved = bp.preserve_bundle(board, tid, root)
    assert [entry["path"] for entry in preserved["files"]] == [HEAD_MEMBER]
    assert _payloads(board, tid) == {HEAD_MEMBER: SOURCE}
    assert (root / SOURCE_REL).read_bytes() == NAMESAKE
    assert bp.preservation_gate(tid, root, conn=board) is True
    ws._cleanup_workspace(board, tid)
    assert not root.exists()
    assert _payloads(board, tid) == {HEAD_MEMBER: SOURCE}


@pytest.mark.parametrize("kind", ["escape", "absolute"])
def test_structured_finding_outside_checkout_fails_closed(board, kind):
    tid, root = task(board, marker=False)
    head = root / "head"
    head.mkdir()
    landed = root / "outside" / "payload.txt"
    _write(landed, b"escaped-workspace-bytes\n")
    external = root.parent / "external" / "payload.txt"
    _write(external, b"absolute-outside-bytes\n")
    finding = "../outside/payload.txt" if kind == "escape" else str(external)
    _record(
        board, tid, _metadata(head, finding), result=str(root / "accepted.patch"),
    )

    with pytest.raises(ValueError) as caught:
        bp.plan_bundle(board, tid, root)
    assert finding in str(caught.value)
    with pytest.raises(ValueError) as caught:
        bp.preserve_bundle(board, tid, root, dry_run=True)
    assert finding in str(caught.value)
    assert bp.preservation_gate(tid, root, conn=board) is False
    assert root.exists()
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert (root / "accepted.patch").is_file()
    assert landed.read_bytes() == b"escaped-workspace-bytes\n"
    assert external.read_bytes() == b"absolute-outside-bytes\n"
    assert not kb.list_attachments(board, tid)
    assert board.execute(
        "SELECT body FROM task_comments WHERE task_id=?", (tid,),
    ).fetchone()


def test_structured_finding_symlink_does_not_follow_outside_secret(board):
    tid, root = task(board, marker=False)
    head = root / "head"
    secret = root.parent / "outside" / "secret"
    secret_bytes = b"outside-secret-not-followed\n"
    _write(secret, secret_bytes)
    link = head / SOURCE_REL
    link.parent.mkdir(parents=True)
    link.symlink_to(secret)
    _write(root / SOURCE_REL, NAMESAKE)
    _record(
        board, tid, _metadata(head, SOURCE_REL), result=str(root / "accepted.patch"),
    )

    with pytest.raises(ValueError):
        bp.plan_bundle(board, tid, root)
    with pytest.raises(ValueError):
        bp.preserve_bundle(board, tid, root, dry_run=True)
    assert bp.preservation_gate(tid, root, conn=board) is False
    assert root.exists()
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert link.is_symlink()
    assert secret.read_bytes() == secret_bytes
    assert (root / SOURCE_REL).read_bytes() == NAMESAKE
    assert not kb.list_attachments(board, tid)


def test_structured_finding_missing_under_checkout_fails_closed(board):
    tid, root = task(board, marker=False)
    head = root / "head"
    head.mkdir()
    (head / "README").write_text("checkout without the source\n")
    _write(root / SOURCE_REL, NAMESAKE)
    elsewhere = root.parent / "elsewhere" / SOURCE_REL
    _write(elsewhere, b"namesake-elsewhere\n")
    assert not (head / SOURCE_REL).exists()
    _record(
        board, tid, _metadata(head, SOURCE_REL), result=str(root / "accepted.patch"),
    )

    with pytest.raises(ValueError):
        bp.plan_bundle(board, tid, root)
    with pytest.raises(ValueError):
        bp.preserve_bundle(board, tid, root, dry_run=True)
    assert bp.preservation_gate(tid, root, conn=board) is False
    assert root.exists()
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert (root / SOURCE_REL).read_bytes() == NAMESAKE
    assert elsewhere.read_bytes() == b"namesake-elsewhere\n"
    assert (root / "accepted.patch").is_file()
    assert not kb.list_attachments(board, tid)


def test_rootless_structured_finding_is_ambiguous_and_retained(board):
    tid, root = task(board, marker=False)
    _write(root / SOURCE_REL, NAMESAKE)
    _record(board, tid, _metadata(None, SOURCE_REL), result=str(root / "accepted.patch"))

    plan = bp.plan_bundle(board, tid, root)
    assert [entry["path"] for entry in plan["files"]] == ["accepted.patch"]
    assert plan["ambiguous_refs"] == [SOURCE_REL]
    assert SOURCE_REL not in plan["unresolved_refs"]
    assert plan["bytes"] == (root / "accepted.patch").stat().st_size
    dry = bp.preserve_bundle(board, tid, root, dry_run=True)
    assert [entry["path"] for entry in dry["files"]] == ["accepted.patch"]
    assert dry["ambiguous_refs"] == [SOURCE_REL]
    assert not kb.list_attachments(board, tid)
    assert bp.preservation_gate(tid, root, conn=board) is False
    assert Path(plan["workspace"]) == root
    assert root.exists()
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert (root / "accepted.patch").read_text() == "diff accepted\n"
    assert (root / SOURCE_REL).read_bytes() == NAMESAKE
    for item in kb.list_attachments(board, tid):
        if not str(item.filename).endswith(".tgz"):
            continue
        with tarfile.open(item.stored_path) as bundle:
            assert all(not name.endswith("gp_fund_setup.py") for name in bundle.getnames())


def _paths(report):
    return [entry["path"] for entry in report["files"]]


def test_structured_finding_without_workspace_namesake_preserves_head_bytes(board):
    tid, root = task(board, marker=False)
    head = root / "head"
    _write(head / SOURCE_REL, SOURCE)
    assert not (root / SOURCE_REL).exists()
    _record(board, tid, _metadata(head, SOURCE_REL))
    plan = bp.plan_bundle(board, tid, root)
    assert _paths(plan) == [HEAD_MEMBER]
    assert plan["files"][0]["sha256"] == hashlib.sha256(SOURCE).hexdigest()
    assert bp.preservation_gate(tid, root, conn=board) is True
    assert _payloads(board, tid) == {HEAD_MEMBER: SOURCE}


def test_recorded_source_secret_name_fails_closed(board):
    tid, root = task(board, marker=False)
    head = root / "head"
    secret = b"in-root-secret\n"
    _write(head / "secrets.json", secret)
    _record(
        board, tid, _metadata(head, "secrets.json"), result=str(root / "accepted.patch"),
    )
    with pytest.raises(ValueError):
        bp.plan_bundle(board, tid, root)
    with pytest.raises(ValueError):
        bp.preserve_bundle(board, tid, root, dry_run=True)
    assert bp.preservation_gate(tid, root, conn=board) is False
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert (head / "secrets.json").read_bytes() == secret
    assert not kb.list_attachments(board, tid)


def test_recorded_checkout_symlink_is_not_followed(board):
    tid, root = task(board, marker=False)
    real = root.parent / "real-head"
    _write(real / SOURCE_REL, SOURCE)
    head = root / "head"
    head.symlink_to(real, target_is_directory=True)
    _write(root / SOURCE_REL, NAMESAKE)
    _record(board, tid, _metadata(head, SOURCE_REL), result=str(root / "accepted.patch"))
    with pytest.raises(ValueError):
        bp.plan_bundle(board, tid, root)
    assert bp.preservation_gate(tid, root, conn=board) is False
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert head.is_symlink()
    assert (real / SOURCE_REL).read_bytes() == SOURCE
    assert (root / SOURCE_REL).read_bytes() == NAMESAKE
    assert not kb.list_attachments(board, tid)


def test_each_run_resolves_only_its_own_checkout(board):
    tid, root = task(board, marker=False)
    head_a = root / "head-a"
    head_b = root / "head-b"
    _write(head_a / "a.py", b"from-a\n")
    _write(head_b / "b.py", b"from-b\n")
    _write(root / "a.py", b"namesake-a\n")
    _write(root / "b.py", b"namesake-b\n")
    _record(board, tid, {"worktree": str(head_a), "findings": [{"file": "a.py"}]})
    _record(board, tid, {"worktree": str(head_b), "findings": [{"file": "b.py"}]})
    plan = bp.plan_bundle(board, tid, root)
    assert _paths(plan) == ["head-a/a.py", "head-b/b.py"]
    assert {entry["path"]: entry["sha256"] for entry in plan["files"]} == {
        "head-a/a.py": hashlib.sha256(b"from-a\n").hexdigest(),
        "head-b/b.py": hashlib.sha256(b"from-b\n").hexdigest(),
    }
    assert plan["ambiguous_refs"] == []
    assert bp.preservation_gate(tid, root, conn=board) is True
    assert _payloads(board, tid) == {"head-a/a.py": b"from-a\n", "head-b/b.py": b"from-b\n"}


def test_rootless_run_does_not_borrow_another_runs_checkout(board):
    tid, root = task(board, marker=False)
    head = root / "head"
    _write(head / SOURCE_REL, SOURCE)
    _write(root / SOURCE_REL, NAMESAKE)
    _write(root / "other.py", b"other-namesake\n")
    _record(board, tid, _metadata(head, SOURCE_REL))
    _record(board, tid, _metadata(None, "other.py"), result=str(root / "accepted.patch"))
    plan = bp.plan_bundle(board, tid, root)
    assert _paths(plan) == ["accepted.patch", HEAD_MEMBER]
    assert plan["files"][1]["sha256"] == hashlib.sha256(SOURCE).hexdigest()
    assert plan["ambiguous_refs"] == ["other.py"]
    assert "other.py" not in plan["unresolved_refs"]
    assert bp.preservation_gate(tid, root, conn=board) is False
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert (root / "other.py").read_bytes() == b"other-namesake\n"


def test_rootless_generic_prohibited_workspace_member_is_rejected(board):
    # Rule 53892 rejects an existing secret-named workspace file instead of calling it ambiguous.
    tid, root = task(board, marker=False)
    hint = "docs/secret-policy.md"
    _write(root / hint, b"namesake-policy\n")
    board.execute(
        "UPDATE tasks SET result=? WHERE id=?",
        (f"See {hint}\n{root / 'accepted.patch'}", tid),
    )
    board.commit()
    with pytest.raises(ValueError, match="referenced member has a prohibited name"):
        bp.plan_bundle(board, tid, root)
    assert bp.preservation_gate(tid, root, conn=board) is False
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert (root / hint).read_bytes() == b"namesake-policy\n"
    assert not kb.list_attachments(board, tid)


def test_different_root_copies_are_ambiguous(board):
    # Rule 53892 leaves workspace and checkout copies ambiguous when their archive paths differ.
    tid, root = task(board, marker=False)
    head = root / "head"
    _write(head / SOURCE_REL, SOURCE)
    _write(root / SOURCE_REL, NAMESAKE)
    _record(board, tid, {"worktree": str(head)}, result=SOURCE_REL)
    plan = bp.plan_bundle(board, tid, root)
    assert _paths(plan) == []
    assert plan["ambiguous_refs"] == [SOURCE_REL]
    assert SOURCE_REL not in plan["unresolved_refs"]
    assert bp.preservation_gate(tid, root, conn=board) is False
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert (root / SOURCE_REL).read_bytes() == NAMESAKE
    assert (head / SOURCE_REL).read_bytes() == SOURCE
    assert not kb.list_attachments(board, tid)


def test_conflicting_checkouts_make_result_source_ambiguous(board):
    tid, root = task(board, marker=False)
    head_a = root / "head-a"
    head_b = root / "head-b"
    _write(head_a / SOURCE_REL, b"from-a\n")
    _write(head_b / SOURCE_REL, b"from-b\n")
    _write(root / SOURCE_REL, NAMESAKE)
    _record(board, tid, {"worktree": str(head_a)})
    _record(
        board, tid, {"worktree": str(head_b)},
        result=f"{SOURCE_REL}\n{root / 'accepted.patch'}",
    )
    plan = bp.plan_bundle(board, tid, root)
    assert _paths(plan) == ["accepted.patch"]
    assert plan["ambiguous_refs"] == [SOURCE_REL]
    assert SOURCE_REL not in plan["unresolved_refs"]
    assert bp.preservation_gate(tid, root, conn=board) is False
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert (head_a / SOURCE_REL).read_bytes() == b"from-a\n"
    assert (root / SOURCE_REL).read_bytes() == NAMESAKE


def test_finding_sibling_missing_absolute_proof_fails_closed(board):
    tid, root = task(board, marker=False)
    head = root / "head"
    _write(head / "a.py", b"from-head\n")
    proof = root / "missing-proof.txt"
    _record(
        board, tid,
        {"worktree": str(head), "findings": [{"file": "a.py", "proof": str(proof)}]},
        result=str(root / "accepted.patch"),
    )
    with pytest.raises(ValueError):
        bp.plan_bundle(board, tid, root)
    assert bp.preservation_gate(tid, root, conn=board) is False
    assert root.exists()
    assert not proof.exists()
    assert (head / "a.py").read_bytes() == b"from-head\n"
    assert not kb.list_attachments(board, tid)


def test_finding_extra_values_preserve_absolute_bytes(board):
    tid, root = task(board, marker=False)
    head = root / "head"
    _write(head / "a.py", b"from-head\n")
    proof = root / "evidence" / "proof.txt"
    note = root / "evidence" / "note.txt"
    nested = root / "evidence" / "nested.txt"
    proof_bytes = b"proof-bytes\n"
    note_bytes = b"note-bytes\n"
    nested_bytes = b"nested-bytes\n"
    _write(proof, proof_bytes)
    _write(note, note_bytes)
    _write(nested, nested_bytes)
    _record(
        board, tid,
        {
            "worktree": str(head),
            "findings": [
                {"file": "a.py", "proof": str(proof), "detail": {"path": str(nested)}},
                str(note),
            ],
        },
        result=str(root / "accepted.patch"),
    )
    plan = bp.plan_bundle(board, tid, root)
    paths = [entry["path"] for entry in plan["files"]]
    assert "evidence/proof.txt" in paths
    assert "evidence/note.txt" in paths
    assert "evidence/nested.txt" in paths
    preserved = bp.preserve_bundle(board, tid, root)
    assert "evidence/proof.txt" in [entry["path"] for entry in preserved["files"]]
    assert bp.preservation_gate(tid, root, conn=board) is True
    payloads = _payloads(board, tid)
    assert payloads["evidence/proof.txt"] == proof_bytes
    assert payloads["evidence/note.txt"] == note_bytes
    assert payloads["evidence/nested.txt"] == nested_bytes


def test_finding_file_url_stays_non_local(board):
    tid, root = task(board, marker=False)
    head = root / "head"
    head.mkdir()
    _record(
        board, tid,
        {"worktree": str(head), "findings": [{"file": "https://example.com/head/a.py"}]},
        result=str(root / "accepted.patch"),
    )
    plan = bp.plan_bundle(board, tid, root)
    assert [entry["path"] for entry in plan["files"]] == ["accepted.patch"]
    assert bp.preservation_gate(tid, root, conn=board) is True


def test_recorded_checkout_may_be_the_workspace(board):
    tid, root = task(board, marker=False)
    payload = b"workspace-root-source\n"
    _write(root / "a.py", payload)
    _record(board, tid, {"worktree": str(root), "findings": [{"file": "a.py"}]})
    plan = bp.plan_bundle(board, tid, root)
    assert [entry["path"] for entry in plan["files"]] == ["a.py"]
    assert plan["files"][0]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert bp.preservation_gate(tid, root, conn=board) is True
    assert _payloads(board, tid)["a.py"] == payload


def test_missing_extensionless_finding_file_fails_closed(board):
    tid, root = task(board, marker=False)
    head = root / "head"
    head.mkdir()
    wrong = b"wrong-workspace-readme\n"
    _write(root / "README", wrong)
    _record(
        board, tid,
        {"worktree": str(head), "findings": [{"file": "README"}]},
        result=str(root / "accepted.patch"),
    )
    with pytest.raises(ValueError):
        bp.plan_bundle(board, tid, root)
    assert bp.preservation_gate(tid, root, conn=board) is False
    assert root.exists()
    assert not (head / "README").exists()
    assert (root / "README").read_bytes() == wrong
    assert not kb.list_attachments(board, tid)


def test_present_extensionless_finding_archives_head_bytes(board):
    tid, root = task(board, marker=False)
    head = root / "head"
    payload = b"head-readme-bytes\n"
    _write(head / "README", payload)
    _write(root / "README", b"wrong-workspace-readme\n")
    _record(board, tid, {"worktree": str(head), "findings": [{"file": "README"}]})
    plan = bp.plan_bundle(board, tid, root)
    assert _paths(plan) == ["head/README"]
    assert plan["files"][0]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert bp.preservation_gate(tid, root, conn=board) is True
    assert _payloads(board, tid) == {"head/README": payload}


def test_spaced_finding_file_archives_exact_head_path(board):
    tid, root = task(board, marker=False)
    head = root / "head"
    relative = "notes/my report.md"
    payload = b"spaced-source\n"
    _write(head / relative, payload)
    _write(root / "report.md", b"fragment\n")
    _write(root / "notes" / "my", b"fragment-token\n")
    _record(board, tid, {"worktree": str(head), "findings": [{"file": relative}]})
    plan = bp.plan_bundle(board, tid, root)
    assert _paths(plan) == ["head/notes/my report.md"]
    assert plan["files"][0]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert bp.preservation_gate(tid, root, conn=board) is True
    assert _payloads(board, tid) == {"head/notes/my report.md": payload}


def test_rootless_extensionless_finding_is_ambiguous(board):
    tid, root = task(board, marker=False)
    namesake = b"workspace-readme\n"
    _write(root / "README", namesake)
    _record(
        board, tid,
        {"findings": [{"file": "README"}]},
        result=str(root / "accepted.patch"),
    )
    plan = bp.plan_bundle(board, tid, root)
    assert _paths(plan) == ["accepted.patch"]
    assert plan["ambiguous_refs"] == ["README"]
    assert "README" not in plan["unresolved_refs"]
    assert bp.preservation_gate(tid, root, conn=board) is False
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert (root / "README").read_bytes() == namesake
