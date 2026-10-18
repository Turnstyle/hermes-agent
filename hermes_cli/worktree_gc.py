"""Local worktree/branch audit with compatibility reclaim entry points.

Every reclaim request retains its objects while destructive authority is OFF.
Ages and old classification records cannot enable removal. Audit entry points
do not deepen history, contact remotes, copy files or save merge caches.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Branches never considered for deletion, in any mode.
_PROTECTED_BRANCHES = {"main", "master", "develop", "dev", "trunk"}

# Trees owned by another lifecycle (kanban dispatcher gc) — never touched.
_KANBAN_RE = re.compile(r"^t_[0-9a-f]+$")

# Bounded cherry probe: a branch this far ahead of upstream is a stale-base
# lane, not merged scratch; checking it is expensive and it stays preserved.
_MAX_CHERRY_AHEAD = 50


@dataclass
class TreeRecord:
    name: str
    path: str
    branch: str
    age_days: float
    size_mb: Optional[int]
    verdict: str          # reap | reap-archive | keep
    reason: str
    untracked: list[str] = field(default_factory=list)


@dataclass
class BranchRecord:
    name: str
    verdict: str          # delete | keep
    reason: str


def _run(cmd: list, timeout: int, cwd: Optional[str] = None,
         env: Optional[dict] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, cwd=cwd, env=env, stdin=subprocess.DEVNULL, check=False)


@dataclass
class ExternalTreeRecord:
    """A linked worktree registered on the repo but living OUTSIDE
    ``.worktrees/`` — created by hand or by another tool. Reported for
    visibility only; the reclaim paths never touch these."""

    path: str
    branch: str           # branch name, or "detached @<sha>" when detached
    locked: bool
    missing: bool         # registered but the directory no longer exists


def _git(args: list, cwd: str, timeout: int = 15) -> subprocess.CompletedProcess:
    """Run git, translating timeouts into returncode 124. Every verdict fails safe toward "keep"
    on nonzero, so a slow ``git cherry`` on a huge repo degrades to keep instead of aborting the
    audit mid-list. :func:`noninteractive_repo_git_env` because ``status`` executes the repo's
    ``core.fsmonitor`` and clean filters (GHSA-7x36-8jrh-v4pw)."""
    from hermes_cli._subprocess_compat import FILTER_DISCOVERY_FAILED, noninteractive_repo_git_env
    env = noninteractive_repo_git_env(cwd)
    if env is None:
        return subprocess.CompletedProcess(args=["git", *args], returncode=1, stdout="",
                                           stderr=FILTER_DISCOVERY_FAILED)
    try:
        return _run(["git", *args], timeout, cwd, env=env)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args=["git", *args], returncode=124, stdout="",
                                           stderr=f"timeout after {timeout}s")


def _tree_size_mb(path: Path, timeout: int = 30) -> Optional[int]:
    """Cheap directory size via ``du -sm`` — best-effort, None on failure."""
    try:
        result = _run(["du", "-sm", str(path)], timeout)
        return int(result.stdout.split()[0]) if result.returncode == 0 and result.stdout.strip() else None
    except Exception:
        return None


def _dirty_split(path: str) -> tuple[bool, list[str]]:
    """(has_tracked_modifications, untracked_paths) — tracked = real work, untracked = archivable."""
    try:
        result = _git(["status", "--porcelain"], cwd=path, timeout=10)
        if result.returncode != 0:
            return True, []  # fail safe: treat as real work
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        untracked = [line[3:].strip() for line in lines if line.startswith("??")]
        return len(untracked) != len(lines), untracked
    except Exception:
        return True, []


def _archive_untracked(tree: Path, untracked: list[str]) -> Optional[Path]:
    """Copy untracked files out of a doomed tree; None on any failure (caller must then keep)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    from hermes_constants import get_hermes_home
    dest = get_hermes_home() / "archive" / "worktree-prune" / f"{tree.name}-{stamp}"
    try:
        for rel in untracked:
            src = tree / rel
            if not src.exists() or src.is_symlink():
                continue
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dest / rel, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dest / rel)
        return dest if dest.exists() else None
    except Exception as exc:
        logger.warning("Could not archive untracked files from %s: %s", tree, exc)
        return None


def _classify_tree(_ops, repo_root: str, entry: Path, merge_cache, remote_heads) -> tuple[str, str, list[str]]:
    """Return (verdict, reason, untracked) for one tree under ``.worktrees/``."""
    path = str(entry)
    if _KANBAN_RE.match(entry.name):
        return "keep", "kanban task tree (owned by kanban gc)", []
    if _ops._worktree_lock_is_live(repo_root, path, timeout=5) == "live":
        return "keep", "in use by a running hermes session", []
    tracked_dirty, untracked = _dirty_split(path)
    if tracked_dirty:
        return "keep", "uncommitted tracked changes (real work)", []
    archive_note = f"{len(untracked)} untracked file(s) will be archived"
    if _ops._worktree_has_unpushed_commits(path, timeout=5) and not _ops._worktree_commits_all_merged_upstream(
        path, timeout=30, cache=merge_cache, max_ahead=_MAX_CHERRY_AHEAD):
        # Pushed-branch tier: single-branch fetch refspecs (managed-install default) leave pushed
        # PR branches with no refs/remotes/* entry, so `git log HEAD --not --remotes` reads them
        # as unpushed forever. A head EXACTLY matching the remote branch has nothing origin lacks.
        if not _ops._worktree_branch_pushed_exact(path, remote_heads, timeout=10):
            return "keep", "unpushed commits not found upstream", []
        if untracked:
            return "reap-keep-branch", f"pushed to origin (open-PR lane); branch kept; {archive_note}", untracked
        return "reap-keep-branch", "pushed to origin (open-PR lane); branch kept", []
    if untracked:
        return "reap-archive", f"merged/pushed; {archive_note}", untracked
    return "reap", "clean and fully merged/pushed", []


