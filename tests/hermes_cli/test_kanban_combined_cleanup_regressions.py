"""Clean-room Combined Kanban Preservation Regression Test Suite.

Exercises all 14 regression categories, the 18 frozen input specifications,
and the 8 finishing hardening requirements:
1. GCP-SA: Service account identifier nonlocal metadata + exact fixtures + grammar + local collision negatives.
2. GCP-RESOURCE: Secret resource path nonlocal metadata + exact fixtures + grammar + local collision negatives.
3. ZERO-REF: Zero initial attachments with referenced file (backtick, quoted, plain_rel, absolute) retains.
4. ZERO-UNREF: Full-inventory capture-or-retain for unreferenced files, siblings, and stale attachments.
5. CALLERS: Direct behavioral call-through tests for completion, archive, deferred parent, worktree (incl. conn=None), and GC.
6. SECRET: Prohibited/excluded components (dirs & files, key*.json, delimited auth, .git pointers) fail closed without opening.
7. LINK: Root, ancestor, member, dangling symlinks, symlink swap, and FIFOs fail closed without opening.
8. MISSING: Missing required refs and malformed task_runs.metadata JSON fail closed.
9. MARKER: HOLD-WORKSPACE and KEEP-BUNDLE probe failures retain before registration.
10. ROOT: Recorded head/ checkout resolution (with workspace namesake), multiple findings, rootless ambiguity, and outside-root rejection.
11. NONPATH: Any scheme:// URL (incl. malformed ports), owner/repo, model/provider, and prose ratio 5/10. handling without sweeping prose words.
12. COMPATIBILITY: Spaced paths in result/summary/metadata, prefix non-collision, empty directories in tar, and cap enforcement.
13. EXTERNAL: External declared artifacts fail closed without reading outside workspace.
14. DURABILITY: Repeated automatic zero-initial-attachment retention, unique non-overwriting bundle names, and strict existing-bundle verification.
"""
from __future__ import annotations

import builtins
import errno
import hashlib
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tarfile
from argparse import Namespace
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from hermes_cli import kanban_bundle_preserve as bp
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_ops


