"""Supported Python 3.11 lacks Path.is_junction; reparse guards cannot require it."""
from pathlib import Path
from types import SimpleNamespace

from hermes_cli import backup, backup_retention


def test_capture_without_path_is_junction(tmp_path, monkeypatch):
    monkeypatch.delattr(Path, 'is_junction', raising=False)
    home = tmp_path / 'home'
    (home / 'kanban/boards/fleet').mkdir(parents=True)
    (home / 'config.yaml').write_text('fixture: true')
    (home / 'kanban/boards/fleet/board.json').write_text('{}')
    assert backup.create_quick_snapshot(hermes_home=home)


def test_windows_reparse_attribute_is_rejected(monkeypatch):
    monkeypatch.setattr(backup_retention.stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400, raising=False)
    fake = SimpleNamespace(lstat=lambda: SimpleNamespace(st_file_attributes=0x400))
    assert backup_retention.is_reparse(fake)
