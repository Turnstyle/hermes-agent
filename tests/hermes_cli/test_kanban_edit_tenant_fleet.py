"""Fleet lease integration; add the adapter's scripts directory to pytest pythonpath."""

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def fleet_board(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    adapter = pytest.importorskip("fleet_kanban_sqlite")
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.create_board("fleet")
    with kb.scoped_current_board("fleet"), kbc.connect_closing() as conn:
        assert adapter.install_adapter_schema(
            conn, board_slug="fleet", node_id="turnerbook", source_profile="test",
        )
        yield conn, adapter


def _mirror_lease(conn, adapter, task_id, seconds, holder):
    issue_id = conn.execute(
        "SELECT issue_id FROM fleet_kanban_issue_map WHERE local_task_id = ?",
        (task_id,),
    ).fetchone()[0]
    lease = {
        "issue_id": issue_id, "holder_profile": "worker", "holder_node": holder,
        "generation": 1, "revision": 1,
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(),
    }
    adapter.mirror_verified_lease(conn, lease, dict(lease))


@pytest.mark.parametrize("holder", ["turnerbook", "sheldon"])
@pytest.mark.parametrize("destination", ["turnerbook", "max"])
def test_kanban_edit_tenant_refuses_any_unexpired_lease(fleet_board, capsys, holder, destination):
    conn, adapter = fleet_board
    task_id = kb.create_task(conn, title="leased ready", tenant="turnerbook")
    _mirror_lease(conn, adapter, task_id, 240, holder)
    before = kb.get_task(conn, task_id)
    assert before.status == "ready"
    events = kb.list_events(conn, task_id)
    changes = conn.total_changes
    with pytest.raises(ValueError, match="unexpired fleet lease"):
        kb.edit_task(conn, task_id, tenant=destination, title="must not change", priority=7)
    assert conn.total_changes == changes

    parser = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["kanban", "edit", task_id, "--tenant", destination])
    assert kc.kanban_command(args) != 0
    assert "unexpired fleet lease" in capsys.readouterr().err
    assert kb.get_task(conn, task_id) == before
    assert kb.list_events(conn, task_id) == events


@pytest.mark.parametrize("lease_state", ["absent", "expired", "other-task"])
def test_kanban_edit_tenant_ignores_inactive_or_unrelated_leases(fleet_board, lease_state):
    conn, adapter = fleet_board
    task_id = kb.create_task(conn, title="route me", tenant="turnerbook")
    if lease_state == "expired":
        _mirror_lease(conn, adapter, task_id, -240, "turnerbook")
    if lease_state == "other-task":
        other = kb.create_task(conn, title="other", tenant="turnerbook")
        _mirror_lease(conn, adapter, other, 240, "turnerbook")
    assert kb.edit_task(conn, task_id, tenant="sheldon", priority=7)
    assert kb.get_task(conn, task_id).tenant == "sheldon"
    assert [r[0] for r in conn.execute(
        "SELECT operation FROM fleet_kanban_outbox ORDER BY journal_sequence DESC LIMIT 2",
    )] == ["issue.priority", "issue.route"]
    changes = conn.total_changes
    assert kb.edit_task(conn, task_id, tenant="sheldon")
    assert conn.total_changes == changes
