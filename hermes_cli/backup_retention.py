"""Recovery-aware quick-snapshot accounting. Callers hold the install quick lock.

The 8 GiB budget counts logical regular-file bytes (including manifests, hidden
partials and unknown entries) in root/state-snapshots and immediate non-link
profiles/*/state-snapshots only. No recursive profile discovery or ZIP budget.
Unknown/unreadable/link trees are never deleted; unaccountable trees refuse
admission. Protected recovery may exceed the cap: stop writing, never erase it.
"""
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import stat
import tempfile
import time
from contextlib import contextmanager

logger = logging.getLogger(__name__)
PARTIAL_TTL = 24 * 60 * 60
OWNER = '.quick-owner.json'


def exists_strict(path):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def is_reparse(path):
    """Reject Windows junction/reparse trees, including on supported Python 3.11."""
    try:
        return bool(getattr(path.lstat(), 'st_file_attributes', 0)
                    & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))
    except FileNotFoundError:
        return False


def tree_bytes(path: Path) -> int:
    """Strict no-link accounting; scandir errors must not look like an empty tree."""
    info = path.lstat()
    if stat.S_ISREG(info.st_mode):
        return info.st_size
    if not stat.S_ISDIR(info.st_mode) or is_reparse(path):
        raise OSError(f'Unaccountable snapshot entry: {path}')
    return sum(tree_bytes(p) for p in path.iterdir())


def snapshot_roots(scope: Path, home: Path) -> list[Path]:
    roots = {home / 'state-snapshots', scope / 'state-snapshots'}
    profiles = scope / 'profiles'
    if profiles.is_symlink() or is_reparse(profiles):
        raise OSError('Linked profiles root')
    if exists_strict(profiles):
        for p in profiles.iterdir():
            if p.is_symlink() or is_reparse(p):
                # Do not follow aliases; they are outside the finite scope.
                continue
            if p.is_dir() and not p.name.startswith('.'):
                roots.add(p / 'state-snapshots')
    return sorted(roots)


def _manifest(path: Path):
    try:
        meta = json.loads((path / 'manifest.json').read_text(encoding='utf-8-sig'))
        files = meta['files']
        if not isinstance(files, dict) or not files:
            return None
        for rel, size in files.items():
            p = Path(rel)
            if p.is_absolute() or '..' in p.parts or type(size) is not int or size < 0:
                return None
            if (path / p).stat().st_size != size or not (path / p).is_file():
                return None
        return meta
    except (OSError, ValueError, KeyError, TypeError):
        return None


def inventory(roots):
    """Return total, known generations and mandatory per-profile recovery anchors."""
    total, entries, protected = 0, [], set()
    for root in roots:
        if not exists_strict(root):
            continue
        total += tree_bytes(root)
        known = []
        for p in root.iterdir():
            if p.is_dir() and not p.name.startswith('.') and not p.name.endswith('.partial'):
                meta = _manifest(p)
                if meta is not None:
                    known.append((p, meta, tree_bytes(p)))
        # A persistent sequence survives same-second labels and wall-clock rollback;
        # touching an old payload must not make it the newest recovery anchor.
        known.sort(key=lambda item: (sequence(item[1]), str(item[1].get('timestamp', '')),
                                     item[0].name), reverse=True)
        dbs, complete = set(), False
        for index, (p, meta, size) in enumerate(known):
            if index == 0:
                protected.add(p)
            if not complete and not meta.get('failed_dbs') and not meta.get('oversized_skipped'):
                protected.add(p)
                complete = True
            captured = {rel for rel in meta['files'] if rel.endswith('.db')}
            if captured - dbs:
                protected.add(p)
            dbs.update(captured)
            entries.append((p, size, index))
    return total, entries, protected


def sequence(meta):
    value = meta.get('capture_sequence', 0)
    return value if type(value) is int and value >= 0 else 0


def next_sequence(root):
    return 1 + max((sequence(meta) for p in root.iterdir()
                    if p.is_dir() and not p.name.startswith('.')
                    and (meta := _manifest(p)) is not None), default=0)


def retain(roots, keep: int, budget: int, reserve: int = 0, prune_root=None):
    """Prune only known unprotected generations, oldest first; return (fit, deleted).

    A profile's count limit is local; aggregate byte pressure can evict optional
    history in siblings, but each sibling's current/complete/per-DB anchors survive.
    """
    total, entries, protected = inventory(roots)
    deleted = 0
    for p, size, index in sorted(entries, key=lambda e: (e[0].stat().st_mtime_ns, str(e[0]))):
        over_count = (prune_root is None or p.parent == prune_root) and index >= max(keep, 1)
        if p in protected or not (over_count or total + reserve > budget):
            continue
        try:
            shutil.rmtree(p)
            total -= size
            deleted += 1
        except OSError as exc:
            logger.warning('Failed to prune quick snapshot %s: %s', p, exc)
    return total + reserve <= budget, deleted


