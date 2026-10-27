"""Phase A Focused Reference Classification Tests for `plan_bundle`.

Covers ONLY Phase A reference classification in `hermes_cli/kanban_bundle_preserve.py`:
- Case (a): GCP service-account emails (`*.iam.gserviceaccount.com`) and GCP Secret
  Manager resource identifiers (`projects/<digits>/secrets/<name>/versions/<n>`).
- Case (d): External skill provenance under `metadata.skill_updated` (singular/nested
  and semantic variants, relative and synthetic absolute paths).
- Case (e): Plural/list external skill provenance under
  `metadata.updated_own_skill_references` (including prohibited-word substrings in
  external skill reference names).
- Case (g): Non-path scalar slash labels under documented scalar descriptor keys
  such as nested `metadata.review.family = 'Gemini/Google'`.

Also verifies:
- Nested explicit local `path`/`required`/`reference`/`files_changed`/`report_file`
  children inside external skill provenance or scalar metadata dicts are NEVER
  swallowed and fail closed.
- Raising/recording spies prove zero external filesystem `open`/`stat`/`lstat`/`scandir`
  probes on nonlocal identifiers, external skill provenance, and scalar slash labels.
- Genuine local workspace references still fail closed on missing, prohibited/secret,
  symlink, traversal/outside-root, and ambiguous-root conditions.
- Prior classifier snapshots fail (RED) in an isolated disposable module copy without
  mutating production code.
"""
from __future__ import annotations

import builtins
import importlib.util
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from hermes_cli import kanban_bundle_preserve as bp
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def isolated_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Tuple[sqlite3.Connection, Path]:
    """Provide an isolated disposable Kanban board and GIT_CEILING_DIRECTORIES."""
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    for k in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_DIR",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_ATTACHMENTS_ROOT",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    existing_ceiling = os.environ.get("GIT_CEILING_DIRECTORIES", "")
    ceiling_parts = [str(tmp_path)]
    if existing_ceiling:
        ceiling_parts.append(existing_ceiling)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", os.pathsep.join(ceiling_parts))

    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn, tmp_path


def _create_workspace_task(
    conn: sqlite3.Connection,
    *,
    title: str = "Phase A classification task",
    status: str = "done",
    result: Optional[str] = None,
    files: Optional[Dict[str, bytes | str]] = None,
) -> Tuple[str, Path]:
    tid = kb.create_task(conn, title=title, assignee="worker", workspace_kind="scratch")
    ws = kb.workspaces_root() / tid
    ws.mkdir(parents=True, exist_ok=True)
    if files:
        for rel_path, content in files.items():
            target = ws / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content, encoding="utf-8")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = ?, workspace_path = ?, result = ? WHERE id = ?",
            (status, str(ws), result, tid),
        )
    return tid, ws


def _insert_run(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    summary: Optional[str] = "Completed run",
    metadata: Optional[Dict[str, Any]] = None,
) -> int:
    meta_str = json.dumps(metadata) if metadata is not None else None
    with kb.write_txn(conn):
        cur = conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, summary, metadata, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?, 1, 2)",
            (task_id, summary, meta_str),
        )
        return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# 1. Case (a): GCP Service-Account Emails & Secret Manager Resource IDs
# ---------------------------------------------------------------------------


