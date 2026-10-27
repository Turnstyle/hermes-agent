"""Task workspace lifecycle: scratch/dir/worktree resolution (incl. git worktree creation), post-completion cleanup with containment guards, worker tmux teardown and the first-use scratch-workspace tip.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Optional
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task

_REMOVABLE_KINDS = ("scratch", "worktree")

_TERMINAL_TARGET_STATUSES = frozenset({"done", "archived"})
_TERMINAL_CHILD_STATUSES = frozenset({"done", "archived", "failed", "cancelled"})
_TERMINAL_RUN_STATUSES = frozenset({
    "done",
    "completed",
    "archived",
    "failed",
    "cancelled",
    "timed_out",
    "crashed",
    "gave_up",
    "rate_limited",
    "reclaimed",
    "blocked",
    "changes_requested",
})

# Statuses after which a child no longer needs its parent's workspace artifacts.
_ACTIVE_CHILDREN_SQL = (
    "SELECT 1 FROM task_links l "
    "JOIN tasks t ON t.id = l.child_id "
    "WHERE l.parent_id = ? AND t.status NOT IN ('done', 'archived', 'failed', 'cancelled') "
    "LIMIT 1"
)

_WORKSPACE_ROW_SQL = "SELECT status, workspace_kind, workspace_path, branch_name FROM tasks WHERE id = ?"


def _git(repo_root: Path, *args: str, timeout: int) -> subprocess.CompletedProcess:
    """``git -C repo_root args``; never raises on a non-zero exit."""
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True, encoding='utf-8', errors='replace',
        timeout=timeout,
        check=False,
    )


def _is_delegated_child_context() -> bool:
    """True when running inside a delegated child worker context that must not destroy parent/board state."""
    val = os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _task_has_active_run(conn: sqlite3.Connection, task_id: str) -> bool:
    """Return True if *task_id* has any active/non-terminal run in ``task_runs``."""
    rows = conn.execute(
        "SELECT status, outcome, ended_at, worker_pid FROM task_runs WHERE task_id = ?",
        (task_id,),
    ).fetchall()
    for r in rows:
        if r["worker_pid"] is not None:
            return True
        st = (r["status"] or "").strip().lower()
        if st not in _TERMINAL_RUN_STATUSES:
            return True
        if r["ended_at"] is None and not (r["outcome"] or "").strip():
            return True
    return False


def _has_active_children(conn: sqlite3.Connection, task_id: str) -> bool:
    """Return True if *task_id* has any direct, transitive, or self descendant that is
    non-terminal, has an active run/worker, has an unknown status, is missing, or forms a cycle,
    or if querying descendant state fails. Fails closed (returns True) on any error.
    """
    try:
        if not task_id or not isinstance(task_id, str):
            return True
        # 3-color DFS over task_links: 1 = visiting (on current recursion path), 2 = verified clean
        color: dict[str, int] = {task_id: 1}

        def _visit(parent: str) -> bool:
            link_rows = conn.execute(
                "SELECT child_id FROM task_links WHERE parent_id = ? ORDER BY child_id",
                (parent,),
            ).fetchall()
            for lr in link_rows:
                cid = lr["child_id"]
                if not cid or not isinstance(cid, str):
                    return True
                if cid == parent or color.get(cid) == 1:
                    # Self-descendant or cycle in task_links -> fail closed
                    return True
                if color.get(cid) == 2:
                    continue
                t_row = conn.execute(
                    "SELECT status, worker_pid, current_run_id FROM tasks WHERE id = ?",
                    (cid,),
                ).fetchone()
                if t_row is None:
                    return True
                st = (t_row["status"] or "").strip().lower()
                if st not in _TERMINAL_CHILD_STATUSES:
                    return True
                if t_row["worker_pid"] is not None or t_row["current_run_id"] is not None:
                    return True
                if _task_has_active_run(conn, cid):
                    return True
                color[cid] = 1
                if _visit(cid):
                    return True
                color[cid] = 2
            return False

        return _visit(task_id)
    except Exception:
        return True


def _is_task_cleanup_fenced(conn: sqlite3.Connection, task_id: str) -> bool:
    """Return True if *task_id* must be fenced from workspace preservation/destruction."""
    try:
        if _is_delegated_child_context():
            return True
        if not task_id or not isinstance(task_id, str):
            return True
        t_row = conn.execute(
            "SELECT status, worker_pid, current_run_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if t_row is None:
            return True
        st = (t_row["status"] or "").strip().lower()
        if st not in _TERMINAL_TARGET_STATUSES:
            return True
        if t_row["worker_pid"] is not None or t_row["current_run_id"] is not None:
            return True
        if _task_has_active_run(conn, task_id):
            return True
        if _has_active_children(conn, task_id):
            return True
        return False
    except Exception:
        return True


def _record_deferred_shared_event(
    conn: sqlite3.Connection, task_id: str, reason: str, wp: Path
) -> None:
    try:
        with _kb.write_txn(conn):
            _kb._append_event(
                conn,
                task_id,
                "workspace_cleanup_deferred_shared",
                {"reason": reason, "workspace_path": str(wp)},
            )
    except Exception:
        pass


def _is_shared_scratch_workspace_in_use(
    conn: sqlite3.Connection, task_id: str, wp: Path
) -> bool:
    """Return True (and emit ``workspace_cleanup_deferred_shared``) when a live task on
    this board or another board shares *wp*, or when board enumeration/querying fails.
    """
    try:
        wp_resolved = wp.expanduser().resolve(strict=False)
    except Exception:
        _record_deferred_shared_event(conn, task_id, "unknown", wp)
        return True

    def _paths_overlap(other_raw: Optional[str]) -> bool:
        if not other_raw:
            return False
        try:
            other_p = Path(other_raw).expanduser().resolve(strict=False)
            return (
                other_p == wp_resolved
                or other_p.is_relative_to(wp_resolved)
                or wp_resolved.is_relative_to(other_p)
            )
        except Exception:
            return True

    try:
        # 1. Check live tasks on the current connection's board
        live_rows = conn.execute(
            "SELECT id, status, workspace_path FROM tasks WHERE id != ? AND workspace_path IS NOT NULL",
            (task_id,),
        ).fetchall()
        for r in live_rows:
            st = (r["status"] or "").strip().lower()
            if st not in _TERMINAL_CHILD_STATUSES and _paths_overlap(r["workspace_path"]):
                _record_deferred_shared_event(conn, task_id, "shared", wp)
                return True

        # 2. Check other boards under kanban_home()/kanban/boards (and default board if needed)
        cur_db_file = ""
        with contextlib.suppress(Exception):
            db_list = conn.execute("PRAGMA database_list").fetchall()
            if db_list:
                cur_db_file = str(Path(db_list[0][2]).resolve(strict=False)) if db_list[0][2] else ""

        home = _kb.kanban_home()
        other_db_paths: list[Path] = []
        default_db = home / "kanban.db"
        if default_db.exists():
            other_db_paths.append(default_db)
        boards_dir = home / "kanban" / "boards"
        if boards_dir.exists():
            for entry in boards_dir.iterdir():
                if entry.is_dir():
                    bdb = entry / "kanban.db"
                    if bdb.exists():
                        other_db_paths.append(bdb)

        from hermes_cli import kanban_db_connect as kbc

        for bdb in other_db_paths:
            if cur_db_file and str(bdb.resolve(strict=False)) == cur_db_file:
                continue
            with kbc.connect_closing(db_path=bdb) as other_conn:
                other_rows = other_conn.execute(
                    "SELECT id, status, workspace_path FROM tasks WHERE workspace_path IS NOT NULL"
                ).fetchall()
                for r in other_rows:
                    st = (r["status"] or "").strip().lower()
                    if st not in _TERMINAL_CHILD_STATUSES and _paths_overlap(r["workspace_path"]):
                        _record_deferred_shared_event(conn, task_id, "shared", wp)
                        return True
    except Exception:
        _record_deferred_shared_event(conn, task_id, "unknown", wp)
        return True

    return False


def _managed_scratch_path_info(p: Path) -> tuple[bool, Optional[str]]:
    """Return whether *p* is managed scratch storage and the matching board."""
    try:
        p_abs = p.resolve(strict=False)
    except OSError:
        return False, None
    roots: list[tuple[Path, Optional[str]]] = []
    override = os.environ.get("HERMES_KANBAN_WORKSPACES_ROOT", "").strip()
    if override:
        with contextlib.suppress(OSError):
            roots.append((Path(override).expanduser().resolve(strict=False), None))
    try:
        home = _kb.kanban_home()
    except OSError:
        home = None
    if home is not None:
        with contextlib.suppress(OSError):
            roots.append(((home / "kanban" / "workspaces").resolve(strict=False), _kb.DEFAULT_BOARD))
        entries: list[Path] = []
        with contextlib.suppress(OSError):
            entries = list((home / "kanban" / "boards").resolve(strict=False).iterdir())
        for entry in entries:
            with contextlib.suppress(OSError):
                if entry.is_dir():
                    roots.append(((entry / "workspaces").resolve(strict=False), entry.name))
    for root, board in roots:
        if p_abs == root:
            continue
        try:
            if p_abs.is_relative_to(root):
                return True, board
        except ValueError:
            continue
    return False, None


def _scratch_workspace(conn: sqlite3.Connection, task_id: str) -> Optional[Path]:
    """Expanded ``workspace_path`` when the task uses a scratch workspace, else ``None``."""
    row = conn.execute(
        "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row or row["workspace_kind"] != "scratch" or not row["workspace_path"]:
        return None
    return Path(row["workspace_path"]).expanduser()


def _is_managed_scratch_path(p: Path) -> bool:
    """True iff *p* is a STRICT descendant of a kanban-managed ``workspaces/``
    root (``HERMES_KANBAN_WORKSPACES_ROOT``, ``<kanban_home>/kanban/workspaces``,
    or ``<kanban_home>/kanban/boards/<slug>/workspaces``). A path equal to a
    root is not managed (deleting it would wipe every task's scratch dir);
    ``<kanban_home>/kanban``, ``.../logs`` and ``.../boards/<slug>`` hold
    Hermes' own DB and metadata. :func:`_cleanup_workspace` refuses
    ``rmtree`` outside managed storage — a board ``default_workdir`` on a real
    source tree paired with ``workspace_kind='scratch'`` would otherwise make
    task completion delete user data.

    See #28818.
    """
    return _managed_scratch_path_info(p)[0]


def _cleanup_workspace(conn: sqlite3.Connection, task_id: str) -> None:
    """Remove a task's scratch workspace dir or clean linked worktree when eligible.
    Called from :func:`complete_task` / :func:`archive_task` after the transaction commits;
    best-effort so cleanup never blocks completion. ``scratch`` is removed only when all
    fences and ``preservation_gate`` pass; ``worktree`` only when provably free of work
    and ``preservation_gate`` passes; ``dir`` is intentionally preserved. Never kills
    worker tmux sessions or releases runtime/LSP resources during cleanup."""
    try:
        if _is_delegated_child_context():
            return
        row = conn.execute(_WORKSPACE_ROW_SQL, (task_id,)).fetchone()
        if not row:
            return
        kind: Optional[str] = row["workspace_kind"]
        path: Optional[str] = row["workspace_path"]
        if kind not in _REMOVABLE_KINDS or not path:
            # Not removable itself, but completing may still unblock a deferred
            # parent scratch cleanup (e.g. a 'dir' child of a scratch parent).
            # See #33774.
            _try_cleanup_parent_workspaces(conn, task_id)
            return
        # Defer while task is non-terminal or any direct/transitive descendant is active/indeterminate.
        if _is_task_cleanup_fenced(conn, task_id):
            _kb._log.debug(
                "Deferring %s workspace cleanup for task %s: "
                "active/indeterminate descendants or runs still need workspace at %s",
                kind, task_id, path,
            )
            return
        if kind == "worktree":
            _cleanup_worktree_workspace(task_id, path, row["branch_name"], conn=conn)
            _try_cleanup_parent_workspaces(conn, task_id)
            return
        wp = Path(path)
        if wp.is_dir():
            # Containment guard: a board's ``default_workdir`` can pair
            # ``workspace_kind='scratch'`` with a user path pointing at a real
            # source tree; without this, completion would rmtree the user's data.
            # See #28818.
            if _is_managed_scratch_path(wp):
                if _is_shared_scratch_workspace_in_use(conn, task_id, wp):
                    return
                from hermes_cli import kanban_bundle_preserve as bp
                if not bp.preservation_gate(task_id, wp, conn=conn):
                    _kb._log.info("Preserving workspace for task %s: preservation gate held", task_id)
                    return
                shutil.rmtree(wp, ignore_errors=True)
                _kb._log.debug("Removed scratch workspace: %s", wp)
            else:
                _kb._log.warning(
                    "Refusing to remove out-of-scratch workspace for task %s: %s "
                    "(workspace_kind='scratch' but path is outside any "
                    "kanban-managed workspaces root)",
                    task_id, wp,
                )
        # After cleaning up this task's workspace, check if any parent tasks now have all children done —
        # their deferred cleanup can proceed (#33774).
        _try_cleanup_parent_workspaces(conn, task_id)
    except Exception:
        pass  # best-effort — never block completion


def _cleanup_worktree_workspace(
    task_id: str, path: str, branch_name: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Remove a finished task's linked git worktree when it holds no work.
    Mirrors the CLI startup pruner (``cli._prune_stale_worktrees``): removal
    requires a clean tree AND every commit reachable from a remote-tracking
    ref; any doubt (dirty, unpushed, unresolvable repo, failing git) preserves
    it. The auto-generated ``wt/<task-id>`` branch is deleted with it; custom
    branches are kept. Best-effort."""
    if _is_delegated_child_context():
        return
    try:
        from hermes_cli.worktree_ops import _worktree_has_unpushed_commits, _worktree_is_dirty
    except Exception:
        return  # CLI safety predicates unavailable — preserve
    try:
        if conn is not None:
            if _is_task_cleanup_fenced(conn, task_id):
                return
        else:
            from hermes_cli import kanban_db_connect as kbc

            db_path = _kb.kanban_db_path()
            if db_path.exists():
                with kbc.connect_closing(db_path=db_path) as tmp_conn:
                    has_record = (
                        tmp_conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
                        or tmp_conn.execute("SELECT 1 FROM task_links WHERE parent_id = ?", (task_id,)).fetchone()
                        or tmp_conn.execute("SELECT 1 FROM task_runs WHERE task_id = ?", (task_id,)).fetchone()
                    )
                    if has_record and _is_task_cleanup_fenced(tmp_conn, task_id):
                        return

        wp = Path(path).expanduser()
        if not wp.is_dir():
            return
        common = _git_common_dir(wp)
        if common is None or common.name != ".git":
            return  # not a linked worktree of a normal repo — never guess
        repo_root = common.parent
        wp_resolved = wp.resolve(strict=False)
        if wp_resolved == repo_root.resolve(strict=False):
            return  # never remove the main checkout
        if _worktree_is_dirty(str(wp)) or _worktree_has_unpushed_commits(str(wp)):
            _kb._log.info(
                "Preserving worktree for task %s: dirty or unpushed work at %s",
                task_id, wp,
            )
            return
        from hermes_cli import kanban_bundle_preserve as bp
        if not bp.preservation_gate(task_id, wp, conn=conn):
            _kb._log.info("Preserving worktree for task %s: preservation gate held", task_id)
            return
        try:
            cwd = Path.cwd().resolve(strict=False)
            if cwd == wp_resolved or cwd.is_relative_to(wp_resolved):
                os.chdir(repo_root)
        except OSError:
            with contextlib.suppress(OSError):
                os.chdir(repo_root)
        # No --force: git's own dirty guard re-verifies at removal time, so if
        # the tree became dirty since our check (TOCTOU) removal fails safe.
        result = _git(repo_root, "worktree", "remove", str(wp), timeout=60)
        if result.returncode != 0:
            time.sleep(0.05)
            result = _git(repo_root, "worktree", "remove", str(wp), timeout=60)
        if result.returncode != 0:
            _kb._log.warning(
                "git worktree remove failed for task %s at %s: %s",
                task_id, wp, (result.stderr or result.stdout or "").strip(),
            )
            return
        _kb._log.debug("Removed worktree workspace: %s", wp)
        branch = (branch_name or "").strip() or f"wt/{task_id}"
        if branch.startswith("wt/"):
            _git(repo_root, "branch", "-D", branch, timeout=30)
    except Exception:
        pass  # best-effort — never block completion


def _try_cleanup_parent_workspaces(conn: sqlite3.Connection, task_id: str) -> None:
    """Run the deferred cleanup of any parent scratch/worktree workspace whose
    children are now all done/archived/failed/cancelled (called after each
    child completes).

    See #33774 and #133501.
    """
    try:
        if _is_delegated_child_context():
            return
        parents = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?",
            (task_id,),
        ).fetchall()
        for (parent_id,) in parents:
            row = conn.execute(_WORKSPACE_ROW_SQL, (parent_id,)).fetchone()
            if (
                not row
                or row["workspace_kind"] not in _REMOVABLE_KINDS
                or not row["workspace_path"]
                or _is_task_cleanup_fenced(conn, parent_id)
            ):
                continue
            if row["workspace_kind"] == "worktree":
                _cleanup_worktree_workspace(parent_id, row["workspace_path"], row["branch_name"], conn=conn)
                continue
            wp = Path(row["workspace_path"])
            if wp.is_dir() and _is_managed_scratch_path(wp):
                if _is_shared_scratch_workspace_in_use(conn, parent_id, wp):
                    continue
                from hermes_cli import kanban_bundle_preserve as bp
                if not bp.preservation_gate(parent_id, wp, conn=conn):
                    _kb._log.info("Preserving parent workspace for task %s: preservation gate held", parent_id)
                    continue
                shutil.rmtree(wp, ignore_errors=True)
                _kb._log.debug("Deferred cleanup: removed parent %s scratch workspace: %s", parent_id, wp)
    except Exception:
        pass  # best-effort