def audit_external_trees(repo_root: str) -> list[ExternalTreeRecord]:
    """List linked worktrees registered OUTSIDE ``.worktrees/``.

    ``hermes -w`` scratch trees all live under ``<repo>/.worktrees/``, but
    ``git worktree list --porcelain`` also knows about trees the user (or
    another tool) registered elsewhere. Those are someone else's state, so
    the reclaim paths never touch them — but hiding them entirely makes the
    audit lie about what the repo is carrying. Report them read-only, and
    flag registrations whose directory has vanished. Missing registrations
    remain recovery evidence and are never automatically pruned.
    """
    result = _git(["worktree", "list", "--porcelain"], cwd=repo_root, timeout=10)
    if result.returncode != 0:
        return []

    managed_root = os.path.realpath(str(Path(repo_root) / ".worktrees"))
    main_root = os.path.realpath(repo_root)

    records: list[ExternalTreeRecord] = []
    current: dict = {}

    def _flush():
        path = current.get("path")
        if not path:
            return
        real = os.path.realpath(path)
        if real == main_root:
            return  # the main checkout itself
        if real == managed_root or real.startswith(managed_root + os.sep):
            return  # hermes-managed scratch tree — covered by audit_worktrees
        branch = current.get("branch", "")
        if not branch and current.get("head"):
            branch = f"detached @{current['head'][:10]}"
        records.append(ExternalTreeRecord(
            path=path,
            branch=branch,
            locked=bool(current.get("locked")),
            missing=not os.path.exists(path),
        ))

    for line in result.stdout.splitlines():
        line = line.rstrip()
        if not line:
            _flush()
            current = {}
            continue
        if line.startswith("worktree "):
            current["path"] = line[len("worktree "):]
        elif line.startswith("branch refs/heads/"):
            current["branch"] = line[len("branch refs/heads/"):]
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):]
        elif line == "locked" or line.startswith("locked "):
            current["locked"] = True
    _flush()
    return records


def prune_missing_registrations(repo_root: str, *, dry_run: bool = False) -> list[str]:
    """Missing directories do not authorize removing recovery registrations."""
    return [f"retained registration {r.path} (destructive_authority_off)"
            for r in audit_external_trees(repo_root) if r.missing]


def audit_worktrees(repo_root: str, *, with_sizes: bool = True,
                    older_than_days: Optional[float] = None) -> list[TreeRecord]:
    """List local trees read-only. No remote probes, archive copies or cache writes."""
    worktrees_dir = Path(repo_root) / ".worktrees"
    if not worktrees_dir.is_dir():
        return []
    records = []
    for entry in sorted(worktrees_dir.iterdir()):
        if not entry.is_dir():
            continue
        try:
            age = (time.time() - entry.stat().st_mtime) / 86400
        except OSError:
            age = 0.0
        records.append(TreeRecord(entry.name, str(entry), "", age,
                                  _tree_size_mb(entry) if with_sizes else None,
                                  "keep", "destructive_authority_off; branch/status not inspected"))
    return records


_REAP_VERDICTS = {"reap", "reap-archive", "reap-keep-branch"}


def reclaim_worktrees(
    repo_root: str, *, dry_run: bool = False, records: Optional[list[TreeRecord]] = None
) -> list[str]:
    """Retain even caller-supplied reap records; no frozen plan is authority."""
    if records is None:
        records = audit_worktrees(repo_root, with_sizes=False)
    return [f"retained {r.name} (destructive_authority_off)" for r in records]


def audit_branches(repo_root: str) -> list[BranchRecord]:
    """List local refs without network, merge-cache writes or deletion verdicts."""
    result = _git(["branch", "--format=%(refname:short)"], cwd=repo_root, timeout=10)
    if result.returncode != 0:
        return []
    return [BranchRecord(name.strip(), "keep", "destructive_authority_off")
            for name in result.stdout.splitlines() if name.strip()]


def reclaim_branches(
    repo_root: str, *, dry_run: bool = False, records: Optional[list[BranchRecord]] = None
) -> list[str]:
    """Retain refs even when a stale caller supplies a delete verdict."""
    if records is None:
        records = audit_branches(repo_root)
    return [f"retained branch {r.name} (destructive_authority_off)" for r in records]


def worktrees_summary(repo_root: str) -> tuple[int, Optional[int]]:
    """(tree_count, total_size_mb) for the escalation notice; size is best-effort with a timeout."""
    worktrees_dir = Path(repo_root) / ".worktrees"
    if not worktrees_dir.exists():
        return 0, None
    try:
        count = sum(1 for e in worktrees_dir.iterdir() if e.is_dir())
    except Exception:
        return 0, None
    return count, _tree_size_mb(worktrees_dir, timeout=20)
