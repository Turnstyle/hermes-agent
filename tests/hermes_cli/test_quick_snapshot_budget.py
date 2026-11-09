"""Bound repeated incomplete captures without sacrificing database recovery."""
import json
import sqlite3

import pytest
from hermes_cli import backup


def seed(home):
    home.mkdir(parents=True, exist_ok=True)
    (home / 'config.yaml').write_text('model: fixture\n')
    with sqlite3.connect(home / 'projects.db') as db:
        db.execute('create table if not exists recovery (v text)')
        db.execute("insert into recovery values ('recover me')")


def usage(root):
    return sum(p.stat().st_size for p in root.rglob('*') if p.is_file())


@pytest.mark.parametrize('failure', ['size', 'copy'])
def test_incomplete_repeats_are_bounded_and_keep_recovery(tmp_path, monkeypatch, failure):
    home = tmp_path / 'home'
    seed(home)
    good = backup.create_quick_snapshot(hermes_home=home, label='complete', keep=1)
    if failure == 'copy':
        monkeypatch.setattr(backup, '_safe_copy_db', lambda *a: False)
    kwargs = {'max_file_size': 100} if failure == 'size' else {}
    for n in range(12):
        backup.create_quick_snapshot(hermes_home=home, label=f'incomplete-{n:02}', keep=1, **kwargs)
    snaps = backup.list_quick_snapshots(100, home)
    assert len(snaps) <= 2
    assert (home / 'state-snapshots' / good / 'projects.db').exists()
    assert any(s['failed_dbs'] or s['oversized_skipped'] for s in snaps)


def test_byte_admission_refuses_to_delete_protected_and_counts_siblings(tmp_path, monkeypatch):
    root = tmp_path / 'install'
    sibling = root / 'profiles/peer'
    seed(root)
    seed(sibling)
    first = backup.create_quick_snapshot(hermes_home=root, keep=1)
    peer = backup.create_quick_snapshot(hermes_home=sibling, keep=1)
    before = usage(root / 'state-snapshots') + usage(sibling / 'state-snapshots')
    monkeypatch.setattr(backup, '_QUICK_MAX_TOTAL_BYTES', before + 1, raising=False)
    assert backup.create_quick_snapshot(hermes_home=root, keep=1) is None
    assert (root / 'state-snapshots' / first).is_dir()
    assert (sibling / 'state-snapshots' / peer).is_dir()
    assert usage(root / 'state-snapshots') + usage(sibling / 'state-snapshots') == before


