"""Pinned-descriptor bundle publication with immutable retained operation records.

These records are recovery inventory, never deletion permission. Failed operations
are not expired or automatically adopted; a retry uses a new immutable identity.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import stat
import tarfile
import uuid

from hermes_cli import retention_store as store
from hermes_cli.retention_markers import _chain_stable, _open_directory_chain


def _directory_matches(path: Path, descriptor: int) -> None:
    descriptors = _open_directory_chain(Path(os.path.abspath(path)))
    try:
        expected, actual = os.fstat(descriptor), os.fstat(descriptors[-1])
        if ((expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino) or
                not _chain_stable(Path(os.path.abspath(path)), descriptors)):
            raise ValueError('bundle destination identity changed')
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _leaf_matches(parent: int, name: str, descriptor: int) -> None:
    expected = os.fstat(descriptor)
    actual = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (not stat.S_ISREG(actual.st_mode) or
            (expected.st_dev, expected.st_ino, expected.st_size) !=
            (actual.st_dev, actual.st_ino, actual.st_size)):
        raise ValueError('bundle leaf identity changed')


def _exclusive_file(parent: int, name: str):
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    return os.fdopen(fd, 'w+b')


def _open_published(parent: int, name: str):
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError('published bundle is not regular')
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        _leaf_matches(parent, name, fd)
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, 'rb')


def _verify_archive(source, report) -> None:
    source.seek(0)
    with tarfile.open(fileobj=source, mode='r:gz') as archive:
        members = archive.getmembers()
        if [m.name for m in members] != [e['path'] for e in report['files']]:
            raise ValueError('archive inventory mismatch')
        for member, entry in zip(members, report['files']):
            if not member.isfile() or member.size != entry['bytes']:
                raise ValueError('archive member mismatch')
            payload = archive.extractfile(member)
            if payload is None:
                raise ValueError('archive member unreadable')
            with payload:
                if hashlib.file_digest(payload, 'sha256').hexdigest() != entry['sha256']:
                    raise ValueError('archive checksum mismatch')


def _digest(source):
    source.seek(0)
    return {'sha256': hashlib.file_digest(source, 'sha256').hexdigest(),
            'bytes': os.fstat(source.fileno()).st_size}


def _journal(parent: int, operation: str, state: str, record: dict) -> None:
    store._write_exclusive(parent, operation + '.' + state.lower() + '.json',
                           json.dumps({**record, 'state': state}, sort_keys=True).encode() + b'\n')
    os.fsync(parent)


def _reuse_registered(conn, task_id, destination, parent, report, manifest, identity):
    """Only a registered matching pair is eligible for read-only reuse."""
    for row in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='bundle_registered' ORDER BY id DESC", (task_id,)):
        payload = json.loads(row['payload'])
        if (payload.get('member_manifest_sha256') != identity or
                payload.get('workspace') != report['workspace']):
            continue
        paths = [Path(payload[k]) for k in ('archive', 'manifest')]
        if any(p.parent != destination for p in paths):
            raise ValueError('registered bundle owner mismatch')
        for path in paths:
            if not conn.execute('SELECT 1 FROM task_attachments WHERE task_id=? AND stored_path=? AND uploaded_by=?',
                                (task_id, str(path), 'workspace_preservation')).fetchone():
                raise ValueError('registered bundle attachment missing')
        with _open_published(parent, paths[0].name) as source:
            _verify_archive(source, report)
            if _digest(source) != payload['compressed']:
                raise ValueError('registered compressed bundle mismatch')
        with _open_published(parent, paths[1].name) as source:
            if source.read(len(manifest) + 1) != manifest:
                raise ValueError('registered checksum manifest mismatch')
        _directory_matches(destination, parent)
        report['attachments'] = [str(p) for p in paths]
        return True
    return False


def _register(conn, task_id, record, sizes):
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, 'bundle_registered', record)
        for key, content_type in (('archive', 'application/gzip'), ('manifest', 'application/json')):
            path = Path(record[key])
            conn.execute(
                "INSERT INTO task_attachments (task_id,filename,stored_path,content_type,size,uploaded_by,created_at) VALUES(?,?,?,?,?,'workspace_preservation',strftime('%s','now'))",
                (task_id, path.name, str(path), content_type, sizes[key]),
            )
            kb._append_event(conn, task_id, 'attached', {'filename': path.name, 'size': sizes[key], 'by': 'workspace_preservation'})


def publish_report(conn, task_id, workspace, report, *, max_bytes):
    from hermes_cli import kanban_bundle_preserve as bp
    destination = store.attachment_destination(conn, task_id)
    if destination.is_relative_to(Path(os.path.abspath(workspace))):
        raise ValueError('attachments must be outside workspace')
    manifest = json.dumps(report['files'], sort_keys=True, indent=2).encode() + b'\n'
    identity = hashlib.sha256(manifest).hexdigest()
    journal_path = destination.parent / '.retention' / 'bundles'
    with ExitStack() as stack:
        journal = store._open_destination(journal_path)
        stack.callback(os.close, journal)
        parent = store._open_destination(destination)
        stack.callback(os.close, parent)
        if _reuse_registered(conn, task_id, destination, parent, report, manifest, identity):
            return report
        operation = uuid.uuid4().hex
        names = {'archive': f'accepted-bundle-{identity}-{operation}.tgz',
                 'manifest': f'accepted-bundle-{identity}-{operation}.sha256.json'}
        temps = {key: '.bundle-' + operation + '.' + key for key in names}
        record = {'schema': 'hermes.retention.bundle/v1', 'operation': operation,
                  'task_id': task_id, 'database': conn.execute('PRAGMA database_list').fetchone()[2],
                  'workspace': report['workspace'], 'member_manifest_sha256': identity,
                  'destination': str(destination), 'temporary': temps,
                  'destination_identity': {'device': os.fstat(parent).st_dev, 'inode': os.fstat(parent).st_ino},
                  **{key: str(destination / name) for key, name in names.items()},
                  'destructive_authority': 'off'}
        _directory_matches(journal_path, journal)
        _directory_matches(destination, parent)
        _journal(journal, operation, 'PREPARED', record)
        archive = stack.enter_context(_exclusive_file(parent, temps['archive']))
        checksum = stack.enter_context(_exclusive_file(parent, temps['manifest']))
        # Neither the descriptor nor its locator is closed/reopened for writing.
        with tarfile.open(fileobj=archive, mode='w:gz') as tar:
            for entry in report['files']:
                with bp._open_regular(Path(workspace), entry['path']) as source:
                    info = tarfile.TarInfo(entry['path'])
                    info.size, info.mode = entry['bytes'], 0o600
                    tar.addfile(info, source)
        archive.flush()
        os.fsync(archive.fileno())
        _verify_archive(archive, report)
        compressed = _digest(archive)
        checksum.write(manifest)
        checksum.flush()
        os.fsync(checksum.fileno())
        checksum.seek(0)
        if checksum.read(len(manifest) + 1) != manifest:
            raise ValueError('checksum manifest verification failed')
        if bp.plan_bundle(conn, task_id, Path(workspace), max_bytes=max_bytes) != report:
            raise ValueError('bundle inventory changed while preserving')
        streams = {'archive': archive, 'manifest': checksum}
        for key, source in streams.items():
            _directory_matches(destination, parent)
            _leaf_matches(parent, temps[key], source.fileno())
            os.link(temps[key], names[key], src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            os.fsync(parent)
            _leaf_matches(parent, names[key], source.fileno())
        record.update(compressed=compressed,
                      manifest_sha256=hashlib.sha256(manifest).hexdigest(),
                      objects={key: {'device': os.fstat(source.fileno()).st_dev,
                                     'inode': os.fstat(source.fileno()).st_ino,
                                     'bytes': os.fstat(source.fileno()).st_size}
                               for key, source in streams.items()})
        _directory_matches(destination, parent)
        _directory_matches(journal_path, journal)
        _journal(journal, operation, 'BLOBS_DURABLE', record)
        for key, source in streams.items():
            _leaf_matches(parent, names[key], source.fileno())
        _verify_archive(archive, report)
        if _digest(archive) != compressed:
            raise ValueError('published compressed archive changed')
        checksum.seek(0)
        if checksum.read(len(manifest) + 1) != manifest:
            raise ValueError('published checksum manifest changed')
        _directory_matches(destination, parent)
        _directory_matches(journal_path, journal)
        sizes = {key: os.fstat(source.fileno()).st_size for key, source in streams.items()}
        _register(conn, task_id, record, sizes)
        _journal(journal, operation, 'REGISTERED', record)
        # Temp aliases also remain retained inventory, including on all failures.
        report['attachments'] = [record['archive'], record['manifest']]
        return report