@pytest.fixture
def isolated_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Disposable isolated HERMES_HOME and SQLite board under pytest tmp_path."""
    home = tmp_path / "hermes_home"
    home.mkdir(parents=True, exist_ok=True)
    for k in (
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_ATTACHMENTS_ROOT",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kbw, "_cleanup_worker_tmux", lambda *_a, **_kw: None)
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn, tmp_path


def _create_scratch_task(
    conn: sqlite3.Connection,
    *,
    status: str = "done",
    result: str | None = None,
    files: Dict[str, bytes | str] | None = None,
) -> Tuple[str, Path]:
    tid = kb.create_task(conn, title="fixture-task", assignee="worker", workspace_kind="scratch")
    ws = kb.workspaces_root() / tid
    ws.mkdir(parents=True, exist_ok=True)
    if files:
        for rel, content in files.items():
            p = ws / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                p.write_bytes(content)
            else:
                p.write_text(content, encoding="utf-8")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = ?, workspace_path = ?, result = ? WHERE id = ?",
            (status, str(ws), result, tid),
        )
    return tid, ws


def _insert_completed_run(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    summary: str | None = None,
    metadata: dict | str | None = None,
) -> None:
    md_str = (
        metadata
        if isinstance(metadata, str) or metadata is None
        else json.dumps(metadata)
    )
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, summary, metadata, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?, 1, 2)",
            (task_id, summary, md_str),
        )


def _extract_latest_archive_payloads(conn: sqlite3.Connection, task_id: str) -> Dict[str, bytes]:
    atts = kb.list_attachments(conn, task_id)
    arc = [a for a in atts if a.filename.endswith(".tgz")][-1]
    with tarfile.open(arc.stored_path, "r:gz") as tar:
        return {
            m.name: tar.extractfile(m).read()
            for m in tar.getmembers()
            if m.isfile()
        }


# ---------------------------------------------------------------------------
# 0. Preflight: Git Ceiling & Disposable Scratch Isolation
# ---------------------------------------------------------------------------


def test_preflight_git_ceiling_and_disposable_tmp_isolation(tmp_path: Path):
    """Verify GIT_CEILING_DIRECTORIES and disposable tmp_path survive test runner env stripping."""
    ceiling = os.environ.get("GIT_CEILING_DIRECTORIES", "")
    assert ceiling, "GIT_CEILING_DIRECTORIES must be set inside pytest process"

    # Verify child git subprocess inherits GIT_CEILING_DIRECTORIES
    probe_dir = tmp_path / "outside_git_probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=str(probe_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0, "git rev-parse outside a repo must not discover a parent git repo"

    # Verify live guard blocks non-disposable paths when loaded
    if "pytest_live_guard" in sys.modules:
        guard = sys.modules["pytest_live_guard"]
        with pytest.raises(RuntimeError, match="LIVE GUARD BLOCKED"):
            guard.assert_disposable_scratch_path("/nonexistent_outside_root/live_target", "test_probe")


# ---------------------------------------------------------------------------
# 1 & 2. Categories GCP-SA & GCP-RESOURCE: Exact Fixtures, Grammar, Negatives
# ---------------------------------------------------------------------------


def test_gcp_sa_identifier_exact_fixtures_and_grammar_positives(isolated_board):
    """Category 1 (GCP-SA): Exact JSON fixture, required= prose context, and varied grammar."""
    conn, _ = isolated_board
    sa_id = "keys-router@mission-control-000000.iam.gserviceaccount.com"

    # 1a. Exact JSON structure from two-cloud-regression-fixtures.json + unreferenced sibling
    tid1, ws1 = _create_scratch_task(
        conn,
        files={"a.py": "x", "unreferenced_sibling.py": "sibling_code\n"},
    )
    _insert_completed_run(conn, tid1, metadata={"cloud_account": sa_id})

    plan1 = bp.plan_bundle(conn, tid1, ws1)
    assert plan1["unresolved_refs"] == []
    assert plan1["ambiguous_refs"] == []
    assert [f["path"] for f in plan1["files"]] == ["a.py", "unreferenced_sibling.py"]

    kbw._cleanup_workspace(conn, tid1)
    assert ws1.exists(), "zero initial attachments must retain workspace"
    assert (ws1 / "a.py").read_text(encoding="utf-8") == "x"
    assert (ws1 / "unreferenced_sibling.py").read_text(encoding="utf-8") == "sibling_code\n"
    assert _extract_latest_archive_payloads(conn, tid1) == {
        "a.py": b"x",
        "unreferenced_sibling.py": b"sibling_code\n",
    }

    # 1b. Exact written prose context: required=<service-account-email> + a.py + sibling
    tid2, ws2 = _create_scratch_task(
        conn,
        result=f"required={sa_id} and a.py",
        files={"a.py": "x", "extra.txt": "extra_payload"},
    )
    plan2 = bp.plan_bundle(conn, tid2, ws2)
    assert plan2["unresolved_refs"] == []
    assert [f["path"] for f in plan2["files"]] == ["a.py", "extra.txt"]
    preserved2 = bp.preserve_bundle(conn, tid2, ws2)
    assert [f["path"] for f in preserved2["files"]] == ["a.py", "extra.txt"]
    assert _extract_latest_archive_payloads(conn, tid2) == {
        "a.py": b"x",
        "extra.txt": b"extra_payload",
    }
    assert bp.preservation_gate(tid2, ws2, conn=conn) is False

    # 1c. Varied grammar positives
    for variant in (
        "my.sa_1+tag@sub.project-123.iam.gserviceaccount.com",
        "ci-bot-99@prod-env-001.iam.gserviceaccount.com",
    ):
        assert bp.is_non_path_metadata(variant) is True


def test_gcp_sa_negative_controls_and_local_collision(isolated_board):
    """Category 1 (GCP-SA): Near-match negatives and local collision fail closed."""
    conn, _ = isolated_board
    sa_id = "keys-router@mission-control-000000.iam.gserviceaccount.com"

    for neg in (
        f"{sa_id}.json",
        f"dir/{sa_id}",
        "keys-router@example.com",
        "service_account.json",
        "keys-router-key.json",
        "gcp-credentials.json",
    ):
        assert bp.is_non_path_metadata(neg) is False

    # Explicit local collision: file=<sa_id> or local secret file alongside sa_id must fail closed
    tid, ws = _create_scratch_task(
        conn,
        result=f"required={sa_id} file={sa_id}.json",
        files={"a.py": "x"},
    )
    with pytest.raises(ValueError, match="prohibited name"):
        bp.plan_bundle(conn, tid, ws)
    assert bp.preservation_gate(tid, ws, conn=conn) is False
    kbw._cleanup_workspace(conn, tid)
    assert ws.exists()


def test_gcp_secret_resource_exact_fixtures_and_grammar_positives(isolated_board):
    """Category 2 (GCP-RESOURCE): Exact JSON fixture, placement= prose context, and varied grammar."""
    conn, _ = isolated_board
    res_id = "projects/100000000001/secrets/po-example-fixture/versions/1"

    # 2a. Exact JSON structure from two-cloud-regression-fixtures.json + sibling
    tid1, ws1 = _create_scratch_task(
        conn,
        files={"a.py": "x", "sibling.md": "notes\n"},
    )
    _insert_completed_run(conn, tid1, metadata={"placement": {"name": res_id}})

    plan1 = bp.plan_bundle(conn, tid1, ws1)
    assert plan1["unresolved_refs"] == []
    assert plan1["ambiguous_refs"] == []
    assert [f["path"] for f in plan1["files"]] == ["a.py", "sibling.md"]

    kbw._cleanup_workspace(conn, tid1)
    assert ws1.exists()
    assert (ws1 / "a.py").read_text(encoding="utf-8") == "x"
    assert (ws1 / "sibling.md").read_text(encoding="utf-8") == "notes\n"
    assert _extract_latest_archive_payloads(conn, tid1) == {
        "a.py": b"x",
        "sibling.md": b"notes\n",
    }

    # 2b. Exact written prose context: placement=projects/... + a.py + sibling
    tid2, ws2 = _create_scratch_task(
        conn,
        result=f"placement={res_id} and a.py",
        files={"a.py": "x", "sibling.md": "notes\n"},
    )
    plan2 = bp.plan_bundle(conn, tid2, ws2)
    assert plan2["unresolved_refs"] == []
    assert [f["path"] for f in plan2["files"]] == ["a.py", "sibling.md"]
    bp.preserve_bundle(conn, tid2, ws2)
    assert _extract_latest_archive_payloads(conn, tid2) == {
        "a.py": b"x",
        "sibling.md": b"notes\n",
    }
    assert bp.preservation_gate(tid2, ws2, conn=conn) is False

    # 2c. Varied grammar positive
    assert bp.is_non_path_metadata("projects/987654321098/secrets/my_secret-v2/versions/42") is True


def test_gcp_resource_negative_controls_and_local_collision(isolated_board):
    """Category 2 (GCP-RESOURCE): Near-match negatives and local path collision fail closed."""
    conn, _ = isolated_board
    res_id = "projects/100000000001/secrets/po-example-fixture/versions/1"

    for neg in (
        "projects/not-digits/secrets/po-example-fixture/versions/1",
        "projects/100000000001/secrets/po-example-fixture/versions/latest",
        f"{res_id}/extra",
        f"./{res_id}",
        "secrets/po-example-fixture",
    ):
        assert bp.is_non_path_metadata(neg) is False

    # Explicit local path context (`path:projects/...`) or local directory collision must fail closed
    tid1, ws1 = _create_scratch_task(
        conn,
        result=f"path:{res_id}",
        files={"a.py": "x"},
    )
    with pytest.raises(ValueError, match="prohibited name"):
        bp.plan_bundle(conn, tid1, ws1)
    assert bp.preservation_gate(tid1, ws1, conn=conn) is False

    # Local collision on disk: creating `projects/100000000001/secrets/...` inside workspace fails closed
    tid2, ws2 = _create_scratch_task(
        conn,
        result=f"placement={res_id}",
        files={"a.py": "x", res_id: "local-secret-bytes"},
    )
    with pytest.raises(ValueError, match="prohibited name"):
        bp.plan_bundle(conn, tid2, ws2)
    assert bp.preservation_gate(tid2, ws2, conn=conn) is False
    kbw._cleanup_workspace(conn, tid2)
    assert ws2.exists()


# ---------------------------------------------------------------------------
# 3. Category ZERO-REF: Zero Initial Attachments with Referenced File (4 Forms)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("form", ["backtick", "quoted", "plain_rel", "absolute"])
def test_zero_initial_attachments_referenced_file_retains_bytes(isolated_board, form: str):
    """Category 3 (ZERO-REF): Real _cleanup_workspace retains workspace on attachments=0 in all 4 forms."""
    conn, _ = isolated_board
    payload = b"important deliverable payload\n"
    tid, ws = _create_scratch_task(conn, files={"deliverable.txt": payload})

    ref_map = {
        "backtick": "Completed `deliverable.txt`",
        "quoted": 'Completed "deliverable.txt"',
        "plain_rel": "deliverable.txt",
        "absolute": f"Completed {ws / 'deliverable.txt'}",
    }
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET result = ? WHERE id = ?", (ref_map[form], tid))

    assert kb.list_attachments(conn, tid) == []
    kbw._cleanup_workspace(conn, tid)

    assert ws.is_dir(), f"workspace must be retained for form={form} when initial attachments=0"
    assert (ws / "deliverable.txt").read_bytes() == payload
    assert _extract_latest_archive_payloads(conn, tid) == {"deliverable.txt": payload}


# ---------------------------------------------------------------------------
# 4. Category ZERO-UNREF: Unreferenced Files, Siblings, and Stale Attachments
# ---------------------------------------------------------------------------


def test_zero_unreferenced_files_full_inventory_and_stale_attachments(isolated_board):
    """Category 4 (ZERO-UNREF): No-reference files, unreferenced siblings, and stale attachments retain."""
    conn, tmp_path = isolated_board

    # 4a. Two unreferenced files with no result and attachments=0
    tid1, ws1 = _create_scratch_task(
        conn,
        result=None,
        files={"one.txt": b"first\n", "two.txt": b"second\n"},
    )
    kbw._cleanup_workspace(conn, tid1)
    assert ws1.is_dir()
    assert (ws1 / "one.txt").read_bytes() == b"first\n"
    assert (ws1 / "two.txt").read_bytes() == b"second\n"
    assert _extract_latest_archive_payloads(conn, tid1) == {
        "one.txt": b"first\n",
        "two.txt": b"second\n",
    }

    # 4b. Referenced file + unreferenced sibling
    tid2, ws2 = _create_scratch_task(
        conn,
        result="Ship `referenced.txt`",
        files={"referenced.txt": b"ref\n", "unreferenced_sibling.txt": b"sib\n"},
    )
    kbw._cleanup_workspace(conn, tid2)
    assert ws2.is_dir()
    assert (ws2 / "referenced.txt").read_bytes() == b"ref\n"
    assert (ws2 / "unreferenced_sibling.txt").read_bytes() == b"sib\n"
    assert _extract_latest_archive_payloads(conn, tid2) == {
        "referenced.txt": b"ref\n",
        "unreferenced_sibling.txt": b"sib\n",
    }

    # 4c. Preexisting unrelated/stale attachment does not authorize deletion
    tid3, ws3 = _create_scratch_task(
        conn,
        result="Ship `referenced.txt`",
        files={"referenced.txt": b"ref3\n", "sibling3.txt": b"sib3\n"},
    )
    unrelated_file = tmp_path / "unrelated.txt"
    unrelated_file.write_text("unrelated attachment\n", encoding="utf-8")
    kb.add_attachment(
        conn,
        tid3,
        filename="unrelated.txt",
        stored_path=str(unrelated_file),
        content_type="text/plain",
        size=unrelated_file.stat().st_size,
        uploaded_by="user",
    )
    assert bp.preservation_gate(tid3, ws3, conn=conn) is False
    kbw._cleanup_workspace(conn, tid3)
    assert ws3.is_dir()
    assert (ws3 / "referenced.txt").read_bytes() == b"ref3\n"
    assert (ws3 / "sibling3.txt").read_bytes() == b"sib3\n"
    # Second call also retains
    kbw._cleanup_workspace(conn, tid3)
    assert ws3.is_dir()


# ---------------------------------------------------------------------------
# 5. Category CALLERS: Direct Call-Through for All Deletion Callsites
# ---------------------------------------------------------------------------


def test_deletion_callers_direct_paths_cannot_bypass_preservation(isolated_board):
    """Category 5 (CALLERS): complete_task, archive_task, deferred parent, worktree (incl. conn=None), and GC."""
    conn, tmp_path = isolated_board

    # 5a. complete_task & archive_task on scratch workspaces
    tid_comp, ws_comp = _create_scratch_task(conn, status="ready", files={"out.txt": b"comp\n"})
    assert kb.claim_task(conn, tid_comp, claimer="w1") is not None
    assert kb.complete_task(conn, tid_comp, result="done `out.txt`") is True
    assert ws_comp.is_dir() and (ws_comp / "out.txt").read_bytes() == b"comp\n"

    tid_arch, ws_arch = _create_scratch_task(conn, status="ready", files={"arch.txt": b"arch\n"})
    assert kb.archive_task(conn, tid_arch) is True
    assert ws_arch.is_dir() and (ws_arch / "arch.txt").read_bytes() == b"arch\n"

    # 5b. Deferred parent scratch cleanup via child completion
    parent_id, ws_parent = _create_scratch_task(conn, status="ready", files={"handoff.txt": b"parent\n"})
    child_id, ws_child = _create_scratch_task(conn, status="ready", files={"child.txt": b"child\n"})
    kb.link_tasks(conn, parent_id, child_id)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent_id,))
    assert kb.claim_task(conn, parent_id, claimer="w1") is not None
    assert kb.complete_task(conn, parent_id, result="handoff") is True
    assert ws_parent.is_dir()

    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child_id,))
    assert kb.claim_task(conn, child_id, claimer="w2") is not None
    assert kb.complete_task(conn, child_id, result="child done") is True
    assert ws_parent.is_dir() and (ws_parent / "handoff.txt").read_bytes() == b"parent\n"

    # 5c. Linked git worktree cleanup (with conn=conn AND conn=None)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", str(origin), str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True, capture_output=True)
    (repo / "README.md").write_text("repo readme\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "push", "origin", "HEAD"], check=True, capture_output=True)

    tid_wt = kb.create_task(conn, title="wt-task", assignee="worker", workspace_kind="worktree")
    wt_path = repo / ".worktrees" / tid_wt
    kbw._ensure_git_worktree(repo, wt_path, f"wt/{tid_wt}")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'archived', workspace_kind = 'worktree', workspace_path = ?, branch_name = ? WHERE id = ?",
            (str(wt_path), f"wt/{tid_wt}", tid_wt),
        )

    # Direct call with conn=None must still pass through preservation_gate and retain
    kbw._cleanup_worktree_workspace(tid_wt, str(wt_path), f"wt/{tid_wt}", conn=None)
    assert wt_path.is_dir() and (wt_path / "README.md").read_text(encoding="utf-8") == "repo readme\n"

    # Direct call with conn=conn also retains (worktree has .git pointer / zero initial attachments)
    kbw._cleanup_worktree_workspace(tid_wt, str(wt_path), f"wt/{tid_wt}", conn=conn)
    assert wt_path.is_dir()

    # 5d. _cmd_gc on archived scratch & worktree
    tid_gc, ws_gc = _create_scratch_task(conn, status="archived", files={"gc_keep.txt": b"gc_data\n"})
    rc = kanban_ops._cmd_gc(Namespace(event_retention_days=30, log_retention_days=30))
    assert rc == 0
    assert ws_gc.is_dir() and (ws_gc / "gc_keep.txt").read_bytes() == b"gc_data\n"
    assert wt_path.is_dir()


# ---------------------------------------------------------------------------
# 6. Category SECRET: Prohibited/Excluded Components & Never-Open Spy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel_path",
    [
        ".env",
        ".env.production",
        "sub/.env.local",
        "secrets.json",
        "secrets/note.txt",
        "my_secret_config.yaml",
        "credentials.json",
        "api_token.txt",
        "passwords.txt",
        "key.json",
        "monkey.json",
        "api-key-prod.json",
        "private_key_1.json",
        "auth",
        "auth.json",
        "user_auth.txt",
        "service-auth.yaml",
        "app.auth_config.json",
        "service_account.json",
        "service-account.txt",
        "serviceaccount.cfg",
        "id_rsa",
        "id_ed25519.pub",
        "tls.pem",
        "signing.key",
        ".git",
        ".git/config",
        "node_modules/pkg/index.js",
        "__pycache__/mod.pyc",
    ],
)
def test_prohibited_and_excluded_components_fail_closed_without_open(
    isolated_board, monkeypatch: pytest.MonkeyPatch, rel_path: str
):
    """Category 6 (SECRET): Prohibited/excluded file or directory components fail closed without opening."""
    conn, _ = isolated_board
    tid, ws = _create_scratch_task(
        conn,
        files={"safe.txt": b"safe\n", rel_path: b"synthetic-forbidden-payload\n"},
    )
    forbidden_target = (ws / rel_path).resolve()

    opened_paths: List[str] = []
    real_os_open = os.open
    real_builtins_open = builtins.open

    def _spy_os_open(path, flags, *args, **kwargs):
        if not isinstance(path, int):
            p_res = Path(path).resolve(strict=False)
            if p_res == forbidden_target or forbidden_target in p_res.parents:
                opened_paths.append(str(path))
        return real_os_open(path, flags, *args, **kwargs)

    def _spy_builtins_open(file, *args, **kwargs):
        if not isinstance(file, int):
            p_res = Path(file).resolve(strict=False)
            if p_res == forbidden_target or forbidden_target in p_res.parents:
                opened_paths.append(str(file))
        return real_builtins_open(file, *args, **kwargs)

    monkeypatch.setattr(os, "open", _spy_os_open)
    monkeypatch.setattr(builtins, "open", _spy_builtins_open)

    with pytest.raises(ValueError, match="unsafe bundle|prohibited name|excluded name"):
        bp.plan_bundle(conn, tid, ws)

    assert bp.preservation_gate(tid, ws, conn=conn) is False
    kbw._cleanup_workspace(conn, tid)
    assert ws.is_dir()
    assert (ws / "safe.txt").read_bytes() == b"safe\n"
    assert opened_paths == [], f"Forbidden entry {rel_path} must NEVER be opened, got {opened_paths}"
    assert kb.list_attachments(conn, tid) == []


def test_missing_prohibited_references_relative_vs_absolute(isolated_board):
    """Category 6 (SECRET): Missing relative prohibited hint records unresolved; missing absolute raises."""
    conn, _ = isolated_board

    # Relative missing prohibited hint in prose -> recorded in unresolved_refs, gate returns False
    tid1, ws1 = _create_scratch_task(
        conn,
        result="See docs/secret-policy.md and safe.txt",
        files={"safe.txt": b"ok\n"},
    )
    plan1 = bp.plan_bundle(conn, tid1, ws1)
    assert plan1["unresolved_refs"] == ["docs/secret-policy.md"]
    assert bp.preservation_gate(tid1, ws1, conn=conn) is False
    kbw._cleanup_workspace(conn, tid1)
    assert ws1.is_dir()

    # Absolute missing prohibited reference -> raises ValueError
    tid2, ws2 = _create_scratch_task(
        conn,
        result=None,
        files={"safe.txt": b"ok\n"},
    )
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET result = ? WHERE id = ?", (f"See {ws2 / 'secrets.json'}", tid2))
    with pytest.raises(ValueError, match="prohibited"):
        bp.plan_bundle(conn, tid2, ws2)
    assert bp.preservation_gate(tid2, ws2, conn=conn) is False


# ---------------------------------------------------------------------------
# 7. Category LINK: Symlinks, Ancestor Links, Dangling Links, FIFOs & Swap Race
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["root_link", "member_link", "dangling_link", "ancestor_link", "fifo"])
def test_symlinks_and_special_entries_fail_closed_without_opening(
    isolated_board, monkeypatch: pytest.MonkeyPatch, kind: str
):
    """Category 7 (LINK): Symlinks and FIFOs fail closed before any open on target."""
    conn, tmp_path = isolated_board
    outside_target = tmp_path / "outside_secret_target.txt"
    outside_target.write_bytes(b"outside-target-bytes\n")

    tid, ws = _create_scratch_task(conn, result="safe.txt", files={"safe.txt": b"safe\n"})
    probe_target = outside_target

    if kind == "root_link":
        link_root = tmp_path / "linked_ws_root"
        link_root.symlink_to(ws, target_is_directory=True)
        assert bp.preservation_gate(tid, link_root, conn=conn) is False
        with pytest.raises(ValueError, match="linked"):
            bp.plan_bundle(conn, tid, link_root)
        return
    elif kind == "member_link":
        (ws / "linked.txt").symlink_to(outside_target)
    elif kind == "dangling_link":
        (ws / "dangling.txt").symlink_to(tmp_path / "does_not_exist.txt")
    elif kind == "ancestor_link":
        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir(exist_ok=True)
        (outside_dir / "inner.txt").write_bytes(b"inner\n")
        probe_target = outside_dir / "inner.txt"
        (ws / "linked_sub").symlink_to(outside_dir, target_is_directory=True)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET result = 'linked_sub/inner.txt' WHERE id = ?", (tid,))
    elif kind == "fifo":
        fifo_path = ws / "named.pipe"
        os.mkfifo(fifo_path)
        probe_target = fifo_path

    opened_targets: List[str] = []
    real_os_open = os.open

    def _spy_open(path, flags, *args, **kwargs):
        if not isinstance(path, int):
            if Path(path).resolve(strict=False) == probe_target.resolve(strict=False):
                opened_targets.append(str(path))
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", _spy_open)

    with pytest.raises(ValueError):
        bp.plan_bundle(conn, tid, ws)
    assert bp.preservation_gate(tid, ws, conn=conn) is False
    kbw._cleanup_workspace(conn, tid)
    assert ws.is_dir()
    assert opened_targets == [], f"Special/linked target must never be opened for kind={kind}"


def test_symlink_swap_or_inode_instability_fails_closed(isolated_board, monkeypatch: pytest.MonkeyPatch):
    """Category 7 (LINK): Swapping a file with a symlink or different inode between lstat and open fails closed."""
    conn, tmp_path = isolated_board
    tid, ws = _create_scratch_task(conn, files={"race.txt": b"initial\n"})
    race_file = ws / "race.txt"
    outside = tmp_path / "swapped_outside.txt"
    outside.write_bytes(b"outside\n")

    real_open = os.open

    def _swap_before_open(path, flags, *args, **kwargs):
        if not isinstance(path, int) and Path(path) == race_file:
            race_file.unlink()
            race_file.symlink_to(outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", _swap_before_open)
    with pytest.raises(ValueError):
        bp.plan_bundle(conn, tid, ws)


# ---------------------------------------------------------------------------
# 8. Category MISSING: Missing Required References & Malformed Metadata JSON
# ---------------------------------------------------------------------------


def test_required_missing_references_and_malformed_metadata_json_retain(isolated_board):
    """Category 8 (MISSING): Missing required refs and malformed task_runs.metadata JSON fail closed."""
    conn, _ = isolated_board

    # 8a. Missing absolute reference in result
    tid1, ws1 = _create_scratch_task(conn, files={"present.txt": b"here\n"})
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET result = ? WHERE id = ?", (str(ws1 / "missing.txt"), tid1))
    with pytest.raises(ValueError, match="missing"):
        bp.plan_bundle(conn, tid1, ws1)
    assert bp.preservation_gate(tid1, ws1, conn=conn) is False
    kbw._cleanup_workspace(conn, tid1)
    assert ws1.is_dir()

    # 8b. Malformed JSON in task_runs.metadata must NOT be silently swallowed
    tid2, ws2 = _create_scratch_task(conn, files={"present.txt": b"here\n"})
    _insert_completed_run(conn, tid2, metadata="{malformed_json:")
    with pytest.raises(ValueError, match="malformed task_runs.metadata"):
        bp.plan_bundle(conn, tid2, ws2)
    assert bp.preservation_gate(tid2, ws2, conn=conn) is False
    kbw._cleanup_workspace(conn, tid2)
    assert ws2.is_dir()

    # 8c. Non-object JSON in task_runs.metadata (e.g. list) fails closed
    tid3, ws3 = _create_scratch_task(conn, files={"present.txt": b"here\n"})
    _insert_completed_run(conn, tid3, metadata="[\"not-an-object\"]")
    with pytest.raises(ValueError, match="malformed task_runs.metadata"):
        bp.plan_bundle(conn, tid3, ws3)
    assert bp.preservation_gate(tid3, ws3, conn=conn) is False


# ---------------------------------------------------------------------------
# 9. Category MARKER: HOLD-WORKSPACE & KEEP-BUNDLE Probe Failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["lstat", "open", "read", "symlink", "directory", "oversize", "chmod"])
def test_keep_marker_probe_failures_retain_before_registration(
    isolated_board, monkeypatch: pytest.MonkeyPatch, kind: str
):
    """Category 9 (MARKER): KEEP-BUNDLE probe failures retain before any attachment registration."""
    conn, _ = isolated_board
    tid, ws = _create_scratch_task(
        conn,
        result="`accepted.patch`",
        files={"accepted.patch": b"diff\n", "other.txt": b"other\n"},
    )
    marker = ws / bp.KEEP_MARKER

    if kind == "symlink":
        marker.symlink_to(ws / "accepted.patch")
    elif kind == "directory":
        marker.mkdir()
    elif kind == "oversize":
        marker.write_bytes(b"k" * (bp.KEEP_MARKER_MAX_BYTES + 1))
    else:
        marker.write_text("keep\n", encoding="utf-8")
        if kind == "chmod":
            os.chmod(marker, 0)
        elif kind == "lstat":
            real_lstat = os.lstat

            def _deny_lstat(p, *a, **kw):
                if str(p).endswith(bp.KEEP_MARKER):
                    raise PermissionError(errno.EACCES, "denied")
                return real_lstat(p, *a, **kw)

            monkeypatch.setattr(os, "lstat", _deny_lstat)
        elif kind == "open":
            real_open = os.open

            def _deny_open(p, flags, *a, **kw):
                if not isinstance(p, int) and str(p).endswith(bp.KEEP_MARKER):
                    raise PermissionError(errno.EACCES, "denied")
                return real_open(p, flags, *a, **kw)

            monkeypatch.setattr(os, "open", _deny_open)
        elif kind == "read":
            real_open = os.open
            real_read = os.read
            keep_fds = set()

            def _track_open(p, flags, *a, **kw):
                fd = real_open(p, flags, *a, **kw)
                if not isinstance(p, int) and str(p).endswith(bp.KEEP_MARKER):
                    keep_fds.add(fd)
                return fd

            def _fail_read(fd, n, *a, **kw):
                if fd in keep_fds:
                    raise OSError(errno.EIO, "io error")
                return real_read(fd, n, *a, **kw)

            monkeypatch.setattr(os, "open", _track_open)
            monkeypatch.setattr(os, "read", _fail_read)

    try:
        with pytest.raises(ValueError, match="keep marker probe failed"):
            bp.plan_bundle(conn, tid, ws)
        assert bp.preservation_gate(tid, ws, conn=conn) is False
        kbw._cleanup_workspace(conn, tid)
        assert ws.is_dir()
        assert (ws / "accepted.patch").read_bytes() == b"diff\n"
        assert (ws / "other.txt").read_bytes() == b"other\n"
        assert kb.list_attachments(conn, tid) == []
    finally:
        if kind == "chmod" and marker.exists() and not marker.is_symlink():
            os.chmod(marker, 0o644)


def test_hold_marker_retains_before_planning_and_dry_run_is_clean(
    isolated_board, monkeypatch: pytest.MonkeyPatch
):
    """Category 9 (MARKER): HOLD-WORKSPACE retains before planning and dry-run writes nothing."""
    conn, _ = isolated_board
    tid, ws = _create_scratch_task(conn, result="`accepted.patch`", files={"accepted.patch": b"diff\n"})
    (ws / bp.HOLD_MARKER).write_text("hold\n", encoding="utf-8")

    before_changes = conn.total_changes
    with pytest.raises(ValueError, match="hold marker"):
        bp.preserve_bundle(conn, tid, ws, dry_run=True)
    assert conn.total_changes == before_changes

    planned = {"called": 0}
    monkeypatch.setattr(bp, "plan_bundle", lambda *_a, **_kw: planned.__setitem__("called", 1))
    assert bp.preservation_gate(tid, ws, conn=conn) is False
    assert planned["called"] == 0


# ---------------------------------------------------------------------------
# 10. Category ROOT: Recorded Checkout (head/), Multiple Findings, Ambiguity & Boundaries
# ---------------------------------------------------------------------------


def test_recorded_checkout_head_only_with_namesake_and_multiple_findings(
    isolated_board, monkeypatch: pytest.MonkeyPatch
):
    """Category 10 (ROOT): Recorded head/ finding resolves cleanly without ambiguity alongside workspace namesake."""
    conn, tmp_path = isolated_board
    rel_src = "app_services/services/gp_fund_setup.py"
    rel_second = "app_services/services/second_service.py"
    head_bytes = b"recorded-head-gp-fund-setup\n"
    second_bytes = b"recorded-head-second-service\n"
    namesake_bytes = b"workspace-root-namesake\n"
    proof_bytes = b"proof-artifact-bytes\n"

    tid, ws = _create_scratch_task(
        conn,
        files={
            f"head/{rel_src}": head_bytes,
            f"head/{rel_second}": second_bytes,
            rel_src: namesake_bytes,
            "evidence/proof.txt": proof_bytes,
        },
    )
    head_dir = ws / "head"
    _insert_completed_run(
        conn,
        tid,
        metadata={
            "worktree": str(head_dir),
            "findings": [
                {"file": rel_src, "proof": str(ws / "evidence" / "proof.txt")},
                {"file": rel_second},
            ],
        },
    )

    plan = bp.plan_bundle(conn, tid, ws)
    assert plan["unresolved_refs"] == []
    assert plan["ambiguous_refs"] == []
    files_by_path = {f["path"]: f["sha256"] for f in plan["files"]}
    assert files_by_path == {
        rel_src: hashlib.sha256(namesake_bytes).hexdigest(),
        "evidence/proof.txt": hashlib.sha256(proof_bytes).hexdigest(),
        f"head/{rel_src}": hashlib.sha256(head_bytes).hexdigest(),
        f"head/{rel_second}": hashlib.sha256(second_bytes).hexdigest(),
    }

    bp.preserve_bundle(conn, tid, ws)
    assert bp.preservation_gate(tid, ws, conn=conn) is False
    payloads = _extract_latest_archive_payloads(conn, tid)
    assert payloads[f"head/{rel_src}"] == head_bytes
    assert payloads[f"head/{rel_second}"] == second_bytes
    assert payloads[rel_src] == namesake_bytes
    assert payloads["evidence/proof.txt"] == proof_bytes

    # Rootless structured finding is explicitly AMBIGUOUS even when workspace namesake exists
    tid_rootless, ws_rootless = _create_scratch_task(
        conn,
        files={rel_src: namesake_bytes},
    )
    _insert_completed_run(conn, tid_rootless, metadata={"findings": [{"file": rel_src}]})
    plan_rootless = bp.plan_bundle(conn, tid_rootless, ws_rootless)
    assert plan_rootless["ambiguous_refs"] == [rel_src]
    assert rel_src not in plan_rootless["unresolved_refs"]
    assert bp.preservation_gate(tid_rootless, ws_rootless, conn=conn) is False

    # Conflicting checkouts across runs make prose relative reference ambiguous
    tid_conf, ws_conf = _create_scratch_task(
        conn,
        result=rel_src,
        files={
            f"head-a/{rel_src}": b"from-a\n",
            f"head-b/{rel_src}": b"from-b\n",
        },
    )
    _insert_completed_run(conn, tid_conf, metadata={"worktree": str(ws_conf / "head-a")})
    _insert_completed_run(conn, tid_conf, metadata={"worktree": str(ws_conf / "head-b")})
    plan_conf = bp.plan_bundle(conn, tid_conf, ws_conf)
    assert plan_conf["ambiguous_refs"] == [rel_src]
    assert bp.preservation_gate(tid_conf, ws_conf, conn=conn) is False

    # Recorded worktree outside workspace fails closed BEFORE any lstat/open on outside path
    outside_wt = tmp_path / "outside_worktree"
    outside_wt.mkdir(exist_ok=True)
    (outside_wt / "file.py").write_bytes(b"outside\n")
    tid_out, ws_out = _create_scratch_task(conn, files={"safe.py": b"safe\n"})
    _insert_completed_run(
        conn,
        tid_out,
        metadata={"worktree": str(outside_wt), "findings": [{"file": "file.py"}]},
    )

    real_lstat = os.lstat
    touched_outside: List[str] = []

    def _spy_lstat(p, *a, **kw):
        p_str = str(p)
        if "outside_worktree" in p_str:
            touched_outside.append(p_str)
        return real_lstat(p, *a, **kw)

    monkeypatch.setattr(os, "lstat", _spy_lstat)
    with pytest.raises(ValueError, match="outside workspace"):
        bp.plan_bundle(conn, tid_out, ws_out)
    assert touched_outside == [], f"Must never lstat outside recorded checkout: {touched_outside}"


# ---------------------------------------------------------------------------
# 11. Category NONPATH: URLs (incl. Malformed Ports), Slugs, Models, Prose Ratios
# ---------------------------------------------------------------------------


def test_non_path_identifiers_and_malformed_port_uri_sanitization(isolated_board):
    """Category 11 (NONPATH): URLs (incl. malformed ports), repo slugs, models, and 5/10. prose."""
    conn, _ = isolated_board
    raw_badport_url = "https://user:secret_pass@example.com:badport/path/to/doc?token=my_tok#frag"
    sanitized_badport = bp.sanitize_uri(raw_badport_url)
    assert sanitized_badport == "https://example.com:badport/path/to/doc"
    assert "secret_pass" not in sanitized_badport
    assert "my_tok" not in sanitized_badport
    assert "frag" not in sanitized_badport

    prose = (
        f"Reviewed Turnstyle/conductive-admin, ratio 5/10. "
        f"Model openai/gpt-4o and gpt-6.1-sol/openai-codex with [link]({raw_badport_url}) "
        f"and custom+git://admin:pw@host:999999/repo/file.md?k=v#s. "
        f"Produced `local_output.txt`."
    )
    tid, ws = _create_scratch_task(
        conn,
        result=prose,
        files={"local_output.txt": b"verified output\n"},
    )
    plan = bp.plan_bundle(conn, tid, ws)
    assert plan["unresolved_refs"] == []
    assert plan["ambiguous_refs"] == []
    assert [f["path"] for f in plan["files"]] == ["local_output.txt"]
    assert plan["unresolved_non_path_refs"] == [
        "Turnstyle/conductive-admin",
        "custom+git://host:999999/repo/file.md",
        "https://example.com:badport/path/to/doc",
    ]

    preserved = bp.preserve_bundle(conn, tid, ws)
    assert preserved["unresolved_refs"] == []
    assert _extract_latest_archive_payloads(conn, tid) == {"local_output.txt": b"verified output\n"}
    assert bp.preservation_gate(tid, ws, conn=conn) is False

    # Explicit local context wins over owner/repo and N/M heuristics
    tid_local, ws_local = _create_scratch_task(
        conn,
        result="artifact=Turnstyle/conductive-admin path:5/10",
        files={"local_output.txt": b"x\n"},
    )
    plan_local = bp.plan_bundle(conn, tid_local, ws_local)
    assert plan_local["unresolved_refs"] == ["5/10", "Turnstyle/conductive-admin"]
    assert bp.preservation_gate(tid_local, ws_local, conn=conn) is False


# ---------------------------------------------------------------------------
# 12. Category COMPATIBILITY: Spaced Paths in Result, Prefix Non-Collision, Directories & Cap
# ---------------------------------------------------------------------------


def test_spaced_paths_in_result_and_missing_spaced_prefix_rejection(isolated_board):
    """Category 12 (COMPATIBILITY): Spaced absolute/relative paths in result, empty dirs, and cap."""
    conn, _ = isolated_board
    spaced_rel = "sub folder/report final.md"
    spaced_bytes = b"spaced report content\n"
    prefix_bytes = b"prefix file only\n"

    tid, ws = _create_scratch_task(
        conn,
        files={
            spaced_rel: spaced_bytes,
            "sub folder/report": prefix_bytes,
        },
    )
    (ws / "empty_dir" / "nested_empty").mkdir(parents=True, exist_ok=True)
    # Reference the spaced absolute path directly in result prose
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET result = ? WHERE id = ?",
            (f"Generated {ws / spaced_rel} successfully.", tid),
        )

    plan = bp.plan_bundle(conn, tid, ws)
    assert plan["unresolved_refs"] == []
    assert plan["ambiguous_refs"] == []
    assert "empty_dir" in plan["directories"]
    assert "empty_dir/nested_empty" in plan["directories"]
    assert [f["path"] for f in plan["files"]] == ["sub folder/report", spaced_rel]

    bp.preserve_bundle(conn, tid, ws)
    assert _extract_latest_archive_payloads(conn, tid) == {
        "sub folder/report": prefix_bytes,
        spaced_rel: spaced_bytes,
    }
    assert bp.preservation_gate(tid, ws, conn=conn) is False

    # Missing spaced absolute path must fail closed even when prefix file `sub folder/report` exists
    tid_miss, ws_miss = _create_scratch_task(
        conn,
        files={"sub folder/report": prefix_bytes},
    )
    missing_spaced = ws_miss / spaced_rel
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET result = ? WHERE id = ?",
            (f"Generated {missing_spaced} successfully.", tid_miss),
        )
    with pytest.raises(ValueError, match="missing"):
        bp.plan_bundle(conn, tid_miss, ws_miss)
    assert bp.preservation_gate(tid_miss, ws_miss, conn=conn) is False

    # Cap enforcement
    with pytest.raises(ValueError, match="exceeds maximum allowed cap"):
        bp.plan_bundle(conn, tid, ws, max_bytes=5)


# ---------------------------------------------------------------------------
# 13. Category EXTERNAL: Declared Artifact Outside Workspace Fails Closed
# ---------------------------------------------------------------------------


def test_declared_artifact_outside_workspace_fails_closed_without_open(
    isolated_board, monkeypatch: pytest.MonkeyPatch
):
    """Category 13 (EXTERNAL): Declared artifact outside workspace fails closed before reading."""
    conn, tmp_path = isolated_board
    external = tmp_path / "outside_deliverable.txt"
    external.write_bytes(b"external payload\n")

    tid, ws = _create_scratch_task(conn, files={"inside.txt": b"inside\n"})
    _insert_completed_run(conn, tid, metadata={"artifacts": [str(external)]})

    opened_external: List[str] = []
    real_open = os.open

    def _spy_open(p, flags, *a, **kw):
        if not isinstance(p, int) and Path(p).resolve(strict=False) == external.resolve():
            opened_external.append(str(p))
        return real_open(p, flags, *a, **kw)

    monkeypatch.setattr(os, "open", _spy_open)

    with pytest.raises(ValueError, match="outside workspace"):
        bp.plan_bundle(conn, tid, ws)
    assert bp.preservation_gate(tid, ws, conn=conn) is False
    kbw._cleanup_workspace(conn, tid)
    assert ws.is_dir()
    assert opened_external == []


# ---------------------------------------------------------------------------
# 14. Category DURABILITY: Repeated Zero-Attachment Retention, Unique Names & Verification
# ---------------------------------------------------------------------------


def test_durability_repeated_zero_initial_attachment_retains_and_unique_captures(isolated_board):
    """Category 14 (DURABILITY) & Finding #1/#4:
    - Repeated automatic preservation_gate / _cleanup_workspace calls on a task that began with
      zero initial attachments must retain on BOTH first and subsequent calls.
    - Modifying workspace between captures allocates collision-free unique attachment names
      (bundle-<id>.tgz, bundle-<id>-2.tgz) without overwriting prior attachment files.
    """
    conn, _ = isolated_board
    tid, ws = _create_scratch_task(
        conn,
        result="Ship `alpha.txt`",
        files={"alpha.txt": b"v1\n"},
    )
    assert kb.list_attachments(conn, tid) == []

    # First automatic gate/cleanup call: captures bundle-<tid>.tgz as auto-capture and retains
    assert bp.preservation_gate(tid, ws, conn=conn) is False
    kbw._cleanup_workspace(conn, tid)
    assert ws.is_dir()
    assert (ws / "alpha.txt").read_bytes() == b"v1\n"

    atts_after_1 = kb.list_attachments(conn, tid)
    assert [a.filename for a in atts_after_1] == [
        f"bundle-{tid}.tgz",
        f"bundle-{tid}-manifest.json",
    ]
    first_arc_path = Path(atts_after_1[0].stored_path)
    first_arc_bytes = first_arc_path.read_bytes()

    # Second automatic gate/cleanup call: self-created auto-bundle must NOT authorize deletion!
    assert bp.preservation_gate(tid, ws, conn=conn) is False
    kbw._cleanup_workspace(conn, tid)
    assert ws.is_dir()
    assert (ws / "alpha.txt").read_bytes() == b"v1\n"

    # Modify workspace and add sibling -> new capture uses collision-free bundle-<tid>-2.tgz
    # without overwriting bundle-<tid>.tgz!
    (ws / "alpha.txt").write_bytes(b"v2-modified\n")
    (ws / "beta.txt").write_bytes(b"new-sibling\n")
    bp.preserve_bundle(conn, tid, ws)

    atts_after_2 = kb.list_attachments(conn, tid)
    assert [a.filename for a in atts_after_2] == [
        f"bundle-{tid}.tgz",
        f"bundle-{tid}-manifest.json",
        f"bundle-{tid}-2.tgz",
        f"bundle-{tid}-2-manifest.json",
    ]
    # Original archive bytes were NEVER overwritten
    assert first_arc_path.read_bytes() == first_arc_bytes
    assert _extract_latest_archive_payloads(conn, tid) == {
        "alpha.txt": b"v2-modified\n",
        "beta.txt": b"new-sibling\n",
    }


def test_existing_bundle_verification_rejects_corrupt_forged_and_half_registered(
    isolated_board, monkeypatch: pytest.MonkeyPatch
):
    """Finding #4: Corrupt archive, forged members, tampered manifest, symlinked artifact, or half-registration retain."""
    conn, tmp_path = isolated_board
    tid, ws = _create_scratch_task(
        conn,
        result="Ship `alpha.txt`",
        files={"alpha.txt": b"good-bytes\n", "sub/nested.txt": b"nested-bytes\n"},
    )
    # Seed an original independent attachment so the task began with >= 1 independent attachment
    kb.store_attachment_bytes(
        conn,
        tid,
        filename="origin-context.txt",
        data=b"origin-independent-attachment\n",
        content_type="text/plain",
        uploaded_by="user",
    )
    bp.preserve_bundle(conn, tid, ws)
    assert bp.preservation_gate(tid, ws, conn=conn) is True

    atts = kb.list_attachments(conn, tid)
    arc_att = next(a for a in atts if a.filename.endswith(".tgz"))
    man_att = next(a for a in atts if a.filename.endswith("-manifest.json"))
    arc_path = Path(arc_att.stored_path)
    man_path = Path(man_att.stored_path)
    good_arc_bytes = arc_path.read_bytes()
    good_man_bytes = man_path.read_bytes()

    # 1. Corrupt archive bytes on disk (manifest still matches) -> must retain!
    arc_path.write_bytes(b"corrupt-not-a-valid-gzip")
    with kb.write_txn(conn):
        conn.execute("UPDATE task_attachments SET size = ? WHERE id = ?", (arc_path.stat().st_size, arc_att.id))
    assert bp.preservation_gate(tid, ws, conn=conn) is False

    # 2. Forged archive missing `sub` directory entry -> must retain!
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, b_val in (("alpha.txt", b"good-bytes\n"), ("sub/nested.txt", b"nested-bytes\n")):
            ti = tarfile.TarInfo(name=rel)
            ti.size = len(b_val)
            tar.addfile(ti, io.BytesIO(b_val))
    arc_path.write_bytes(buf.getvalue())
    with kb.write_txn(conn):
        conn.execute("UPDATE task_attachments SET size = ? WHERE id = ?", (arc_path.stat().st_size, arc_att.id))
    assert bp.preservation_gate(tid, ws, conn=conn) is False

    # Restore valid archive
    arc_path.write_bytes(good_arc_bytes)
    with kb.write_txn(conn):
        conn.execute("UPDATE task_attachments SET size = ? WHERE id = ?", (len(good_arc_bytes), arc_att.id))
    assert bp.preservation_gate(tid, ws, conn=conn) is True

    # 3. Symlinked manifest file -> must retain!
    ext_man = tmp_path / "external_manifest.json"
    ext_man.write_bytes(good_man_bytes)
    man_path.unlink()
    man_path.symlink_to(ext_man)
    assert bp.preservation_gate(tid, ws, conn=conn) is False
    man_path.unlink()
    man_path.write_bytes(good_man_bytes)
    assert bp.preservation_gate(tid, ws, conn=conn) is True

    # 3b. Stale capture: modifying a file or adding a sibling after capture -> must retain!
    (ws / "alpha.txt").write_bytes(b"mutated-bytes\n")
    assert bp.preservation_gate(tid, ws, conn=conn) is False
    (ws / "alpha.txt").write_bytes(b"good-bytes\n")
    assert bp.preservation_gate(tid, ws, conn=conn) is True

    (ws / "new_sibling.txt").write_bytes(b"new-sibling\n")
    assert bp.preservation_gate(tid, ws, conn=conn) is False
    (ws / "new_sibling.txt").unlink()
    assert bp.preservation_gate(tid, ws, conn=conn) is True

    # 4. Half-registration (delete manifest row so only archive row remains) -> must retain!
    kb.delete_attachment(conn, int(man_att.id))
    assert bp.preservation_gate(tid, ws, conn=conn) is False

    # 5. Half-registration failure during preserve_bundle cleans up first attachment row
    tid_half, ws_half = _create_scratch_task(conn, files={"a.txt": b"hello\n"})
    orig_add = kb.add_attachment
    call_count = {"n": 0}

    def _fail_second_add(c, t, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated manifest registration failure")
        return orig_add(c, t, **kwargs)

    monkeypatch.setattr(kb, "add_attachment", _fail_second_add)
    with pytest.raises(ValueError, match="preservation failed"):
        bp.preserve_bundle(conn, tid_half, ws_half)
    assert kb.list_attachments(conn, tid_half) == [], "half-registered archive row must be rolled back"


# ---------------------------------------------------------------------------
# 15. Case (d): External-Skill Provenance (`metadata.skill_updated` & Semantic Variants)
# ---------------------------------------------------------------------------


def test_case_d_external_skill_provenance_positive_and_semantic_variants(
    isolated_board, monkeypatch: pytest.MonkeyPatch
):
    """Case (d) (EXTERNAL-SKILL-SPEC.md & CASE-D-ADDENDUM.md):
    - metadata.skill_updated = 'fleet-bot-standup/references/canonical-bot-chat-repair.md'
      is classified as external skill provenance, not a missing required workspace member.
    - Semantic variants (another skill reference under skill_updated/skill_created and an
      absolute synthetic external skill path) satisfy the same rule WITHOUT opening,
      stat-traversing, or hashing external skill paths.
    - Actual workspace files (a.py, preservation_probe.py, unreferenced_sibling.txt) are
      fully captured with exact archive/member/manifest/attachment readback; external
      skill provenance is never reported as captured local coverage.
    - Repeated automatic cleanup on zero initial attachments still retains the workspace.
    """
    conn, tmp_path = isolated_board

    exact_skill_ref = "fleet-bot-standup/references/canonical-bot-chat-repair.md"
    variant_skill_ref = "ops-escalation/references/triage-playbook.md"
    ext_skill_root = tmp_path / "external_profile_skills"
    abs_ext_skill = ext_skill_root / "fleet-bot-standup" / "references" / "canonical-bot-chat-repair.md"
    abs_ext_skill.parent.mkdir(parents=True, exist_ok=True)
    abs_ext_skill.write_bytes(b"# Synthetic external skill reference\n")
    ext_root_resolved = ext_skill_root.resolve()

    expected_local_files = {
        "a.py": b"print('a')\n",
        "preservation_probe.py": b"# diagnostic probe script\nassert True\n",
        "unreferenced_sibling.txt": b"sibling content\n",
    }

    # Install open/lstat/stat/scandir spies to prove external skill path is never opened or stat-traversed
    touched_external: List[Tuple[str, str]] = []
    real_open = os.open
    real_lstat = os.lstat
    real_stat = os.stat
    real_scandir = os.scandir

    def _check_not_ext(op: str, p: Any) -> None:
        if isinstance(p, (str, Path)):
            s_p = str(p)
            if "fleet-bot-standup" in s_p or "ops-escalation" in s_p or s_p.startswith(str(ext_root_resolved)):
                touched_external.append((op, s_p))

    def _spy_open(p, flags, *a, **kw):
        _check_not_ext("open", p)
        return real_open(p, flags, *a, **kw)

    def _spy_lstat(p, *a, **kw):
        _check_not_ext("lstat", p)
        return real_lstat(p, *a, **kw)

    def _spy_stat(p, *a, **kw):
        _check_not_ext("stat", p)
        return real_stat(p, *a, **kw)

    def _spy_scandir(p, *a, **kw):
        _check_not_ext("scandir", p)
        return real_scandir(p, *a, **kw)

    monkeypatch.setattr(os, "open", _spy_open)
    monkeypatch.setattr(os, "lstat", _spy_lstat)
    monkeypatch.setattr(os, "stat", _spy_stat)
    monkeypatch.setattr(os, "scandir", _spy_scandir)

    variants = [
        {"skill_updated": exact_skill_ref},
        {"skill_updated": variant_skill_ref, "skill_created": "hermes-board-ops/SKILL.md"},
        {"skill_updated": str(abs_ext_skill)},
    ]

    for meta_payload in variants:
        tid, ws = _create_scratch_task(conn, files=expected_local_files)
        _insert_completed_run(conn, tid, metadata=meta_payload)

        plan = bp.plan_bundle(conn, tid, ws)
        assert plan["unresolved_refs"] == []
        assert plan["ambiguous_refs"] == []
        assert [f["path"] for f in plan["files"]] == [
            "a.py",
            "preservation_probe.py",
            "unreferenced_sibling.txt",
        ]
        # External skill provenance is recorded as provenance and NEVER claimed as a local file
        for ext_val in meta_payload.values():
            assert ext_val in plan["external_skill_refs"]
            assert ext_val not in [f["path"] for f in plan["files"]]

        # First and second automatic cleanup calls capture full bundle and retain workspace (attachments=0)
        assert bp.preservation_gate(tid, ws, conn=conn) is False
        kbw._cleanup_workspace(conn, tid)
        assert ws.is_dir()
        assert bp.preservation_gate(tid, ws, conn=conn) is False
        kbw._cleanup_workspace(conn, tid)
        assert ws.is_dir()

        for rel_name, expected_bytes in expected_local_files.items():
            assert (ws / rel_name).read_bytes() == expected_bytes

        # Verify durable archive, manifest, and attachment readbacks contain exact local files only
        assert _extract_latest_archive_payloads(conn, tid) == expected_local_files
        atts = kb.list_attachments(conn, tid)
        assert len(atts) == 2
        man_att = next(a for a in atts if a.filename.endswith("-manifest.json"))
        man_entries = json.loads(Path(man_att.stored_path).read_text(encoding="utf-8"))
        assert [e["path"] for e in man_entries] == [
            "a.py",
            "preservation_probe.py",
            "unreferenced_sibling.txt",
        ]

        # Source run metadata remains completely unaltered
        run_row = conn.execute("SELECT metadata FROM task_runs WHERE task_id = ?", (tid,)).fetchone()
        assert json.loads(run_row["metadata"]) == meta_payload

    assert touched_external == [], f"External skill paths must never be opened or stat-traversed: {touched_external}"


def test_case_d_external_skill_negative_controls_fail_closed(
    isolated_board, monkeypatch: pytest.MonkeyPatch
):
    """Case (d) Negative Controls:
    - Same string 'fleet-bot-standup/references/canonical-bot-chat-repair.md' in findings[].file
      or result or non-provenance metadata remains a required local reference and fails closed
      when missing, rootless/ambiguous, outside-root, prohibited, or linked.
    - Declared external artifact in metadata['artifacts'] still fails closed.
    - Symlink or prohibited file in workspace still fails closed even when metadata.skill_updated is set.
    """
    conn, tmp_path = isolated_board
    skill_str = "fleet-bot-standup/references/canonical-bot-chat-repair.md"
    base_files = {
        "a.py": b"print('a')\n",
        "preservation_probe.py": b"assert True\n",
    }

    # 1. Same string in findings[].file under recorded checkout when missing -> raises ValueError & retains
    tid1, ws1 = _create_scratch_task(conn, files=base_files)
    (ws1 / "head").mkdir()
    _insert_completed_run(
        conn,
        tid1,
        metadata={
            "skill_updated": skill_str,
            "worktree": str(ws1 / "head"),
            "findings": [{"file": skill_str}],
        },
    )
    with pytest.raises(ValueError, match="missing"):
        bp.plan_bundle(conn, tid1, ws1)
    assert bp.preservation_gate(tid1, ws1, conn=conn) is False
    kbw._cleanup_workspace(conn, tid1)
    assert ws1.is_dir()

    # 2. Same string in findings[].file without recorded checkout -> ambiguous_refs & retains
    tid2, ws2 = _create_scratch_task(conn, files=base_files)
    _insert_completed_run(
        conn,
        tid2,
        metadata={
            "skill_updated": skill_str,
            "findings": [{"file": skill_str}],
        },
    )
    plan2 = bp.plan_bundle(conn, tid2, ws2)
    assert skill_str in plan2["ambiguous_refs"]
    assert bp.preservation_gate(tid2, ws2, conn=conn) is False
    kbw._cleanup_workspace(conn, tid2)
    assert ws2.is_dir()

    # 3. Same string in task result (required local reference context) when missing -> unresolved_refs & retains
    tid3, ws3 = _create_scratch_task(
        conn,
        result=f"Required output `{skill_str}` and a.py",
        files=base_files,
    )
    _insert_completed_run(conn, tid3, metadata={"skill_updated": skill_str})
    plan3 = bp.plan_bundle(conn, tid3, ws3)
    assert skill_str in plan3["unresolved_refs"]
    assert bp.preservation_gate(tid3, ws3, conn=conn) is False
    kbw._cleanup_workspace(conn, tid3)
    assert ws3.is_dir()

    # 4. Same string under an explicit artifact/file metadata key -> unresolved_refs & retains
    tid4, ws4 = _create_scratch_task(conn, files=base_files)
    _insert_completed_run(conn, tid4, metadata={"artifacts": [skill_str]})
    plan4 = bp.plan_bundle(conn, tid4, ws4)
    assert skill_str in plan4["unresolved_refs"]
    assert bp.preservation_gate(tid4, ws4, conn=conn) is False

    # 5. Absolute external skill path under metadata['artifacts'] or result -> outside-root ValueError & retains
    ext_file = tmp_path / "ext_skill_neg" / "canonical-bot-chat-repair.md"
    ext_file.parent.mkdir(parents=True, exist_ok=True)
    ext_file.write_bytes(b"outside\n")
    tid5, ws5 = _create_scratch_task(conn, files=base_files)
    _insert_completed_run(
        conn,
        tid5,
        metadata={"skill_updated": skill_str, "artifacts": [str(ext_file)]},
    )
    with pytest.raises(ValueError, match="outside workspace"):
        bp.plan_bundle(conn, tid5, ws5)
    assert bp.preservation_gate(tid5, ws5, conn=conn) is False

    # 6. Workspace containing a symlinked or secret-named file alongside metadata.skill_updated -> fails closed
    tid6, ws6 = _create_scratch_task(conn, files=base_files)
    link_p = ws6 / "fleet-bot-standup" / "references" / "canonical-bot-chat-repair.md"
    link_p.parent.mkdir(parents=True, exist_ok=True)
    link_p.symlink_to(ext_file)
    _insert_completed_run(conn, tid6, metadata={"skill_updated": skill_str})
    with pytest.raises(ValueError, match="symlink"):
        bp.plan_bundle(conn, tid6, ws6)
    assert bp.preservation_gate(tid6, ws6, conn=conn) is False

    tid7, ws7 = _create_scratch_task(
        conn,
        files={**base_files, "fleet-bot-standup/references/service_key_1.json": b"{}\n"},
    )
    _insert_completed_run(conn, tid7, metadata={"skill_updated": skill_str})
    with pytest.raises(ValueError, match="prohibited"):
        bp.plan_bundle(conn, tid7, ws7)
    assert bp.preservation_gate(tid7, ws7, conn=conn) is False