def _cleanup_worker_tmux(conn: sqlite3.Connection, task_id: str) -> None:
    """Kill the tmux session associated with a task's assignee, if dead."""
    try:
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row or not row["assignee"]:
            return
        # Workers named swarm1-12 use tmux sessions named swarm-swarm1 etc.
        session = f"swarm-{row['assignee']}"
        out = subprocess.run(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_dead}"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5,
        )
        if out.stdout.strip() == "1":
            subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True, timeout=5)
            _kb._log.debug("Killed stale tmux session: %s", session)
    except Exception:
        pass  # best-effort — never block completion


_SCRATCH_TIP_SENTINEL_NAME = ".scratch_tip_shown"


_SCRATCH_TIP_MESSAGE = (
    "scratch workspaces are ephemeral — they're deleted when the task "
    "completes. Use --workspace worktree: (git worktree) or "
    "--workspace dir:/abs/path (existing dir) to preserve worker output."
)


def _scratch_tip_sentinel_path() -> Path:
    """Path to the per-install scratch-workspace-tip sentinel file."""
    return _kb.kanban_home() / _SCRATCH_TIP_SENTINEL_NAME


def _scratch_tip_shown() -> bool:
    """True iff the scratch-workspace tip was already emitted on this install.
    Best-effort — any error re-emits, the safer failure mode for a help message."""
    try:
        return _scratch_tip_sentinel_path().exists()
    except OSError:
        return False


