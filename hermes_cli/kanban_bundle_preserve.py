"""Fail-closed preservation gate for terminal Kanban workspace removal.

No extraction occurs here. Archives contain only relative regular-file members.
Existing workspaces may be inventoried with dry_run=True without writing blobs/DB.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tarfile
import tempfile

MAX_BYTES = 500 * 1024 * 1024
EXCLUDED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__"}
SECRET_NAME = re.compile(r"(?:^\.env(?:\.|$)|secret|credential|token|password|key.*\.json$|(?:^|[._-])auth(?:[._-]|$)|service[_-]?account|id_rsa|id_ed25519|\.pem$|\.key$)", re.I)


def _unsafe(parts):
    return any(p in EXCLUDED_DIRS or SECRET_NAME.search(p) for p in parts)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (tuple, list)):
        for v in value:
            yield from _strings(v)


def _references(text):
    # Remove whole URL spans before filename extraction, including prose URLs.
    text = re.sub(r"\b[\w+.-]+://[^\s`<>\"']+", "", text)
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("/", "./", "../")) or (not re.search(r"\s", line) and "/" in line):
            yield line, line.startswith("/")
            if not re.search(r"\s", line):
                continue
        for candidate in re.findall(r"(?:/|\./|\b)[\w./-]+\.[A-Za-z0-9]{1,12}\b", line):
            # Relative names may refer to a repo or branch, not this workspace.
            yield candidate, candidate.startswith("/")


def _open_regular(root: Path, rel: str):
    """Walk via no-follow directory descriptors, including the workspace root."""
    parts = Path(rel).parts
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    finally:
        os.close(fd)
    if not stat.S_ISREG(os.fstat(source).st_mode):
        os.close(source)
        raise ValueError("not a regular bundle member")
    return os.fdopen(source, "rb")


def plan_bundle(conn, task_id: str, workspace: Path, *, max_bytes=MAX_BYTES):
    root = Path(os.path.abspath(workspace))
    row = conn.execute("SELECT status, result FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row or row["status"] not in {"done", "archived"}:
        raise ValueError("workspace is not terminal")
    if root.is_symlink() or root.resolve() != root or not root.is_dir():
        raise ValueError("workspace root is missing or linked")
    selected = set()
    directories = set()
    skipped = set()
    unresolved_refs = set()
    marker = root / "KEEP-BUNDLE"
    if marker.is_symlink():
        raise ValueError("KEEP-BUNDLE is linked")
    keep_all = marker.is_file()
    texts = [row["result"] or ""]
    for run in conn.execute("SELECT metadata, summary FROM task_runs WHERE task_id=?", (task_id,)):
        if run["metadata"]:
            metadata = json.loads(run["metadata"])
            if isinstance(metadata, dict):
                # Run-level checkout custody is not a bundle artifact reference.
                metadata = {key: value for key, value in metadata.items() if key != "worktree"}
            texts.extend(_strings(metadata))
        texts.append(run["summary"] or "")
    for text in texts:
        for candidate, required in _references(text):
            candidate = candidate.rstrip(".,:;)")
            p = Path(candidate)
            if not p.is_absolute():
                p = root / p
            p = Path(os.path.abspath(p))
            if not p.is_relative_to(root):
                continue
            rel = p.relative_to(root)
            if _unsafe(rel.parts):
                raise ValueError("referenced member has a prohibited name")
            if p.resolve() != p:
                raise ValueError("referenced member is linked")
            if p.is_dir():
                directories.add(p)
                continue
            if not p.is_file():
                if required or p.exists():
                    raise ValueError("referenced bundle member is missing")
                unresolved_refs.add(rel.as_posix())
                continue
            selected.add(rel.as_posix())
    if keep_all:
        directories = {root}
    for directory in sorted(directories):
        for folder, dirs, files in os.walk(directory, followlinks=False):
            parent = Path(folder).relative_to(root)
            for name in list(dirs):
                rel = parent / name
                if (root / rel).is_symlink() or SECRET_NAME.search(name):
                    raise ValueError("unsafe bundle directory; retain workspace")
                if _unsafe(rel.parts):
                    dirs.remove(name)
                    skipped.add(rel.as_posix())
            for name in files:
                rel = parent / name
                p = root / rel
                if p.is_symlink() or SECRET_NAME.search(name) or not p.is_file():
                    raise ValueError("unsafe bundle file; retain workspace")
                if _unsafe(rel.parts):
                    skipped.add(rel.as_posix())
                    continue
                selected.add(rel.as_posix())
    entries = []
    total = 0
    for rel in sorted(selected):
        with _open_regular(root, rel) as f:
            before = os.fstat(f.fileno())
            total += before.st_size
            if total > max_bytes:
                raise ValueError("bundle exceeds preservation size cap")
            digest = hashlib.sha256()
            read = 0
            while chunk := f.read(1024 * 1024):
                read += len(chunk)
                if read > before.st_size:
                    raise ValueError("bundle member changed while hashing")
                digest.update(chunk)
            after = os.fstat(f.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or read != before.st_size:
                raise ValueError("bundle member changed while hashing")
            entries.append({"path": rel, "bytes": read, "sha256": digest.hexdigest()})
    return {"task_id": task_id, "workspace": str(root), "files": entries,
            "bytes": total, "excluded": sorted(skipped), "keep_bundle": keep_all,
            "unresolved_refs": sorted(unresolved_refs)}


def _record_unresolved_refs(conn, task_id, refs):
    """Commit cleanup provenance even when there are no files to archive."""
    if not refs:
        return
    from hermes_cli import kanban_db as kb

    payload = {"unresolved_refs": sorted(set(refs))}
    # A savepoint in a caller-owned transaction is not durable cleanup proof.
    if conn.in_transaction:
        raise ValueError("unresolved bundle references require their own commit")
    query = (
        "SELECT 1 FROM task_events WHERE task_id=? "
        "AND kind='bundle_unresolved_refs' AND payload=?"
    )
    params = (task_id, kb._json_or_null(payload))
    with kb.write_txn(conn):
        if not conn.execute(query, params).fetchone():
            kb._append_event(conn, task_id, "bundle_unresolved_refs", payload)
    if not conn.execute(query, params).fetchone():
        raise ValueError("unresolved bundle references were not recorded")


def preserve_bundle(conn, task_id, workspace, *, dry_run=False, max_bytes=MAX_BYTES):
    from hermes_cli import kanban_db as kb
    report = plan_bundle(conn, task_id, Path(workspace), max_bytes=max_bytes)
    if dry_run:
        return report
    _record_unresolved_refs(conn, task_id, report["unresolved_refs"])
    if not report["files"]:
        return report
    manifest = json.dumps(report["files"], sort_keys=True, indent=2).encode() + b"\n"
    identity = hashlib.sha256(manifest).hexdigest()
    db_file = conn.execute('PRAGMA database_list').fetchone()[2]
    if Path(db_file).parent.parent.name == 'boards':
        destination = Path(db_file).parent / 'attachments' / task_id
    elif Path(db_file).resolve() == kb.kanban_db_path().resolve():
        destination = kb.task_attachments_dir(task_id)
    else:
        raise ValueError('cannot establish attachment board for workspace')
    if destination.is_symlink() or destination.resolve() != Path(os.path.abspath(destination)):
        raise ValueError("attachment destination is linked")
    destination.mkdir(parents=True, exist_ok=True)
    if destination.resolve().is_relative_to(Path(workspace).resolve()):
        raise ValueError("attachments must be outside workspace")
    archive = destination / f"accepted-bundle-{identity}.tgz"
    checksum = destination / f"accepted-bundle-{identity}.sha256.json"
    fd, temporary = tempfile.mkstemp(prefix=".bundle-", dir=destination)
    os.close(fd)
    temp = Path(temporary)
    try:
        with tarfile.open(temp, "w:gz") as tar:
            for entry in report["files"]:
                with _open_regular(Path(workspace), entry["path"]) as source:
                    info = tarfile.TarInfo(entry["path"])
                    info.size = entry["bytes"]
                    info.mode = 0o600
                    tar.addfile(info, source)
        # Verify compressed bytes by reopening; no extraction and no links.
        with tarfile.open(temp, "r:gz") as tar:
            members = tar.getmembers()
            if [m.name for m in members] != [e["path"] for e in report["files"]]:
                raise ValueError("archive inventory mismatch")
            for member, entry in zip(members, report["files"]):
                if not member.isfile() or member.size != entry["bytes"]:
                    raise ValueError("archive member mismatch")
                f = tar.extractfile(member)
                if f is None:
                    raise ValueError("archive member unreadable")
                with f:
                    hasher = hashlib.sha256()
                    while chunk := f.read(1024 * 1024):
                        hasher.update(chunk)
                    digest = hasher.hexdigest()
                if digest != entry["sha256"]:
                    raise ValueError("archive checksum mismatch")
        if plan_bundle(conn, task_id, Path(workspace), max_bytes=max_bytes) != report:
            raise ValueError("bundle inventory changed while preserving")
        os.replace(temp, archive)
        # A partial checksum write is safe: registration/deletion is not reached.
        checksum_temp = temp.with_suffix('.sha256')
        with checksum_temp.open('xb') as out:
            out.write(manifest)
            out.flush()
            os.fsync(out.fileno())
        os.replace(checksum_temp, checksum)
        if checksum.read_bytes() != manifest:
            raise ValueError("checksum manifest verification failed")
        with kb.write_txn(conn):
            for p, content_type in ((archive, "application/gzip"), (checksum, "application/json")):
                existing = conn.execute("SELECT id FROM task_attachments WHERE task_id=? AND stored_path=?", (task_id, str(p))).fetchone()
                if not existing:
                    size = p.stat().st_size
                    conn.execute(
                        "INSERT INTO task_attachments (task_id, filename, stored_path, content_type, size, uploaded_by, created_at) VALUES (?, ?, ?, ?, ?, ?, strftime('%s','now'))",
                        (task_id, p.name, str(p), content_type, size, "workspace_preservation"),
                    )
                    kb._append_event(conn, task_id, "attached", {"filename": p.name, "size": size, "by": "workspace_preservation"})
        registered = {r["stored_path"] for r in conn.execute("SELECT stored_path FROM task_attachments WHERE task_id=?", (task_id,))}
        if not {str(archive), str(checksum)} <= registered:
            raise ValueError("bundle attachment registration failed")
        report["attachments"] = [str(archive), str(checksum)]
        return report
    finally:
        temp.unlink(missing_ok=True)
        temp.with_suffix('.sha256').unlink(missing_ok=True)


def preservation_gate(task_id, workspace, *, conn=None):
    """Any copy/check/DB failure retains the workspace; callers own deletion."""
    from hermes_cli import kanban_db_connect as kbc
    try:
        if conn is not None:
            preserve_bundle(conn, task_id, workspace)
        else:
            with kbc.connect_closing() as connection:
                preserve_bundle(connection, task_id, workspace)
        return True
    except Exception as exc:
        message = f"[kanban] Retained workspace {task_id}: bundle preservation failed ({type(exc).__name__}). Inspect bundle paths/cap and retry cleanup after repair."
        print(message, flush=True)
        # A DB failure must never turn the preservation failure into deletion.
        try:
            from hermes_cli import kanban_db as kb
            if conn is not None:
                kb.add_comment(conn, task_id, author="workspace_preservation", body=message)
            else:
                with kbc.connect_closing() as connection:
                    kb.add_comment(connection, task_id, author="workspace_preservation", body=message)
        except Exception:
            pass
        return False
