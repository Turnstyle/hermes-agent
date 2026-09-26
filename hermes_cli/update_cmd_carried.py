"""``updates.carried_commits_policy``: refuse an update that would reset away commits carried on the
branch it updates.

Field incident (2026-09-25): the desktop hand-off updated an install whose ``main`` carried 13 local
commits; the same-branch reset parked them behind a rescue ref, the gateway restarted on code without
them, and the update exited 0. The guard fails closed: anything short of a readable ``reset`` refuses.
"""

import subprocess
from pathlib import Path
from typing import Optional

from hermes_cli.update_cmd_git import _GIT_TEXT_KW


def _carried_commits_policy() -> str:
    """``reset`` only from a config.yaml this run read successfully; ``unreadable`` when the read failed
    (the loader then serves defaults or the last good copy, and neither may authorize dropping
    commits); otherwise ``refuse``."""
    from hermes_cli.config import load_config
    from hermes_cli.config_read_errors import FailedConfigRead
    try:
        config = load_config()
    except Exception:
        return "unreadable"
    if isinstance(config, FailedConfigRead):
        return "unreadable"
    section = config.get("updates")
    policy = section.get("carried_commits_policy") if isinstance(section, dict) else None
    return "reset" if str(policy).strip().lower() == "reset" else "refuse"


def _git(git_cmd: list[str], cwd: Path, *args: str, stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(git_cmd + list(args), cwd=cwd, input=stdin, **_GIT_TEXT_KW)


def _target_history(git_cmd: list[str], cwd: Path, target_ref: str) -> list[str]:
    """Every value *target_ref* has had, as far as its reflog knows: each entry's new value plus the
    oldest entry's old value (``clone`` logs no entry for the value it creates)."""
    logged = _git(git_cmd, cwd, "reflog", "show", "--format=%H", target_ref, "--")
    values = logged.stdout.split() if logged.returncode == 0 else []
    oldest = _git(git_cmd, cwd, "rev-parse", "--verify", "--quiet", f"{target_ref}@{{{len(values)}}}")
    if oldest.returncode == 0 and oldest.stdout.strip().strip("0"):
        values.append(oldest.stdout.strip())
    return values


def _carried_commits(git_cmd: list[str], cwd: Path, branch_ref: str, target_ref: str) -> Optional[list[str]]:
    """Commits on *branch_ref* that *target_ref* never pointed at or reached, newest first; None when
    Git cannot list them. No merge-base: an orphan history is carried in full. Earlier values of
    *target_ref* count as origin's own history, so neither an upstream force-push nor a shallow graft
    (a depth-1 ``hermes update --check`` fetch hides that origin/main descends from HEAD) reads as
    local work."""
    negated = "".join(f"^{rev}\n" for rev in (target_ref, *_target_history(git_cmd, cwd, target_ref)))
    listed = _git(git_cmd, cwd, "rev-list", "--ignore-missing", "--stdin", branch_ref, stdin=negated)
    return listed.stdout.split() if listed.returncode == 0 else None
