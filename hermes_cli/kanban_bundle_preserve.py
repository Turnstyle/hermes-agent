"""Fail-closed preservation gate for terminal Kanban workspace removal.

No extraction occurs here. Archives contain only relative regular-file members.
Existing workspaces may be inventoried with dry_run=True without writing blobs/DB.

Removal is unconditionally disabled. ``preservation_gate`` always returns
False. Explicit ``preserve_bundle`` planning/registration never grants authority.

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
  ``KEEP_MARKER_MAX_BYTES`` bytes, opened without following a link and read within the bound. Contents
  are not a document format. A link, directory, oversized file, missing
  owner-read bit, or any probe error retains before archive registration.
  It is not a hold and it does not authorize an empty plan.
- An empty plan retains. A symlink workspace root retains. A missing required
  reference, or a failed copy, checksum, size, symlink, secret, or database
  step retains. A preexisting unrelated attachment is not proof. Dry-run
  never writes or deletes. All scoped cleanup callers retain independently of preservation success.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

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
    from hermes_cli.retention_markers import _open_directory_chain
    parts = Path(rel).parts
    if not parts or Path(rel).is_absolute() or ".." in parts:
        raise ValueError("unsafe bundle member locator")
    descriptors = _open_directory_chain(Path(os.path.abspath(root)))
    try:
        for part in parts[:-1]:
            descriptors.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=descriptors[-1]))
        fd = descriptors[-1]
        before = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("not a regular bundle member")
        source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    if not stat.S_ISREG(os.fstat(source).st_mode):
        os.close(source)
        raise ValueError("not a regular bundle member")
    return os.fdopen(source, "rb")


def _keep_selects_all(root: Path) -> bool:
    """Preserve the marker selection policy, with typed refusal before planning."""
    from hermes_cli.retention_markers import MarkerStatus, RetainAll, inspect_keep_bundle
    result = inspect_keep_bundle(root, max_bytes=KEEP_MARKER_MAX_BYTES)
    if result.status is MarkerStatus.ABSENT:
        return False
    if result.status is MarkerStatus.PRESENT:
        return True
    raise RetainAll("keep marker probe failed before preservation: " + result.reason)


def _root_retention_reason(root: Path) -> str | None:
    try:
        if root.is_symlink() or not root.is_dir():
            return "workspace root is missing or linked"
    except OSError:
        return "workspace root is missing or linked"
    return None


def _hold_probe(root: Path) -> str | None:
    """Any hold name or uncertain probe retains; no marker payload is read."""
    try:
        (root / HOLD_MARKER).lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return "hold probe failed before preservation"
    return "hold marker requires retention before archive or removal"


def plan_bundle(conn, task_id: str, workspace: Path, *, max_bytes=MAX_BYTES):
    from hermes_cli.retention_markers import RetainAll
    root = Path(os.path.abspath(workspace))
    row = conn.execute("SELECT status, result FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row or row["status"] not in {"done", "archived"}:
        raise ValueError("workspace is not terminal")
    hold_reason = _hold_probe(root)
    if hold_reason:
        raise RetainAll(hold_reason)
    # The inspector pins the root and all ancestors before classifying the
    # leaf. A missing/replaced ancestor cannot masquerade as marker absence.
    keep_all = _keep_selects_all(root)
    selected = set()
    directories = set()
    skipped = set()
    unresolved_refs = set()
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
    from hermes_cli.retention_bundle import publish_report
    if conn.in_transaction:
        raise ValueError("bundle preservation requires its own commit")
    report = plan_bundle(conn, task_id, Path(workspace), max_bytes=max_bytes)
    if dry_run:
        return report
    _record_unresolved_refs(conn, task_id, report["unresolved_refs"])
    if not report["files"]:
        return report
    return publish_report(conn, task_id, workspace, report, max_bytes=max_bytes)


def _retain(task_id, conn, reason: str) -> bool:
    """Record a stable refusal using only a supplied owning-board connection."""
    message = (
        f"[kanban] Retained workspace {task_id}: {reason}. "
        "Destructive authority is disabled; preservation does not authorize removal."
    )
    print(message, flush=True)
    if conn is None or conn.in_transaction:
        return False
    try:
        from hermes_cli import kanban_db as kb
        existing = conn.execute(
            "SELECT 1 FROM task_comments WHERE task_id=? AND author=? AND body=?",
            (task_id, "workspace_preservation", message),
        ).fetchone()
        if not existing:
            kb.add_comment(conn, task_id, author="workspace_preservation", body=message)
    except Exception as exc:
        # Audit failure must remain refusal. Do not open an ambient board to
        # manufacture provenance for an unknown owner.
        import logging
        logging.getLogger(__name__).warning("Retention audit unavailable: %s", type(exc).__name__)
    return False


def preservation_gate(task_id, workspace, *, conn=None):
    """Compatibility removal gate, always False while destructive authority is OFF.

    Use preserve_bundle explicitly to plan/register artifacts. Its success cannot
    authorize scratch/worktree cleanup, including aliases that captured this gate.
    """
    from hermes_cli.retention import removal_decision
    return _retain(task_id, conn, removal_decision().reason)
