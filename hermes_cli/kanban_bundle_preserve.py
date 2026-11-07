"""Fail-closed preservation gate for terminal Kanban workspace removal.

Before completion, archive, deferred parent cleanup or ``kanban gc`` deletes a
managed scratch dir or a linked worktree, :func:`preservation_gate` archives
the accepted bundle into the task's attachments: every regular file in the
workspace when it holds a ``KEEP-BUNDLE`` marker, plus every workspace file or
directory that the task result, run summaries or run metadata reference. The
archive (``accepted-bundle-<manifest sha256>.tgz``) and its ``.sha256.json``
manifest are verified by re-reading them before their attachment rows commit.
Nothing is extracted, and archive members are relative regular files only.

Reference policy (the Sheldon contract): a *required* reference is an exact
path. That is a whole result/metadata string that is a path, or a prose token
that starts with ``/``, ``./`` or ``../``. Any other file-like prose token is
*inferred*. These refuse cleanup: a missing required reference, a non-regular
member, a link, a prohibited (secret / VCS / venv) name, or the size cap. A
missing inferred reference goes into a durable ``bundle_unresolved_refs``
event. That event commits in its own transaction before anything is deleted,
never under a caller-owned transaction whose rollback would erase the proof.
Every failure retains the workspace. Archive bytes written before a failed
attachment registration stay on disk under their content-addressed name.

Existing workspaces may be inventoried with ``dry_run=True``; that writes no
blobs and no DB rows.
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
import time

MAX_BYTES = 500 * 1024 * 1024
EXCLUDED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__"}
SECRET_NAME = re.compile(r"(?:^\.env(?:\.|$)|secret|credential|token|password|key.*\.json$|(?:^|[._-])auth(?:[._-]|$)|service[_-]?account|id_rsa|id_ed25519|\.pem$|\.key$)", re.I)
_EXPLICIT_PREFIXES = ("/", "./", "../")


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
    """Yield ``(candidate, required)``: exact paths are required, prose tokens inferred."""
    # Metadata commonly contains exact paths; prose contains delimited paths.
    text = text.strip()
    if text.startswith(_EXPLICIT_PREFIXES) or (" " not in text and "/" in text):
        yield text, True
    for candidate in re.findall(r"(?:/|\./|\b)[\w./-]+\.[A-Za-z0-9]{1,12}\b", text):
        yield candidate, candidate.startswith(_EXPLICIT_PREFIXES)


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


def _reference_texts(conn, task_id: str, result) -> list[str]:
    texts = [result or ""]
    for run in conn.execute("SELECT metadata, summary FROM task_runs WHERE task_id=?", (task_id,)):
        if run["metadata"]:
            texts.extend(_strings(json.loads(run["metadata"])))
        texts.append(run["summary"] or "")
    return texts


def _classify_references(root: Path, texts, selected: set, directories: set, unresolved: set) -> None:
    """Sort referenced workspace members; raise for a reference that must refuse cleanup."""
    for text in texts:
        for candidate, required in _references(text):
            p = Path(candidate.rstrip(".,:;)"))
            if not p.is_absolute():
                p = root / p
            p = Path(os.path.abspath(p))
            if not p.is_relative_to(root):
                continue
            rel = p.relative_to(root)
            # Name and link checks run before the missing-member skip, so a
            # dangling link or a secret name can never be filed as unresolved.
            if _unsafe(rel.parts):
                raise ValueError("referenced member has a prohibited name")
            if p.resolve() != p:
                raise ValueError("referenced member is linked")
            if p.is_dir():
                directories.add(p)
            elif p.is_file():
                selected.add(rel.as_posix())
            elif required or p.exists():
                raise ValueError("referenced bundle member is missing")
            else:
                unresolved.add(rel.as_posix())


def _walk_directories(root: Path, directories, selected: set, skipped: set) -> None:
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


def _stat_identity(st) -> tuple:
    return st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _hash_open_member(f, rel: str, before) -> dict:
    digest = hashlib.sha256()
    read = 0
    while chunk := f.read(1024 * 1024):
        read += len(chunk)
        if read > before.st_size:
            raise ValueError("bundle member changed while hashing")
        digest.update(chunk)
    if _stat_identity(before) != _stat_identity(os.fstat(f.fileno())) or read != before.st_size:
        raise ValueError("bundle member changed while hashing")
    return {"path": rel, "bytes": read, "sha256": digest.hexdigest()}


def _hash_members(root: Path, selected, max_bytes: int) -> tuple[list[dict], int]:
    entries = []
    total = 0
    for rel in sorted(selected):
        with _open_regular(root, rel) as f:
            before = os.fstat(f.fileno())
            # Cap before reading so an oversized member is never streamed.
            total += before.st_size
            if total > max_bytes:
                raise ValueError("bundle exceeds preservation size cap")
            entries.append(_hash_open_member(f, rel, before))
    return entries, total


def plan_bundle(conn, task_id: str, workspace: Path, *, max_bytes=MAX_BYTES):
    """Inventory the bundle of a terminal task; raises when cleanup must be refused."""
    root = Path(os.path.abspath(workspace))
    row = conn.execute("SELECT status, result FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row or row["status"] not in {"done", "archived"}:
        raise ValueError("workspace is not terminal")
    if root.is_symlink() or root.resolve() != root or not root.is_dir():
        raise ValueError("workspace root is missing or linked")
    marker = root / "KEEP-BUNDLE"
    if marker.is_symlink():
        raise ValueError("KEEP-BUNDLE is linked")
    keep_all = marker.is_file()
    selected: set = set()
    directories: set = set()
    skipped: set = set()
    unresolved: set = set()
    _classify_references(root, _reference_texts(conn, task_id, row["result"]), selected, directories, unresolved)
    _walk_directories(root, {root} if keep_all else directories, selected, skipped)
    entries, total = _hash_members(root, selected, max_bytes)
    return {"task_id": task_id, "workspace": str(root), "files": entries,
            "bytes": total, "excluded": sorted(skipped), "keep_bundle": keep_all,
            "unresolved_refs": sorted(unresolved)}


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


def _bundle_destination(conn, task_id: str, workspace) -> Path:
    """The attachments dir of the board that owns *conn*, never inside the workspace."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    db_file = kbc._main_db_file(conn)
    if db_file is None:
        raise ValueError("cannot establish attachment board for workspace")
    db_path = Path(db_file)
    if db_path.parent.parent.name == "boards":
        destination = db_path.parent / "attachments" / task_id
    elif db_path.resolve() == kb.kanban_db_path().resolve():
        destination = kb.task_attachments_dir(task_id)
    else:
        raise ValueError("cannot establish attachment board for workspace")
    if destination.is_symlink() or destination.resolve() != Path(os.path.abspath(destination)):
        raise ValueError("attachment destination is linked")
    destination.mkdir(parents=True, exist_ok=True)
    if destination.resolve().is_relative_to(Path(workspace).resolve()):
        raise ValueError("attachments must be outside workspace")
    return destination


