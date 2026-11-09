"""Quick snapshots retain current board recovery, not nested backup history."""
import json
import sqlite3

from hermes_cli import backup


def test_board_history_excluded_and_live_wal_round_trips(tmp_path):
    home = tmp_path / 'home'
    board = home / 'kanban/boards/fleet'
    board.mkdir(parents=True)
    (board / 'kanban.db.bak-20261109').write_bytes(b'legacy history')
    (board / 'notes.bak-important').write_text('user metadata')
    (board / 'board.json').write_text('{"name":"fleet"}')
    db = sqlite3.connect(board / 'kanban.db')
    try:
        db.execute('pragma journal_mode=wal')
        db.execute('pragma wal_autocheckpoint=0')
        db.execute('create table cards (title text)')
        db.execute("insert into cards values ('committed in WAL')")
        db.commit()
        assert (board / 'kanban.db-wal').stat().st_size > 0
        sid = backup.create_quick_snapshot(hermes_home=home)
        snap = home / 'state-snapshots' / sid
        meta = json.loads((snap / 'manifest.json').read_text())
        assert 'kanban/boards/fleet/kanban.db.bak-20261109' not in meta['files']
        assert not any(x.endswith(('.db-wal', '.db-shm', '.db-journal')) for x in meta['files'])
        assert 'kanban/boards/fleet/notes.bak-important' in meta['files']
        assert meta['failed_dbs'] == meta['oversized_skipped'] == []
    finally:
        db.close()
    (board / 'kanban.db').unlink()
    assert backup.restore_quick_snapshot(sid, hermes_home=home)
    with sqlite3.connect(board / 'kanban.db') as restored:
        assert restored.execute('select title from cards').fetchall() == [('committed in WAL',)]
        assert restored.execute('pragma integrity_check').fetchone() == ('ok',)