def _mark_scratch_tip_shown() -> None:
    """Touch the sentinel so future scratch workspaces stay silent. Best-effort:
    a failure means the tip may appear once more, preferable to crashing dispatch."""
    try:
        path = _scratch_tip_sentinel_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    except OSError:
        pass


def _maybe_emit_scratch_tip(
    conn: sqlite3.Connection,
    task_id: str,
    workspace_kind: Optional[str],
) -> None:
    """Emit the first-use scratch-workspace tip once per install, right after a
    scratch workspace is materialized. No-op for ``worktree``/``dir`` (preserved
    by design) and once the sentinel exists."""
    if (workspace_kind or "scratch") != "scratch" or _scratch_tip_shown():
        return
    try:
        _kb._log.warning("kanban: %s (task %s)", _SCRATCH_TIP_MESSAGE, task_id)
        with _kb.write_txn(conn):
            _kb._append_event(
                conn, task_id, "tip_scratch_workspace",
                {"message": _SCRATCH_TIP_MESSAGE},
            )
    except Exception:
        # Best-effort — never block the spawn loop over a help message.
        pass
    finally:
        _mark_scratch_tip_shown()


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------

def _git_toplevel(path: Path) -> Optional[Path]:
    """Return the git toplevel containing ``path``, or ``None`` if not in a repo."""
    out = _kb._git_out(path, "rev-parse", "--show-toplevel")
    if out is None:
        return None
    try:
        return Path(out).expanduser().resolve()
    except Exception:
        return Path(out).expanduser()


