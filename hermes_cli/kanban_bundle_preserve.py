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
- Structured ``findings[].file`` uses only that run's top-level
  ``metadata.worktree`` when it is an absolute directory inside the
  workspace. With no such root the finding is ambiguous: not an archive
  member, not an unresolved leftover, and not removal authority.
- A generic file-like relative hint is resolved in the workspace first.
  Every component is lstat'ed, symlinks are rejected, and realpath must stay
  inside the workspace. A regular file found there is kept. A miss with no
  recorded checkout is unresolved. A miss falls through to that checkout.
  When the workspace and the checkout both have the hint and the archive
  paths differ, the hint is ambiguous and neither copy is archived.
  A generic ``..`` that leaves the workspace is ambiguous: nothing outside
  is read or archived, and the gate stays closed. A ``..`` that normalizes
  back inside is not archived; a missing target stays unresolved.
  Structured ``findings[].file`` values that contain ``..`` still raise.
  Explicit ``artifacts``, absolute workspace paths, and ``KEEP-BUNDLE`` stay
  workspace selections. A real extensionless owner/repo file in the
  workspace or the recorded checkout is archived. A missing slug stays
  unresolved and is also reported as non-path. Whole URLs are sanitized
  and reported as non-path; they are not local paths and are not fetched.
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
from urllib.parse import urlsplit, urlunsplit

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


_URI = re.compile(r"\b[\w+.-]+://[^\s`<>\"']+")


def _sanitized_uri(url: str) -> str:
    """Drop userinfo, query, and fragment. Scheme, host, and path remain."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _references(text):
    # Markdown links name the destination, not a possibly filename-like label.
    text = re.sub(r"\[[^\]\n]*\]\(([^()\n]+)\)", r"<\1>", text)
    # Remove whole URL spans before filename extraction, including prose URLs.
    text = _URI.sub("", text)
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


class _Selection:
    def __init__(self):
        self.selected = set()
        self.directories = set()
        self.skipped = set()
        self.unresolved = set()
        self.ambiguous = set()
        self.non_path = set()


def _file_like(candidate: str) -> bool:
    """File hints carry a suffix. Owner/repo and branch tokens do not.

    A real extensionless file is still archived from the workspace or the
    recorded checkout. Only a miss is reported as a non-path slug.
    """
    name = SOURCE_SUFFIX.sub("", Path(candidate).name)
    return "." in name and name not in {".", ".."}


def _finding_generic_strings(item):
    if not isinstance(item, dict):
        yield from _strings(item)
        return
    for key, value in item.items():
        if key == "file" and isinstance(value, str):
            continue
        yield from _strings(value)


def _explicit_file_identity(file_val: str) -> str | None:
    text = file_val.strip()
    if not text:
        return None
    link = re.fullmatch(r"\[[^\]\n]*\]\(([^()\n]+)\)", text)
    target = link.group(1).strip() if link else text
    if re.fullmatch(r"[\w+.-]+://[^\s`<>\"']+", target):
        return None
    return file_val


def _partition_metadata(metadata):
    """Split one run's metadata into checkout, sources, artifacts, and prose."""
    if not isinstance(metadata, dict):
        return None, [], [], list(_strings(metadata))
    raw_root = metadata.get("worktree")
    checkout = raw_root if isinstance(raw_root, str) and Path(raw_root).is_absolute() else None
    sources = []
    generic_extra = []
    findings = metadata.get("findings")
    if isinstance(findings, list):
        for item in findings:
            if isinstance(item, dict):
                file_val = item.get("file")
                if isinstance(file_val, str):
                    identity = _explicit_file_identity(file_val)
                    if identity is not None:
                        sources.append((identity, True))
                generic_extra.extend(_finding_generic_strings(item))
            else:
                generic_extra.extend(_strings(item))
    elif findings is not None:
        generic_extra.extend(_strings(findings))
    artifact_texts = list(_strings(metadata["artifacts"])) if "artifacts" in metadata else []
    skip = {"worktree", "findings"}
    if "artifacts" in metadata:
        skip.add("artifacts")
    generic = list(_strings({key: value for key, value in metadata.items() if key not in skip}))
    generic.extend(generic_extra)
    return checkout, sources, artifact_texts, generic


