"""Explicit soft hides, separate from physical-delete compatibility APIs.

The bounded contract admits archived, unclaimed tasks only. Original rows, files,
workflow state and dependency scans remain unchanged; audit has no task cascade.
"""
from __future__ import annotations

import json
from pathlib import Path
import time
import uuid

from hermes_cli.retention import RetentionRefused


def _eligible_task(conn, task_id):
    from hermes_cli import kanban_db as kb
    task = kb.get_task(conn, task_id)
    if task is None:
        return None
    if (task.status != 'archived' or task.worker_pid is not None or task.claim_lock is not None or task.claim_expires is not None or
            conn.execute("SELECT 1 FROM task_runs WHERE task_id=? AND (status NOT IN ('completed','failed','cancelled') OR worker_pid IS NOT NULL OR claim_lock IS NOT NULL)", (task_id,)).fetchone() or
            (task.current_run_id is not None and not conn.execute('SELECT 1 FROM task_runs WHERE id=? AND task_id=?', (task.current_run_id, task_id)).fetchone()) or
            conn.execute("""SELECT 1 FROM task_links l LEFT JOIN tasks t ON t.id=l.child_id
                WHERE l.parent_id=? AND (t.id IS NULL
                    OR t.status NOT IN ('done','archived','failed','cancelled')
                    OR t.worker_pid IS NOT NULL OR t.claim_lock IS NOT NULL OR t.claim_expires IS NOT NULL
                    OR EXISTS (SELECT 1 FROM task_runs r WHERE r.task_id=t.id AND
                        (r.status NOT IN ('completed','failed','cancelled') OR r.worker_pid IS NOT NULL OR r.claim_lock IS NOT NULL))
                    OR (t.current_run_id IS NOT NULL AND NOT EXISTS
                        (SELECT 1 FROM task_runs r WHERE r.id=t.current_run_id AND r.task_id=t.id)))""", (task_id,)).fetchone()):
        raise RetentionRefused('soft_hide_requires_archived_unclaimed')
    return task


def _protected(conn, attachment, destination):
    from hermes_cli.retention_bundle import _directory_matches, _open_published
    import os
    if (attachment.uploaded_by == 'workspace_preservation' or
            attachment.filename.startswith('accepted-bundle-') or
            Path(attachment.stored_path).name.startswith('accepted-bundle-')):
        return True
    path = Path(attachment.stored_path)
    if not path.is_absolute() or path.parent != destination or path.name in {'.', '..'}:
        raise RetentionRefused('soft_hide_unproven_locator')
    # Read-only traversal: hiding must never create a missing attachment root.
    from hermes_cli.retention_markers import _open_directory_chain
    descriptors = []
    try:
        descriptors = _open_directory_chain(destination)
        with _open_published(descriptors[-1], path.name) as source:
            info = os.fstat(source.fileno())
        if info.st_nlink != 1:
            # An unmanaged alias can conceal protected ownership.
            raise RetentionRefused('soft_hide_unproven_alias')
        for row in conn.execute("SELECT stored_path,filename,uploaded_by FROM task_attachments"):
            if row['stored_path'] == attachment.stored_path and (
                    row['uploaded_by'] == 'workspace_preservation' or row['filename'].startswith('accepted-bundle-')):
                return True
        for row in conn.execute("SELECT payload FROM task_events WHERE kind='bundle_registered'"):
            record = json.loads(row['payload'])
            if attachment.stored_path in {record.get('archive'), record.get('manifest')}:
                return True
            for identity in record.get('objects', {}).values():
                if (identity['device'], identity['inode']) == (info.st_dev, info.st_ino):
                    return True
        _directory_matches(destination, descriptors[-1])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RetentionRefused('soft_hide_unproven_provenance') from exc
    finally:
        for fd in reversed(descriptors):
            os.close(fd)
    return False


def _record(conn, kind, object_id, original, actor, reason):
    existing = conn.execute('SELECT operation_id FROM retention_tombstones WHERE object_kind=? AND object_id=?', (kind, str(object_id))).fetchone()
    if existing:
        operation = existing['operation_id']
        if not conn.execute('SELECT 1 FROM retention_audit WHERE operation_id=? AND object_kind=? AND object_id=?',
                            (operation, kind, str(object_id))).fetchone():
            raise RetentionRefused('soft_hide_audit_unavailable')
    else:
        operation = uuid.uuid4().hex
        now = int(time.time())
        payload = json.dumps({'operation': operation, 'kind': kind, 'object_id': str(object_id),
                              'original': original, 'actor': actor, 'reason': reason,
                              'tombstoned': True, 'removed': False}, sort_keys=True)
        conn.execute('INSERT INTO retention_tombstones VALUES(?,?,?,?,?,?,?)',
                     (kind, str(object_id), operation, json.dumps(original, sort_keys=True), actor, reason, now))
        conn.execute('INSERT INTO retention_audit VALUES(?,?,?,?,?)', (operation, kind, str(object_id), payload, now))
    return {'tombstoned': True, 'removed': False, 'operation_id': operation}


def _validate(conn, actor, reason):
    if conn.in_transaction:
        raise ValueError('soft hide requires its own commit')
    if not actor.strip() or not reason.strip() or len(actor) > 256 or len(reason) > 4096:
        raise ValueError('soft hide requires bounded actor and reason')


def hide_task(conn, task_id, *, actor, reason):
    from hermes_cli import kanban_db as kb
    _validate(conn, actor, reason)
    with kb.write_txn(conn):
        task = _eligible_task(conn, task_id)
        if task is None:
            return False
        return _record(conn, 'task', task_id, {'status': task.status, 'workspace_path': task.workspace_path}, actor, reason)


def hide_attachment(conn, attachment_id, *, actor, reason):
    from hermes_cli import kanban_db as kb
    from hermes_cli.retention_store import attachment_destination
    _validate(conn, actor, reason)
    attachment = kb.get_attachment(conn, attachment_id)
    if attachment is None:
        return None
    destination = attachment_destination(conn, attachment.task_id)
    if _protected(conn, attachment, destination):
        raise RetentionRefused('protected_attachment')
    with kb.write_txn(conn):
        # Recheck the row after acquiring SQLite writer serialization. This
        # governs metadata hiding only, never a filesystem cleanup operation.
        if kb.get_attachment(conn, attachment_id) != attachment:
            raise RetentionRefused('soft_hide_attachment_changed')
        if _protected(conn, attachment, destination):
            raise RetentionRefused('protected_attachment')
        if _eligible_task(conn, attachment.task_id) is None:
            raise RetentionRefused('soft_hide_missing_owner')
        return _record(conn, 'attachment', attachment_id,
                       {'task_id': attachment.task_id, 'filename': attachment.filename,
                        'stored_path': attachment.stored_path}, actor, reason)