def _git_branch_exists(repo_root: Path, branch_name: str) -> bool:
    try:
        result = _git(repo_root, "show-ref", "--verify", f"refs/heads/{branch_name}", timeout=30)
    except Exception:
        return False
    return result.returncode == 0


def _git_abs_path(path: Path, flag: str) -> Optional[Path]:
    out = _kb._git_out(path, "rev-parse", "--path-format=absolute", flag)
    return Path(out).expanduser().resolve(strict=False) if out else None


def _git_common_dir(path: Path) -> Optional[Path]:
    return _git_abs_path(path, "--git-common-dir")


def _git_dir(path: Path) -> Optional[Path]:
    return _git_abs_path(path, "--git-dir")


def _git_current_branch(path: Path) -> Optional[str]:
    return _kb._git_out(path, "branch", "--show-current")


def _is_linked_worktree_checkout(path: Path) -> bool:
    git_dir = _git_dir(path)
    common_dir = _git_common_dir(path)
    return git_dir is not None and common_dir is not None and git_dir != common_dir


def _nearest_existing_path(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _repo_root_for_worktree_target(path: Path) -> Optional[Path]:
    current = _nearest_existing_path(path).resolve(strict=False)
    while True:
        repo_root = _git_toplevel(current)
        if repo_root is not None:
            return repo_root
        if current == current.parent:
            return None
        current = current.parent


def _ensure_git_worktree(repo_root: Path, target: Path, branch_name: str) -> None:
    """Materialize ``target`` as a linked git worktree under ``repo_root``."""
    target = target.expanduser()
    repo_common = _git_common_dir(repo_root)
    if target.exists() and repo_common is not None and _git_common_dir(target) == repo_common:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if _git_branch_exists(repo_root, branch_name):
        args = ["worktree", "add", str(target), branch_name]
    else:
        args = ["worktree", "add", "-b", branch_name, str(target), "HEAD"]
    result = _git(repo_root, *args, timeout=60)
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"git worktree add failed for {target} on branch {branch_name}: {stderr}"
        )


