"""PR-state evidence: board-confined cache, bounded refresher, hold association.

The dispatcher's ``active_pr`` hold consumes ``active_pr_keys`` + ``all_terminal``;
these tests pin the evidence contract itself (no dispatcher tick, no network).
"""

from __future__ import annotations

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


def _card(conn, number: int, *, second: int | None = None, body: str | None = None,
          author: str = "worker") -> str:
    task_id = kb.create_task(conn, title="implementation", assignee="worker", body=body)
    urls = [f"https://github.com/Owner/Repo/pull/{number}"]
    if second is not None:
        urls.append(f"https://github.com/Owner/Repo/pull/{second}")
    kb.add_comment(conn, task_id, author=author, body="Opened " + " and ".join(urls))
    return task_id


def _keys(conn, task_id: str, now: int | None = None):
    row = conn.execute("SELECT assignee, body FROM tasks WHERE id=?", (task_id,)).fetchone()
    return state.active_pr_keys(conn, task_id, row["assignee"], row["body"],
                                int(time.time()) if now is None else now)


def _cache(path: Path, **entries: tuple[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({key: {"state": value[0], "fetched_at": value[1]}
                                for key, value in entries.items()}), encoding="utf-8")


def _event(conn, task_id: str, kind: str, payload: dict, created_at: int) -> None:
    with kb.write_txn(conn):
        conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                     (task_id, kind, json.dumps(payload), created_at))


@pytest.mark.parametrize("pr_state,lifted", [("MERGED", True), ("CLOSED", True), ("OPEN", False)])
def test_only_fresh_terminal_evidence_lifts(board, pr_state, lifted):
    now = int(time.time())
    _cache(_cache_path(), **{"owner/repo#51": (pr_state, now)})
    with kbc.connect_closing() as conn:
        task_id = _card(conn, 51)
        prs = _keys(conn, task_id, now)
    assert prs == {("owner", "repo", 51)}
    assert state.all_terminal(prs, now) is lifted


@pytest.mark.parametrize("cache_kind", ["stale", "missing", "corrupt", "malformed", "future", "oversized"])
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
    elif cache_kind == "oversized":
        entry = json.dumps({"owner/repo#51": {"state": "MERGED", "fetched_at": now}})
        path.write_text(entry[:-1] + ', "pad": "' + "x" * (1024 * 1024) + '"}', encoding="utf-8")
    with kbc.connect_closing() as conn:
        prs = _keys(conn, _card(conn, 51), now)
    assert prs and state.all_terminal(prs, now) is False


def test_every_triggering_pr_must_be_terminal(board):
    path = _cache_path()
    now = int(time.time())
    _cache(path, **{"owner/repo#51": ("MERGED", now), "owner/repo#52": ("OPEN", now)})
    with kbc.connect_closing() as conn:
        prs = _keys(conn, _card(conn, 51, second=52), now)
    assert prs == {("owner", "repo", 51), ("owner", "repo", 52)}
    assert state.all_terminal(prs, now) is False
    _cache(path, **{"owner/repo#51": ("MERGED", now), "owner/repo#52": ("CLOSED", now)})
    assert state.all_terminal(prs, now) is True
    assert state.all_terminal(set(), now) is False


def test_association_counts_only_the_current_assignees_new_prs(board):
    with kbc.connect_closing() as conn:
        other = _card(conn, 51, author="reviewer")
        assert _keys(conn, other) == set()
        # A PR named in the body is an input to the work, not this worker's output.
        input_only = _card(conn, 51, body="Review https://github.com/owner/repo/pull/51")
        assert _keys(conn, input_only) == set()
        mixed = _card(conn, 51, second=52, body="Fix https://github.com/OWNER/REPO/pull/51")
        assert _keys(conn, mixed) == {("owner", "repo", 52)}
        assert _keys(conn, kb.create_task(conn, title="unassigned")) == set()


def test_unblock_or_handoff_after_the_comment_releases(board):
    now = int(time.time())
    with kbc.connect_closing() as conn:
        unblocked = kb.create_task(conn, title="impl", assignee="worker")
        handed = kb.create_task(conn, title="impl", assignee="worker")
        noop = kb.create_task(conn, title="impl", assignee="worker")
        with kb.write_txn(conn):
            for tid in (unblocked, handed, noop):
                kb._insert_comment(conn, tid, "worker", "https://github.com/o/r/pull/9", now - 60)
        _event(conn, unblocked, "unblocked", {}, now - 30)
        _event(conn, handed, "assigned", {"assignee": "closer", "from": "worker"}, now - 30)
        _event(conn, noop, "assigned", {"assignee": "worker", "from": "worker"}, now - 30)
        assert _keys(conn, unblocked, now) == set()
        assert _keys(conn, handed, now) == set()
        assert _keys(conn, noop, now) == {("o", "r", 9)}


def test_unreadable_comment_time_fails_closed(board):
    now = int(time.time())
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="impl", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                         (tid, "worker", "https://github.com/o/r/pull/9", "not-a-time"))
        assert _keys(conn, tid, now) == {("o", "r", 9)}


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