def _archive_member_digest(tar, member) -> str:
    f = tar.extractfile(member)
    if f is None:
        raise ValueError("archive member unreadable")
    hasher = hashlib.sha256()
    with f:
        while chunk := f.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _write_verified_archive(path: Path, workspace, files) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for entry in files:
            with _open_regular(Path(workspace), entry["path"]) as source:
                info = tarfile.TarInfo(entry["path"])
                info.size = entry["bytes"]
                info.mode = 0o600
                tar.addfile(info, source)
    # Verify compressed bytes by reopening; no extraction and no links.
    with tarfile.open(path, "r:gz") as tar:
        members = tar.getmembers()
        if [m.name for m in members] != [e["path"] for e in files]:
            raise ValueError("archive inventory mismatch")
        for member, entry in zip(members, files):
            if not member.isfile() or member.size != entry["bytes"]:
                raise ValueError("archive member mismatch")
            if _archive_member_digest(tar, member) != entry["sha256"]:
                raise ValueError("archive checksum mismatch")


def _write_manifest(temp: Path, checksum: Path, manifest: bytes) -> None:
    # A partial checksum write is safe: registration/deletion is not reached.
    with temp.open("xb") as out:
        out.write(manifest)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, checksum)
    if checksum.read_bytes() != manifest:
        raise ValueError("checksum manifest verification failed")


