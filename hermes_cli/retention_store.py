"""Connection-bound, collision-safe attachment publication with retained recovery intent.

This is preservation only. No record here grants removal authority. Full distributed
custody and power-loss acceptance remain prerequisites to any future executor.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import uuid


def attachment_destination(conn, task_id: str, *, board=None) -> Path:
    from hermes_cli import kanban_db as kb
    if conn.in_transaction:
        raise ValueError('attachment publication requires its own commit')
    # Task ids are opaque DB identities, but cannot become filesystem components
    # supplied by an untrusted imported row without validation.
    if not task_id or task_id in {'.', '..'} or Path(task_id).name != task_id or '\\' in task_id:
        raise ValueError('unsafe attachment task identity')
    kb._require_task(conn, task_id)
    filename = conn.execute('PRAGMA database_list').fetchone()[2]
    if not filename:
        raise ValueError('cannot establish attachment owner board')
    db_file = Path(filename).absolute()
    if board is not None and kb.kanban_db_path(board=board).absolute() != db_file:
        raise ValueError('attachment owner board mismatch')
    if db_file.parent.parent.name == 'boards':
        return db_file.parent / 'attachments' / task_id
    if db_file != kb.kanban_db_path(board=kb.DEFAULT_BOARD).absolute():
        raise ValueError('cannot establish attachment owner board')
    return kb.task_attachments_dir(task_id, board=kb.DEFAULT_BOARD)


def _open_destination(path: Path) -> int:
    """Create required directories with no-follow descriptor-relative traversal."""
    root = Path(os.path.abspath(path))
    fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in root.parts[1:]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
                os.fsync(fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _write_exclusive(parent: int, name: str, data: bytes):
    fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    with os.fdopen(fd, 'wb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
        return os.fstat(output.fileno())


def publish_bytes(conn, task_id: str, filename: str, data: bytes, *, content_type=None,
                  uploaded_by=None, board=None) -> int:
    """Durable exclusive publication precedes the short registration transaction.

    Same-name writers cannot overwrite each other's bytes. An insert/fsync failure
    retains the original input, any published blob, temp and a detached intent.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.retention_bundle import _directory_matches, _open_published
    safe_name = kb._safe_attachment_name(filename)
    destination = attachment_destination(conn, task_id, board=board)
    # Keep recovery intents outside any per-task attachment directory.
    journal_dir = destination.parent / '.retention'
    journal = _open_destination(journal_dir)
    try:
        parent = _open_destination(destination)
    except BaseException:
        os.close(journal)
        raise
    operation = uuid.uuid4().hex
    temporary = '.attachment-' + operation
    intent = {'schema': 'hermes.retention.store/v1', 'operation': operation,
              'database': conn.execute('PRAGMA database_list').fetchone()[2], 'task_id': task_id,
              'destination': str(destination), 'temporary': temporary, 'filename': safe_name,
              'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data), 'state': 'PREPARED'}
    try:
        _write_exclusive(journal, operation + '.json', json.dumps(intent, sort_keys=True).encode())
        os.fsync(journal)
        _write_exclusive(parent, temporary, data)
        _directory_matches(destination, parent)
        stem, dot, ext = safe_name.partition('.')
        for index in range(10000):
            final = safe_name if index == 0 else f'{stem} ({index}){dot}{ext}'
            try:
                # Exclusive creation cannot clobber an existing locator. Keep
                # a separate recovery copy, not an ambiguous hard-link alias.
                expected = _write_exclusive(parent, final, data)
                break
            except FileExistsError:
                continue
        else:
            raise ValueError('attachment name collision limit reached')
        os.fsync(parent)
        def verify_publication():
            _directory_matches(destination, parent)
            _directory_matches(journal_dir, journal)
            with _open_published(parent, final) as source:
                info = os.fstat(source.fileno())
                if ((info.st_dev, info.st_ino, info.st_size) !=
                        (expected.st_dev, expected.st_ino, len(data))):
                    raise ValueError('published attachment identity mismatch')
                if hashlib.file_digest(source, 'sha256').hexdigest() != intent['sha256']:
                    raise ValueError('published attachment digest mismatch')
            return info
        info = verify_publication()
        published = {**intent, 'state': 'BLOBS_DURABLE', 'stored_path': str(destination / final),
                     'device': info.st_dev, 'inode': info.st_ino}
        _write_exclusive(journal, operation + '.published.json', json.dumps(published, sort_keys=True).encode())
        os.fsync(journal)
        verify_publication()
        aid = kb.add_attachment(conn, task_id, filename=final, stored_path=str(destination / final),
                                content_type=content_type, size=len(data), uploaded_by=uploaded_by)
        _write_exclusive(journal, operation + '.registered.json', json.dumps({**published, 'state': 'REGISTERED', 'attachment_id': aid}, sort_keys=True).encode())
        os.fsync(journal)
        # Keep recovery inventory even on success. A substituted temporary
        # locator never grants permission to unlink unrelated bytes.
        return aid
    finally:
        os.close(journal)
        os.close(parent)
