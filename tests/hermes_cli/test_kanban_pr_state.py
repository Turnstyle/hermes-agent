"""PR-state evidence lifts only the ready-lane hold it proves terminal."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_pr_state as state
from hermes_cli.kanban import kanban_command
from hermes_cli.kanban_parser import build_parser


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(home / "kanban" / "workspaces"))
    monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_ROOT", str(home / "kanban" / "attachments"))
    with kb.scoped_current_board("default"):
        kb.board_dir().mkdir(parents=True, exist_ok=True)
        kb.init_db()
        yield home


def _cache_path() -> Path:
    return kb.board_dir("default") / "pr-state.json"


def _card(conn, number: int, *, second: int | None = None) -> str:
    task_id = kb.create_task(conn, title="implementation", assignee="worker")
    urls = [f"https://github.com/Owner/Repo/pull/{number}"]
    if second is not None:
        urls.append(f"https://github.com/Owner/Repo/pull/{second}")
    kb.add_comment(conn, task_id, author="worker", body="Opened " + " and ".join(urls))
    return task_id


def _cache(path: Path, **entries: tuple[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({key: {"state": value[0], "fetched_at": value[1]}
                                for key, value in entries.items()}), encoding="utf-8")


@pytest.mark.parametrize("pr_state,expected", [
    ("MERGED", None), ("CLOSED", None), ("OPEN", "active_pr"),
])
def test_terminal_states_lift_only_ready_guard(board, pr_state, expected):
    path = _cache_path()
    _cache(path, **{"owner/repo#51": (pr_state, int(time.time()))})
    with kbc.connect_closing() as conn:
        task_id = _card(conn, 51)
        lifted = []
        assert kbd.check_respawn_guard(conn, task_id, lifted=lifted) == expected
        assert lifted == ([(task_id, "active_pr_lifted_terminal")] if expected is None else [])
        assert kbd.check_respawn_guard(conn, task_id, lane="review") is None


@pytest.mark.parametrize("cache_kind", ["stale", "missing", "corrupt", "malformed", "future"])
def test_untrusted_cache_keeps_hold(board, cache_kind):
    path = _cache_path()
    now = int(time.time())
    if cache_kind == "stale":
        _cache(path, **{"owner/repo#51": ("MERGED", now - state.PR_STATE_MAX_AGE_SECONDS - 1)})
    elif cache_kind == "future":
        _cache(path, **{"owner/repo#51": ("CLOSED", now + 1)})
    elif cache_kind == "corrupt":
        path.write_text("{", encoding="utf-8")
    elif cache_kind == "malformed":
        path.write_text('{"owner/repo#51":{"state":"MERGED","fetched_at":"now"}}', encoding="utf-8")
    with kbc.connect_closing() as conn:
        task_id = _card(conn, 51)
        assert kbd.check_respawn_guard(conn, task_id) == "active_pr"


def test_every_triggering_pr_must_be_terminal(board):
    path = _cache_path()
    now = int(time.time())
    _cache(path, **{"owner/repo#51": ("MERGED", now), "owner/repo#52": ("OPEN", now)})
    with kbc.connect_closing() as conn:
        task_id = _card(conn, 51, second=52)
        assert kbd.check_respawn_guard(conn, task_id) == "active_pr"
        _cache(path, **{"owner/repo#51": ("MERGED", now), "owner/repo#52": ("CLOSED", now)})
        assert kbd.check_respawn_guard(conn, task_id) is None


def test_dispatch_diagnostics_record_lift(board, monkeypatch):
    path = _cache_path()
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)
    monkeypatch.setattr(state.subprocess, "run", lambda *a, **k: pytest.fail("dispatcher called gh"))
    _cache(path, **{"owner/repo#51": ("MERGED", int(time.time()))})
    with kbc.connect_closing() as conn:
        task_id = _card(conn, 51)
        result = kbd.dispatch_once(conn, dry_run=True)
    assert result.respawn_guard_lifted == [(task_id, "active_pr_lifted_terminal")]
    assert task_id in [item[0] for item in result.spawned]


def test_refresh_timeout_does_not_write_failed_pr(board, monkeypatch):
    path = _cache_path()
    with kbc.connect_closing() as conn:
        _card(conn, 51)

        def timeout(*args, **kwargs):
            assert kwargs["timeout"] == state.PR_REFRESH_TIMEOUT_SECONDS
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

        monkeypatch.setattr(state.subprocess, "run", timeout)
        result = state.refresh_pr_state(conn, path=path)
    assert result == {"candidates": 1, "attempted": 1, "refreshed": 0, "failed": 1}
    assert not path.exists()


def test_refresh_cap_and_atomic_replace(board, monkeypatch):
    path = _cache_path()
    _cache(path, **{"owner/repo#99": ("OPEN", int(time.time()))})
    with kbc.connect_closing() as conn:
        for number in (51, 52, 53):
            _card(conn, number)
        calls = []

        def fake_gh(argv, **kwargs):
            calls.append(argv)
            assert kwargs["timeout"] == 2
            return SimpleNamespace(returncode=0, stdout='{"state":"MERGED","mergedAt":"now"}')

        monkeypatch.setattr(state.subprocess, "run", fake_gh)
        original_replace = state.os.replace

        def observe_replace(src, dst):
            assert dst == path
            assert json.loads(path.read_text())["owner/repo#99"]["state"] == "OPEN"
            staged = json.loads(Path(src).read_text())
            assert staged["owner/repo#51"]["state"] == "MERGED"
            assert staged["owner/repo#52"]["state"] == "MERGED"
            return original_replace(src, dst)

        monkeypatch.setattr(state.os, "replace", observe_replace)
        result = state.refresh_pr_state(conn, path=path, max_prs=2, timeout=2)
        next_result = state.refresh_pr_state(conn, path=path, max_prs=1, timeout=2)
    assert result == {"candidates": 3, "attempted": 2, "refreshed": 2, "failed": 0}
    assert next_result == {"candidates": 3, "attempted": 1, "refreshed": 1, "failed": 0}
    assert [call[4] for call in calls] == ["--repo", "--repo", "--repo"]
    assert [call[3] for call in calls] == ["51", "52", "53"]
    assert json.loads(path.read_text())["owner/repo#53"]["state"] == "MERGED"


def test_default_cache_is_board_local(board):
    assert state.cache_path() == kb.board_dir() / "pr-state.json"


def test_configured_relative_cache_path_is_board_local(board):
    (board / "config.yaml").write_text(
        "kanban:\n  pr_state_cache_path: cache/prs.json\n", encoding="utf-8",
    )
    assert state.cache_path() == kb.board_dir() / "cache/prs.json"


@pytest.mark.parametrize("configured", ["absolute", "traversal"])
def test_cache_path_outside_board_cannot_lift_hold(board, caplog, configured):
    outside = board / "kanban" / "boards" / "outside.json"
    _cache(outside, **{"owner/repo#51": ("MERGED", int(time.time()))})
    value = str(outside) if configured == "absolute" else "../outside.json"
    (board / "config.yaml").write_text(
        f"kanban:\n  pr_state_cache_path: {json.dumps(value)}\n", encoding="utf-8",
    )
    with kbc.connect_closing() as conn, caplog.at_level("WARNING", logger=state.__name__):
        task_id = _card(conn, 51)
        assert kbd.check_respawn_guard(conn, task_id) == "active_pr"
        assert kbd.check_respawn_guard(conn, task_id) == "active_pr"
    assert len([record for record in caplog.records if record.name == state.__name__]) == 1


def test_malformed_cache_path_config_keeps_hold(board):
    _cache(kb.board_dir() / "pr-state.json", **{
        "owner/repo#51": ("MERGED", int(time.time())),
    })
    (board / "config.yaml").write_text(
        "kanban:\n  pr_state_cache_path: 42\n", encoding="utf-8",
    )
    with kbc.connect_closing() as conn:
        task_id = _card(conn, 51)
        assert kbd.check_respawn_guard(conn, task_id) == "active_pr"


def test_cli_refresh_uses_local_board_and_fake_gh(board, monkeypatch, capsys):
    with kbc.connect_closing() as conn:
        _card(conn, 51)
    monkeypatch.setattr(
        state.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout='{"state":"CLOSED"}'),
    )
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["kanban", "refresh-pr-state", "--max-prs", "1"])
    assert kanban_command(args) == 0
    assert "refreshed=1" in capsys.readouterr().out
    assert state.read_cache()["owner/repo#51"]["state"] == "CLOSED"


@pytest.mark.parametrize("age_seconds,expected", [(3600, "active_pr"), (10800, None)])
def test_active_pr_hold_expires_after_two_hours(board, monkeypatch, age_seconds, expected):
    now = 5_000_000
    monkeypatch.setattr(kbd.time, "time", lambda: now - age_seconds)
    with kbc.connect_closing() as conn:
        task_id = _card(conn, 51)
        monkeypatch.setattr(kbd.time, "time", lambda: now)
        assert kbd.check_respawn_guard(conn, task_id) == expected


def _guard_events(conn, task_id):
    return [event.payload for event in kb.list_events(conn, task_id)
            if event.kind == "degree_warning"]


def _guard_tick(conn):
    return kbd.dispatch_once(conn, dry_run=False, reconcile_orphans=False,
                             spawn_fn=lambda *a, **k: 99999)


def test_active_pr_advisory_dedupes_and_names_evaluation_route(board, all_assignees_spawnable):
    with kbc.connect_closing() as conn:
        task_id = _card(conn, 51)
        for _ in range(3):
            result = _guard_tick(conn)
            assert result.respawn_guarded == []
        events = _guard_events(conn, task_id)
        assert len(events) == 1
        assert events[0]["reason"] == "active_pr"
        assert "existing card/PR" in events[0]["next_step"]
        assert "hermes jev evaluate --file" in events[0]["next_step"]
        assert "Decider" in events[0]["next_step"]
        assert kb.get_task(conn, task_id).status == "running"


def test_active_pr_advisory_preserves_new_explicit_owner_hold(board, all_assignees_spawnable):
    with kbc.connect_closing() as conn:
        task_id = _card(conn, 51)
        _guard_tick(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL,claim_expires=NULL WHERE id=?", (task_id,))
            kb._insert_comment(conn, task_id, "ops", "Do not dispatch: archiving", int(time.time()))
        result = _guard_tick(conn)
        _guard_tick(conn)
        assert result.respawn_guarded == [(task_id, "explicit_do_not_dispatch")]
        assert [event["reason"] for event in _guard_events(conn, task_id)] == [
            "active_pr", "explicit_do_not_dispatch",
        ]
        assert kb.get_task(conn, task_id).status == "ready"


def test_intervening_event_does_not_repeat_same_advisory(board, all_assignees_spawnable):
    with kbc.connect_closing() as conn:
        task_id = _card(conn, 51)
        _guard_tick(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "diagnostic", {"note": "other event"})
        _guard_tick(conn)
        _guard_tick(conn)
        assert [event["reason"] for event in _guard_events(conn, task_id)] == ["active_pr"]
        assert kb.get_task(conn, task_id).status == "running"
