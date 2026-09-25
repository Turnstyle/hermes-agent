"""Tenant edits preserve task state and leave an atomic routing audit trail."""

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _cli_edit(task_id, tenant, *extra):
    parser = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["kanban", "edit", task_id, "--tenant", tenant, *extra])
    return kc.kanban_command(args)


def _create_task(conn, status, tenant):
    parents = ()
    if status == "todo":
        parents = (kb.create_task(conn, title="unfinished parent"),)
    task_id = kb.create_task(
        conn, title="route me", tenant=tenant, assignee="worker", parents=parents,
        triage=status == "triage",
        initial_status="blocked" if status == "blocked" else "running",
    )
    if status == "running":
        assert kb.claim_task(conn, task_id, claimer="worker") is not None
    if status == "done":
        assert kb.complete_task(conn, task_id, result="finished")
    if status == "archived":
        assert kb.archive_task(conn, task_id)
    assert kb.get_task(conn, task_id).status == status
    return task_id


@pytest.mark.parametrize("status,origin", [
    ("ready", "turnerbook"), ("ready", None), ("todo", "turnerbook"),
    ("triage", "turnerbook"), ("blocked", "turnerbook"),
])
@pytest.mark.parametrize("with_priority", [False, True])
def test_kanban_edit_tenant_commits_routing_and_unchanged_is_noop(
    kanban_home, status, origin, with_priority,
):
    with kbc.connect_closing() as conn:
        task_id = _create_task(conn, status, origin)
        before = kb.get_task(conn, task_id)

    extra = ("--priority", "12") if with_priority else ()
    assert _cli_edit(task_id, "sheldon", *extra) == 0
    with kbc.connect_closing() as conn:
        after = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)
        assert (after.tenant, after.status, after.assignee) == (
            "sheldon", before.status, before.assignee,
        )
        assert after.priority == (12 if with_priority else before.priority)
        assert [e.payload for e in events if e.kind == "rehomed"] == [
            {"from": origin, "to": "sheldon"},
        ]
        changes = conn.total_changes
        assert kb.edit_task(conn, task_id, tenant="sheldon") is True
        assert conn.total_changes == changes

    assert _cli_edit(task_id, "sheldon") == 0
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, task_id) == after
        assert kb.list_events(conn, task_id) == events


@pytest.mark.parametrize("status,destination,error", [
    ("running", "sheldon", "running"),
    ("running", "turnerbook", "running"),
    ("done", "sheldon", "done"),
    ("archived", "sheldon", "archived"),
    ("ready", "", "non-empty"),
    ("ready", " \t ", "non-empty"),
])
def test_kanban_edit_tenant_refusal_preserves_all_fields_and_events(
    kanban_home, capsys, status, destination, error,
):
    with kbc.connect_closing() as conn:
        task_id = _create_task(conn, status, "turnerbook")
        before = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)
        with pytest.raises(ValueError, match=error):
            kb.edit_task(conn, task_id, tenant=destination, title="must not change")
        assert kb.get_task(conn, task_id) == before
        assert kb.list_events(conn, task_id) == events

    assert _cli_edit(task_id, destination, "--title", "must not change") != 0
    assert error in capsys.readouterr().err
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, task_id) == before
        assert kb.list_events(conn, task_id) == events
