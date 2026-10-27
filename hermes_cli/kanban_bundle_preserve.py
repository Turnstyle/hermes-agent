"""Clean-room Kanban Bundle Preservation and Fail-Closed Cleanup Module.

Provides full-inventory capture-or-retain workspace preservation, nonlocal
metadata classification (GCP service account identifiers, secret resource
names, URLs, repo slugs, model tokens, prose ratios), and fail-closed
lifecycle gating.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import stat
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

logger = logging.getLogger(__name__)

# Constants as specified
KEEP_MARKER = "KEEP-BUNDLE"
HOLD_MARKER = "HOLD-WORKSPACE"
KEEP_MARKER_MAX_BYTES = 4096
MAX_BUNDLE_BYTES = 500 * 1024 * 1024  # 500 MB cap

PRESERVE_UPLOADER = "kanban_bundle_preserve"

EXCLUDED_DIR_NAMES = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__"})

# Non-path structural patterns
_URL_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_GCP_SA_PATTERN = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.iam\.gserviceaccount\.com$")
_GCP_RESOURCE_PATTERN = re.compile(r"^projects/\d+/secrets/[^/\s]+/versions/\d+$")
_MODEL_PROVIDER_PATTERN = re.compile(
    r"^(?:(?:openai|anthropic|google|meta|deepseek|mistral|cohere|qwen)/[a-zA-Z0-9._-]+|"
    r"[a-zA-Z0-9._-]+/(?:openai|anthropic|google|claude|gpt)[a-zA-Z0-9._-]*|"
    r"(?:gpt|claude|gemini|o1|o3|llama|qwen|deepseek)-[a-zA-Z0-9._/-]+)$"
)
_PROSE_RATIO_PATTERN = re.compile(r"^\d+/\d+$")
_GITHUB_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# Source location suffixes like :40, :40-42, :40:5, #L5, #section
_SOURCE_SUFFIX_PATTERN = re.compile(r"(?::\d+(?:-\d+|:\d+)?|#L\d+|#[\w-]+)$")

# Common file extensions for bare token classification in prose
_FILE_EXT_PATTERN = re.compile(
    r"\.(?:py|md|txt|json|yaml|yml|toml|ini|cfg|conf|lock|patch|diff|sh|bash|zsh|"
    r"js|jsx|ts|tsx|mjs|cjs|css|scss|html|htm|xml|svg|png|jpg|jpeg|gif|webp|ico|"
    r"pdf|csv|tsv|db|sqlite|sqlite3|sql|log|out|err|tar|gz|tgz|zip|pem|key|crt|"
    r"pub|env|pipe|sock|c|cc|cpp|h|hpp|rs|go|java|kt|swift|rb|php|pl|r|lua)$",
    re.IGNORECASE,
)

_KNOWN_EXTENSIONLESS_FILES = frozenset({
    "README",
    "LICENSE",
    "Makefile",
    "Dockerfile",
    KEEP_MARKER,
    HOLD_MARKER,
    "DISPOSABLE-WORKSPACE",
})

_SKILL_PROVENANCE_WORDS = frozenset({
    "skill",
    "skills",
    "updated",
    "created",
    "patched",
    "modified",
    "installed",
    "applied",
    "used",
    "own",
    "external",
    "provenance",
    "ref",
    "refs",
    "reference",
    "references",
    "source",
    "sources",
    "path",
    "paths",
    "doc",
    "docs",
    "name",
    "names",
    "id",
    "ids",
    "list",
})

_NON_PATH_SCALAR_METADATA_KEYS = frozenset({
    "family",
    "reviewer_family",
    "writer_family",
    "model_family",
    "provider",
    "model",
    "main_model",
    "finishing_model",
    "reviewer_model",
    "requested_model",
    "served_model",
    "vendor",
    "subscription",
    "tier",
    "effort",
    "reasoning_effort",
    "writer_tool",
    "tool",
    "client",
    "transport",
    "route",
    "reviewer_route",
    "backend",
    "platform",
    "verdict",
    "outcome",
    "status",
    "source_status",
    "approval",
    "stage",
    "phase",
    "role",
    "lane",
    "owner",
    "flow_owner",
    "assignee",
    "author",
    "authority",
    "kind",
    "type",
    "mode",
    "category",
    "severity",
    "priority",
    "decision",
    "action",
})

_NON_PATH_SCALAR_KEY_SUFFIXES = (
    "_family",
    "_provider",
    "_model",
    "_vendor",
    "_tier",
    "_effort",
    "_verdict",
    "_status",
    "_route",
    "_transport",
    "_role",
    "_lane",
    "_tool",
)

_EXPLICIT_LOCAL_METADATA_KEYS = frozenset({
    "artifact",
    "artifacts",
    "bundle",
    "bundles",
    "path",
    "paths",
    "local_path",
    "local_paths",
    "workspace_path",
    "workspace_paths",
    "file_path",
    "file_paths",
    "target_path",
    "target_paths",
    "output_path",
    "output_paths",
    "report_path",
    "report_paths",
    "file",
    "files",
    "local_file",
    "local_files",
    "workspace_file",
    "workspace_files",
    "required_file",
    "required_files",
    "output_file",
    "output_files",
    "report_file",
    "report_files",
    "files_changed",
    "changed_files",
    "local_ref",
    "local_refs",
    "local_reference",
    "local_references",
    "workspace_ref",
    "workspace_refs",
    "finding",
    "findings",
    "deliverable",
    "deliverables",
    "member",
    "members",
})

_GENERAL_REFERENCE_METADATA_KEYS = frozenset({
    "required",
    "requirements",
    "reference",
    "references",
    "ref",
    "refs",
})


def is_explicit_local_metadata_key(key: str) -> bool:
    """Return True if a structured metadata key explicitly requires a local workspace path/member."""
    if not key or not isinstance(key, str):
        return False
    return key.strip().lower() in _EXPLICIT_LOCAL_METADATA_KEYS


def is_external_skill_provenance_key(key: str) -> bool:
    """Return True if a structured metadata key denotes external skill provenance."""
    if not key or not isinstance(key, str):
        return False
    if is_explicit_local_metadata_key(key):
        return False
    words = [w for w in re.split(r"[_-]+", key.strip().lower()) if w]
    if len(words) < 2:
        return False
    if "skill" not in words and "skills" not in words:
        return False
    return all(w in _SKILL_PROVENANCE_WORDS for w in words)


def is_non_path_scalar_metadata_key(key: str) -> bool:
    """Return True if a structured metadata key denotes a non-path scalar descriptor
    (e.g. review.family, provider, model, vendor, tier, effort, verdict, status, role).
    """
    if not key or not isinstance(key, str):
        return False
    k = key.strip().lower()
    if not k or is_explicit_local_metadata_key(k) or k in _GENERAL_REFERENCE_METADATA_KEYS or k in {"worktree"}:
        return False
    if is_external_skill_provenance_key(k):
        return False
    if k in _NON_PATH_SCALAR_METADATA_KEYS:
        return True
    return k.endswith(_NON_PATH_SCALAR_KEY_SUFFIXES)


def is_non_path_scalar_value(val: str) -> bool:
    """Return True if a string under a non-path scalar metadata key is a valid
    scalar descriptor token (e.g. 'Gemini/Google', 'Google/gemini-4-argon-high',
    'PASS/READY') rather than a local/external filesystem path or prose with file refs.
    """
    if not val or not isinstance(val, str):
        return False
    s = val.strip()
    if not s or any(ch in s for ch in " \t\r\n`\"'<>[]"):
        return False
    if _URL_PATTERN.match(s):
        return False
    if os.path.isabs(s) or s.startswith(("./", "../", "~", "\\")):
        return False
    if re.match(r"^(?:artifact|path|file|required|placement)[:=]", s, re.IGNORECASE):
        return False
    norm = s.replace("\\", "/")
    parts = [p for p in norm.split("/") if p]
    if not parts or len(parts) > 3:
        return False
    if any(p in (".", "..") or p.startswith(".") for p in parts):
        return False
    if is_prohibited_name(norm) or has_excluded_component(norm):
        return False
    for p in parts:
        base_p = _SOURCE_SUFFIX_PATTERN.sub("", p)
        if _FILE_EXT_PATTERN.search(base_p) or base_p in _KNOWN_EXTENSIONLESS_FILES:
            return False
    return True


def is_non_path_metadata(token: str) -> bool:
    """Classify structural non-path identifiers as nonlocal metadata."""
    t = token.strip()
    if not t:
        return False
    if _URL_PATTERN.match(t):
        return True
    if _GCP_SA_PATTERN.match(t):
        return True
    if _GCP_RESOURCE_PATTERN.match(t):
        return True
    if _MODEL_PROVIDER_PATTERN.match(t):
        return True
    if _PROSE_RATIO_PATTERN.match(t):
        return True
    return False


def sanitize_uri(url: str) -> str:
    """Drop userinfo, query, and fragment from a URI for safe reporting.

    Never echoes raw unredacted credentials/queries even when a port is malformed.
    """
    u = url.strip()
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)([^/?#]*)(.*)$", u)
    if m:
        scheme_prefix, authority, rest = m.group(1), m.group(2), m.group(3)
        if "@" in authority:
            hostport = authority.rsplit("@", 1)[1]
        else:
            hostport = authority
        path_only = re.split(r"[?#]", rest, maxsplit=1)[0]
        return f"{scheme_prefix}{hostport}{path_only}"
    # Fallback redaction if not standard scheme://
    no_qf = re.split(r"[?#]", u, maxsplit=1)[0]
    return re.sub(r"://[^/@?#]+@", "://", no_qf)


def has_excluded_component(name: str) -> bool:
    """Return True if any path component is in EXCLUDED_DIR_NAMES."""
    for part in re.split(r"[/\\]+", name.strip()):
        if part in EXCLUDED_DIR_NAMES:
            return True
    return False


def is_prohibited_name(name: str) -> bool:
    """Case-insensitive check for prohibited secret/credential path components."""
    n = name.strip().lower()
    if not n:
        return False
    for base in re.split(r"[/\\]+", n):
        if not base or base in (".", ".."):
            continue
        if base.startswith(".env"):
            return True
        for kw in (
            "secret",
            "credential",
            "token",
            "password",
            "service_account",
            "service-account",
            "serviceaccount",
            "id_rsa",
            "id_ed25519",
        ):
            if kw in base:
                return True
        if re.search(r"key.*\.json$", base):
            return True
        if base.endswith(".pem") or base.endswith(".key"):
            return True
        if re.search(r"(?:^|[._-])auth(?:$|[._-])", base):
            return True
    return False


def _safe_read_regular_file(
    path: Path,
    max_read_bytes: Optional[int] = None,
) -> Tuple[bytes, os.stat_result]:
    """Read a regular file without following symlinks, verifying inode/mtime stability."""
    try:
        st_before = os.lstat(path)
    except Exception as exc:
        raise ValueError(f"failed to lstat {path}: {exc}") from exc

    if stat.S_ISLNK(st_before.st_mode):
        raise ValueError(f"unsafe bundle file: symlink {path}")
    if not stat.S_ISREG(st_before.st_mode):
        raise ValueError(f"unsafe non-regular entry: {path}")
    if max_read_bytes is not None and st_before.st_size > max_read_bytes:
        raise ValueError(f"bundle size exceeds maximum allowed cap ({max_read_bytes} bytes)")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK

    try:
        fd = os.open(path, flags)
    except Exception as exc:
        raise ValueError(f"failed to open regular file {path}: {exc}") from exc

    try:
        st_fd = os.fstat(fd)
        if stat.S_ISLNK(st_fd.st_mode) or not stat.S_ISREG(st_fd.st_mode):
            raise ValueError(f"unsafe non-regular descriptor: {path}")
        if (st_fd.st_dev, st_fd.st_ino) != (st_before.st_dev, st_before.st_ino):
            raise ValueError(f"unstable file identity (inode changed on open): {path}")

        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if max_read_bytes is not None and total > max_read_bytes:
                raise ValueError(f"bundle size exceeds maximum allowed cap ({max_read_bytes} bytes)")
            chunks.append(chunk)

        st_after = os.fstat(fd)
    finally:
        os.close(fd)

    try:
        st_path_after = os.lstat(path)
    except Exception as exc:
        raise ValueError(f"file disappeared during read {path}: {exc}") from exc

    if (st_path_after.st_dev, st_path_after.st_ino) != (st_before.st_dev, st_before.st_ino):
        raise ValueError(f"unstable file identity (replaced during read): {path}")
    if st_after.st_size != total or st_before.st_size != total:
        raise ValueError(f"unstable file size during read: {path}")
    if st_after.st_mtime_ns != st_before.st_mtime_ns:
        raise ValueError(f"unstable file mtime during read: {path}")

    return b"".join(chunks), st_after


def _check_hold_marker(workspace: Path) -> Tuple[bool, Optional[str]]:
    """Probe for HOLD-WORKSPACE without following symlinks.

    Returns (hold_active, reason_if_held_or_failed).
    """
    marker = workspace / HOLD_MARKER
    try:
        st = os.lstat(marker)
    except FileNotFoundError:
        return False, None
    except Exception as exc:
        return True, f"hold probe failed: {exc}"

    if stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(marker, flags)
            os.close(fd)
        except Exception as exc:
            return True, f"hold probe failed: {exc}"

    return True, "hold marker"


def _probe_keep_marker(workspace: Path) -> Tuple[bool, Optional[str]]:
    """Probe KEEP-BUNDLE marker.

    Returns (is_valid, error_if_invalid).
    A missing marker returns (False, None).
    A present marker must be safe, regular, <= 4096 bytes, owner-readable, and readable without error.
    """
    marker = workspace / KEEP_MARKER
    try:
        st = os.lstat(marker)
    except FileNotFoundError:
        return False, None
    except Exception as exc:
        return False, f"keep marker probe failed: {exc}"

    if stat.S_ISLNK(st.st_mode):
        return False, "keep marker probe failed: marker is a symlink"
    if not stat.S_ISREG(st.st_mode):
        return False, "keep marker probe failed: marker is not a regular file"
    if not (st.st_mode & stat.S_IRUSR):
        return False, "keep marker probe failed: marker lacks owner read permission"
    if st.st_size > KEEP_MARKER_MAX_BYTES:
        return False, "keep marker probe failed: marker exceeds max bytes limit"

    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(marker, flags)
        try:
            st_fd = os.fstat(fd)
            if (st_fd.st_dev, st_fd.st_ino) != (st.st_dev, st.st_ino) or not stat.S_ISREG(st_fd.st_mode):
                return False, "keep marker probe failed: unstable marker descriptor"
            content = os.read(fd, KEEP_MARKER_MAX_BYTES + 1)
            if len(content) > KEEP_MARKER_MAX_BYTES:
                return False, "keep marker probe failed: read exceeds max bytes"
        finally:
            os.close(fd)
    except Exception as exc:
        return False, f"keep marker probe failed: {exc}"

    return True, None


def _lexical_rel_parts_in_root(root: Path, candidate: str | Path) -> Optional[Tuple[str, ...]]:
    """If candidate is lexically inside root (without resolving symlinks in candidate),
    return the relative path parts tuple; otherwise return None.
    """
    cand_str = str(candidate).strip()
    if not cand_str:
        return None

    if os.path.isabs(cand_str):
        # Reject any raw '..' segment in an absolute path before normalization
        raw_parts = [p for p in re.split(r"[/\\]+", cand_str) if p and p != "."]
        if ".." in raw_parts:
            return None
        norm_cand = Path(os.path.normpath(cand_str))
        for base in (Path(os.path.normpath(str(root))), root.resolve()):
            try:
                rel = norm_cand.relative_to(base)
                return tuple(p for p in rel.parts if p not in ("", "."))
            except ValueError:
                continue
        return None
    else:
        raw_parts = [p for p in re.split(r"[/\\]+", cand_str) if p and p != "."]
        norm_rel = os.path.normpath(cand_str)
        if norm_rel in (".", ""):
            return ()
        norm_parts = tuple(p for p in Path(norm_rel).parts if p not in ("", "."))
        if not norm_parts or norm_parts[0] == ".." or ".." in norm_parts:
            return None
        if ".." in raw_parts:
            return None
        return norm_parts


def _validate_safe_path_in_root(
    root: Path,
    candidate: str | Path,
    *,
    allow_root: bool = False,
    require_dir: bool = False,
    require_regular: bool = False,
) -> Tuple[Path, str, os.stat_result]:
    """Validate that candidate stays inside root with no symlink, prohibited, or excluded
    components. Never calls lstat/stat/open outside root.
    """
    rel_parts = _lexical_rel_parts_in_root(root, candidate)
    if rel_parts is None:
        raise ValueError(f"referenced bundle member is outside workspace or checkout: {candidate}")

    if not rel_parts:
        if not allow_root:
            raise ValueError(f"path refers to root itself: {candidate}")
        st_root = os.lstat(root)
        if stat.S_ISLNK(st_root.st_mode) or not stat.S_ISDIR(st_root.st_mode):
            raise ValueError(f"root is linked or not a directory: {candidate}")
        return root, ".", st_root

    # Check prohibited/excluded component names BEFORE any lstat on components
    for part in rel_parts:
        if part in EXCLUDED_DIR_NAMES:
            raise ValueError(f"unsafe bundle component '{part}' in {candidate}")
        if is_prohibited_name(part):
            raise ValueError(f"referenced member has a prohibited name: {candidate}")

    cur = root
    st_cur: Optional[os.stat_result] = None
    for idx, part in enumerate(rel_parts):
        cur = cur / part
        try:
            st_cur = os.lstat(cur)
        except FileNotFoundError as exc:
            raise ValueError(f"referenced bundle member is missing: {candidate}") from exc
        except Exception as exc:
            raise ValueError(f"failed to lstat {candidate}: {exc}") from exc

        if stat.S_ISLNK(st_cur.st_mode):
            raise ValueError(f"referenced member is linked: {candidate}")
        if idx < len(rel_parts) - 1 and not stat.S_ISDIR(st_cur.st_mode):
            raise ValueError(f"ancestor component is not a directory: {candidate}")

    assert st_cur is not None
    if require_dir and not stat.S_ISDIR(st_cur.st_mode):
        raise ValueError(f"expected directory but found non-directory: {candidate}")
    if require_regular and not stat.S_ISREG(st_cur.st_mode):
        raise ValueError(f"unsafe non-regular entry: {candidate}")

    return cur, "/".join(rel_parts), st_cur


def _exists_in_root_lexically(root: Optional[Path], rel_or_abs: str) -> bool:
    """Safely check if a path entry (including a symlink or FIFO) exists inside root via lstat
    without ever leaving root or following symlinks.
    """
    if root is None:
        return False
    rel_parts = _lexical_rel_parts_in_root(root, rel_or_abs)
    if not rel_parts:
        return False
    cur = root
    for idx, part in enumerate(rel_parts):
        cur = cur / part
        try:
            st = os.lstat(cur)
        except OSError:
            return False
        if idx < len(rel_parts) - 1:
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                return True  # symlink ancestor exists inside root and must be caught by validator
    return True


def _has_existing_top_dir(root: Optional[Path], token: str) -> bool:
    """Return True if token is 'dir/...' where 'dir' exists inside root."""
    if root is None or "/" not in token:
        return False
    first = token.split("/", 1)[0].strip()
    if not first or first in (".", ".."):
        return False
    try:
        st = os.lstat(root / first)
        return stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode)
    except OSError:
        return False


def _strip_source_suffix(ref_str: str, workspace: Path, recorded_checkout: Optional[Path]) -> str:
    """Strip :line or #L source location suffix unless the literal suffixed filename exists."""
    suffix_match = _SOURCE_SUFFIX_PATTERN.search(ref_str)
    if not suffix_match:
        return ref_str
    if _exists_in_root_lexically(workspace, ref_str) or _exists_in_root_lexically(recorded_checkout, ref_str):
        return ref_str
    suffix = suffix_match.group(0)
    return ref_str[: -len(suffix)]