def reap_partials(roots, lock):
    """Only our expired protocol + dead PID + unlocked lease proves abandonment.

    A live/reused PID is deliberately ambiguous and never eligible. Wall clock
    rollback/future mtimes, unknown ownership, symlinks and unreadable trees skip.
    The install lock fences cooperating writers; the partial lock fences its owner.
    """
    import psutil
    from hermes_cli.backup import BackupInProgressError
    now = time.time()
    for root in roots:
        if not exists_strict(root) or root.is_symlink() or is_reparse(root):
            continue
        for p in root.glob('.*.partial'):
            try:
                tree_bytes(p)
                marker = json.loads((p / OWNER).read_text())
                pid, created = marker['pid'], marker['created']
                if (marker.get('version') != 1 or marker.get('host') != socket.gethostname()
                        or type(pid) is not int or pid <= 0):
                    continue
                if type(created) not in (int, float) or not 0 < created <= now - PARTIAL_TTL:
                    continue
                # Include descendants: a recent write means work, not an expired lease.
                times = [x.lstat().st_mtime for x in [p, *p.rglob('*')]]
                if any(not 0 < t <= now - PARTIAL_TTL for t in times) or psutil.pid_exists(pid):
                    continue
                if not (p / '.backup.lock').is_file():
                    continue
                with lock(p, timeout_seconds=0):
                    shutil.rmtree(p)
            except (OSError, ValueError, KeyError, TypeError, BackupInProgressError):
                continue


def quick_directory_files(directory):
    """Do not even descend into runtime trees or follow source directory links."""
    def walk_error(exc):
        raise exc
    for base, dirs, files in os.walk(directory, followlinks=False, onerror=walk_error):
        dirs[:] = [n for n in dirs if n not in {'workspaces', 'attachments'}
                   and not (Path(base) / n).is_symlink()
                   and not is_reparse(Path(base) / n)]
        for name in files:
            p = Path(base) / name
            if p.is_symlink() or is_reparse(p):
                continue
            if name.endswith(('.db-wal', '.db-shm', '.db-journal')):
                continue
            # Only legacy board database backup history, not arbitrary .bak files.
            if directory.as_posix().endswith('/kanban/boards') and '.db.bak-' in name:
                continue
            yield p


@contextmanager
def prepare_capture(home, scope, selected, max_file_size, budget, lock):
    """Reserve before writes; actual staged bytes are rechecked by the publisher.

    Reserve source logical bytes + WAL bytes + 64KiB manifest/ownership overhead.
    SQLite/ordinary files can grow during copying: publication rechecks actual
    usage, so the steady-state cap is hard, not a bound on transient write peaks.
    """
    stage = None
    try:
        roots = snapshot_roots(scope, home)
        # Account first: never let orphan cleanup traverse an untrusted root.
        inventory(roots)
        reap_partials(roots, lock)
        reserve = 65536
        for rel in selected:
            src = home / rel
            if not src.exists():
                continue
            paths = quick_directory_files(src) if src.is_dir() else [src]
            for p in paths:
                size = p.stat().st_size
                if max_file_size is not None and size > max_file_size:
                    continue
                reserve += size
                if p.suffix == '.db':
                    wal = p.with_name(p.name + '-wal')
                    if wal.exists():
                        reserve += wal.stat().st_size
        # Admission may evict optional history, never protected recovery.
        fits, _ = retain(roots, 20, budget, reserve=reserve, prune_root=home / 'state-snapshots')
        if not fits:
            yield None
            return
        root = home / 'state-snapshots'
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != 'nt':
            root.chmod(0o700)
        stage = Path(tempfile.mkdtemp(prefix='.quick-', suffix='.partial', dir=root))
        (stage / OWNER).write_text(json.dumps({'version': 1, 'pid': os.getpid(),
                                              'host': socket.gethostname(), 'created': time.time()}))
    except OSError as exc:
        logger.warning('Quick snapshot admission refused: %s', exc)
        yield None
        return
    # Exceptions in the copy body intentionally leave an owned crash partial.
    with lock(stage):
        yield stage, roots
