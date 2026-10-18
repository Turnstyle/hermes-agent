"""Fail-closed preservation gate for terminal Kanban workspace removal.

No extraction occurs here. Archives contain only relative regular-file members.
Existing workspaces may be inventoried with dry_run=True without writing blobs/DB.

Removal is retain-by-default. Callers delete only when ``preservation_gate``
returns true.

- ``HOLD-WORKSPACE`` retains before planning, archive, or deletion whenever
  the name exists. A no-follow probe error retains before preservation too.
  It wins over ``KEEP-BUNDLE`` and over any worker-written file.
- No workspace file authorizes an empty plan. A name that looks disposable
  or like a versioned attestation is not removal authority. There is no
  acceptance path for that kind of marker.
- ``KEEP-BUNDLE`` does not select the whole tree when it is absent. Absence
  leaves explicit references in charge, including an absolute path that
  points inside the workspace. A present marker selects the whole tree only
  when it is a regular file with the owner-read bit set, at most
  ``KEEP_MARKER_MAX_BYTES`` bytes, opened without following a link. Contents
  are not a document format. A link, directory, oversized file, missing
  owner-read bit, or any probe error retains before archive registration.
  It is not a hold and it does not authorize an empty plan.
- An empty plan retains. A symlink workspace root retains. A missing required
  reference, or a failed copy, checksum, size, symlink, secret, or database
  step retains. A preexisting unrelated attachment is not proof. Dry-run
  never writes or deletes. This gate is not a universal delete barrier.
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
SOURCE_SUFFIX = re.compile(r"(?::\d+(?:-\d+|:\d+)?|#[^/#]+)$")
HOLD_MARKER = "HOLD-WORKSPACE"
KEEP_MARKER = "KEEP-BUNDLE"
# Presence only. A larger file is not a selection decision.
KEEP_MARKER_MAX_BYTES = 4096


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
    # Markdown links name the destination, not a possibly filename-like label.
    text = re.sub(r"\[[^\]\n]*\]\(([^()\n]+)\)", r"<\1>", text)
    # Remove whole URL spans before filename extraction, including prose URLs.
    text = re.sub(r"\b[\w+.-]+://[^\s`<>\"']+", "", text)
    explicit = []

    def consume(match):
        # Preserve quoted identity, including spaces and terminal punctuation.
        explicit.append(next(part for part in match.groups() if part is not None))
        return " "

    text = re.sub(r"(?<![\w/])(?:`([^`\n]+)`|\"([^\"\n]+)\"|'([^'\n]+)'|<([^<>\n]+)>)", consume, text)
    for candidate in explicit:
        yield candidate, candidate.startswith("/")
    for token in text.split():
        # Only outside prose boundaries are removed; never match a path prefix
        # such as bundle/v1.0 within bundle/v1.0/README and select its siblings.
        candidate = token.lstrip("([{*").rstrip(".,:;)]}*")
        candidate = re.sub(r"^[A-Za-z_][\w-]*[=:]", "", candidate)
        if "/" in candidate or re.fullmatch(r"[\w.-]+\.[A-Za-z0-9]{1,12}", SOURCE_SUFFIX.sub("", candidate)):
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


def _keep_selects_all(root: Path) -> bool:
    """Whether a present ``KEEP-BUNDLE`` adds the whole tree.

    Absence returns ``False`` and does not select the tree. A regular file
    with the owner-read bit, size at or under ``KEEP_MARKER_MAX_BYTES``,
    opened with ``O_NOFOLLOW``, returns ``True``. Every other status or
    probe error raises before archive registration. Contents are not parsed.
    """
    path = root / KEEP_MARKER
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValueError("keep marker probe failed before preservation") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("keep marker probe failed before preservation")
    if info.st_size > KEEP_MARKER_MAX_BYTES or (info.st_mode & stat.S_IRUSR) == 0:
        raise ValueError("keep marker probe failed before preservation")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError("keep marker probe failed before preservation") from exc
    try:
        try:
            os.read(fd, 1)
        except OSError as exc:
            raise ValueError("keep marker probe failed before preservation") from exc
    finally:
        os.close(fd)
    return True


def _root_retention_reason(root: Path) -> str | None:
    try:
        if root.is_symlink() or not root.is_dir():
            return "workspace root is missing or linked"
    except OSError:
        return "workspace root is missing or linked"
    return None


def _hold_probe(root: Path) -> str | None:
    """Return a retention reason before any preservation walk.

    The hold name existing is enough, including a link or a directory, and
    the probe never follows a link. Any other probe error retains too.
    """
    path = root / HOLD_MARKER
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return "hold probe failed before preservation"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return "hold marker requires retention before archive or removal"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return "hold probe failed before preservation"
    try:
        try:
            os.read(fd, 1)
        except OSError:
            return "hold probe failed before preservation"
    finally:
        os.close(fd)
    return "hold marker requires retention before archive or removal"


def plan_bundle(conn, task_id: str, workspace: Path, *, max_bytes=MAX_BYTES):
    root = Path(os.path.abspath(workspace))
    row = conn.execute("SELECT status, result FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row or row["status"] not in {"done", "archived"}:
        raise ValueError("workspace is not terminal")
    if root.is_symlink() or root.resolve() != root or not root.is_dir():
        raise ValueError("workspace root is missing or linked")
    hold_reason = _hold_probe(root)
    if hold_reason:
        raise ValueError(hold_reason)
    selected = set()
    directories = set()
    skipped = set()
    unresolved_refs = set()
    keep_all = _keep_selects_all(root)
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
            p = Path(candidate)
            if not p.is_absolute():
                p = root / p
            p = Path(os.path.abspath(p))
            if p == root:
                # Workspace custody is context; only KEEP-BUNDLE selects all.
                skipped.add(".")
                continue
            if not p.is_relative_to(root):
                continue
            if p.resolve() != p:
                raise ValueError("referenced member is linked")
            if not p.exists():
                # Source locations are aliases only when the literal is absent.
                # Never shorten an existing path, or expand a directory prefix.
                name = SOURCE_SUFFIX.sub("", p.name)
                if name and name != p.name:
                    target = p.with_name(name)
                    if target.resolve() != target:
                        raise ValueError("referenced member is linked")
                    if not target.is_dir():
                        p = target
            rel = p.relative_to(root)
            if not required and not p.exists():
                unresolved_refs.add(rel.as_posix())
                continue
            if _unsafe(rel.parts):
                raise ValueError("referenced member has a prohibited name")
            if p.is_dir():
                directories.add(p)
                continue
            if not p.is_file():
                raise ValueError("referenced bundle member is missing")
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


def _retain(task_id, conn, reason: str) -> bool:
    """Record a stable reason and refuse removal. Never echo file contents."""
    message = (
        f"[kanban] Retained workspace {task_id}: {reason}. "
        "Inspect bundle markers and retry cleanup after repair."
    )
    print(message, flush=True)
    # A comment-write failure must not become permission to delete.
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc
        if conn is not None:
            kb.add_comment(conn, task_id, author="workspace_preservation", body=message)
        else:
            with kbc.connect_closing() as connection:
                kb.add_comment(connection, task_id, author="workspace_preservation", body=message)
    except Exception:
        pass
    return False


def preservation_gate(task_id, workspace, *, conn=None):
    """True only when this workspace may be removed. Callers own deletion.

    Hold presence and hold-probe errors return false before preservation.
    An empty plan returns false. A worker-written marker file is not
    authority. ``dry_run`` is not this function; ``preserve_bundle`` with
    ``dry_run=True`` stays read-only.
    """
    from hermes_cli import kanban_db_connect as kbc

    root = Path(os.path.abspath(workspace))
    reason = _root_retention_reason(root) or _hold_probe(root)
    if reason:
        return _retain(task_id, conn, reason)
    def _authorized(connection, report) -> bool:
        if report.get("files"):
            return True
        return _retain(
            task_id, connection, "empty preservation plan is not removal authorization",
        )

    try:
        if conn is not None:
            return _authorized(conn, preserve_bundle(conn, task_id, workspace))
        with kbc.connect_closing() as connection:
            return _authorized(connection, preserve_bundle(connection, task_id, workspace))
    except Exception as exc:
        return _retain(
            task_id, conn, f"bundle preservation failed ({type(exc).__name__})",
        )