def _is_root_context_token(ref_str: str, workspace: Path) -> Tuple[bool, bool]:
    """Return (is_root_ref, mark_excluded_dot) for workspace root self-references."""
    t = ref_str.strip()
    if t == ".":
        return True, False
    if t == "./":
        return True, True
    ws_norm = os.path.normpath(str(workspace))
    ws_real = str(workspace.resolve())
    if t in (ws_norm, f"{ws_norm}/", ws_real, f"{ws_real}/"):
        return True, True
    if t == f"../{workspace.name}/" or t == f"../{workspace.name}":
        return True, True
    # Check relative path that normalizes to '.' inside workspace (e.g. 'bundle/..')
    if not os.path.isabs(t) and ".." in t:
        parts = [p for p in re.split(r"[/\\]+", t) if p and p != "."]
        if len(parts) == 2 and parts[0] != ".." and parts[1] == "..":
            if _exists_in_root_lexically(workspace, parts[0]):
                return True, True
    return False, False


def _clean_unquoted_token_punctuation(
    tok: str, workspace: Path, recorded_checkout: Optional[Path]
) -> str:
    """Strip surrounding punctuation from an unquoted token while preserving balanced parens
    and literal filenames that end with a dot.
    """
    t = tok.strip(" \t\r\n;,\"'<>[]`*")
    # Strip outer parens only if wrapped as (foo)
    while t.startswith("(") and t.endswith(")"):
        t = t[1:-1].strip(" \t\r\n;,\"'<>[]`*")
    if t.startswith("(") and ")" not in t[1:]:
        t = t.lstrip("(")
    if t.endswith(")") and "(" not in t[:-1]:
        t = t.rstrip(")")

    # Trailing sentence punctuation (. ! ? : ;)
    while t and t[-1] in ".;:!?":
        if _exists_in_root_lexically(workspace, t) or _exists_in_root_lexically(recorded_checkout, t):
            break
        t = t[:-1]
    return t