def _register_attachments(conn, task_id: str, blobs) -> None:
    """Attachment rows for both blobs commit together, or the workspace stays."""
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn):
        for p, content_type in blobs:
            if conn.execute(
                "SELECT 1 FROM task_attachments WHERE task_id=? AND stored_path=?", (task_id, str(p)),
            ).fetchone():
                continue
            size = p.stat().st_size
            conn.execute(
                "INSERT INTO task_attachments (task_id, filename, stored_path, content_type, size, uploaded_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, p.name, str(p), content_type, size, "workspace_preservation", int(time.time())),
            )
            kb._append_event(conn, task_id, "attached", {"filename": p.name, "size": size, "by": "workspace_preservation"})
    registered = {r["stored_path"] for r in conn.execute("SELECT stored_path FROM task_attachments WHERE task_id=?", (task_id,))}
    if not {str(p) for p, _ in blobs} <= registered:
        raise ValueError("bundle attachment registration failed")


def preserve_bundle(conn, task_id, workspace, *, dry_run=False, max_bytes=MAX_BYTES):
    report = plan_bundle(conn, task_id, Path(workspace), max_bytes=max_bytes)
    if dry_run:
        return report
    _record_unresolved_refs(conn, task_id, report["unresolved_refs"])
    if not report["files"]:
        return report
    manifest = json.dumps(report["files"], sort_keys=True, indent=2).encode() + b"\n"
    identity = hashlib.sha256(manifest).hexdigest()
    destination = _bundle_destination(conn, task_id, workspace)
    archive = destination / f"accepted-bundle-{identity}.tgz"
    checksum = destination / f"accepted-bundle-{identity}.sha256.json"
    fd, temporary = tempfile.mkstemp(prefix=".bundle-", dir=destination)
    os.close(fd)
    temp = Path(temporary)
    try:
        _write_verified_archive(temp, workspace, report["files"])
        if plan_bundle(conn, task_id, Path(workspace), max_bytes=max_bytes) != report:
            raise ValueError("bundle inventory changed while preserving")
        os.replace(temp, archive)
        _write_manifest(temp.with_suffix(".sha256"), checksum, manifest)
        _register_attachments(conn, task_id, ((archive, "application/gzip"), (checksum, "application/json")))
        report["attachments"] = [str(archive), str(checksum)]
        return report
    finally:
        temp.unlink(missing_ok=True)
        temp.with_suffix(".sha256").unlink(missing_ok=True)


def preservation_gate(task_id, workspace, *, conn=None):
    """Any copy/check/DB failure retains the workspace; callers own deletion."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    try:
        if conn is not None:
            preserve_bundle(conn, task_id, workspace)
        else:
            with kbc.connect_closing() as connection:
                preserve_bundle(connection, task_id, workspace)
        return True
    except Exception as exc:
        message = (
            f"[kanban] Retained workspace {task_id}: bundle preservation failed "
            f"({type(exc).__name__}). Inspect bundle paths/cap and retry cleanup after repair."
        )
        # Logged, not printed: cleanup also runs inside stdio JSON-RPC backends.
        kb._log.warning("%s Detail: %s", message, exc)
        # A DB failure must never turn the preservation failure into deletion.
        try:
            if conn is not None:
                kb.add_comment(conn, task_id, author="workspace_preservation", body=message)
            else:
                with kbc.connect_closing() as connection:
                    kb.add_comment(connection, task_id, author="workspace_preservation", body=message)
        except Exception:
            pass
        return False
