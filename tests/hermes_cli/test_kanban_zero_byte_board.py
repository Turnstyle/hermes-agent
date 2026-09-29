"""Regression coverage for stray Kanban board database paths."""

import contextlib
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb._INITIALIZED_PATHS.clear()
    return root


def _assert_bad_path(path, open_db):
    with pytest.raises(ValueError, match="canonical") as exc:
        result = open_db()
        if isinstance(result, sqlite3.Connection):
            result.close()
    assert isinstance(exc.value, kbc.KanbanBoardPathError)
    assert str(path) in str(exc.value)


def test_env_override_cannot_create_db_or_locks_in_boards_root(home, monkeypatch):
    boards = kb.boards_root()
    path = boards / "fleet.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    before = list(boards.rglob("*")) if boards.exists() else []

    _assert_bad_path(path, kbc.connect)

    assert (list(boards.rglob("*")) if boards.exists() else []) == before


@pytest.mark.parametrize("relative", [
    "default/kanban.db",
    "_archived/x/kanban.db",
    "fleet/other.db",
    "Bad_Slug/kanban.db",
])
def test_noncanonical_board_path_creates_nothing(home, relative):
    path = kb.boards_root() / relative
    _assert_bad_path(path, lambda: kbc.connect(db_path=path))
    assert not path.parent.exists()


def test_nested_kanban_db_is_not_default_board(home):
    path = home / "kanban" / "kanban.db"
    _assert_bad_path(path, lambda: kbc.connect(db_path=path))
    assert not path.parent.exists()


def test_symlink_to_noncanonical_board_path_is_rejected(home, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(kb.boards_root(), target_is_directory=True)
    path = alias / "fleet.db"
    _assert_bad_path(path, lambda: kbc.connect(db_path=path))
    assert not kb.boards_root().exists()


def test_fenced_connect_rejects_bad_path_before_readonly_open(home, monkeypatch):
    from agent import delegation_context

    path = kb.boards_root() / "fleet.db"
    monkeypatch.setattr(delegation_context, "kanban_path_is_fenced", lambda _path: True)
    _assert_bad_path(path, lambda: kbc.connect(db_path=path))
    assert not kb.boards_root().exists()


def test_existing_shadow_sqlite_is_untouched(home):
    path = kb.boards_root() / "fleet.db"
    path.parent.mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE marker (id INTEGER)")
        conn.commit()
    before = (path.read_bytes(), path.stat().st_mtime_ns)

    _assert_bad_path(path, lambda: kbc.connect(db_path=path))

    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["fleet.db"]


def test_existing_zero_byte_shadow_is_untouched(home):
    path = kb.boards_root() / "fleet.db"
    path.parent.mkdir(parents=True)
    path.touch()

    _assert_bad_path(path, lambda: kbc.connect(db_path=path))

    assert path.stat().st_size == 0
    assert sorted(p.name for p in path.parent.iterdir()) == ["fleet.db"]


def test_zero_byte_db_does_not_make_ghost_board(home):
    path = kb.boards_root() / "ghost" / "kanban.db"
    path.parent.mkdir(parents=True)
    path.touch()

    assert not kb.board_exists("ghost")
    assert "ghost" not in [board["slug"] for board in kb.list_boards()]


def test_readonly_rejects_zero_byte_db_before_sqlite_open(home, monkeypatch):
    path = home / "empty.db"
    path.touch()
    def unexpected_open(*args, **kwargs):
        raise AssertionError("read-only SQLite open should not run for an empty file")
    monkeypatch.setattr(kbc.sqlite3, "connect", unexpected_open)
    with pytest.raises(ValueError, match="not initialized"):
        with kbc.connect_readonly_closing(db_path=path):
            pass


def test_canonical_and_outside_paths_still_initialize(home):
    kb.create_board("fleet")
    with kbc.connect_closing(board="fleet") as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='tasks'").fetchone()
    for path in (home / "kanban.db", home / "outside" / "k.db", home / "kanban" / "custom.db"):
        with kbc.connect_closing(db_path=path) as conn:
            assert conn.execute("SELECT name FROM sqlite_master WHERE name='tasks'").fetchone()


def test_zero_byte_canonical_db_recovers(home):
    path = kb.boards_root() / "fleet" / "kanban.db"
    path.parent.mkdir(parents=True)
    path.touch()
    with kbc.connect_closing(board="fleet") as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='tasks'").fetchone()
    assert path.stat().st_size > 0


def test_init_db_rejects_bad_path_before_mkdir(home):
    path = kb.boards_root() / "fleet.db"
    _assert_bad_path(path, lambda: kbc.init_db(db_path=path))
    assert not path.parent.exists()