def test_unknown_partial_and_symlink_fail_closed(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    seed(home)
    root = home / 'state-snapshots'
    root.mkdir()
    unknown = root / '.legacy.12.partial'
    unknown.mkdir()
    (unknown / 'payload').write_bytes(b'x' * 2048)
    monkeypatch.setattr(backup, '_QUICK_MAX_TOTAL_BYTES', 1024, raising=False)
    assert backup.create_quick_snapshot(hermes_home=home) is None
    assert (unknown / 'payload').stat().st_size == 2048
    (root / 'foreign').symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setattr(backup, '_QUICK_MAX_TOTAL_BYTES', 1 << 30)
    assert backup.create_quick_snapshot(hermes_home=home) is None
    assert (root / 'foreign').is_symlink()


def test_complementary_db_recovery_survives_manual_prune(tmp_path):
    home = tmp_path / 'home'
    seed(home)
    first = backup.create_quick_snapshot(hermes_home=home, keep=1)
    (home / 'projects.db').unlink()
    with sqlite3.connect(home / 'memory_store.db') as db:
        db.execute('create table memories (v text)')
    second = backup.create_quick_snapshot(hermes_home=home, keep=1)
    backup.prune_quick_snapshots(keep=0, hermes_home=home)
    assert (home / 'state-snapshots' / first / 'projects.db').exists()
    assert (home / 'state-snapshots' / second / 'memory_store.db').exists()


def test_actual_overshoot_is_not_published(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    seed(home)
    good = backup.create_quick_snapshot(hermes_home=home)
    root = home / 'state-snapshots'
    before = usage(root)
    monkeypatch.setattr(backup, '_QUICK_MAX_TOTAL_BYTES', before + 100000)
    copy = backup.shutil.copy2
    def grow(src, dst, *args, **kwargs):
        result = copy(src, dst, *args, **kwargs)
        if str(src).endswith('config.yaml'):
            dst.write_bytes(b'x' * 200000)
        return result
    monkeypatch.setattr(backup.shutil, 'copy2', grow)
    assert backup.create_quick_snapshot(hermes_home=home) is None
    assert usage(root) == before
    assert (root / good / 'projects.db').exists()
    assert not list(root.glob('.*.partial'))


def test_byte_pressure_prunes_optional_sibling_history(tmp_path, monkeypatch):
    root = tmp_path / 'install'
    sibling = root / 'profiles/peer'
    seed(root)
    seed(sibling)
    ids = [backup.create_quick_snapshot(hermes_home=sibling) for _ in range(4)]
    backup.create_quick_snapshot(hermes_home=root)
    before = usage(root / 'state-snapshots') + usage(sibling / 'state-snapshots')
    # Reservation 64KiB plus current DB; make optional history sufficiently large.
    for sid in ids[:-1]:
        snap = sibling / 'state-snapshots' / sid
        (snap / 'padding').write_bytes(b'p' * 80000)
    before = usage(root / 'state-snapshots') + usage(sibling / 'state-snapshots')
    monkeypatch.setattr(backup, '_QUICK_MAX_TOTAL_BYTES', before)
    assert backup.create_quick_snapshot(hermes_home=root)
    assert not (sibling / 'state-snapshots' / ids[0]).exists()
    assert (sibling / 'state-snapshots' / ids[-1] / 'projects.db').exists()
    assert usage(root / 'state-snapshots') + usage(sibling / 'state-snapshots') <= before


def test_custom_roots_do_not_account_unrelated_siblings(tmp_path, monkeypatch):
    home = tmp_path / 'one'
    other = tmp_path / 'two/state-snapshots'
    seed(home)
    other.mkdir(parents=True)
    (other / 'unknown').write_bytes(b'x' * 100000)
    monkeypatch.setattr(backup, '_QUICK_MAX_TOTAL_BYTES', 100000)
    assert backup.create_quick_snapshot(hermes_home=home)
    assert (other / 'unknown').exists()


def test_unreadable_accounting_refuses_without_deletion(tmp_path, monkeypatch):
    from hermes_cli import backup_retention as retention
    home = tmp_path / 'home'
    seed(home)
    good = backup.create_quick_snapshot(hermes_home=home)
    def denied(path):
        raise PermissionError('unreadable fixture')
    monkeypatch.setattr(retention, 'tree_bytes', denied)
    assert backup.create_quick_snapshot(hermes_home=home) is None
    assert (home / 'state-snapshots' / good).exists()


def test_corrupt_manifest_remains_unknown_not_evicted(tmp_path):
    home = tmp_path / 'home'
    seed(home)
    root = home / 'state-snapshots'
    unknown = root / '20200101-corrupt'
    unknown.mkdir(parents=True)
    (unknown / 'manifest.json').write_text('{broken')
    (unknown / 'recovery.db').write_bytes(b'unknown recovery')
    backup.create_quick_snapshot(hermes_home=home, keep=1)
    backup.create_quick_snapshot(hermes_home=home, keep=1)
    assert (unknown / 'recovery.db').read_bytes() == b'unknown recovery'


def test_clock_rollback_cannot_retire_newer_capture(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    seed(home)
    first = backup.create_quick_snapshot(hermes_home=home, keep=1)
    from datetime import datetime as real_datetime
    class Earlier(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2000, 1, 1)
    monkeypatch.setattr(backup, 'datetime', Earlier)
    second = backup.create_quick_snapshot(hermes_home=home, keep=1)
    assert (home / 'state-snapshots' / second / 'projects.db').exists()
    assert not (home / 'state-snapshots' / first).exists()