def _points_at_workspace(workspace: Path, candidate: str) -> bool:
    path = Path(candidate)
    if path.is_absolute():
        return Path(os.path.abspath(candidate)) == workspace
    return Path(os.path.abspath(workspace / candidate)) == workspace


def _select_workspace_ref(workspace, candidate, required, plan: _Selection):
    p = Path(candidate)
    if not p.is_absolute():
        p = workspace / p
    p = Path(os.path.abspath(p))
    if p == workspace:
        plan.skipped.add(".")
        return
    if not p.is_relative_to(workspace):
        return
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
    rel = p.relative_to(workspace)
    if not required and not p.exists():
        plan.unresolved.add(rel.as_posix())
        return
    if _unsafe(rel.parts):
        raise ValueError("referenced member has a prohibited name")
    if p.is_dir():
        plan.directories.add(p)
        return
    if not p.is_file():
        raise ValueError("referenced bundle member is missing")
    plan.selected.add(rel.as_posix())


def _archival_checkout(workspace: Path, raw: str, candidate: str) -> Path:
    """Absolute directory inside the workspace. Links are not followed."""
    if not Path(raw).is_absolute():
        raise ValueError(f"recorded checkout is not absolute: {candidate}")
    lexical = Path(os.path.abspath(raw))
    if lexical == workspace:
        return workspace
    if not lexical.is_relative_to(workspace):
        raise ValueError(f"recorded checkout is outside the workspace: {candidate}")
    current = workspace
    for part in lexical.relative_to(workspace).parts:
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise ValueError(f"recorded checkout is not a directory: {candidate}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"recorded checkout is linked: {candidate}")
    return lexical


def _source_inside(checkout: Path, candidate: str) -> Path:
    path = Path(candidate)
    if path.is_absolute():
        lexical = Path(os.path.abspath(candidate))
        if lexical == checkout or not lexical.is_relative_to(checkout):
            raise ValueError(f"referenced source is outside recorded checkout: {candidate}")
        return lexical
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"referenced source escapes recorded checkout: {candidate}")
    lexical = Path(os.path.abspath(checkout.joinpath(*path.parts)))
    if lexical == checkout or not lexical.is_relative_to(checkout):
        raise ValueError(f"referenced source escapes recorded checkout: {candidate}")
    return lexical


def _stat_under(root: Path, lexical: Path):
    """lstat each component. Missing is None. A link raises; it is not opened."""
    current = root
    info = None
    parts = lexical.relative_to(root).parts
    for index, part in enumerate(parts):
        try:
            info = (current / part).lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("referenced member is linked")
        current = current / part
        if index != len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            return None
    return info


def _locate_recorded_source(checkout: Path, candidate: str) -> Path:
    lexical = _source_inside(checkout, candidate)
    info = _stat_under(checkout, lexical)
    if info is None:
        stripped = SOURCE_SUFFIX.sub("", lexical.name)
        alias = lexical.with_name(stripped) if stripped and stripped != lexical.name else None
        if alias is not None and alias != checkout and alias.is_relative_to(checkout):
            info = _stat_under(checkout, alias)
            if info is not None and stat.S_ISREG(info.st_mode):
                return alias
        raise ValueError(f"referenced bundle member is missing: {candidate}")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"referenced bundle member is missing: {candidate}")
    return lexical


def _select_recorded_source(workspace: Path, candidate: str, checkout_raw, plan: _Selection):
    if not checkout_raw:
        plan.ambiguous.add(candidate)
        return
    checkout = _archival_checkout(workspace, checkout_raw, candidate)
    located = _locate_recorded_source(checkout, candidate)
    relative = located.relative_to(workspace)
    if _unsafe(relative.parts) or _unsafe(located.relative_to(checkout).parts):
        raise ValueError("referenced member has a prohibited name")
    plan.selected.add(relative.as_posix())


# More than one in-workspace checkout. Not a path, and not "no root".
_CONFLICTING_CHECKOUTS = object()