def _looks_like_bare_reference(
    tok: str, workspace: Path, recorded_checkout: Optional[Path]
) -> bool:
    """Determine whether an unquoted whitespace token in prose is a path/metadata reference
    rather than an ordinary English prose word.
    """
    if not tok:
        return False
    is_root, _ = _is_root_context_token(tok, workspace)
    if is_root:
        return True
    if tok.startswith(("./", "../", "/")):
        return True
    if is_non_path_metadata(tok):
        return True
    if "/" in tok:
        return True
    base_no_suffix = _strip_source_suffix(tok, workspace, recorded_checkout)
    if _exists_in_root_lexically(workspace, tok) or _exists_in_root_lexically(recorded_checkout, tok):
        return True
    if _exists_in_root_lexically(workspace, base_no_suffix) or _exists_in_root_lexically(
        recorded_checkout, base_no_suffix
    ):
        return True
    if base_no_suffix in _KNOWN_EXTENSIONLESS_FILES:
        return True
    if base_no_suffix.lower().startswith(".env"):
        return True
    if _FILE_EXT_PATTERN.search(base_no_suffix):
        return True
    return False


def _extract_references_from_text(
    text: str,
    workspace: Path,
    recorded_checkout: Optional[Path],
    *,
    default_explicit_local: bool = False,
) -> List[Tuple[str, bool]]:
    """Extract (candidate_ref, is_explicit_local) pairs from prose or metadata text."""
    if not text or not text.strip():
        return []

    results: List[Tuple[str, bool]] = []
    working = text

    # 1. Markdown links [label](target)
    def _replace_md_link(m: re.Match) -> str:
        target = m.group(2).strip()
        if target:
            results.append((target, default_explicit_local))
        return " " * len(m.group(0))

    working = re.sub(r"\[([^\]]*)\]\(([^)\r\n]+)\)", _replace_md_link, working)

    # 2. Key=quoted or Key=unquoted (artifact=, path:, file=, required=, placement=)
    def _replace_kv_quoted(m: re.Match) -> str:
        key = m.group(1).lower()
        val = m.group(3).strip()
        is_local = default_explicit_local or (key in {"artifact", "path", "file"})
        if val:
            results.append((val, is_local))
        return " " * len(m.group(0))

    working = re.sub(
        r"\b(artifact|path|file|required|placement)\s*[:=]\s*([`\"'])([^`\"'\r\n]+)\2",
        _replace_kv_quoted,
        working,
        flags=re.IGNORECASE,
    )

    # 3. Wrapped tokens: `...`, "...", '...', <...>, **...**
    wrapper_patterns = [
        r"`([^`\r\n]+)`",
        r'"([^"\r\n]+)"',
        r"(?<![A-Za-z0-9_])'([^'\r\n]+)'(?![A-Za-z0-9_])",
        r"<([^>\s][^>\r\n]*)>",
        r"\*\*([^*\r\n]+)\*\*",
    ]
    for pat in wrapper_patterns:
        def _replace_wrapped(m: re.Match) -> str:
            val = m.group(1).strip()
            if val:
                is_wrapped_local = default_explicit_local or not is_non_path_metadata(val)
                results.append((val, is_wrapped_local))
            return " " * len(m.group(0))

        working = re.sub(pat, _replace_wrapped, working)

    # 4. Unquoted key=value or key:value (artifact=, path:, file=, required=, placement=)
    def _replace_kv_unquoted(m: re.Match) -> str:
        key = m.group(1).lower()
        raw_val = m.group(2).strip()
        val = _clean_unquoted_token_punctuation(raw_val, workspace, recorded_checkout)
        is_local = default_explicit_local or (key in {"artifact", "path", "file"})
        if val:
            results.append((val, is_local))
        return " " * len(m.group(0))

    working = re.sub(
        r"\b(artifact|path|file|required|placement)[:=]([^\s,;]+)",
        _replace_kv_unquoted,
        working,
        flags=re.IGNORECASE,
    )

    # 5. Spaced absolute paths under workspace or recorded_checkout in remaining text
    roots_to_scan: List[str] = []
    for r_path in (recorded_checkout, workspace):
        if r_path is not None:
            for s in (str(r_path), str(r_path.resolve())):
                if s not in roots_to_scan:
                    roots_to_scan.append(s)
    roots_to_scan.sort(key=len, reverse=True)

    new_lines: List[str] = []
    for line in working.splitlines():
        cur_line = line
        for root_str in roots_to_scan:
            prefix = root_str.rstrip("/") + "/"
            if prefix not in cur_line:
                continue
            # Match from prefix up to a file extension (+ optional source suffix) or end of clause
            ext_pat = (
                re.escape(prefix)
                + r"[^\r\n`\"'<>;&|]+?"
                + r"(?:\.[A-Za-z0-9]{1,10}(?::\d+(?:-\d+|:\d+)?|#L\d+|#[\w-]+)?|(?=[;,)\]\r\n]|$))"
            )
            def _replace_abs_spaced(m: re.Match) -> str:
                cand = _clean_unquoted_token_punctuation(
                    m.group(0).strip(), workspace, recorded_checkout
                )
                if cand:
                    results.append((cand, True))
                return " " * len(m.group(0))

            cur_line = re.sub(ext_pat, _replace_abs_spaced, cur_line)
        new_lines.append(cur_line)
    working = "\n".join(new_lines)

    # 6. Remaining bare whitespace tokens
    for line in working.splitlines():
        for raw_tok in line.split():
            # Handle parenthesized or bracketed single tokens like (bundle/report.md) or [bundle/report.md]
            was_bracketed = (
                (raw_tok.startswith("(") and raw_tok.endswith(")"))
                or (raw_tok.startswith("[") and raw_tok.endswith("]"))
            )
            clean = _clean_unquoted_token_punctuation(raw_tok, workspace, recorded_checkout)
            if not clean:
                continue
            # Strip ../<workspace.name>/ prefix when referring back into workspace
            if clean.startswith(f"../{workspace.name}/"):
                clean = clean[len(f"../{workspace.name}/") :]
                if not clean:
                    clean = "./"
            if was_bracketed or default_explicit_local or _looks_like_bare_reference(
                clean, workspace, recorded_checkout
            ):
                results.append((clean, default_explicit_local))

    return results


