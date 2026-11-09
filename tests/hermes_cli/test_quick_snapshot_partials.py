"""Only expired, owned, unlocked crash partials are reclaimed."""
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from hermes_cli import backup


def test_crash_partial_has_ownership_and_is_not_blindly_removed(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('fixture: true')
    original = backup.shutil.copy2
    monkeypatch.setattr(backup.shutil, 'copy2', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('crash fixture')))
    with pytest.raises(RuntimeError, match='crash fixture'):
        backup.create_quick_snapshot(hermes_home=home)
    partial, = (home / 'state-snapshots').glob('.*.partial')
    owner_path = partial / '.quick-owner.json'
    assert owner_path.is_file()
    marker = json.loads(owner_path.read_text())
    assert marker['pid'] == os.getpid()
    monkeypatch.setattr(backup.shutil, 'copy2', original)
    assert backup.create_quick_snapshot(hermes_home=home)
    assert partial.exists()  # live PID, including possible PID reuse: never reap


@pytest.mark.parametrize('condition', ['dead', 'active', 'future', 'unknown', 'symlink', 'reused_pid', 'foreign_host', 'future_mtime', 'recent'])
def test_partial_ttl_and_lease_guards(tmp_path, monkeypatch, condition):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('fixture: true')
    root = home / 'state-snapshots'
    root.mkdir()
    partial = root / '.crash.fixture.partial'
    partial.mkdir()
    marker = {'version': 1, 'host': socket.gethostname(), 'pid': 99999999, 'created': backup.time.time() - 90000}
    (partial / '.quick-owner.json').write_text(json.dumps(marker))
    (partial / '.backup.lock').touch()
    if condition == 'future':
        marker['created'] = backup.time.time() + 90000
        (partial / '.quick-owner.json').write_text(json.dumps(marker))
    if condition == 'unknown':
        (partial / '.quick-owner.json').unlink()
    if condition in ('reused_pid', 'foreign_host', 'recent'):
        if condition == 'reused_pid':
            marker['pid'] = os.getpid()
        elif condition == 'foreign_host':
            marker['host'] = 'other-host'
        else:
            marker['created'] = backup.time.time()
        (partial / '.quick-owner.json').write_text(json.dumps(marker))
    if condition == 'symlink':
        (partial / 'foreign').symlink_to(home / 'config.yaml')
    old = backup.time.time() - 90000
    for p in [*partial.iterdir(), partial]:
        os.utime(p, (old, old), follow_symlinks=False)
    if condition == 'future_mtime':
        future = backup.time.time() + 90000
        os.utime(partial, (future, future))
    if condition == 'active':
        with backup._backup_operation_lock(partial):
            assert backup.create_quick_snapshot(hermes_home=home)
            assert partial.exists()
    else:
        result = backup.create_quick_snapshot(hermes_home=home)
        assert bool(result) == (condition != 'symlink')
        assert partial.exists() == (condition != 'dead')


def test_real_child_crash_partial_reclaimed_after_proven_expiry(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('fixture: true')
    script = tmp_path / 'crash_capture.py'
    script.write_text('''import sys
from pathlib import Path
from hermes_cli import backup
def crash(*a, **kw):
    raise RuntimeError('owned child crash fixture')
backup.shutil.copy2 = crash
backup.create_quick_snapshot(hermes_home=Path(sys.argv[1]))
''')
    proc = subprocess.Popen([sys.executable, str(script), str(home)],
                            env={'PYTHONPATH': str(Path(backup.__file__).parent.parent),
                                 'HOME': str(tmp_path), 'TMPDIR': str(tmp_path)},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _stdout, stderr = proc.communicate(timeout=20)
    print(f'crash_child_pid={proc.pid} returncode={proc.returncode} terminated={proc.poll() is not None}')
    assert proc.returncode != 0 and 'owned child crash fixture' in stderr
    partial, = (home / 'state-snapshots').glob('.*.partial')
    marker_file = partial / '.quick-owner.json'
    marker = json.loads(marker_file.read_text())
    assert marker['pid'] == proc.pid
    old = backup.time.time() - 90000
    marker['created'] = old
    marker_file.write_text(json.dumps(marker))
    for p in [*partial.rglob('*'), partial]:
        os.utime(p, (old, old), follow_symlinks=False)
    assert backup.create_quick_snapshot(hermes_home=home)
    assert not partial.exists()