def _lexical_in_workspace(workspace: Path, candidate: str) -> Path | None:
    lexical = Path(os.path.abspath(workspace / candidate))
    if lexical == workspace or not lexical.is_relative_to(workspace):
        return None
    return lexical


def _classified_member(root: Path, lexical: Path):
    """lstat each component. Missing is None. Links and non-regular files raise.

    Realpath runs only after a directory or regular file has been lstat'ed,
    so a FIFO or device is never opened or resolved.
    """
    info = _stat_under(root, lexical)
    if info is None:
        return None
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise ValueError(f"referenced bundle member is missing: {lexical.name}")
    real = Path(os.path.realpath(lexical))
    if not real.is_relative_to(Path(os.path.realpath(root))):
        raise ValueError("referenced member is linked")
    return info


def _without_source_suffix(lexical: Path) -> Path | None:
    name = SOURCE_SUFFIX.sub("", lexical.name)
    if not name or name == lexical.name:
        return None
    return lexical.with_name(name)


def _reject_parent_traversal(workspace: Path, candidate: str, plan: _Selection) -> bool:
    """Handle generic ``..``. Outside paths are ambiguous and are not read.

    A target that normalizes back inside the workspace is not archived. A
    missing inside target stays unresolved under its normalized name.
    """
    if ".." not in Path(candidate).parts:
        return False
    lexical = _lexical_in_workspace(workspace, candidate)
    if lexical is None:
        plan.ambiguous.add(candidate)
        return True
    if _classified_member(workspace, lexical) is None:
        plan.unresolved.add(lexical.relative_to(workspace).as_posix())
        return True
    raise ValueError(f"referenced source escapes workspace: {candidate}")


def _workspace_generic_file(workspace: Path, candidate: str) -> tuple[Path, os.stat_result | None] | None:
    """Return the regular workspace file for a relative hint, or None if absent."""
    lexical = _lexical_in_workspace(workspace, candidate)
    if lexical is None:
        return None
    info = _classified_member(workspace, lexical)
    alias = _without_source_suffix(lexical)
    if info is None and alias is not None and alias.is_relative_to(workspace):
        alias_info = _classified_member(workspace, alias)
        if alias_info is not None and not stat.S_ISDIR(alias_info.st_mode):
            return alias, alias_info
        if alias_info is None:
            return alias, None
    if info is None:
        return lexical, None
    return lexical, info


def _recorded_generic_path(workspace: Path, checkout_raw, candidate: str) -> Path | None:
    """Checkout member, or None when that checkout does not have the hint."""
    checkout = _archival_checkout(workspace, checkout_raw, candidate)
    lexical = _source_inside(checkout, candidate)
    info = _stat_under(checkout, lexical)
    if info is None:
        alias = _without_source_suffix(lexical)
        if alias is None or not alias.is_relative_to(checkout):
            return None
        info = _stat_under(checkout, alias)
        if info is None or not stat.S_ISREG(info.st_mode):
            return None
        lexical = alias
    elif not stat.S_ISREG(info.st_mode):
        raise ValueError(f"referenced bundle member is missing: {candidate}")
    real = Path(os.path.realpath(lexical))
    if not real.is_relative_to(Path(os.path.realpath(workspace))):
        raise ValueError("referenced member is linked")
    relative = lexical.relative_to(workspace)
    if _unsafe(relative.parts) or _unsafe(lexical.relative_to(checkout).parts):
        raise ValueError("referenced member has a prohibited name")
    return lexical