def test_phase_a_case_a_gcp_identifiers_metadata_and_prose_without_fs_probes(
    isolated_board: Tuple[sqlite3.Connection, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case (a) Positive Classification:
    - Exact shapes from `two-cloud-regression-fixtures.json`:
      1. `metadata={"cloud_account": "keys-router@mission-control-000000.iam.gserviceaccount.com"}`
      2. `metadata={"placement": {"name": "projects/100000000001/secrets/po-example-fixture/versions/1"}}`
    - Also tested in `required=<sa-email>`, `placement=<resource-id>`, `run.summary`, and list shapes.
    - Must classify as nonlocal metadata without probing the filesystem (`os.lstat`/`os.stat`/`os.open`)
      for the nonlocal identifier strings.
    - Captures full safe workspace inventory (`a.py` + unreferenced siblings).
    """
    conn, _ = isolated_board
    sa_id = "keys-router@mission-control-000000.iam.gserviceaccount.com"
    sa_variant = "ci-deployer+canary_1@sub.prod-project-99.iam.gserviceaccount.com"
    res_id = "projects/100000000001/secrets/po-example-fixture/versions/1"
    res_variant = "projects/987654321098/secrets/my_secret-v2/versions/42"

    probed_nonlocal: List[str] = []
    real_lstat = os.lstat
    real_stat = os.stat
    real_os_open = os.open
    real_builtins_open = builtins.open

    def _guard_nonlocal(path_arg: object, op: str) -> None:
        if isinstance(path_arg, int):
            return
        p_str = os.fspath(path_arg)
        if isinstance(p_str, bytes):
            p_str = p_str.decode("utf-8", errors="replace")
        for token in (sa_id, sa_variant, res_id, res_variant, "iam.gserviceaccount.com", "po-example-fixture"):
            if token in p_str:
                probed_nonlocal.append(f"{op}:{p_str}")
                raise AssertionError(f"Forbidden filesystem probe {op} on nonlocal GCP identifier: {p_str}")

    monkeypatch.setattr(
        os, "lstat", lambda p, *a, **kw: (_guard_nonlocal(p, "os.lstat"), real_lstat(p, *a, **kw))[1]
    )
    monkeypatch.setattr(
        os, "stat", lambda p, *a, **kw: (_guard_nonlocal(p, "os.stat"), real_stat(p, *a, **kw))[1]
    )
    monkeypatch.setattr(
        os,
        "open",
        lambda p, flags, *a, **kw: (_guard_nonlocal(p, "os.open"), real_os_open(p, flags, *a, **kw))[1],
    )
    monkeypatch.setattr(
        builtins,
        "open",
        lambda p, *a, **kw: (_guard_nonlocal(p, "builtins.open"), real_builtins_open(p, *a, **kw))[1],
    )

    # 1. Exact fixture 1: gcp_service_account_identifier_is_not_a_missing_file
    tid1, ws1 = _create_workspace_task(
        conn,
        files={"a.py": b"x", "unreferenced_sibling.py": b"sibling = 1\n"},
    )
    _insert_run(conn, tid1, metadata={"cloud_account": sa_id})
    plan1 = bp.plan_bundle(conn, tid1, ws1)
    assert plan1["unresolved_refs"] == []
    assert plan1["ambiguous_refs"] == []
    assert [f["path"] for f in plan1["files"]] == ["a.py", "unreferenced_sibling.py"]

    # 2. Exact fixture 2: gcp_secret_version_resource_is_not_a_secret_path
    tid2, ws2 = _create_workspace_task(
        conn,
        files={"a.py": b"x", "sibling.md": b"notes\n"},
    )
    _insert_run(conn, tid2, metadata={"placement": {"name": res_id}})
    plan2 = bp.plan_bundle(conn, tid2, ws2)
    assert plan2["unresolved_refs"] == []
    assert plan2["ambiguous_refs"] == []
    assert [f["path"] for f in plan2["files"]] == ["a.py", "sibling.md"]

    # 3. Written contexts (required=<sa> and placement=<res>) + nested list/dict variants
    tid3, ws3 = _create_workspace_task(
        conn,
        result=f"Verified required={sa_id} placement={res_id} and updated `a.py`",
        files={"a.py": b"x", "extra.txt": b"extra\n"},
    )
    _insert_run(
        conn,
        tid3,
        summary=f"Used {sa_variant} with {res_variant}",
        metadata={
            "required": sa_id,
            "cloud_accounts": [sa_id, sa_variant],
            "placement": {"name": res_id, "alternates": [res_variant]},
        },
    )
    plan3 = bp.plan_bundle(conn, tid3, ws3)
    assert plan3["unresolved_refs"] == []
    assert plan3["ambiguous_refs"] == []
    assert [f["path"] for f in plan3["files"]] == ["a.py", "extra.txt"]
    assert plan3["referenced_files"] == ["a.py"]
    assert probed_nonlocal == []


def test_phase_a_case_a_negative_controls_fail_closed(
    isolated_board: Tuple[sqlite3.Connection, Path],
) -> None:
    """Case (a) Negative Controls:
    - Near-match strings and local secret filenames are rejected by `is_non_path_metadata`.
    - Explicit local path contexts (`file=<sa>`, `path:<res>`, `artifacts: [...]`, `findings[].file`)
      and local filesystem collisions fail closed.
    """
    conn, _ = isolated_board
    sa_id = "keys-router@mission-control-000000.iam.gserviceaccount.com"
    res_id = "projects/100000000001/secrets/po-example-fixture/versions/1"

    for invalid_token in (
        f"{sa_id}.json",
        f"dir/{sa_id}",
        "keys-router@example.com",
        "service_account.json",
        "projects/not-digits/secrets/po-example-fixture/versions/1",
        "projects/100000000001/secrets/po-example-fixture/versions/latest",
        f"{res_id}/extra",
        f"./{res_id}",
        "secrets/po-example-fixture",
    ):
        assert bp.is_non_path_metadata(invalid_token) is False

    # Explicit local `file=` / `path:` context fails closed
    tid1, ws1 = _create_workspace_task(
        conn,
        result=f"required={sa_id} file={sa_id}.json",
        files={"a.py": b"x"},
    )
    with pytest.raises(ValueError, match="prohibited name"):
        bp.plan_bundle(conn, tid1, ws1)

    tid2, ws2 = _create_workspace_task(
        conn,
        result=f"path:{res_id}",
        files={"a.py": b"x"},
    )
    with pytest.raises(ValueError, match="prohibited name"):
        bp.plan_bundle(conn, tid2, ws2)

    # Explicit metadata["artifacts"] with SA email fails closed as prohibited local ref
    tid3, ws3 = _create_workspace_task(conn, files={"a.py": b"x"})
    _insert_run(conn, tid3, metadata={"artifacts": [sa_id]})
    with pytest.raises(ValueError, match="prohibited name"):
        bp.plan_bundle(conn, tid3, ws3)

    # Local disk collision with secret resource path fails closed
    tid4, ws4 = _create_workspace_task(
        conn,
        result=f"placement={res_id}",
        files={"a.py": b"x", res_id: b"local-secret-material"},
    )
    with pytest.raises(ValueError, match="prohibited name"):
        bp.plan_bundle(conn, tid4, ws4)


# ---------------------------------------------------------------------------
# 2. Case (d): Singular/Nested External Skill Provenance (`skill_updated`)
# ---------------------------------------------------------------------------


def test_phase_a_case_d_external_skill_updated_and_semantic_variants(
    isolated_board: Tuple[sqlite3.Connection, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case (d) Positive Classification:
    - Origin `t_51216964` shape: `metadata={"skill_updated": "fleet-bot-standup/references/canonical-bot-chat-repair.md"}`
      plus semantic key variants and synthetic absolute external skill paths.
    - Raising/recording spies prove zero external `open`/`stat`/`lstat`/`scandir` probes.
    - Full safe workspace inventory (`a.py`, `preservation_probe.py`, unreferenced siblings) is planned.
    """
    conn, tmp_path = isolated_board
    rel_skill = "fleet-bot-standup/references/canonical-bot-chat-repair.md"
    abs_skill = tmp_path / "synthetic_profile_d" / "skills" / "fleet-bot-standup" / "references" / "canonical-bot-chat-repair.md"
    abs_skill.parent.mkdir(parents=True, exist_ok=True)
    abs_skill.write_text("# external skill doc\n", encoding="utf-8")

    real_lstat = os.lstat
    real_stat = os.stat
    real_os_open = os.open
    real_builtins_open = builtins.open
    real_scandir = os.scandir

    def _forbid_ext_skill(path_arg: object, op: str) -> None:
        if isinstance(path_arg, int):
            return
        p_str = os.fspath(path_arg)
        if isinstance(p_str, bytes):
            p_str = p_str.decode("utf-8", errors="replace")
        if "canonical-bot-chat-repair.md" in p_str or "synthetic_profile_d" in p_str:
            raise AssertionError(f"Forbidden external skill filesystem probe {op}: {p_str}")

    monkeypatch.setattr(
        os, "lstat", lambda p, *a, **kw: (_forbid_ext_skill(p, "os.lstat"), real_lstat(p, *a, **kw))[1]
    )
    monkeypatch.setattr(
        os, "stat", lambda p, *a, **kw: (_forbid_ext_skill(p, "os.stat"), real_stat(p, *a, **kw))[1]
    )
    monkeypatch.setattr(
        os, "open", lambda p, flags, *a, **kw: (_forbid_ext_skill(p, "os.open"), real_os_open(p, flags, *a, **kw))[1]
    )
    monkeypatch.setattr(
        builtins, "open", lambda p, *a, **kw: (_forbid_ext_skill(p, "builtins.open"), real_builtins_open(p, *a, **kw))[1]
    )
    monkeypatch.setattr(
        os, "scandir", lambda p, *a, **kw: (_forbid_ext_skill(p, "os.scandir"), real_scandir(p, *a, **kw))[1]
    )

    workspace_files = {
        "a.py": b"print('a')\n",
        "preservation_probe.py": b"assert True\n",
        "unreferenced_sibling.txt": b"sibling\n",
    }

    for meta_payload, expected_ext in (
        ({"skill_updated": rel_skill}, [rel_skill]),
        ({"skill_reference": rel_skill}, [rel_skill]),
        ({"external_skill_path": str(abs_skill)}, [str(abs_skill)]),
        ({"provenance": {"skill_updated": rel_skill}}, [rel_skill]),
    ):
        tid, ws = _create_workspace_task(
            conn,
            result="Updated `a.py` and `preservation_probe.py`",
            files=workspace_files,
        )
        _insert_run(conn, tid, metadata=meta_payload)
        plan = bp.plan_bundle(conn, tid, ws)
        assert plan["unresolved_refs"] == []
        assert plan["ambiguous_refs"] == []
        assert plan["external_skill_refs"] == expected_ext
        assert [f["path"] for f in plan["files"]] == sorted(workspace_files.keys())
        assert plan["referenced_files"] == ["a.py", "preservation_probe.py"]


def test_phase_a_case_d_nested_explicit_local_children_not_swallowed_and_negatives(
    isolated_board: Tuple[sqlite3.Connection, Path],
) -> None:
    """Case (d) Negative Controls & Nested Explicit Local Child Escape Protection:
    - External provenance context (`skill_updated`) MUST NOT swallow nested explicit local
      `path`, `required`, `reference`, `local_path`, `files_changed`, or `report_file` children.
    - Bare single-segment local filenames (`missing_local.py`), relative dot-paths (`./...`),
      and arbitrary non-skill outside-root absolute paths (`/etc/passwd`) under `skill_updated`
      must NOT be classified as external skill provenance and must fail closed.
    """
    conn, _ = isolated_board
    rel_skill = "fleet-bot-standup/references/canonical-bot-chat-repair.md"
    base_files = {"a.py": b"print('a')\n", "preservation_probe.py": b"assert True\n"}

    # 1. Nested explicit local child keys inside `skill_updated` dict must fail closed
    for child_key, missing_local in (
        ("required", "missing_required_script.py"),
        ("path", "missing_explicit_path.py"),
        ("local_path", "sub/missing_local_path.py"),
        ("reference", "sub/missing_local_reference.md"),
        ("files_changed", ["missing_changed_file.py"]),
        ("report_file", "missing_report.md"),
    ):
        tid, ws = _create_workspace_task(conn, files=base_files)
        _insert_run(
            conn,
            tid,
            metadata={
                "skill_updated": {
                    "skill_updated": rel_skill,
                    child_key: missing_local,
                }
            },
        )
        plan = bp.plan_bundle(conn, tid, ws)
        expected_missing = missing_local[0] if isinstance(missing_local, list) else missing_local
        assert expected_missing in plan["unresolved_refs"], (
            f"Nested explicit local child key {child_key!r} under skill_updated was swallowed!"
        )
        assert expected_missing not in plan["external_skill_refs"]
        assert rel_skill in plan["external_skill_refs"]

    # 2. Bare single-segment local filename under `skill_updated` is NOT valid skill provenance
    tid_bare, ws_bare = _create_workspace_task(conn, files=base_files)
    _insert_run(conn, tid_bare, metadata={"skill_updated": "missing_bare_local.py"})
    plan_bare = bp.plan_bundle(conn, tid_bare, ws_bare)
    assert "missing_bare_local.py" in plan_bare["unresolved_refs"]
    assert "missing_bare_local.py" not in plan_bare["external_skill_refs"]

    # 3. Arbitrary non-skill outside-root absolute path under `skill_updated` fails closed
    tid_abs, ws_abs = _create_workspace_task(conn, files=base_files)
    _insert_run(conn, tid_abs, metadata={"skill_updated": "/etc/passwd"})
    with pytest.raises(ValueError, match="outside workspace"):
        bp.plan_bundle(conn, tid_abs, ws_abs)

    # 4. Relative `./` prefix under `skill_updated` fails closed as local ref
    tid_dot, ws_dot = _create_workspace_task(conn, files=base_files)
    _insert_run(conn, tid_dot, metadata={"skill_updated": f"./{rel_skill}"})
    plan_dot = bp.plan_bundle(conn, tid_dot, ws_dot)
    assert rel_skill in plan_dot["unresolved_refs"]
    assert plan_dot["external_skill_refs"] == []


# ---------------------------------------------------------------------------
# 3. Case (e): Plural/List External Skill Provenance (`updated_own_skill_references`)
# ---------------------------------------------------------------------------


def test_phase_a_case_e_plural_skill_provenance_and_nested_local_child_protection(
    isolated_board: Tuple[sqlite3.Connection, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case (e) Positive & Nested Child Negative Controls:
    - Origin `t_b7dead19` shape: `metadata={"updated_own_skill_references": [
      "fleet-bot-standup/references/blind-credential-helpers.md", "jev/references/context-tiers.md"]}`.
    - Prohibited substring `'credential'` in external skill provenance does NOT trigger local
      prohibited-name rejection, and zero external filesystem probes occur.
    - Nested explicit local `required`, `reference`, `path`, `local_path` children inside list
      elements under `updated_own_skill_references` are NOT swallowed and fail closed.
    """
    conn, tmp_path = isolated_board
    cred_skill = "fleet-bot-standup/references/blind-credential-helpers.md"
    jev_skill = "jev/references/context-tiers.md"
    abs_secret_skill = (
        tmp_path / "synthetic_profile_e" / "skills" / "auth-router" / "references" / "secret-token-rotation.md"
    )
    abs_secret_skill.parent.mkdir(parents=True, exist_ok=True)
    abs_secret_skill.write_text("# secret token skill\n", encoding="utf-8")

    real_lstat = os.lstat
    real_stat = os.stat
    real_os_open = os.open

    def _forbid_ext_e(path_arg: object, op: str) -> None:
        if isinstance(path_arg, int):
            return
        p_str = os.fspath(path_arg)
        if isinstance(p_str, bytes):
            p_str = p_str.decode("utf-8", errors="replace")
        for marker in ("blind-credential-helpers.md", "context-tiers.md", "secret-token-rotation.md"):
            if marker in p_str:
                raise AssertionError(f"Forbidden external skill probe {op}: {p_str}")

    monkeypatch.setattr(
        os, "lstat", lambda p, *a, **kw: (_forbid_ext_e(p, "os.lstat"), real_lstat(p, *a, **kw))[1]
    )
    monkeypatch.setattr(
        os, "stat", lambda p, *a, **kw: (_forbid_ext_e(p, "os.stat"), real_stat(p, *a, **kw))[1]
    )
    monkeypatch.setattr(
        os, "open", lambda p, flags, *a, **kw: (_forbid_ext_e(p, "os.open"), real_os_open(p, flags, *a, **kw))[1]
    )

    workspace_files = {
        "a.py": b"print('case-e')\n",
        "b.txt": b"sibling b\n",
        "sub/c.json": b'{"c": 1}\n',
        "preservation_probe.py": b"assert True\n",
    }

    tid, ws = _create_workspace_task(
        conn,
        result="Updated `a.py` and `preservation_probe.py`",
        files=workspace_files,
    )
    _insert_run(
        conn,
        tid,
        metadata={
            "updated_own_skill_references": [
                cred_skill,
                jev_skill,
                str(abs_secret_skill),
            ]
        },
    )
    plan = bp.plan_bundle(conn, tid, ws)
    assert plan["unresolved_refs"] == []
    assert plan["ambiguous_refs"] == []
    assert plan["external_skill_refs"] == sorted([cred_skill, jev_skill, str(abs_secret_skill)])
    assert [f["path"] for f in plan["files"]] == sorted(workspace_files.keys())

    # Nested explicit local child inside `updated_own_skill_references` list of dicts fails closed
    tid_nested, ws_nested = _create_workspace_task(conn, files=workspace_files)
    _insert_run(
        conn,
        tid_nested,
        metadata={
            "updated_own_skill_references": [
                {
                    "skill_reference": cred_skill,
                    "required": "missing_nested_req.py",
                    "reference": "sub/missing_nested_ref.md",
                }
            ]
        },
    )
    plan_nested = bp.plan_bundle(conn, tid_nested, ws_nested)
    assert cred_skill in plan_nested["external_skill_refs"]
    assert "missing_nested_req.py" in plan_nested["unresolved_refs"]
    assert "sub/missing_nested_ref.md" in plan_nested["unresolved_refs"]
    assert "missing_nested_req.py" not in plan_nested["external_skill_refs"]
    assert "sub/missing_nested_ref.md" not in plan_nested["external_skill_refs"]

    # Same `cred_skill` under explicit child key `reference` inside `updated_own_skill_references`
    # is treated as an explicit local reference and fails closed on prohibited name
    tid_prohib, ws_prohib = _create_workspace_task(conn, files=workspace_files)
    _insert_run(
        conn,
        tid_prohib,
        metadata={
            "updated_own_skill_references": [
                {"skill_reference": jev_skill, "reference": cred_skill}
            ]
        },
    )
    with pytest.raises(ValueError, match="prohibited name"):
        bp.plan_bundle(conn, tid_prohib, ws_prohib)


# ---------------------------------------------------------------------------
# 4. Case (g): Non-Path Scalar Slash Labels (`review.family = 'Gemini/Google'`)
# ---------------------------------------------------------------------------


def test_phase_a_case_g_scalar_slash_labels_and_strict_negative_controls(
    isolated_board: Tuple[sqlite3.Connection, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case (g) Positive & Strict Negative Controls:
    - Coordinator `t_b83c5110` (run 9774) shape: `metadata={"worktree": ..., "review": {"family": "Gemini/Google"}}`
      plus semantic scalar descriptor keys and list of scalar descriptors.
    - Zero filesystem probes (`os.lstat`/`os.stat`/`os.open`) on `'Gemini/Google'` or scalar labels.
    - Negative controls:
      1. File paths with extensions (`'sub/missing_script.py'`), prohibited names (`'bundle/.env'`),
         relative dot-prefixes (`'./Gemini/Google'`), and outside-root absolute paths (`'/outside/file'`)
         placed under `review.family` are NOT accepted as scalar labels and fail closed.
      2. Nested dicts with explicit local keys under scalar keys (`{"status": {"required": "missing.py"}}`)
         are NOT swallowed and fail closed.
      3. Arbitrary unknown metadata keys (`{"unknown_custom_field": "missing_dir/missing_target.py"}`)
         are NOT ignored as scalar metadata and fail closed.
    """
    conn, _ = isolated_board

    real_lstat = os.lstat
    real_stat = os.stat
    real_os_open = os.open

    def _forbid_scalar_probe(path_arg: object, op: str) -> None:
        if isinstance(path_arg, int):
            return
        p_str = os.fspath(path_arg)
        if isinstance(p_str, bytes):
            p_str = p_str.decode("utf-8", errors="replace")
        for tok in ("Gemini/Google", "Google/gemini-4-argon-high", "Anthropic/Claude-Opus", "PASS/READY"):
            if tok in p_str:
                raise AssertionError(f"Forbidden filesystem probe {op} on scalar metadata token: {p_str}")

    monkeypatch.setattr(
        os, "lstat", lambda p, *a, **kw: (_forbid_scalar_probe(p, "os.lstat"), real_lstat(p, *a, **kw))[1]
    )
    monkeypatch.setattr(
        os, "stat", lambda p, *a, **kw: (_forbid_scalar_probe(p, "os.stat"), real_stat(p, *a, **kw))[1]
    )
    monkeypatch.setattr(
        os,
        "open",
        lambda p, flags, *a, **kw: (_forbid_scalar_probe(p, "os.open"), real_os_open(p, flags, *a, **kw))[1],
    )

    workspace_files = {
        "a.py": b"print('case-g')\n",
        "b.txt": b"sibling b\n",
        "sub/c.json": b'{"case": "g"}\n',
        "preservation_probe.py": b"assert True\n",
    }

    tid, ws = _create_workspace_task(
        conn,
        result="Updated `a.py` and `preservation_probe.py`",
        files=workspace_files,
    )
    (ws / "head").mkdir()
    _insert_run(
        conn,
        tid,
        metadata={
            "worktree": str(ws / "head"),
            "review": {
                "family": "Gemini/Google",
                "model": "Google/gemini-4-argon-high",
                "verdict": "PASS/READY",
            },
            "provider": ["Anthropic/Claude-Opus", "OpenAI/GPT-5"],
        },
    )
    plan = bp.plan_bundle(conn, tid, ws)
    assert plan["unresolved_refs"] == []
    assert plan["unresolved_non_path_refs"] == []
    assert plan["ambiguous_refs"] == []
    assert "Gemini/Google" in plan["non_path_metadata_values"]
    assert "Google/gemini-4-argon-high" in plan["non_path_metadata_values"]
    assert "PASS/READY" in plan["non_path_metadata_values"]
    assert "Anthropic/Claude-Opus" in plan["non_path_metadata_values"]
    assert [f["path"] for f in plan["files"]] == sorted(workspace_files.keys())

    # Negative 1: File path with extension under `review.family` must NOT be exempted as scalar label
    tid_ext, ws_ext = _create_workspace_task(conn, files=workspace_files)
    _insert_run(conn, tid_ext, metadata={"review": {"family": "sub/missing_script.py"}})
    plan_ext = bp.plan_bundle(conn, tid_ext, ws_ext)
    assert "sub/missing_script.py" in plan_ext["unresolved_refs"]
    assert "sub/missing_script.py" not in plan_ext["non_path_metadata_values"]

    # Negative 2: Prohibited secret path under `review.family` must fail closed
    tid_sec, ws_sec = _create_workspace_task(conn, files=workspace_files)
    _insert_run(conn, tid_sec, metadata={"review": {"family": "bundle/.env"}})
    plan_sec = bp.plan_bundle(conn, tid_sec, ws_sec)
    assert "bundle/.env" in plan_sec["unresolved_refs"]
    assert "bundle/.env" not in plan_sec["non_path_metadata_values"]

    # Negative 3: Outside-root absolute path under `review.family` must raise ValueError
    tid_abs, ws_abs = _create_workspace_task(conn, files=workspace_files)
    _insert_run(conn, tid_abs, metadata={"review": {"family": "/outside/missing_target"}})
    with pytest.raises(ValueError, match="outside workspace"):
        bp.plan_bundle(conn, tid_abs, ws_abs)

    # Negative 4: Nested dict under a scalar key (`status.required`) must NOT swallow local ref
    tid_nested, ws_nested = _create_workspace_task(conn, files=workspace_files)
    _insert_run(
        conn,
        tid_nested,
        metadata={"status": {"phase": "PASS/READY", "required": "missing_status_dep.py"}},
    )
    plan_nested = bp.plan_bundle(conn, tid_nested, ws_nested)
    assert "PASS/READY" in plan_nested["non_path_metadata_values"]
    assert "missing_status_dep.py" in plan_nested["unresolved_refs"]
    assert "missing_status_dep.py" not in plan_nested["non_path_metadata_values"]

    # Negative 5: Arbitrary unknown metadata key with missing file path must fail closed
    tid_unk, ws_unk = _create_workspace_task(conn, files=workspace_files)
    _insert_run(
        conn,
        tid_unk,
        metadata={"unrecognized_custom_field": "missing_dir/missing_target.py"},
    )
    plan_unk = bp.plan_bundle(conn, tid_unk, ws_unk)
    assert "missing_dir/missing_target.py" in plan_unk["unresolved_refs"]


# ---------------------------------------------------------------------------
# 5. Disposable Isolated Prior Classifier Snapshot RED Verification
# ---------------------------------------------------------------------------


def test_prior_classifier_snapshots_red_in_isolated_disposable_copy(
    isolated_board: Tuple[sqlite3.Connection, Path],
    tmp_path: Path,
) -> None:
    """Verify actual RED failure behavior of prior classifier snapshots in an isolated
    disposable copy of `kanban_bundle_preserve.py` loaded from `tmp_path` without
    modifying production code.
    """
    conn, _ = isolated_board
    src_path = Path(bp.__file__).resolve()
    src_text = src_path.read_text(encoding="utf-8")

    # Construct isolated disposable copy simulating the pre-Phase-A classifier state:
    # (1) no GCP SA / resource regex in `is_non_path_metadata`
    # (2) no external skill provenance key handling (`is_external_skill_provenance_key` -> False)
    # (3) no non-path scalar metadata key handling (`is_non_path_scalar_metadata_key` -> False)
    prior_src = (
        src_text.replace(
            "    if _GCP_SA_PATTERN.match(t):\n        return True\n    if _GCP_RESOURCE_PATTERN.match(t):\n        return True\n",
            "",
        )
        .replace(
            "    if is_non_path_metadata(tok):\n        return True\n",
            "    if is_non_path_metadata(tok) or _GCP_SA_PATTERN.match(tok):\n        return True\n",
        )
        .replace(
            "def is_external_skill_provenance_key(key: str) -> bool:",
            "def is_external_skill_provenance_key(key: str) -> bool:\n    return False",
        )
        .replace(
            "def is_non_path_scalar_metadata_key(key: str) -> bool:",
            "def is_non_path_scalar_metadata_key(key: str) -> bool:\n    return False",
        )
    )
    disp_mod_path = tmp_path / "disposable_prior_classifier.py"
    disp_mod_path.write_text(prior_src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("disposable_prior_classifier", disp_mod_path)
    assert spec is not None and spec.loader is not None
    prior_bp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prior_bp)

    # RED (a1): GCP SA email falsely rejected as unresolved local reference in prior classifier
    tid_a1, ws_a1 = _create_workspace_task(conn, files={"a.py": b"x"})
    _insert_run(
        conn,
        tid_a1,
        metadata={"cloud_account": "keys-router@mission-control-000000.iam.gserviceaccount.com"},
    )
    prior_plan_a1 = prior_bp.plan_bundle(conn, tid_a1, ws_a1)
    assert "keys-router@mission-control-000000.iam.gserviceaccount.com" in prior_plan_a1["unresolved_refs"]

    # RED (a2): GCP Secret Manager resource falsely rejected in prior classifier
    tid_a2, ws_a2 = _create_workspace_task(conn, files={"a.py": b"x"})
    _insert_run(
        conn,
        tid_a2,
        metadata={"placement": {"name": "projects/100000000001/secrets/po-example-fixture/versions/1"}},
    )
    prior_plan_a2 = prior_bp.plan_bundle(conn, tid_a2, ws_a2)
    assert "projects/100000000001/secrets/po-example-fixture/versions/1" in prior_plan_a2["unresolved_refs"]

    # RED (d): `metadata.skill_updated` falsely rejected as unresolved local reference in prior classifier
    tid_d, ws_d = _create_workspace_task(conn, files={"a.py": b"x"})
    _insert_run(
        conn,
        tid_d,
        metadata={"skill_updated": "fleet-bot-standup/references/canonical-bot-chat-repair.md"},
    )
    prior_plan_d = prior_bp.plan_bundle(conn, tid_d, ws_d)
    assert "fleet-bot-standup/references/canonical-bot-chat-repair.md" in prior_plan_d["unresolved_refs"]

    # RED (e): `metadata.updated_own_skill_references` falsely rejected in prior classifier
    tid_e, ws_e = _create_workspace_task(conn, files={"a.py": b"x"})
    _insert_run(
        conn,
        tid_e,
        metadata={
            "updated_own_skill_references": [
                "fleet-bot-standup/references/blind-credential-helpers.md",
                "jev/references/context-tiers.md",
            ]
        },
    )
    prior_plan_e = prior_bp.plan_bundle(conn, tid_e, ws_e)
    assert "fleet-bot-standup/references/blind-credential-helpers.md" in prior_plan_e["unresolved_refs"]
    assert "jev/references/context-tiers.md" in prior_plan_e["unresolved_refs"]

    # RED (g): `metadata.review.family = 'Gemini/Google'` falsely classified as unresolved slug in prior classifier
    tid_g, ws_g = _create_workspace_task(conn, files={"a.py": b"x"})
    (ws_g / "head").mkdir()
    _insert_run(
        conn,
        tid_g,
        metadata={"worktree": str(ws_g / "head"), "review": {"family": "Gemini/Google"}},
    )
    prior_plan_g = prior_bp.plan_bundle(conn, tid_g, ws_g)
    assert "Gemini/Google" in prior_plan_g["unresolved_non_path_refs"]
    assert prior_plan_g.get("non_path_metadata_values", []) == []