def test_refresh_ignores_non_ready_cards_and_unknown_states(board, monkeypatch):
    path = _cache_path()
    with kbc.connect_closing() as conn:
        held = _card(conn, 51)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (held,))
        _card(conn, 52)
        monkeypatch.setattr(state.subprocess, "run",
                            lambda *a, **k: SimpleNamespace(returncode=0, stdout='{"state":"DRAFT"}'))
        result = state.refresh_pr_state(conn, path=path)
    assert result == {"candidates": 1, "attempted": 1, "refreshed": 0, "failed": 1}
    assert not path.exists()


@pytest.mark.parametrize("bounds", [{"max_prs": 0}, {"max_prs": state.PR_REFRESH_MAX + 1},
                                    {"timeout": 0}, {"timeout": state.PR_REFRESH_TIMEOUT_SECONDS + 1}])
def test_refresh_bounds_are_enforced_before_any_gh_call(board, monkeypatch, bounds):
    monkeypatch.setattr(state.subprocess, "run", lambda *a, **k: pytest.fail("gh called"))
    with kbc.connect_closing() as conn, pytest.raises(ValueError, match="bounds"):
        state.refresh_pr_state(conn, path=_cache_path(), **bounds)


def test_default_cache_is_board_local(board):
    assert state.cache_path() == kb.board_dir() / "pr-state.json"


def test_configured_relative_cache_path_is_board_local(board):
    (board / "config.yaml").write_text(
        "kanban:\n  pr_state_cache_path: cache/prs.json\n", encoding="utf-8",
    )
    assert state.cache_path() == kb.board_dir() / "cache/prs.json"


@pytest.mark.parametrize("configured", ["absolute", "traversal"])
def test_cache_path_outside_board_cannot_lift_hold(board, caplog, configured):
    now = int(time.time())
    outside = board / "kanban" / "boards" / "outside.json"
    _cache(outside, **{"owner/repo#51": ("MERGED", now)})
    value = str(outside) if configured == "absolute" else "../outside.json"
    (board / "config.yaml").write_text(
        f"kanban:\n  pr_state_cache_path: {json.dumps(value)}\n", encoding="utf-8",
    )
    state._WARNED_OUTSIDE_PATHS.clear()
    with kbc.connect_closing() as conn, caplog.at_level("WARNING", logger=state.__name__):
        prs = _keys(conn, _card(conn, 51), now)
        assert state.all_terminal(prs, now) is False
        assert state.all_terminal(prs, now) is False
        with pytest.raises(ValueError, match="inside the board directory"):
            state.refresh_pr_state(conn)
    assert len([record for record in caplog.records if record.name == state.__name__]) == 1


def test_malformed_cache_path_config_keeps_hold(board):
    now = int(time.time())
    _cache(kb.board_dir() / "pr-state.json", **{"owner/repo#51": ("MERGED", now)})
    (board / "config.yaml").write_text(
        "kanban:\n  pr_state_cache_path: 42\n", encoding="utf-8",
    )
    with kbc.connect_closing() as conn:
        prs = _keys(conn, _card(conn, 51), now)
    assert state.all_terminal(prs, now) is False


def test_refresh_reads_the_board_over_a_read_only_connection(board, monkeypatch):
    """The CLI refresher opens the board read-only: evidence goes to the cache file only."""
    with kbc.connect_closing() as conn:
        _card(conn, 51)
    monkeypatch.setattr(state.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout='{"state":"CLOSED"}'))
    with kbc.connect_readonly_closing() as conn:
        result = state.refresh_pr_state(conn, max_prs=1)
    assert result == {"candidates": 1, "attempted": 1, "refreshed": 1, "failed": 0}
    assert state.read_cache()["owner/repo#51"]["state"] == "CLOSED"


def test_cli_refresh_uses_local_board_and_fake_gh(board, monkeypatch, capsys):
    import argparse

    from hermes_cli.kanban import kanban_command
    from hermes_cli.kanban_parser import build_parser

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


# --- Dispatcher consumer: check_respawn_guard reads only the fresh cache, never gh ---
@pytest.mark.parametrize("pr_state,expected", [("MERGED", None), ("CLOSED", None), ("OPEN", "active_pr")])
def test_terminal_states_lift_only_ready_guard(board, pr_state, expected):
    _cache(_cache_path(), **{"owner/repo#51": (pr_state, int(time.time()))})
    with kbc.connect_closing() as conn:
        task_id = _card(conn, 51)
        assert kbd.check_respawn_guard(conn, task_id) == expected
        assert kbd.check_respawn_guard(conn, task_id, lane="review") is None


@pytest.mark.parametrize("cache_kind", ["stale", "missing", "corrupt"])
def test_untrusted_cache_keeps_guard(board, cache_kind):
    path, now = _cache_path(), int(time.time())
    if cache_kind == "stale":
        _cache(path, **{"owner/repo#51": ("MERGED", now - state.PR_STATE_MAX_AGE_SECONDS - 1)})
    elif cache_kind == "corrupt":
        path.write_text("{", encoding="utf-8")
    with kbc.connect_closing() as conn:
        assert kbd.check_respawn_guard(conn, _card(conn, 51)) == "active_pr"


def test_guard_never_runs_gh(board, monkeypatch):
    monkeypatch.setattr(state.subprocess, "run", lambda *a, **k: pytest.fail("dispatcher called gh"))
    _cache(_cache_path(), **{"owner/repo#51": ("MERGED", int(time.time()))})
    with kbc.connect_closing() as conn:
        assert kbd.check_respawn_guard(conn, _card(conn, 51)) is None