def _select_generic_relative(workspace, candidate, required, checkout_raw, plan: _Selection):
    """Workspace file first. Recorded checkout only on a miss. Disagreeing copies are ambiguous."""
    if checkout_raw is _CONFLICTING_CHECKOUTS:
        plan.ambiguous.add(candidate)
        return
    if _reject_parent_traversal(workspace, candidate, plan):
        return
    located = _workspace_generic_file(workspace, candidate)
    if located is None:
        return
    chosen, info = located
    if info is None:
        if checkout_raw:
            _select_recorded_source(workspace, candidate, checkout_raw, plan)
            return
        if required:
            raise ValueError(f"referenced bundle member is missing: {candidate}")
        plan.unresolved.add(chosen.relative_to(workspace).as_posix())
        return
    relative = chosen.relative_to(workspace)
    if _unsafe(relative.parts):
        raise ValueError("referenced member has a prohibited name")
    if stat.S_ISDIR(info.st_mode):
        plan.directories.add(chosen)
        return
    if checkout_raw:
        recorded = _recorded_generic_path(workspace, checkout_raw, candidate)
        if recorded is not None and recorded.relative_to(workspace).as_posix() != relative.as_posix():
            plan.ambiguous.add(candidate)
            return
    plan.selected.add(relative.as_posix())


def _select_extensionless(workspace, candidate, required, checkout_raw, plan: _Selection):
    """Real extensionless file first. A miss stays unresolved and non-path.

    Symlinks and ``..`` are not reclassified as a harmless owner/repo slug.
    Two recorded checkouts are not a root to infer.
    """
    if checkout_raw is _CONFLICTING_CHECKOUTS:
        plan.ambiguous.add(candidate)
        return
    if _reject_parent_traversal(workspace, candidate, plan):
        return
    located = _workspace_generic_file(workspace, candidate)
    if located is None:
        return
    chosen, info = located
    if info is None and checkout_raw:
        recorded = _recorded_generic_path(workspace, checkout_raw, candidate)
        if recorded is not None:
            plan.selected.add(recorded.relative_to(workspace).as_posix())
            return
    if info is None:
        if required:
            raise ValueError(f"referenced bundle member is missing: {candidate}")
        plan.unresolved.add(chosen.relative_to(workspace).as_posix())
        plan.non_path.add(candidate)
        return
    relative = chosen.relative_to(workspace)
    if _unsafe(relative.parts):
        raise ValueError("referenced member has a prohibited name")
    if stat.S_ISDIR(info.st_mode):
        plan.directories.add(chosen)
        return
    if checkout_raw:
        recorded = _recorded_generic_path(workspace, checkout_raw, candidate)
        if recorded is not None and recorded.relative_to(workspace).as_posix() != relative.as_posix():
            plan.ambiguous.add(candidate)
            return
    plan.selected.add(relative.as_posix())


def _route_candidate(workspace, candidate, required, checkout_raw, artifact_namespace, plan: _Selection):
    if _points_at_workspace(workspace, candidate):
        plan.skipped.add(".")
        return
    absolute = Path(candidate).is_absolute()
    if artifact_namespace or absolute:
        _select_workspace_ref(workspace, candidate, required, plan)
        return
    if not _file_like(candidate):
        _select_extensionless(workspace, candidate, required, checkout_raw, plan)
        return
    _select_generic_relative(workspace, candidate, required, checkout_raw, plan)


def _consume_text(workspace, text, checkout_raw, artifact_namespace, plan: _Selection):
    if not text:
        return
    for match in _URI.finditer(text):
        plan.non_path.add(_sanitized_uri(match.group(0)))
    # An artifacts entry that is itself one absolute path keeps its spaces.
    # Prose, URLs, and quoted names still go through the reference splitter.
    raw = text.strip()
    if artifact_namespace and raw and Path(raw).is_absolute() and "://" not in raw:
        _route_candidate(workspace, raw, True, checkout_raw, True, plan)
        return
    for candidate, required in _references(text):
        _route_candidate(workspace, candidate, required, checkout_raw, artifact_namespace, plan)


def _single_checkout(workspace: Path, raws):
    """One in-workspace checkout, None when there is nothing to use, or a conflict.

    Two or more in-workspace checkouts are not a license to pick the workspace
    namesake. Zero checkouts stay ``None`` so a generic hint uses the workspace.
    """
    distinct = []
    for raw in raws:
        if raw and raw not in distinct:
            distinct.append(raw)
    if len(distinct) > 1:
        inside = [
            raw for raw in distinct
            if Path(os.path.abspath(raw)).is_relative_to(workspace)
        ]
        if len(inside) > 1:
            return _CONFLICTING_CHECKOUTS
        return None
    if len(distinct) != 1:
        return None
    lexical = Path(os.path.abspath(distinct[0]))
    if not lexical.is_relative_to(workspace):
        return None
    return distinct[0]