def _get_task_metadata(conn: sqlite3.Connection, task_id: str) -> Dict[str, Any]:
    """Retrieve task result, run summaries, and run metadata."""
    t_row = conn.execute("SELECT result FROM tasks WHERE id=?", (task_id,)).fetchone()
    result = t_row["result"] if t_row and t_row["result"] else ""

    runs = conn.execute(
        "SELECT summary, metadata FROM task_runs WHERE task_id=? ORDER BY started_at ASC, id ASC",
        (task_id,),
    ).fetchall()

    return {
        "result": result,
        "runs": runs,
    }


def plan_bundle(
    conn: sqlite3.Connection,
    task_id: str,
    workspace_path: str | Path,
    max_bytes: int = MAX_BUNDLE_BYTES,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Plan the full inventory capture and validate all references.

    Raises ValueError on unsafe files, probe failures, malformed metadata JSON,
    missing required references, outside-workspace paths, or cap violations.
    """
    workspace = Path(workspace_path)
    try:
        root_st = os.lstat(workspace)
    except Exception as exc:
        raise ValueError(f"workspace root is missing or unreadable: {exc}") from exc

    if stat.S_ISLNK(root_st.st_mode):
        raise ValueError("workspace root is missing or linked")
    if not stat.S_ISDIR(root_st.st_mode):
        raise ValueError("workspace root is not a directory")

    # 1. HOLD marker probe
    held, hold_reason = _check_hold_marker(workspace)
    if held:
        raise ValueError(f"cannot plan bundle: {hold_reason}")

    # 2. KEEP marker probe
    _has_keep, keep_err = _probe_keep_marker(workspace)
    if keep_err:
        raise ValueError(keep_err)

    # 3. Parse and validate task_runs.metadata (fail closed on malformed JSON or unsafe worktree)
    meta = _get_task_metadata(conn, task_id)
    worktree_roots: List[Path] = []
    worktree_keys: Set[str] = set()
    parsed_runs: List[Tuple[sqlite3.Row, Optional[Dict[str, Any]], Optional[Path]]] = []

    for r in meta["runs"]:
        raw_md = r["metadata"]
        m_obj: Optional[Dict[str, Any]] = None
        run_wt: Optional[Path] = None
        if raw_md is not None and str(raw_md).strip() != "":
            try:
                loaded = json.loads(raw_md)
            except Exception as exc:
                raise ValueError(f"malformed task_runs.metadata JSON: {exc}") from exc
            if not isinstance(loaded, dict):
                raise ValueError("malformed task_runs.metadata: top-level JSON value must be an object")
            m_obj = loaded

            if "worktree" in m_obj and m_obj["worktree"] is not None:
                wt_val = m_obj["worktree"]
                if not isinstance(wt_val, str) or not wt_val.strip():
                    raise ValueError("invalid recorded worktree in task_runs.metadata")
                wt_path, wt_rel_key, _ = _validate_safe_path_in_root(
                    workspace, wt_val.strip(), allow_root=True, require_dir=True
                )
                run_wt = wt_path
                if wt_rel_key not in worktree_keys:
                    worktree_keys.add(wt_rel_key)
                    worktree_roots.append(wt_path)
        parsed_runs.append((r, m_obj, run_wt))

    conflicting_checkouts = len(worktree_roots) > 1
    recorded_checkout: Optional[Path] = worktree_roots[0] if len(worktree_roots) == 1 else None

    # 4. Full-inventory scan of workspace (before reference resolution; no silent exclusions)
    workspace_files: Dict[str, Dict[str, Any]] = {}
    directories: List[str] = []
    total_bytes = 0
    excluded: List[str] = []

    def _walk(cur_dir: Path) -> None:
        nonlocal total_bytes
        try:
            with os.scandir(cur_dir) as it:
                entries = list(it)
        except Exception as exc:
            raise ValueError(f"directory walk failed: {exc}") from exc

        for entry in sorted(entries, key=lambda e: e.name):
            rel_to_ws = Path(entry.path).relative_to(workspace).as_posix()

            # Check prohibited and excluded component names BEFORE lstat/descent/open
            if entry.name in EXCLUDED_DIR_NAMES:
                raise ValueError(f"unsafe bundle entry (excluded name): {rel_to_ws}")
            if is_prohibited_name(entry.name):
                raise ValueError(f"unsafe bundle file: prohibited name {rel_to_ws}")

            try:
                st = os.lstat(entry.path)
            except Exception as exc:
                raise ValueError(f"failed to lstat {entry.path}: {exc}") from exc

            if stat.S_ISLNK(st.st_mode):
                raise ValueError(f"unsafe bundle file: symlink {rel_to_ws}")

            if stat.S_ISDIR(st.st_mode):
                directories.append(rel_to_ws)
                _walk(Path(entry.path))
            elif stat.S_ISREG(st.st_mode):
                remaining_cap = max_bytes - total_bytes
                if remaining_cap < 0 or st.st_size > remaining_cap:
                    raise ValueError(f"bundle size exceeds maximum allowed cap ({max_bytes} bytes)")
                data, st_verified = _safe_read_regular_file(Path(entry.path), max_read_bytes=remaining_cap)
                total_bytes += len(data)
                workspace_files[rel_to_ws] = {
                    "path": rel_to_ws,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "source": "workspace",
                    "full_path": str(entry.path),
                    "mtime": int(st_verified.st_mtime),
                }
            else:
                raise ValueError(f"unsafe non-regular entry: {rel_to_ws}")

    _walk(workspace)

    # Map any kanban_complete-staged attachments back to their workspace source when present
    staged_complete_map: Dict[str, str] = {}
    try:
        for att in kb.list_attachments(conn, task_id):
            if getattr(att, "uploaded_by", None) == "kanban_complete" and att.stored_path and att.filename:
                if (workspace / att.filename).exists():
                    staged_complete_map[str(Path(att.stored_path))] = str(workspace / att.filename)
                    staged_complete_map[str(Path(att.stored_path).resolve())] = str(workspace / att.filename)
    except Exception:
        pass

    unresolved_refs: List[str] = []
    unresolved_non_path_refs: List[str] = []
    ambiguous_refs: List[str] = []
    external_skill_refs: List[str] = []
    non_path_metadata_values: List[str] = []
    referenced_files: List[str] = []

    def _add_unique(target_list: List[str], item: str) -> None:
        if item not in target_list:
            target_list.append(item)

    # 5. Validate structured findings per run against that run's own recorded worktree
    extra_required_refs: List[Tuple[str, bool]] = []

    def _collect_finding_extras(val: Any) -> None:
        if isinstance(val, str):
            if val.strip():
                extra_required_refs.append((val.strip(), True))
        elif isinstance(val, list):
            for sub in val:
                _collect_finding_extras(sub)
        elif isinstance(val, dict):
            for sub_v in val.values():
                _collect_finding_extras(sub_v)

    for _r_row, m_obj, run_wt in parsed_runs:
        if not m_obj or "findings" not in m_obj:
            continue
        findings_val = m_obj["findings"]
        if not isinstance(findings_val, list):
            raise ValueError("malformed task_runs.metadata: 'findings' must be a list")

        for item in findings_val:
            if isinstance(item, dict):
                for k, v in item.items():
                    if k == "file":
                        if not isinstance(v, str) or not v.strip():
                            raise ValueError("malformed finding 'file' entry")
                        f_ref = v.strip()
                        if _URL_PATTERN.match(f_ref):
                            _add_unique(unresolved_non_path_refs, sanitize_uri(f_ref))
                            continue
                        if is_prohibited_name(f_ref) or has_excluded_component(f_ref):
                            raise ValueError(f"referenced member has a prohibited name: {f_ref}")
                        if run_wt is None:
                            # Rootless structured finding: check escape/absolute first, else mark ambiguous
                            if os.path.isabs(f_ref) or _lexical_rel_parts_in_root(workspace, f_ref) is None:
                                raise ValueError(f"rootless finding escapes workspace: {f_ref}")
                            _add_unique(ambiguous_refs, f_ref)
                        else:
                            # Must resolve inside this run's recorded checkout (run_wt)
                            if os.path.isabs(f_ref) or _lexical_rel_parts_in_root(run_wt, f_ref) is None:
                                raise ValueError(f"finding escapes recorded checkout: {f_ref}")
                            f_full, f_rel, _ = _validate_safe_path_in_root(
                                run_wt, f_ref, allow_root=False, require_regular=True
                            )
                            ws_rel_parts = _lexical_rel_parts_in_root(workspace, f_full)
                            if ws_rel_parts:
                                _add_unique(referenced_files, "/".join(ws_rel_parts))
                            else:
                                _add_unique(referenced_files, f_rel)
                    else:
                        _collect_finding_extras(v)
            elif isinstance(item, str):
                _collect_finding_extras(item)
            else:
                raise ValueError("malformed finding entry in task_runs.metadata")

    # 6. Collect candidate references from task.result, run.summary, and run.metadata
    candidate_refs: List[Tuple[str, bool]] = []
    if meta["result"]:
        candidate_refs.extend(
            _extract_references_from_text(
                meta["result"], workspace, recorded_checkout, default_explicit_local=False
            )
        )

    def _collect_metadata_dict_refs(
        obj: Any,
        *,
        under_explicit_key: bool = False,
        under_general_ref_key: bool = False,
        under_skill_provenance_key: bool = False,
        under_non_path_key: bool = False,
    ) -> None:
        if isinstance(obj, str):
            s = obj.strip()
            if not s:
                return
            if s in staged_complete_map:
                s = staged_complete_map[s]
            if under_skill_provenance_key and not under_explicit_key and not under_general_ref_key:
                # External skill provenance value: classify lexically without opening/stat-traversing external paths
                s_clean = s.strip(" \t\r\n`\"'")
                if (
                    s_clean
                    and not any(ch in s_clean for ch in " \t\r\n`\"'<>[]")
                    and not _URL_PATTERN.match(s_clean)
                    and not s_clean.startswith(("./", "../", "~"))
                    and not re.match(r"^(?:artifact|path|file|required|placement)[:=]", s_clean, re.IGNORECASE)
                    and ".." not in Path(s_clean).parts
                ):
                    if os.path.isabs(s_clean):
                        in_local = (
                            _lexical_rel_parts_in_root(workspace, s_clean) is not None
                            or (
                                recorded_checkout is not None
                                and _lexical_rel_parts_in_root(recorded_checkout, s_clean) is not None
                            )
                            or any(_lexical_rel_parts_in_root(wt, s_clean) is not None for wt in worktree_roots)
                        )
                        lower_parts = [p.lower() for p in Path(s_clean).parts if p and p != "/"]
                        has_skill_marker = (
                            any(
                                p in {"skills", "skill", "optional-skills", "references"} or p.startswith("skill")
                                for p in lower_parts
                            )
                            or (bool(lower_parts) and lower_parts[-1] == "skill.md")
                        )
                        if not in_local and has_skill_marker:
                            _add_unique(external_skill_refs, s_clean)
                            return
                    else:
                        norm_s = s_clean.replace("\\", "/").strip("/")
                        rel_parts = [p for p in norm_s.split("/") if p]
                        if (
                            len(rel_parts) >= 2
                            and not any(p in (".", "..") for p in rel_parts)
                            and not rel_parts[0].startswith(".")
                            and not has_excluded_component(norm_s)
                        ):
                            _add_unique(external_skill_refs, norm_s)
                            return
            if (
                under_non_path_key
                and not under_explicit_key
                and not under_general_ref_key
                and not under_skill_provenance_key
            ):
                # Documented non-path scalar metadata value (e.g. review.family, provider, model, verdict)
                s_clean = s.strip(" \t\r\n`\"'")
                if not s_clean:
                    return
                if _URL_PATTERN.match(s_clean):
                    _add_unique(unresolved_non_path_refs, sanitize_uri(s_clean))
                    return
                if is_non_path_scalar_value(s_clean):
                    _add_unique(non_path_metadata_values, s_clean)
                    return
            if under_explicit_key or (under_general_ref_key and not is_non_path_metadata(s.strip(" \t\r\n`\"'"))):
                # Could be a decorated string or a direct path (including spaced path)
                extracted = _extract_references_from_text(
                    s, workspace, recorded_checkout, default_explicit_local=True
                )
                # If s had no wrapper delimiters and represents a single path (e.g. spaced relative/abs path)
                if not any(ch in s for ch in "`\"'<>[]\r\n") and not re.match(
                    r"^(?:artifact|path|file)[:=]", s, re.IGNORECASE
                ):
                    clean_s = _clean_unquoted_token_punctuation(s, workspace, recorded_checkout)
                    if clean_s:
                        candidate_refs.append((clean_s, True))
                        return
                candidate_refs.extend(extracted)
            else:
                candidate_refs.extend(
                    _extract_references_from_text(
                        s, workspace, recorded_checkout, default_explicit_local=False
                    )
                )
        elif isinstance(obj, list):
            for elem in obj:
                _collect_metadata_dict_refs(
                    elem,
                    under_explicit_key=under_explicit_key,
                    under_general_ref_key=under_general_ref_key,
                    under_skill_provenance_key=under_skill_provenance_key,
                    under_non_path_key=under_non_path_key,
                )
        elif isinstance(obj, dict):
            for k, v in obj.items():
                k_str = str(k)
                k_lower = k_str.strip().lower()
                is_exp = under_explicit_key or is_explicit_local_metadata_key(k_str)
                is_gen_ref = (not is_exp) and (under_general_ref_key or (k_lower in _GENERAL_REFERENCE_METADATA_KEYS))
                is_skill_prov = (
                    (not is_exp)
                    and (not is_gen_ref)
                    and (
                        is_external_skill_provenance_key(k_str)
                        or (under_skill_provenance_key and k_lower in {"skill", "skills", "target", "value", "values"})
                    )
                )
                is_non_path = (
                    (not is_exp)
                    and (not is_gen_ref)
                    and (not is_skill_prov)
                    and is_non_path_scalar_metadata_key(k_str)
                )
                _collect_metadata_dict_refs(
                    v,
                    under_explicit_key=is_exp,
                    under_general_ref_key=is_gen_ref,
                    under_skill_provenance_key=is_skill_prov,
                    under_non_path_key=is_non_path,
                )

    for r_row, m_obj, _run_wt in parsed_runs:
        if r_row["summary"]:
            candidate_refs.extend(
                _extract_references_from_text(
                    r_row["summary"], workspace, recorded_checkout, default_explicit_local=False
                )
            )
        if m_obj:
            for k, v in m_obj.items():
                if k in {"worktree", "findings"}:
                    continue
                k_str = str(k)
                k_lower = k_str.strip().lower()
                is_exp = is_explicit_local_metadata_key(k_str)
                is_gen_ref = (not is_exp) and (k_lower in _GENERAL_REFERENCE_METADATA_KEYS)
                is_skill_prov = (not is_exp) and (not is_gen_ref) and is_external_skill_provenance_key(k_str)
                is_non_path = (
                    (not is_exp)
                    and (not is_gen_ref)
                    and (not is_skill_prov)
                    and is_non_path_scalar_metadata_key(k_str)
                )
                _collect_metadata_dict_refs(
                    v,
                    under_explicit_key=is_exp,
                    under_general_ref_key=is_gen_ref,
                    under_skill_provenance_key=is_skill_prov,
                    under_non_path_key=is_non_path,
                )

    candidate_refs.extend(extra_required_refs)

    co_rel_prefixes: List[str] = []
    for wt in worktree_roots:
        wt_parts = _lexical_rel_parts_in_root(workspace, wt)
        if wt_parts:
            co_rel_prefixes.append("/".join(wt_parts) + "/")

    def _in_scanned_inventory(ref_candidate: str) -> bool:
        norm_c = ref_candidate.replace("\\", "/").strip("/")
        if not norm_c:
            return False
        if norm_c in workspace_files or norm_c in directories:
            return True
        for pref in co_rel_prefixes:
            comb = f"{pref}{norm_c}"
            if comb in workspace_files or comb in directories:
                return True
        return False

    # 7. Resolve and validate all candidate references
    seen_candidates: Set[Tuple[str, bool]] = set()
    for raw_ref, is_explicit_local in candidate_refs:
        ref_str = raw_ref.strip()
        if not ref_str:
            continue
        if ref_str in staged_complete_map:
            ref_str = staged_complete_map[ref_str]

        key = (ref_str, is_explicit_local)
        if key in seen_candidates:
            continue
        seen_candidates.add(key)

        # URLs are always non-local metadata
        if _URL_PATTERN.match(ref_str):
            _add_unique(unresolved_non_path_refs, sanitize_uri(ref_str))
            continue

        # Workspace root context tokens (".", "./", "{workspace}", etc.)
        is_root, mark_dot = _is_root_context_token(ref_str, workspace)
        if is_root:
            if mark_dot:
                _add_unique(excluded, ".")
            continue

        # Strip :line or #L suffix unless literal suffixed path exists
        clean_ref = _strip_source_suffix(ref_str, workspace, recorded_checkout)

        # Non-path metadata check (only when NOT in explicit local context and NOT in scanned workspace inventory)
        if (
            not is_explicit_local
            and not clean_ref.startswith("./")
            and not _in_scanned_inventory(clean_ref)
            and is_non_path_metadata(clean_ref)
        ):
            continue

        # Check if path exists on disk locally inside workspace or recorded_checkout
        exists_ws = _exists_in_root_lexically(workspace, clean_ref)
        exists_co = _exists_in_root_lexically(recorded_checkout, clean_ref)
        exists_any_co = any(_exists_in_root_lexically(wt, clean_ref) for wt in worktree_roots)

        # Prohibited / excluded name check
        if is_prohibited_name(clean_ref) or has_excluded_component(clean_ref):
            if os.path.isabs(clean_ref) or exists_ws or exists_co or exists_any_co or is_explicit_local:
                raise ValueError(f"referenced member has a prohibited name: {clean_ref}")
            _add_unique(unresolved_refs, clean_ref)
            continue

        # Absolute path resolution
        if os.path.isabs(clean_ref):
            in_ws_parts = _lexical_rel_parts_in_root(workspace, clean_ref)
            in_co_parts = (
                _lexical_rel_parts_in_root(recorded_checkout, clean_ref)
                if recorded_checkout is not None
                else None
            )
            if in_ws_parts is None and in_co_parts is None:
                raise ValueError(f"referenced bundle member is outside workspace or checkout: {clean_ref}")

            target_root = workspace if in_ws_parts is not None else recorded_checkout
            assert target_root is not None
            # Validates existence, no symlinks, no prohibited components
            target_full, target_rel, st_target = _validate_safe_path_in_root(
                target_root, clean_ref, allow_root=True
            )
            if stat.S_ISDIR(st_target.st_mode):
                # Directory reference: ensure walking it works (already walked, or verify readable)
                try:
                    with os.scandir(target_full) as it:
                        list(it)
                except Exception as exc:
                    raise ValueError(f"directory walk failed: {exc}") from exc
            elif not stat.S_ISREG(st_target.st_mode):
                raise ValueError(f"unsafe non-regular entry: {clean_ref}")
            else:
                ws_rel = _lexical_rel_parts_in_root(workspace, target_full)
                _add_unique(referenced_files, "/".join(ws_rel) if ws_rel else target_rel)
            continue

        # Relative path resolution
        norm_clean = clean_ref[2:] if clean_ref.startswith("./") else clean_ref

        # Check parent escape '../...'
        if norm_clean.startswith("..") or ".." in Path(norm_clean).parts:
            if _lexical_rel_parts_in_root(workspace, norm_clean) is None:
                _add_unique(ambiguous_refs, clean_ref)
                continue

        # Conflicting checkouts check
        if conflicting_checkouts:
            if exists_ws or exists_any_co:
                # Validate that none of the existing copies are symlinks
                if exists_ws:
                    _validate_safe_path_in_root(workspace, norm_clean, allow_root=False)
                for wt in worktree_roots:
                    if _exists_in_root_lexically(wt, norm_clean):
                        _validate_safe_path_in_root(wt, norm_clean, allow_root=False)
                _add_unique(ambiguous_refs, norm_clean)
                continue

        # Recompute existence on norm_clean
        exists_ws = _exists_in_root_lexically(workspace, norm_clean)
        exists_co = _exists_in_root_lexically(recorded_checkout, norm_clean)

        if exists_ws:
            ws_full, ws_rel_key, ws_st = _validate_safe_path_in_root(workspace, norm_clean, allow_root=False)
            if not (stat.S_ISREG(ws_st.st_mode) or stat.S_ISDIR(ws_st.st_mode)):
                raise ValueError(f"unsafe non-regular entry: {norm_clean}")
            # If the user referenced `file:suffix` where `file` is a directory, do not expand directory
            if clean_ref != ref_str and stat.S_ISDIR(ws_st.st_mode):
                _add_unique(unresolved_refs, ref_str)
                continue

        if exists_co and recorded_checkout is not None:
            co_full, co_rel_key, co_st = _validate_safe_path_in_root(
                recorded_checkout, norm_clean, allow_root=False
            )
            if not (stat.S_ISREG(co_st.st_mode) or stat.S_ISDIR(co_st.st_mode)):
                raise ValueError(f"unsafe non-regular entry: {norm_clean}")

        if exists_ws and exists_co and recorded_checkout is not None:
            if (workspace / norm_clean) != (recorded_checkout / norm_clean):
                _add_unique(ambiguous_refs, norm_clean)
                continue

        if exists_ws or exists_co:
            if exists_ws and stat.S_ISREG(ws_st.st_mode):
                _add_unique(referenced_files, ws_rel_key)
            elif exists_co and recorded_checkout is not None and stat.S_ISREG(co_st.st_mode):
                ws_rel_co = _lexical_rel_parts_in_root(workspace, co_full)
                _add_unique(referenced_files, "/".join(ws_rel_co) if ws_rel_co else co_rel_key)
            continue

        # Neither exists on disk
        if (
            not is_explicit_local
            and not clean_ref.startswith("./")
            and _GITHUB_SLUG_PATTERN.match(norm_clean)
        ):
            seg1, seg2 = norm_clean.split("/", 1)
            has_ext = bool(_FILE_EXT_PATTERN.search(seg2)) or seg2.startswith(".")
            is_known_file = seg2 in _KNOWN_EXTENSIONLESS_FILES
            has_local_dir = _has_existing_top_dir(workspace, norm_clean) or _has_existing_top_dir(
                recorded_checkout, norm_clean
            )
            if not has_ext and not is_known_file and not has_local_dir:
                _add_unique(unresolved_non_path_refs, norm_clean)
                continue

        _add_unique(unresolved_refs, norm_clean)

    all_files = sorted(workspace_files.values(), key=lambda x: x["path"])

    return {
        "workspace": str(workspace),
        "files": [{"path": f["path"], "bytes": f["bytes"], "sha256": f["sha256"]} for f in all_files],
        "_file_details": all_files,
        "bytes": sum(f["bytes"] for f in all_files),
        "referenced_files": sorted(referenced_files),
        "unresolved_refs": sorted(unresolved_refs),
        "unresolved_non_path_refs": sorted(unresolved_non_path_refs),
        "external_skill_refs": sorted(external_skill_refs),
        "non_path_metadata_values": sorted(non_path_metadata_values),
        "ambiguous_refs": sorted(ambiguous_refs),
        "excluded": excluded,
        "directories": sorted(directories),
    }


def _record_event_strict(conn: sqlite3.Connection, task_id: str, kind: str, payload_obj: dict) -> None:
    """Record a task_events row and verify it was committed and not ignored by a trigger."""
    now = int(time.time())
    payload_str = json.dumps(payload_obj)
    with kb.write_txn(conn):
        cur = conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (task_id, kind, payload_str, now),
        )
        if cur.rowcount != 1 or not cur.lastrowid:
            raise ValueError(f"failed to record {kind} event")
        row_id = int(cur.lastrowid)

    check = conn.execute(
        "SELECT id, payload FROM task_events WHERE id = ? AND task_id = ? AND kind = ?",
        (row_id, task_id, kind),
    ).fetchone()
    if not check or check["payload"] != payload_str:
        raise ValueError(f"event read-back verification failed for {kind}")


def _allocate_unique_bundle_names(
    conn: sqlite3.Connection, task_id: str, dest_dir: Path
) -> Tuple[str, str]:
    """Allocate a collision-free (archive_name, manifest_name) pair under dest_dir."""
    existing_names = {a.filename for a in kb.list_attachments(conn, task_id)}
    idx = 1
    while True:
        if idx == 1:
            arc_name = f"bundle-{task_id}.tgz"
            man_name = f"bundle-{task_id}-manifest.json"
        else:
            arc_name = f"bundle-{task_id}-{idx}.tgz"
            man_name = f"bundle-{task_id}-{idx}-manifest.json"

        arc_dest = dest_dir / arc_name
        man_dest = dest_dir / man_name
        arc_exists = True
        man_exists = True
        try:
            os.lstat(arc_dest)
        except FileNotFoundError:
            arc_exists = False
        try:
            os.lstat(man_dest)
        except FileNotFoundError:
            man_exists = False

        if (
            not arc_exists
            and not man_exists
            and arc_name not in existing_names
            and man_name not in existing_names
        ):
            return arc_name, man_name
        idx += 1


def _verify_bundle_artifacts(
    conn: sqlite3.Connection,
    task_id: str,
    att_archive: Any,
    att_manifest: Any,
    expected_plan: Dict[str, Any],
) -> None:
    """Strictly verify attachment rows, manifest JSON, and .tgz archive members/bytes."""
    dest_dir = kb.task_attachments_dir(task_id)
    try:
        dest_st = os.lstat(dest_dir)
        if stat.S_ISLNK(dest_st.st_mode) or not stat.S_ISDIR(dest_st.st_mode):
            raise ValueError("attachments directory is missing or linked")
    except Exception as exc:
        raise ValueError(f"attachments directory check failed: {exc}") from exc

    # Re-fetch exact rows by ID
    row_arc = kb.get_attachment(conn, int(att_archive.id))
    row_man = kb.get_attachment(conn, int(att_manifest.id))
    if row_arc is None or row_man is None:
        raise ValueError("attachment row missing on get_attachment read-back")
    if row_arc.task_id != task_id or row_man.task_id != task_id:
        raise ValueError("attachment task_id mismatch")
    if row_arc.filename != att_archive.filename or row_man.filename != att_manifest.filename:
        raise ValueError("attachment filename mismatch")

    # Verify filename pairing (bundle-<id>[-N].tgz <-> bundle-<id>[-N]-manifest.json)
    if not row_arc.filename.endswith(".tgz") or not row_man.filename.endswith("-manifest.json"):
        raise ValueError("invalid bundle attachment extension")
    arc_prefix = row_arc.filename[: -len(".tgz")]
    man_prefix = row_man.filename[: -len("-manifest.json")]
    if arc_prefix != man_prefix or not arc_prefix.startswith(f"bundle-{task_id}"):
        raise ValueError("mismatched archive and manifest attachment pair")

    # Verify stored_path is safely inside dest_dir and not linked
    arc_path, _, arc_st = _validate_safe_path_in_root(
        dest_dir, row_arc.stored_path, allow_root=False, require_regular=True
    )
    man_path, _, man_st = _validate_safe_path_in_root(
        dest_dir, row_man.stored_path, allow_root=False, require_regular=True
    )
    if row_arc.size is not None and int(row_arc.size) != arc_st.st_size:
        raise ValueError("archive size does not match attachment row")
    if row_man.size is not None and int(row_man.size) != man_st.st_size:
        raise ValueError("manifest size does not match attachment row")

    # Read and verify manifest JSON
    man_bytes, _ = _safe_read_regular_file(man_path, max_read_bytes=MAX_BUNDLE_BYTES)
    try:
        man_data = json.loads(man_bytes.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"corrupt manifest JSON: {exc}") from exc

    if not isinstance(man_data, list):
        raise ValueError("manifest JSON must be a list of file entries")

    normalized_man: List[Dict[str, Any]] = []
    seen_man_paths: Set[str] = set()
    for entry in man_data:
        if not isinstance(entry, dict) or set(entry.keys()) != {"path", "bytes", "sha256"}:
            raise ValueError("invalid manifest entry schema")
        p = entry["path"]
        b = entry["bytes"]
        h = entry["sha256"]
        if not isinstance(p, str) or not isinstance(b, int) or not isinstance(h, str):
            raise ValueError("invalid manifest entry types")
        if p in seen_man_paths:
            raise ValueError(f"duplicate path in manifest: {p}")
        seen_man_paths.add(p)
        normalized_man.append({"path": p, "bytes": b, "sha256": h})

    if normalized_man != expected_plan["files"]:
        raise ValueError("manifest entries do not match current workspace inventory")

    # Read and verify .tgz archive bytes and every member
    arc_bytes, _ = _safe_read_regular_file(arc_path, max_read_bytes=MAX_BUNDLE_BYTES)
    try:
        with tarfile.open(fileobj=io.BytesIO(arc_bytes), mode="r:gz") as tar:
            members = tar.getmembers()
            seen_member_names: Set[str] = set()
            arc_dirs: Set[str] = set()
            arc_files: Dict[str, Tuple[int, str]] = {}

            for m in members:
                norm_name = m.name.rstrip("/")
                if not norm_name or norm_name.startswith("/") or ".." in Path(norm_name).parts:
                    raise ValueError(f"unsafe archive member path: {m.name}")
                if norm_name in seen_member_names:
                    raise ValueError(f"duplicate archive member: {norm_name}")
                seen_member_names.add(norm_name)

                if m.issym() or m.islnk() or m.isfifo() or m.ischr() or m.isblk() or m.isdev():
                    raise ValueError(f"unsafe non-regular archive member: {m.name}")
                if m.isdir():
                    arc_dirs.add(norm_name)
                elif m.isfile():
                    fobj = tar.extractfile(m)
                    if fobj is None:
                        raise ValueError(f"unable to extract archive member: {m.name}")
                    payload = fobj.read()
                    if len(payload) != m.size:
                        raise ValueError(f"archive member size mismatch: {m.name}")
                    arc_files[norm_name] = (len(payload), hashlib.sha256(payload).hexdigest())
                else:
                    raise ValueError(f"unsupported archive member type: {m.name}")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"corrupt bundle archive: {exc}") from exc

    expected_dirs = set(expected_plan.get("directories", []))
    if arc_dirs != expected_dirs:
        raise ValueError("archive directory members do not match workspace directories")

    expected_files_map = {f["path"]: (f["bytes"], f["sha256"]) for f in expected_plan["files"]}
    if arc_files != expected_files_map:
        raise ValueError("archive file members or hashes do not match manifest")


def _is_self_capture_attachment(task_id: str, att: Any) -> bool:
    """Return True if att is a bundle capture artifact generated by kanban_bundle_preserve."""
    uploader = getattr(att, "uploaded_by", None) or ""
    fname = getattr(att, "filename", None) or ""
    if uploader == PRESERVE_UPLOADER:
        return True
    if fname.startswith(f"bundle-{task_id}") and (
        fname.endswith(".tgz") or fname.endswith("-manifest.json")
    ):
        return True
    return False


def preserve_bundle(
    conn: sqlite3.Connection,
    task_id: str,
    workspace_path: str | Path,
    dry_run: bool = False,
    max_bytes: int = MAX_BUNDLE_BYTES,
) -> Dict[str, Any]:
    """Execute bundle capture, write archive and manifest outside workspace, register, and verify."""
    from hermes_cli import kanban_db_workspace as kbw

    if not dry_run and kbw._is_delegated_child_context():
        raise ValueError("delegated child context cannot mutate preserved bundles")

    report = plan_bundle(conn, task_id, workspace_path, max_bytes=max_bytes, dry_run=dry_run)
    if dry_run:
        return report

    if report["unresolved_refs"]:
        _record_event_strict(
            conn, task_id, "bundle_unresolved_refs", {"unresolved_refs": report["unresolved_refs"]}
        )

    if report["unresolved_non_path_refs"]:
        _record_event_strict(
            conn,
            task_id,
            "bundle_non_path_refs",
            {"unresolved_non_path_refs": report["unresolved_non_path_refs"]},
        )

    if report.get("ambiguous_refs"):
        return report

    if not report["files"]:
        return report

    # If an existing bundle on this task already matches the exact current workspace inventory,
    # verify it rather than writing redundant duplicate archives on idempotent preserve_bundle calls.
    existing_atts = kb.list_attachments(conn, task_id)
    if existing_atts:
        man_candidates = [a for a in existing_atts if a.filename.endswith("-manifest.json")]
        arc_candidates = [a for a in existing_atts if a.filename.endswith(".tgz")]
        if man_candidates and arc_candidates:
            try:
                _verify_bundle_artifacts(conn, task_id, arc_candidates[-1], man_candidates[-1], report)
                return report
            except Exception:
                pass

    dest_dir = kb.task_attachments_dir(task_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_st = os.lstat(dest_dir)
    if stat.S_ISLNK(dest_st.st_mode) or not stat.S_ISDIR(dest_st.st_mode):
        raise ValueError("attachments destination directory is linked or not a directory")

    archive_name, manifest_name = _allocate_unique_bundle_names(conn, task_id, dest_dir)
    final_archive = dest_dir / archive_name
    final_manifest = dest_dir / manifest_name

    archive_temp = tempfile.NamedTemporaryFile(dir=dest_dir, prefix=".tmp-bundle-", suffix=".tgz", delete=False)
    archive_path = Path(archive_temp.name)
    archive_temp.close()

    manifest_temp = tempfile.NamedTemporaryFile(dir=dest_dir, prefix=".tmp-manifest-", suffix=".json", delete=False)
    manifest_path = Path(manifest_temp.name)
    manifest_temp.close()

    created_final_arc = False
    created_final_man = False
    att_arc_id: Optional[int] = None
    att_man_id: Optional[int] = None

    try:
        ws_path = Path(workspace_path)
        with tarfile.open(archive_path, "w:gz") as tar:
            for d in report.get("directories", []):
                _, _, d_st = _validate_safe_path_in_root(ws_path, d, allow_root=False, require_dir=True)
                ti = tarfile.TarInfo(name=d)
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
                ti.mtime = int(d_st.st_mtime)
                tar.addfile(ti)

            for item in report["_file_details"]:
                f_data, f_st = _safe_read_regular_file(Path(item["full_path"]), max_read_bytes=max_bytes)
                if len(f_data) != item["bytes"] or hashlib.sha256(f_data).hexdigest() != item["sha256"]:
                    raise ValueError(f"workspace file mutated during archive creation: {item['path']}")
                ti = tarfile.TarInfo(name=item["path"])
                ti.type = tarfile.REGTYPE
                ti.size = len(f_data)
                ti.mode = 0o644
                ti.mtime = int(f_st.st_mtime)
                tar.addfile(ti, io.BytesIO(f_data))

        manifest_data = json.dumps(report["files"], indent=2).encode("utf-8")
        manifest_path.write_bytes(manifest_data)

        # Ensure target paths still do not exist before moving temp files into place (never overwrite)
        for target_p in (final_archive, final_manifest):
            try:
                os.lstat(target_p)
                raise ValueError(f"refusing to overwrite existing attachment file: {target_p}")
            except FileNotFoundError:
                pass

        os.replace(archive_path, final_archive)
        created_final_arc = True
        os.replace(manifest_path, final_manifest)
        created_final_man = True

        arc_stat = os.lstat(final_archive)
        man_stat = os.lstat(final_manifest)

        att_arc_id = kb.add_attachment(
            conn,
            task_id,
            filename=archive_name,
            stored_path=str(final_archive.resolve()),
            content_type="application/gzip",
            size=arc_stat.st_size,
            uploaded_by=PRESERVE_UPLOADER,
        )
        att_man_id = kb.add_attachment(
            conn,
            task_id,
            filename=manifest_name,
            stored_path=str(final_manifest.resolve()),
            content_type="application/json",
            size=man_stat.st_size,
            uploaded_by=PRESERVE_UPLOADER,
        )

        att_arc = kb.get_attachment(conn, int(att_arc_id))
        att_man = kb.get_attachment(conn, int(att_man_id))
        if att_arc is None or att_man is None:
            raise ValueError("attachment read-back verification failed: rows not found")

        _verify_bundle_artifacts(conn, task_id, att_arc, att_man, report)

    except Exception as exc:
        # Clean up only artifacts created in this failed invocation
        archive_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        if created_final_arc:
            with contextlib.suppress(OSError):
                final_archive.unlink(missing_ok=True)
        if created_final_man:
            with contextlib.suppress(OSError):
                final_manifest.unlink(missing_ok=True)
        for aid in (att_arc_id, att_man_id):
            if aid is not None:
                with contextlib.suppress(Exception):
                    kb.delete_attachment(conn, int(aid))
        raise ValueError(f"preservation failed: {exc}") from exc

    return report


def preservation_gate(
    task_id: str,
    workspace_path: str | Path,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """Evaluate whether a workspace may be disposed or must be retained.

    Returns True to allow disposal/removal; returns False to retain the workspace.
    Fails closed on any exception, missing/linked root, hold marker, probe failure,
    zero initial independent attachments, ambiguous roots, or manifest/archive mismatch.
    """
    workspace = Path(workspace_path)

    close_conn = False
    if conn is None:
        try:
            conn = kbc.connect()
            close_conn = True
        except Exception:
            return False

    try:
        from hermes_cli import kanban_db_workspace as kbw

        if kbw._is_delegated_child_context():
            _record_comment(conn, task_id, "delegated child context cannot authorize disposal")
            return False

        if conn.in_transaction:
            # Caller-owned write transaction is not durable proof
            return False

        # 1. Validate root existence and link status
        try:
            st = os.lstat(workspace)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                _record_comment(conn, task_id, "workspace root is missing or linked")
                return False
        except Exception:
            _record_comment(conn, task_id, "workspace root is missing or linked")
            return False

        # 2. Check hold marker BEFORE planning
        held, reason = _check_hold_marker(workspace)
        if held:
            _record_comment(conn, task_id, reason or "hold marker")
            return False

        # 3. Verify task exists, is in a terminal state ('done' or 'archived'), and is not fenced by active/indeterminate descendants or runs
        if kbw._is_task_cleanup_fenced(conn, task_id):
            _record_comment(conn, task_id, "task is missing, non-terminal, or fenced by active/indeterminate descendants")
            return False

        # 4. Snapshot initial attachments BEFORE any action
        initial_attachments = kb.list_attachments(conn, task_id)
        # Self-generated capture attachments do NOT count as original independent attachments.
        independent_attachments = [
            a for a in initial_attachments if not _is_self_capture_attachment(task_id, a)
        ]
        capture_attachments = [
            a for a in initial_attachments if _is_self_capture_attachment(task_id, a)
        ]
        independent_count = len(independent_attachments)

        # Check if workspace has any entries
        try:
            with os.scandir(workspace) as it:
                ws_entries = list(it)
        except Exception as exc:
            _record_comment(conn, task_id, f"workspace unreadable: {exc}")
            return False

        if not ws_entries:
            # Empty workspace (0 entries): still plan to record any unresolved refs
            try:
                empty_plan = plan_bundle(conn, task_id, workspace)
                if empty_plan.get("unresolved_refs"):
                    _record_event_strict(
                        conn,
                        task_id,
                        "bundle_unresolved_refs",
                        {"unresolved_refs": empty_plan["unresolved_refs"]},
                    )
                    return False
                if empty_plan.get("ambiguous_refs"):
                    return False
            except Exception as exc:
                _record_comment(conn, task_id, str(exc))
                return False
            return True

        # 5. Plan the bundle
        try:
            plan = plan_bundle(conn, task_id, workspace)
        except Exception as exc:
            _record_comment(conn, task_id, str(exc))
            return False

        if plan.get("unresolved_refs"):
            try:
                _record_event_strict(
                    conn,
                    task_id,
                    "bundle_unresolved_refs",
                    {"unresolved_refs": plan["unresolved_refs"]},
                )
            except Exception as exc:
                _record_comment(conn, task_id, f"unresolved event record failed: {exc}")
                return False

        if plan.get("unresolved_non_path_refs"):
            try:
                _record_event_strict(
                    conn,
                    task_id,
                    "bundle_non_path_refs",
                    {"unresolved_non_path_refs": plan["unresolved_non_path_refs"]},
                )
            except Exception as exc:
                _record_comment(conn, task_id, f"non-path event record failed: {exc}")
                return False

        if plan.get("ambiguous_refs"):
            _record_comment(conn, task_id, f"ambiguous references: {plan['ambiguous_refs']}")
            return False

        if plan.get("unresolved_refs"):
            _record_comment(conn, task_id, f"unresolved references: {plan['unresolved_refs']}")
            return False

        if not plan["files"]:
            _record_comment(conn, task_id, "empty preservation plan")
            return False

        # 6. Zero independent initial attachments -> capture durably outside workspace and RETAIN
        if independent_count == 0:
            try:
                preserve_bundle(conn, task_id, workspace)
            except Exception as exc:
                _record_comment(conn, task_id, str(exc))
                return False
            _record_comment(
                conn,
                task_id,
                "retaining workspace: task began with zero initial attachments (separate authorized retry required)",
            )
            return False

        # 7. Independent initial attachments exist: verify exact bundle provenance and integrity
        all_filenames = [a.filename for a in initial_attachments]
        if len(all_filenames) != len(set(all_filenames)):
            _record_comment(conn, task_id, "duplicate attachment filenames detected")
            return False

        man_atts = [a for a in capture_attachments if a.filename.endswith("-manifest.json")]
        arc_atts = [a for a in capture_attachments if a.filename.endswith(".tgz")]

        if not man_atts or not arc_atts or len(man_atts) != len(arc_atts):
            _record_comment(conn, task_id, "preexisting attachments do not match bundle manifest")
            return False

        dest_dir = kb.task_attachments_dir(task_id)
        for att_row in initial_attachments:
            try:
                _, _, att_st = _validate_safe_path_in_root(
                    dest_dir, att_row.stored_path, allow_root=False, require_regular=True
                )
                if att_row.size is not None and int(att_row.size) != att_st.st_size:
                    _record_comment(conn, task_id, f"attachment size mismatch: {att_row.filename}")
                    return False
            except Exception as exc:
                _record_comment(conn, task_id, f"attachment validation failed: {exc}")
                return False

        latest_arc = arc_atts[-1]
        latest_man = man_atts[-1]

        try:
            _verify_bundle_artifacts(conn, task_id, latest_arc, latest_man, plan)
        except Exception as exc:
            _record_comment(conn, task_id, f"bundle verification failed: {exc}")
            return False

        # 8. Re-verify workspace inventory immediately before authorizing deletion
        try:
            plan_recheck = plan_bundle(conn, task_id, workspace)
            if (
                plan_recheck["files"] != plan["files"]
                or plan_recheck["directories"] != plan["directories"]
                or plan_recheck["ambiguous_refs"]
                or plan_recheck["unresolved_refs"]
            ):
                _record_comment(conn, task_id, "workspace inventory changed during verification")
                return False
        except Exception as exc:
            _record_comment(conn, task_id, f"pre-deletion revalidation failed: {exc}")
            return False

        return True

    finally:
        if close_conn:
            conn.close()


def _record_comment(conn: Optional[sqlite3.Connection], task_id: str, body: str) -> None:
    """Best-effort comment recording for retention reasons."""
    if conn is None or conn.in_transaction:
        return
    try:
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
            (task_id, "preservation_gate", body, now),
        )
        conn.commit()
    except Exception:
        pass
