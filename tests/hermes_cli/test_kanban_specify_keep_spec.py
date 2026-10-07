"""Tests for `hermes kanban specify <id> --keep-spec` contract.

Verifies:
- exact bytes for whitespace/Unicode/emoji/null vs empty body
- zero model calls (monkeypatched _call_aux and specify_task)
- wrong hash, concurrent edit race refusal (zero mutations/events)
- non-triage tasks refusal
- active holds refusal (block loop, sticky gave_up, explicit blocked, needs_input)
- delegated worker fence refusal
- parent gating (open parent -> todo, parent-free -> ready)
- duplicate call refusal (no second release/audit)
- metadata preservation (assignee, model/provider/effort, workspace, locks)
- audit comment + event payload carry author and hash
- recompute failure path (committed todo, visible warning line, failure type)
- CLI usage errors (--all, missing hash, bad hex, missing author, hash without --keep-spec)
- JSON shape and hash_encoding
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_specify as spec


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_triage(conn, title="rough idea", body=None, assignee=None, **kwargs):
    return kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        triage=True,
        **kwargs,
    )


def _run_cli(*argv: str) -> int:
    """Invoke the `hermes kanban …` argparse surface directly."""
    root = argparse.ArgumentParser()
    subp = root.add_subparsers(dest="cmd")
    kanban_cli.build_parser(subp)
    ns = root.parse_args(["kanban", *argv])
    return kanban_cli.kanban_command(ns)


def _snapshot_task_state(conn, task_id: str):
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    task_dict = dict(row) if row else None
    comments = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM task_comments WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
    ]
    events = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
    ]
    links = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM task_links WHERE parent_id = ? OR child_id = ? ORDER BY parent_id, child_id",
            (task_id, task_id),
        ).fetchall()
    ]
    return task_dict, comments, events, links


# ---------------------------------------------------------------------------
# 1. Exact bytes & hash encoding tests
# ---------------------------------------------------------------------------

def test_exact_bytes_whitespace_unicode_emoji_null_vs_empty(kanban_home):
    """Preserves whitespace, Unicode, emoji, and distinguishes null body from empty string."""
    cases = [
        ("Simple Task", None),
        ("Simple Task", ""),
        ("  Leading and trailing title spaces  ", "  Leading and trailing body spaces  \n\n- [ ] List item\n"),
        ("Unicode 🚀 task: 测试", "Emoji in body 🎯 and non-ASCII: 日本語 / العربية\n\tTab and newline\r\n"),
    ]

    for title, body in cases:
        with kbc.connect() as conn:
            tid = _create_triage(conn, title="temp", body=body)
            # Directly update title to preserve leading/trailing whitespace without create_task stripping
            conn.execute("UPDATE tasks SET title = ? WHERE id = ?", (title, tid))
            conn.commit()

            # Verify DB holds the exact title and body
            db_row = conn.execute("SELECT title, body FROM tasks WHERE id = ?", (tid,)).fetchone()
            assert db_row["title"] == title
            assert db_row["body"] == body

        expected_hash = kb.compute_task_sha256(title, body)

        # Confirm hash definition: compact JSON [title, body] with NO trailing newline
        raw_json = json.dumps([title, body], ensure_ascii=False, separators=(",", ":"))
        assert expected_hash == hashlib.sha256(raw_json.encode("utf-8")).hexdigest()

        outcome = spec.keep_spec_task(tid, expect_sha256=expected_hash, author="test-author")
        assert outcome.ok is True
        assert outcome.kept_spec is True
        assert outcome.sha256 == expected_hash

        with kbc.connect() as conn:
            task = kb.get_task(conn, tid)
            assert task.title == title
            assert task.body == body
            if body is None:
                assert task.body is None
            elif body == "":
                assert task.body == ""


def test_null_body_distinct_from_empty_string(kanban_home):
    """null body produces a different hash than empty string body."""
    null_hash = kb.compute_task_sha256("Title", None)
    empty_hash = kb.compute_task_sha256("Title", "")
    assert null_hash != empty_hash

    with kbc.connect() as conn:
        tid = _create_triage(conn, title="Title", body=None)

    # Calling with empty_hash on a null-body task must refuse
    outcome = spec.keep_spec_task(tid, expect_sha256=empty_hash, author="author")
    assert outcome.ok is False
    assert outcome.committed is False
    assert "sha256 mismatch" in outcome.reason

    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "triage"


# ---------------------------------------------------------------------------
# 2. Zero model calls guarantee
# ---------------------------------------------------------------------------

def test_zero_model_calls_module_and_cli(kanban_home, monkeypatch, capsys):
    """Monkeypatch _call_aux, specify_task, and call_llm to raise; assert zero calls."""
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="Unreviewed Spec", body="Some requirements")
        h = kb.compute_task_sha256("Unreviewed Spec", "Some requirements")

    def _forbidden(*args, **kwargs):
        raise AssertionError("Model call attempted on keep-spec path!")

    monkeypatch.setattr(spec, "_call_aux", _forbidden)
    monkeypatch.setattr(spec, "specify_task", _forbidden)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", _forbidden)

    # 1. Module level
    outcome = spec.keep_spec_task(tid, expect_sha256=h, author="reviewer")
    assert outcome.ok is True
    assert outcome.status_after == "ready"

    # Reset task back to triage for CLI test
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (tid,))
        conn.commit()

    # 2. CLI level
    rc = _run_cli("specify", tid, "--keep-spec", "--expect-sha256", h, "--author", "reviewer")
    assert rc == 0
    out = capsys.readouterr().out
    assert (
        'Encoding: SHA-256 of the UTF-8 bytes of compact JSON [title,body] '
        '(ensure_ascii=False, separators (",",":"), no trailing newline; null body distinct from empty string)'
    ) in out


# ---------------------------------------------------------------------------
# 3. Wrong hash & concurrent edit race refusal
# ---------------------------------------------------------------------------

def test_wrong_hash_refuses_zero_writes(kanban_home, capsys):
    """Refusal on wrong hash leaves zero mutations, zero comments, zero events."""
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="Original Title", body="Original Body")
        snap_before = _snapshot_task_state(conn, tid)

    bad_hash = "0" * 64

    # Module level
    outcome = spec.keep_spec_task(tid, expect_sha256=bad_hash, author="author")
    assert outcome.ok is False
    assert outcome.committed is False
    assert "sha256 mismatch" in outcome.reason

    with kbc.connect() as conn:
        snap_after = _snapshot_task_state(conn, tid)
    assert snap_before == snap_after

    # CLI level
    rc = _run_cli("specify", tid, "--keep-spec", "--expect-sha256", bad_hash, "--author", "author")
    assert rc == 1
    err = capsys.readouterr().err
    assert "sha256 mismatch" in err

    with kbc.connect() as conn:
        snap_after_cli = _snapshot_task_state(conn, tid)
    assert snap_before == snap_after_cli


def test_concurrent_edit_two_connection_interleaving(kanban_home):
    """Simulates concurrent edit between hash observation and keep_spec write transaction."""
    with kbc.connect() as conn_init:
        tid = _create_triage(conn_init, title="Concurrent Task", body="V1 body")
        initial_hash = kb.compute_task_sha256("Concurrent Task", "V1 body")

    with kbc.connect() as conn1, kbc.connect() as conn2:
        # Actor 2 modifies task concurrently in WAL
        conn2.execute("UPDATE tasks SET body = 'V2 body edited concurrently' WHERE id = ?", (tid,))
        conn2.commit()

        # Capture snapshot after Actor 2's edit and immediately before Actor 1's attempt
        snap_before_attempt = _snapshot_task_state(conn1, tid)

        # Actor 1 attempts keep_spec with stale initial_hash
        ok, reason, computed_hash, status_after, committed, recompute_err = kb.keep_spec_triage_task(
            conn1,
            tid,
            expected_sha256=initial_hash,
            author="actor-1",
        )

        assert ok is False
        assert committed is False
        assert status_after == "triage"
        assert "sha256 mismatch" in reason

        # Assert full row, links, comments, and events snapshots unchanged (zero writes by Actor 1)
        snap_after_attempt = _snapshot_task_state(conn1, tid)
        assert snap_before_attempt == snap_after_attempt

        # Assert zero comments added by Actor 1
        comments = conn1.execute("SELECT * FROM task_comments WHERE task_id = ?", (tid,)).fetchall()
        assert len(comments) == 0

        # Assert no 'specified' events added by Actor 1
        events = conn1.execute("SELECT * FROM task_events WHERE task_id = ?", (tid,)).fetchall()
        assert len(events) == len(snap_before_attempt[2])
        assert all(e["kind"] != "specified" for e in events)

        # Title/body still reflect Actor 2's edit
        task = kb.get_task(conn1, tid)
        assert task.body == "V2 body edited concurrently"
        assert task.status == "triage"


# ---------------------------------------------------------------------------
# 4. Non-triage tasks refusal
# ---------------------------------------------------------------------------

def test_non_triage_status_refuses_zero_writes(kanban_home):
    """Tasks in non-triage statuses are refused without mutation."""
    statuses = ["todo", "ready", "running", "blocked", "review", "done", "archived"]
    for st in statuses:
        with kbc.connect() as conn:
            tid = _create_triage(conn, title=f"Task in {st}", body="Body")
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (st, tid))
            conn.commit()
            snap_before = _snapshot_task_state(conn, tid)
            h = kb.compute_task_sha256(f"Task in {st}", "Body")

        outcome = spec.keep_spec_task(tid, expect_sha256=h, author="author")
        assert outcome.ok is False
        assert outcome.committed is False
        assert outcome.status_after == st
        assert "is not in triage" in outcome.reason

        with kbc.connect() as conn:
            snap_after = _snapshot_task_state(conn, tid)
        assert snap_before == snap_after


def test_nonexistent_task_refuses(kanban_home):
    """A missing task id is refused with zero writes across full snapshots."""
    with kbc.connect() as conn:
        tid_existing = _create_triage(conn, title="Existing Task", body="Existing Body")
        snap_existing_before = _snapshot_task_state(conn, tid_existing)
        snap_missing_before = _snapshot_task_state(conn, "t_nonexistent")

    h = "a" * 64
    outcome = spec.keep_spec_task("t_nonexistent", expect_sha256=h, author="author")
    assert outcome.ok is False
    assert outcome.committed is False
    assert outcome.reason == "task not found"

    with kbc.connect() as conn:
        assert snap_missing_before == _snapshot_task_state(conn, "t_nonexistent")
        assert snap_existing_before == _snapshot_task_state(conn, tid_existing)


# ---------------------------------------------------------------------------
# 5. Active holds refusal
# ---------------------------------------------------------------------------

def test_active_hold_unreleased_block_loop_refuses(kanban_home):
    """Unreleased block loop escalation breaker refuses promotion."""
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="Loop task", body="Loop body")
        kb._append_event(conn, tid, "block_loop_detected", {"reason": "recurrence limit"})
        conn.commit()
        snap_before = _snapshot_task_state(conn, tid)
        h = kb.compute_task_sha256("Loop task", "Loop body")

    outcome = spec.keep_spec_task(tid, expect_sha256=h, author="author")
    assert outcome.ok is False
    assert outcome.committed is False
    assert outcome.reason == "task has unreleased block loop hold"

    with kbc.connect() as conn:
        snap_after = _snapshot_task_state(conn, tid)
    assert snap_before == snap_after


def test_active_hold_sticky_block_refuses(kanban_home):
    """Sticky blocks (unreleased blocked or gave_up with sticky: true) refuse promotion."""
    # 1. Newest event is 'blocked'
    with kbc.connect() as conn:
        tid1 = _create_triage(conn, title="Blocked task", body="B1")
        kb._append_event(conn, tid1, "blocked", {"kind": "error"})
        conn.commit()
        snap_before1 = _snapshot_task_state(conn, tid1)
        h1 = kb.compute_task_sha256("Blocked task", "B1")

    outcome1 = spec.keep_spec_task(tid1, expect_sha256=h1, author="author")
    assert outcome1.ok is False
    assert outcome1.committed is False
    assert outcome1.reason == "task has active sticky block hold"
    with kbc.connect() as conn:
        assert snap_before1 == _snapshot_task_state(conn, tid1)

    # 2. gave_up event with sticky: true
    with kbc.connect() as conn:
        tid2 = _create_triage(conn, title="Gave up task", body="B2")
        kb._append_event(conn, tid2, "gave_up", {"sticky": True, "reason": "protocol violation"})
        conn.commit()
        snap_before2 = _snapshot_task_state(conn, tid2)
        h2 = kb.compute_task_sha256("Gave up task", "B2")

    outcome2 = spec.keep_spec_task(tid2, expect_sha256=h2, author="author")
    assert outcome2.ok is False
    assert outcome2.committed is False
    assert outcome2.reason == "task has active sticky block hold"
    with kbc.connect() as conn:
        assert snap_before2 == _snapshot_task_state(conn, tid2)


@pytest.mark.parametrize("title, body", [
    ("Important HOLD FOR TURNER fix", "Details"),
    ("Regular title", "Please hold for turner until reviewed"),
])
def test_turner_marker_text_is_ignored(kanban_home, title, body):
    """Title/body markers cannot park a reviewed spec."""
    with kbc.connect() as conn:
        tid = _create_triage(conn, title=title, body=body)
        conn.commit()
    outcome = spec.keep_spec_task(tid, expect_sha256=kb.compute_task_sha256(title, body), author="author")
    assert outcome.ok is True
    assert outcome.committed is True
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "ready"
        assert (kb.get_task(conn, tid).title, kb.get_task(conn, tid).body) == (title, body)


def test_active_hold_needs_input_refuses_and_unblocked_succeeds(kanban_home):
    """A task with block_kind='needs_input' refuses while hold is active, and succeeds when unblocked."""
    # 1. block_kind='needs_input' with hold-event ordering: 'unblocked' release followed by newest 'blocked'
    with kbc.connect() as conn:
        tid1 = _create_triage(conn, title="Needs input task 1", body="Needs input body 1")
        conn.execute("UPDATE tasks SET block_kind = 'needs_input' WHERE id = ?", (tid1,))
        # Earlier release event, followed by newest hold event
        kb._append_event(conn, tid1, "unblocked", {"reason": "prior release"})
        kb._append_event(conn, tid1, "blocked", {"reason": "waiting for operator clarification"})
        conn.commit()
        snap_before1 = _snapshot_task_state(conn, tid1)
        h1 = kb.compute_task_sha256("Needs input task 1", "Needs input body 1")

    outcome1 = spec.keep_spec_task(tid1, expect_sha256=h1, author="author")
    assert outcome1.ok is False
    assert outcome1.committed is False
    assert outcome1.reason == "task has active hold (needs_input, younger than 24 h)"
    with kbc.connect() as conn:
        assert snap_before1 == _snapshot_task_state(conn, tid1)

    # 2. block_kind='needs_input' with hold-event ordering: newest 'block_loop_detected' after release
    with kbc.connect() as conn:
        tid2 = _create_triage(conn, title="Needs input task 2", body="Needs input body 2")
        conn.execute("UPDATE tasks SET block_kind = 'needs_input' WHERE id = ?", (tid2,))
        kb._append_event(conn, tid2, "unblocked", {"reason": "prior release"})
        kb._append_event(conn, tid2, "block_loop_detected", {"reason": "breaker trip"})
        conn.commit()
        snap_before2 = _snapshot_task_state(conn, tid2)
        h2 = kb.compute_task_sha256("Needs input task 2", "Needs input body 2")

    outcome2 = spec.keep_spec_task(tid2, expect_sha256=h2, author="author")
    assert outcome2.ok is False
    assert outcome2.committed is False
    assert outcome2.reason == "task has active hold (needs_input, younger than 24 h)"
    with kbc.connect() as conn:
        assert snap_before2 == _snapshot_task_state(conn, tid2)

    # 3. block_kind='needs_input' where the hold was released by 'unblocked' (must NOT refuse for that predicate)
    with kbc.connect() as conn:
        tid3 = _create_triage(conn, title="Released needs input", body="Released body")
        conn.execute("UPDATE tasks SET block_kind = 'needs_input' WHERE id = ?", (tid3,))
        kb._append_event(conn, tid3, "blocked", {"reason": "waiting for operator"})
        kb._append_event(conn, tid3, "unblocked", {"reason": "operator answered questions"})
        conn.commit()
        h3 = kb.compute_task_sha256("Released needs input", "Released body")

    outcome3 = spec.keep_spec_task(tid3, expect_sha256=h3, author="author")
    assert outcome3.ok is True
    assert outcome3.committed is True
    assert outcome3.status_after in ("todo", "ready")


def test_foreign_fleet_mirror_refuses(kanban_home, monkeypatch):
    """A foreign fleet mirror refuses promotion."""
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="Foreign task", body="Details")
        snap_before = _snapshot_task_state(conn, tid)
        h = kb.compute_task_sha256("Foreign task", "Details")

    monkeypatch.setattr(kb, "_is_foreign_fleet_mirror", lambda conn, task_id, node_id: True)

    outcome = spec.keep_spec_task(tid, expect_sha256=h, author="author")
    assert outcome.ok is False
    assert outcome.committed is False
    assert outcome.reason == "foreign fleet mirror cannot be promoted locally"

    with kbc.connect() as conn:
        assert snap_before == _snapshot_task_state(conn, tid)


# ---------------------------------------------------------------------------
# 6. Delegated worker fence refusal
# ---------------------------------------------------------------------------

def test_delegated_worker_fence_refuses(kanban_home, capsys):
    """A delegate_task child context must refuse before any write."""
    from agent.delegation_context import delegated_child_context

    with kbc.connect() as conn:
        tid = _create_triage(conn, title="Delegated Task", body="Body")
        snap_before = _snapshot_task_state(conn, tid)
        h = kb.compute_task_sha256("Delegated Task", "Body")

    # Inside delegated child context, calling keep_spec_triage_task raises PermissionError
    with delegated_child_context():
        with pytest.raises(PermissionError) as exc_info:
            with kbc.connect() as conn:
                kb.keep_spec_triage_task(conn, tid, expected_sha256=h, author="child")
        assert "delegate_task child contexts cannot mutate Kanban tasks" in str(exc_info.value)

    with kbc.connect() as conn:
        assert snap_before == _snapshot_task_state(conn, tid)

    # At CLI level
    with delegated_child_context():
        rc = _run_cli("specify", tid, "--keep-spec", "--expect-sha256", h, "--author", "child")
        assert rc == 1
        err = capsys.readouterr().err
        assert "delegate_task child contexts cannot mutate Kanban tasks" in err

    with kbc.connect() as conn:
        assert snap_before == _snapshot_task_state(conn, tid)


# ---------------------------------------------------------------------------
# 7. Parent gating (open parent -> todo, parent-free -> ready)
# ---------------------------------------------------------------------------

def test_parent_gating(kanban_home):
    """Parent dependencies gate promotion: open parent lands in todo, parent-free lands in ready."""
    with kbc.connect() as conn:
        parent_id = kb.create_task(conn, title="Parent task")
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (parent_id,))
        child_id = _create_triage(conn, title="Child task", body="Child body")
        kb.link_tasks(conn, parent_id, child_id)
        conn.commit()

        child_hash = kb.compute_task_sha256("Child task", "Child body")

    # Parent is open (in todo) -> child lands in todo after keep-spec
    outcome = spec.keep_spec_task(child_id, expect_sha256=child_hash, author="author")
    assert outcome.ok is True
    assert outcome.committed is True
    assert outcome.status_after == "todo"

    with kbc.connect() as conn:
        assert kb.get_task(conn, child_id).status == "todo"

    # Now complete the parent task
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent_id,))
        conn.commit()

    # Recompute ready promotes child to ready
    with kbc.connect() as conn:
        promoted = kb.recompute_ready(conn)
        assert promoted >= 1
        assert kb.get_task(conn, child_id).status == "ready"


def test_parent_free_lands_in_ready(kanban_home):
    """Parent-free triage task lands in ready after keep-spec."""
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="Free task", body="Free body")
        h = kb.compute_task_sha256("Free task", "Free body")

    outcome = spec.keep_spec_task(tid, expect_sha256=h, author="author")
    assert outcome.ok is True
    assert outcome.committed is True
    assert outcome.status_after == "ready"

    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "ready"


# ---------------------------------------------------------------------------
# 8. Duplicate call makes no second release or audit
# ---------------------------------------------------------------------------

def test_duplicate_call_refuses(kanban_home):
    """A duplicate call refuses as non-triage without adding second comment or event."""
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="Once only", body="Body")
        h = kb.compute_task_sha256("Once only", "Body")

    outcome1 = spec.keep_spec_task(tid, expect_sha256=h, author="owner-1")
    assert outcome1.ok is True

    with kbc.connect() as conn:
        snap_after_first = _snapshot_task_state(conn, tid)
        assert len(snap_after_first[1]) == 1  # 1 comment
        specified_events = [e for e in snap_after_first[2] if e["kind"] == "specified"]
        assert len(specified_events) == 1

    outcome2 = spec.keep_spec_task(tid, expect_sha256=h, author="owner-2")
    assert outcome2.ok is False
    assert outcome2.committed is False
    assert "is not in triage" in outcome2.reason

    with kbc.connect() as conn:
        snap_after_second = _snapshot_task_state(conn, tid)

    assert snap_after_first == snap_after_second


# ---------------------------------------------------------------------------
# 9. Metadata preservation
# ---------------------------------------------------------------------------

def test_metadata_preservation(kanban_home):
    """Assignee, model/provider/effort, workspace, locks, worker fields remain byte-identical."""
    with kbc.connect() as conn:
        tid = _create_triage(
            conn,
            title="Preserve Meta",
            body="Markdown body",
            assignee="specialist-profile",
            workspace_kind="worktree",
            workspace_path="/tmp/ws/path",
            branch_name="feat/keep-spec",
            project_id="proj-99",
            model_override="claude-3-opus",
            provider_override="anthropic",
            reasoning_effort="high",
            max_retries=5,
            goal_mode=True,
            goal_max_turns=20,
            skills=["git", "python"],
        )
        conn.execute(
            "UPDATE tasks SET worker_pid = 12345, claim_lock = 'lock-token', "
            "claim_expires = 1799999999 WHERE id = ?",
            (tid,),
        )
        conn.commit()

        initial_row = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())
        h = kb.compute_task_sha256("Preserve Meta", "Markdown body")

    outcome = spec.keep_spec_task(tid, expect_sha256=h, author="audit-author")
    assert outcome.ok is True

    with kbc.connect() as conn:
        final_row = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())

    # Only status changed (promoted to ready); all metadata matches initial_row
    assert final_row["status"] == "ready"
    del initial_row["status"]
    del final_row["status"]

    for col in initial_row:
        assert final_row[col] == initial_row[col], f"Column {col} mutated unexpectedly!"


# ---------------------------------------------------------------------------
# 10. Audit comment and event payload
# ---------------------------------------------------------------------------

def test_audit_comment_and_event_payload(kanban_home):
    """Audit comment and specified event carry exact author and sha256 even when unchanged."""
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="Audit Task", body="Audit Body")
        h = kb.compute_task_sha256("Audit Task", "Audit Body")

    outcome = spec.keep_spec_task(tid, expect_sha256=h, author="turner-review")
    assert outcome.ok is True

    with kbc.connect() as conn:
        comments = kb.list_comments(conn, tid)
        events = kb.list_events(conn, tid)

    # 1. Independent assertions on audit comment
    assert len(comments) == 1
    comment = comments[0]
    assert comment.author == "turner-review"
    assert "turner-review" in comment.body
    assert h in comment.body
    assert f"Specified by turner-review (kept original spec, verified sha256 {h}) and promoted to todo." in comment.body

    # 2. Independent assertions on specified event payload
    specified_events = [e for e in events if e.kind == "specified"]
    assert len(specified_events) == 1
    event = specified_events[0]
    payload = json.loads(event.payload) if isinstance(event.payload, str) else event.payload
    assert payload["author"] == "turner-review"
    assert payload["sha256"] == h
    assert payload["kept_spec"] is True
    assert payload["changed_fields"] == []
    assert payload == {
        "author": "turner-review",
        "kept_spec": True,
        "sha256": h,
        "changed_fields": [],
    }


# ---------------------------------------------------------------------------
# 11. Recompute ready failure path & visible warning
# ---------------------------------------------------------------------------

def test_recompute_ready_failure_visibility(kanban_home, monkeypatch, capsys):
    """When recompute_ready raises, task is committed todo, error captured, and warning shown."""
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="Recompute fail", body="Body")
        h = kb.compute_task_sha256("Recompute fail", "Body")

    # Raise OperationalError with message
    monkeypatch.setattr(
        kb, "recompute_ready",
        MagicMock(side_effect=Exception("database is locked")),
    )

    # 1. Module level
    outcome = spec.keep_spec_task(tid, expect_sha256=h, author="author")
    assert outcome.ok is True
    assert outcome.committed is True
    assert outcome.status_after == "todo"
    assert "Exception: database is locked" in outcome.recompute_error

    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "todo"

    # Reset task back to triage
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (tid,))
        conn.commit()

    # 2. CLI level (human output)
    rc = _run_cli("specify", tid, "--keep-spec", "--expect-sha256", h, "--author", "author")
    assert rc == 0
    out = capsys.readouterr().out
    assert f"Kept spec for {tid} → todo" in out
    assert "warning: recompute_ready deferred: Exception: database is locked" in out

    # 3. CLI level (--json output)
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (tid,))
        conn.commit()

    rc = _run_cli("specify", tid, "--keep-spec", "--expect-sha256", h, "--author", "author", "--json")
    assert rc == 0
    data = json.loads(capsys.readouterr().out.strip())
    assert data["ok"] is True
    assert data["committed"] is True
    assert data["status_after"] == "todo"
    assert data["recompute_error"] == "Exception: database is locked"

    # 4. Exception with empty message: failure type still printed
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (tid,))
        conn.commit()

    monkeypatch.setattr(
        kb, "recompute_ready",
        MagicMock(side_effect=RuntimeError("")),
    )
    outcome_empty = spec.keep_spec_task(tid, expect_sha256=h, author="author")
    assert outcome_empty.ok is True
    assert outcome_empty.committed is True
    assert outcome_empty.recompute_error == "RuntimeError"


# ---------------------------------------------------------------------------
# 12. CLI usage errors (exit 2)
# ---------------------------------------------------------------------------

def test_cli_usage_errors(kanban_home, capsys):
    """Usage errors return exit code 2 and leave full DB snapshots unchanged (zero writes)."""
    valid_hash = "f" * 64

    with kbc.connect() as conn:
        tid = _create_triage(conn, title="Usage Error Task", body="Usage Body")
        snap_before = _snapshot_task_state(conn, tid)

    commands = [
        # 1. --all with --keep-spec
        (["specify", "--all", "--keep-spec", "--expect-sha256", valid_hash, "--author", "ace"],
         "--all cannot be used with --keep-spec"),
        # 2. Missing task_id with --keep-spec
        (["specify", "--keep-spec", "--expect-sha256", valid_hash, "--author", "ace"],
         "specify --keep-spec requires a task id"),
        # 3. Missing --expect-sha256 with --keep-spec
        (["specify", tid, "--keep-spec", "--author", "ace"],
         "specify --keep-spec requires --expect-sha256"),
        # 4. --expect-sha256 without --keep-spec
        (["specify", tid, "--expect-sha256", valid_hash, "--author", "ace"],
         "--expect-sha256 requires --keep-spec"),
        # 4b. Empty --expect-sha256 without --keep-spec
        (["specify", tid, "--expect-sha256", "", "--author", "ace"],
         "--expect-sha256 requires --keep-spec"),
        # 5. Invalid hex digest (length != 64)
        (["specify", tid, "--keep-spec", "--expect-sha256", "abc123", "--author", "ace"],
         "invalid sha256 hex digest"),
        # 5b. Empty --expect-sha256 with --keep-spec (length == 0 != 64)
        (["specify", tid, "--keep-spec", "--expect-sha256", "", "--author", "ace"],
         "invalid sha256 hex digest"),
        # 6. Invalid hex digest (non-hex characters)
        (["specify", tid, "--keep-spec", "--expect-sha256", "z" * 64, "--author", "ace"],
         "invalid sha256 hex digest"),
        # 7. Missing --author
        (["specify", tid, "--keep-spec", "--expect-sha256", valid_hash],
         "specify --keep-spec requires --author"),
        # 8. Blank --author
        (["specify", tid, "--keep-spec", "--expect-sha256", valid_hash, "--author", "   "],
         "author cannot be blank"),
    ]

    for argv, err_msg in commands:
        rc = _run_cli(*argv)
        assert rc == 2
        assert err_msg in capsys.readouterr().err
        with kbc.connect() as conn:
            snap_after = _snapshot_task_state(conn, tid)
        assert snap_before == snap_after


# ---------------------------------------------------------------------------
# 13. JSON shape & hash_encoding
# ---------------------------------------------------------------------------

def test_json_shape_and_hash_encoding(kanban_home, capsys):
    """--json output adheres to the required contract and reports explicit hash_encoding."""
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="JSON Task", body="JSON Body")
        h = kb.compute_task_sha256("JSON Task", "JSON Body")

    expected_encoding = (
        'SHA-256 of the UTF-8 bytes of compact JSON [title,body] '
        '(ensure_ascii=False, separators (",",":"), no trailing newline; null body distinct from empty string)'
    )

    # 1. Success case
    rc = _run_cli("specify", tid, "--keep-spec", "--expect-sha256", h, "--author", "tester", "--json")
    assert rc == 0
    data = json.loads(capsys.readouterr().out.strip())
    expected_keys = {
        "task_id", "ok", "reason", "kept_spec", "sha256",
        "hash_encoding", "status_after", "committed", "recompute_error",
    }
    assert set(data.keys()) == expected_keys
    assert data["task_id"] == tid
    assert data["ok"] is True
    assert data["kept_spec"] is True
    assert data["sha256"] == h
    assert data["hash_encoding"] == expected_encoding
    assert data["status_after"] == "ready"
    assert data["committed"] is True
    assert data["recompute_error"] is None

    # 2. Refusal case (wrong hash)
    with kbc.connect() as conn:
        tid2 = _create_triage(conn, title="Refuse JSON", body="Body")

    rc = _run_cli("specify", tid2, "--keep-spec", "--expect-sha256", "0" * 64, "--author", "tester", "--json")
    assert rc == 1
    data_refuse = json.loads(capsys.readouterr().out.strip())
    assert set(data_refuse.keys()) == expected_keys
    assert data_refuse["ok"] is False
    assert data_refuse["committed"] is False
    assert data_refuse["hash_encoding"] == expected_encoding
    assert data_refuse["status_after"] == "triage"
    assert data_refuse["recompute_error"] is None
    assert "sha256 mismatch" in data_refuse["reason"]


def test_parser_help_explicit_hash_encoding(capsys):
    """Parser help for specify includes the explicit hash encoding contract string."""
    from hermes_cli import kanban_parser

    expected_encoding = (
        'SHA-256 of the UTF-8 bytes of compact JSON [title,body] '
        '(ensure_ascii=False, separators (",",":"), no trailing newline; null body distinct from empty string)'
    )

    # 1. Spec definition check
    specify_args = next(args for name, _, args, _ in kanban_parser._SPECS if name == "specify")
    expect_arg_kw = next(kw for flags, kw in specify_args if "--expect-sha256" in flags)
    assert expected_encoding in expect_arg_kw["help"]

    # 2. Rendered CLI help output check
    root = argparse.ArgumentParser()
    subp = root.add_subparsers(dest="cmd")
    kanban_cli.build_parser(subp)
    with pytest.raises(SystemExit):
        root.parse_args(["kanban", "specify", "--help"])
    help_out = capsys.readouterr().out
    normalized_help = " ".join(help_out.split())
    assert expected_encoding in normalized_help
    assert "--keep-spec" in help_out
    assert "--expect-sha256" in help_out


# ---------------------------------------------------------------------------
# 14. Refuse empty --expect-sha256 without --keep-spec & regression checks
# ---------------------------------------------------------------------------

def test_empty_expect_sha256_without_keep_spec_refuses(kanban_home, monkeypatch, capsys):
    """(1) --expect-sha256 '' without --keep-spec refuses exit 2, zero writes, and model call never reached."""
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="Empty Hash No Keep Spec", body="Initial Body")
        snap_before = _snapshot_task_state(conn, tid)

    def _must_not_reach(*args, **kwargs):
        raise AssertionError("model/specify path reached unexpectedly")

    monkeypatch.setattr(spec, "_call_aux", _must_not_reach)
    monkeypatch.setattr(spec, "specify_task", _must_not_reach)

    rc = _run_cli("specify", tid, "--expect-sha256", "", "--author", "alice")
    assert rc == 2
    err = capsys.readouterr().err
    assert "--expect-sha256 requires --keep-spec" in err

    with kbc.connect() as conn:
        snap_after = _snapshot_task_state(conn, tid)
    assert snap_before == snap_after


def test_normal_model_backed_specify_without_hash_flag_runs_unchanged(kanban_home, capsys):
    """(2) normal model-backed specify with NO hash flag still runs unchanged."""
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="rough task", body=None)

    content = json.dumps({
        "title": "Refined rough task",
        "body": "**Goal**\nA concrete goal.\n\n**Approach**\n- Step 1\n\n**Acceptance criteria**\n- [ ] Done\n",
    })
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    mock_call_llm = MagicMock(return_value=resp)

    with patch("agent.auxiliary_client.call_llm", mock_call_llm):
        rc = _run_cli("specify", tid, "--author", "normal-author")

    assert rc == 0
    assert mock_call_llm.call_count == 1
    out = capsys.readouterr().out
    assert f"Specified {tid}" in out

    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status in {"todo", "ready"}
    assert task.title == "Refined rough task"
    assert "**Goal**" in (task.body or "")


def test_empty_expect_sha256_with_keep_spec_refuses(kanban_home, capsys):
    """(3) --keep-spec with '' hash refuses exit 2 as invalid sha256 hex digest."""
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="Empty Hash With Keep Spec", body="Initial Body")
        snap_before = _snapshot_task_state(conn, tid)

    rc = _run_cli("specify", tid, "--keep-spec", "--expect-sha256", "", "--author", "alice")
    assert rc == 2
    err = capsys.readouterr().err
    assert "invalid sha256 hex digest" in err

    with kbc.connect() as conn:
        snap_after = _snapshot_task_state(conn, tid)
    assert snap_before == snap_after

