"""Windows sharing preconditions via real lock lifetimes, on isolated fixtures."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import time

import pytest

from hermes_cli import backup as b, backup_retention as r


@pytest.fixture
def sharing_contract(monkeypatch):
    held = []
    operations = []
    real_lock = b._backup_operation_lock
    real_replace = b.os.replace
    real_remove = b.shutil.rmtree

    @contextmanager
    def tracked(path, *args, **kwargs):
        with real_lock(path, *args, **kwargs):
            entry = (Path(path), kwargs.get('lock_name', '.backup.lock'))
            held.append(entry)
            try:
                yield
            finally:
                held.remove(entry)

    def check(path, operation):
        path = Path(path)
        if not path.name.endswith('.partial'):
            return
        # Model an ordinary Windows child handle without delete sharing.
        if any(directory == path or path in directory.parents for directory, _ in held):
            raise PermissionError('simulated Windows open child lease sharing violation')
        assert any(name == '.quick-snapshot.lock' for _, name in held)
        # Even after the partial lease closes the aggregate exclusion must hold.
        scope = next(directory for directory, name in held if name == '.quick-snapshot.lock')
        with pytest.raises(b.BackupInProgressError):
            with real_lock(scope, timeout_seconds=0, lock_name='.quick-snapshot.lock'):
                pass
        operations.append(operation)

    def replace(src, dst):
        check(src, 'publish')
        return real_replace(src, dst)

    def remove(path, *args, **kwargs):
        check(path, 'remove')
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(b, '_backup_operation_lock', tracked)
    monkeypatch.setattr(b.os, 'replace', replace)
    monkeypatch.setattr(b.shutil, 'rmtree', remove)
    return operations


def partial(home, kind):
    path = home / 'state-snapshots' / f'.{kind}.partial'
    path.mkdir(parents=True, exist_ok=True)
    old = time.time() - 90000
    marker = {'version': 1, 'host': socket.gethostname(), 'pid': 99999999, 'created': old}
    if kind == 'live':
        marker['pid'] = os.getpid()
    if kind != 'unknown':
        (path / r.OWNER).write_text(json.dumps(marker))
    (path / '.backup.lock').touch()
    (path / 'payload').write_text('synthetic fixture')
    for item in [*path.iterdir(), path]:
        os.utime(item, (old, old))
    return path


@pytest.mark.parametrize('transition', ['publish', 'empty', 'overshoot', 'reap'])
def test_partial_transition_closes_lease_under_aggregate_fence(tmp_path, monkeypatch, sharing_contract, transition):
    home = tmp_path / 'home'
    home.mkdir()
    if transition != 'empty':
        (home / 'config.yaml').write_text('fixture: true')
    abandoned = partial(home, 'dead') if transition == 'reap' else None
    live, unknown, active = [partial(home, kind) for kind in ('live', 'unknown', 'active')]
    real_copy = b.shutil.copy2
    if transition == 'overshoot':
        monkeypatch.setattr(b, '_QUICK_MAX_TOTAL_BYTES', 100000)

        def grow(src, dst, *args, **kwargs):
            result = real_copy(src, dst, *args, **kwargs)
            Path(dst).write_bytes(b'x' * 200000)
            return result

        monkeypatch.setattr(b.shutil, 'copy2', grow)
    # Active dead-PID lease must remain protected independent of owner liveness.
    with b._backup_operation_lock(active):
        result = b.create_quick_snapshot(hermes_home=home)
    assert bool(result) == (transition in ('publish', 'reap'))
    assert all(path.exists() for path in (live, unknown, active))
    assert ('publish' if result else 'remove') in sharing_contract
    if abandoned is not None:
        assert not abandoned.exists()
        assert 'remove' in sharing_contract
    remaining = set((home / 'state-snapshots').glob('.*.partial'))
    assert remaining == {live, unknown, active}


def test_selected_directory_link_not_reserved_or_captured(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('fixture: true')
    external = tmp_path / 'external'
    external.mkdir()
    (external / 'external.json').write_bytes(b'x' * 200000)
    (home / 'pairing').symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(b, '_QUICK_MAX_TOTAL_BYTES', 100000)
    assert list(r.quick_directory_files(home / 'pairing')) == []
    sid = b.create_quick_snapshot(hermes_home=home)
    assert sid  # Linked contents must not consume admission reservation either.
    snapshot = home / 'state-snapshots' / sid
    assert not (snapshot / 'pairing').exists()
    assert json.loads((snapshot / 'manifest.json').read_text())['files'] == {'config.yaml': 13}