def _anchored_worktree(repo_root: Path, task_id: str, branch_name: str) -> tuple[Path, str]:
    """Materialize the canonical ``<repo>/.worktrees/<task-id>`` worktree."""
    target = repo_root / ".worktrees" / task_id
    _ensure_git_worktree(repo_root, target, branch_name)
    return target, branch_name


def _resolve_worktree_workspace(task: Task, *, board: Optional[str] = None) -> tuple[Path, str]:
    """Resolve + materialize a linked git worktree for ``task``. With no
    ``task.workspace_path`` the anchor is the board's ``default_workdir`` so
    every worktree lands under a board-owned repo (``<repo>/.worktrees/<id>``)
    instead of the dispatcher's incidental CWD (whatever dir the gateway was
    launched from); with no anchor configured we fail loudly rather than guess."""
    branch_name = (task.branch_name or "").strip() or f"wt/{task.id}"
    if not task.workspace_path:
        board_slug = board if board else _kb.get_current_board()
        board_default = (_kb.read_board_metadata(board_slug).get("default_workdir") or "").strip()
        if not board_default:
            raise ValueError(
                f"task {task.id} has workspace_kind=worktree but no workspace_path, "
                f"and board {board_slug!r} has no default_workdir set. Set a board "
                "default workdir (a git repo) or create the task with "
                "--workspace worktree:<absolute-repo-path>."
            )
        anchor = Path(board_default).expanduser()
        if not anchor.is_absolute():
            raise ValueError(
                f"board {board_slug!r} default_workdir {board_default!r} is not "
                "absolute; use an absolute path to a git repo"
            )
        repo_root = _git_toplevel(anchor)
        if repo_root is None:
            raise ValueError(
                f"task {task.id} has workspace_kind=worktree but board "
                f"{board_slug!r} default_workdir {board_default!r} is not inside a git repo"
            )
        return _anchored_worktree(repo_root, task.id, branch_name)

    requested = Path(task.workspace_path).expanduser()
    if not requested.is_absolute():
        raise ValueError(
            f"task {task.id} has non-absolute worktree path "
            f"{task.workspace_path!r}; use an absolute path"
        )
    requested_resolved = requested.resolve(strict=False)

    if requested.exists() and _is_linked_worktree_checkout(requested):
        actual_branch = _git_current_branch(requested)
        if actual_branch == branch_name:
            return requested_resolved, actual_branch
        # The requested path is an existing checkout of a DIFFERENT task's
        # branch (decompose children inherit the root's workspace_path
        # verbatim, so siblings all point here). Reusing it would run this task
        # on the other task's branch — silent cross-task provenance corruption,
        # unsafe under concurrency — so fall back to our own worktree.
        fallback_root = _repo_root_for_worktree_target(requested.parent)
        if fallback_root is not None:
            fallback = fallback_root / ".worktrees" / task.id
            if fallback.resolve(strict=False) != requested_resolved:
                _ensure_git_worktree(fallback_root, fallback, branch_name)
                return fallback.resolve(strict=False), branch_name
        # No repo to anchor a fallback on (or the occupied path IS this task's
        # own canonical worktree): keep the legacy reuse rather than fail dispatch.
        return requested_resolved, actual_branch or branch_name

    repo_root = _git_toplevel(requested)
    if repo_root is not None and requested_resolved == repo_root:
        return _anchored_worktree(repo_root, task.id, branch_name)

    repo_root = _repo_root_for_worktree_target(requested.parent)
    if repo_root is None:
        raise ValueError(
            f"task {task.id} worktree path {task.workspace_path!r} is not inside a git repo "
            "and does not point at a git repo root"
        )
    _ensure_git_worktree(repo_root, requested, branch_name)
    return requested, branch_name