def _collect_plan_refs(conn, task_id, workspace: Path, result, plan: _Selection):
    checkouts = []
    query = "SELECT metadata, summary FROM task_runs WHERE task_id=?"
    for run in conn.execute(query, (task_id,)):
        parsed = json.loads(run["metadata"]) if run["metadata"] else {}
        checkout, sources, artifacts, generic = _partition_metadata(parsed)
        checkouts.append(checkout)
        for candidate, _required in sources:
            # Structured file refs stay on the checkout, including absolute ones.
            # Generic routing would treat an absolute path as a workspace artifact
            # and silently skip one that leaves the workspace.
            _select_recorded_source(workspace, candidate, checkout, plan)
        for text in artifacts:
            _consume_text(workspace, text, checkout, True, plan)
        for text in generic:
            _consume_text(workspace, text, checkout, False, plan)
        _consume_text(workspace, run["summary"] or "", checkout, False, plan)
    result_root = _single_checkout(workspace, checkouts)
    _consume_text(workspace, result or "", result_root, False, plan)


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
    plan = _Selection()
    keep_all = _keep_selects_all(root)
    _collect_plan_refs(conn, task_id, root, row["result"], plan)
    selected = plan.selected
    directories = plan.directories
    skipped = plan.skipped
    unresolved_refs = plan.unresolved
    if keep_all:
        directories = {root}

    def _raise_walk_error(error):
        raise ValueError("bundle directory walk failed") from error

    for directory in sorted(directories):
        for folder, dirs, files in os.walk(directory, followlinks=False, onerror=_raise_walk_error):
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
            "unresolved_refs": sorted(unresolved_refs),
            "ambiguous_refs": sorted(plan.ambiguous),
            "unresolved_non_path_refs": sorted(plan.non_path)}


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


def _record_non_path_refs(conn, task_id, refs):
    """Durable sanitized URI and slug list. Empty input writes nothing."""
    if not refs:
        return
    from hermes_cli import kanban_db as kb

    payload = {"unresolved_non_path_refs": sorted(set(refs))}
    if conn.in_transaction:
        raise ValueError("non-path bundle references require their own commit")
    query = (
        "SELECT 1 FROM task_events WHERE task_id=? "
        "AND kind='bundle_non_path_refs' AND payload=?"
    )
    params = (task_id, kb._json_or_null(payload))
    with kb.write_txn(conn):
        if not conn.execute(query, params).fetchone():
            kb._append_event(conn, task_id, "bundle_non_path_refs", payload)
    if not conn.execute(query, params).fetchone():
        raise ValueError("non-path bundle references were not recorded")


def preserve_bundle(conn, task_id, workspace, *, dry_run=False, max_bytes=MAX_BYTES):
    from hermes_cli import kanban_db as kb
    report = plan_bundle(conn, task_id, Path(workspace), max_bytes=max_bytes)
    if dry_run:
        return report
    _record_unresolved_refs(conn, task_id, report["unresolved_refs"])
    _record_non_path_refs(conn, task_id, report["unresolved_non_path_refs"])
    # Ambiguity is not removal authority and does not archive the other files.
    if report["ambiguous_refs"] or not report["files"]:
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
    An empty plan returns false. Ambiguous source refs return false and are
    not archived. A worker-written
    marker file is not authority. ``dry_run`` is not this function;
    ``preserve_bundle`` with ``dry_run=True`` stays read-only.
    """
    from hermes_cli import kanban_db_connect as kbc

    root = Path(os.path.abspath(workspace))
    reason = _root_retention_reason(root) or _hold_probe(root)
    if reason:
        return _retain(task_id, conn, reason)
    def _authorized(connection, report) -> bool:
        if report.get("ambiguous_refs"):
            return _retain(
                task_id, connection,
                "ambiguous bundle references are not removal authorization",
            )
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