def resolve_workspace(task: Task, *, board: Optional[str] = None) -> Path:
    """Resolve (and create if needed) the workspace for a task.

    ``scratch``: ``<board-root>/workspaces/<id>/`` — path-stable across the
    dispatcher and every profile worker. ``dir``: ``workspace_path``, created
    if missing; MUST be absolute (relative paths would resolve against the
    dispatcher's CWD — confused-deputy traversal). ``worktree``: a linked git
    worktree; a repo-root ``workspace_path`` anchors ``<repo>/.worktrees/<id>``,
    a concrete path is created/reused, none -> the board's ``default_workdir``
    (raises if unset rather than guessing). Persist via ``set_workspace_path``.
    """
    kind = task.workspace_kind or "scratch"
    if kind == "worktree":
        return _resolve_worktree_workspace(task, board=board)[0]
    if kind == "scratch" and not task.workspace_path:
        p = _kb.workspaces_root(board=board) / task.id
    elif kind == "scratch":
        # Legacy explicit-path scratch tasks get the same absolute-path guard
        # as dir: — same threat model.
        p = Path(task.workspace_path).expanduser()
        if not p.is_absolute():
            raise ValueError(
                f"task {task.id} has non-absolute workspace_path "
                f"{task.workspace_path!r}; workspace paths must be absolute"
            )
    elif kind == "dir":
        if not task.workspace_path:
            raise ValueError(f"task {task.id} has workspace_kind=dir but no workspace_path")
        p = Path(task.workspace_path).expanduser()
        if not p.is_absolute():
            raise ValueError(
                f"task {task.id} has non-absolute workspace_path "
                f"{task.workspace_path!r}; use an absolute path "
                f"(relative paths are ambiguous against the dispatcher's CWD)"
            )
    else:
        raise ValueError(f"unknown workspace_kind: {kind}")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _set_task_column(conn: sqlite3.Connection, task_id: str, column: str, value: str) -> None:
    with _kb.write_txn(conn):
        conn.execute(f"UPDATE tasks SET {column} = ? WHERE id = ?", (value, task_id))


def set_workspace_path(conn: sqlite3.Connection, task_id: str, path: Path | str) -> None:
    _set_task_column(conn, task_id, "workspace_path", str(path))


def set_branch_name(conn: sqlite3.Connection, task_id: str, branch_name: str) -> None:
    _set_task_column(conn, task_id, "branch_name", str(branch_name))


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb  # noqa: E402
