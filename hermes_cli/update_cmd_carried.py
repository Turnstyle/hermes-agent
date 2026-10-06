"""``updates.carried_commits_policy``: refuse an update that would reset away commits carried on the
branch it updates.

Field incident (2026-09-25): the desktop hand-off updated an install whose ``main`` carried 13 local
commits; the same-branch reset parked them behind a rescue ref, the gateway restarted on code without
them, and the update exited 0. The decision runs before the update touches the checkout (lockfile
cleanup, autostash, branch switch), so a refusal leaves it exactly as it was, and it fails closed:
anything short of a readable ``reset`` or a proven zero refuses.
"""

import subprocess
import sys
from pathlib import Path
from typing import Optional

from hermes_cli.update_cmd_git import (
    _BAR, _GIT_TEXT_KW, _ORPHAN_RESCUE_REF_MAX_AGE_DAYS, _branch_head_suffix, _prune_orphan_rescue_refs,
    _rescue_ref_name)


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
    local work. The branch side is strict: a tip that is not a readable commit is None (rev-list
    would skip a missing one under ``--ignore-missing`` and lists nothing for a non-commit). An
    exclusion Git cannot read is dropped instead, which can only over-count."""
    if _git(git_cmd, cwd, "cat-file", "-e", f"{branch_ref}^{{commit}}").returncode != 0:
        return None
    history = (target_ref, *_target_history(git_cmd, cwd, target_ref))
    checked = _git(git_cmd, cwd, "cat-file", "--batch-check=%(objectname) %(objecttype)",
                   stdin="".join(f"{rev}^{{commit}}\n" for rev in history))
    if checked.returncode != 0:
        return None
    negated = "".join(f"^{line.split()[0]}\n" for line in checked.stdout.splitlines() if line.endswith(" commit"))
    listed = _git(git_cmd, cwd, "rev-list", "--stdin", branch_ref, stdin=negated)
    return listed.stdout.split() if listed.returncode == 0 else None


def _print_carried_commits_refusal(
    git_cmd: list[str], cwd: Path, branch: str, target_ref: str, carried: Optional[list[str]], policy: str,
    rescue_ref: Optional[str],
) -> None:
    """LOUD block: the update stopped before resetting *branch* to *target_ref*; what it would drop, how
    to proceed."""
    if carried is None:
        print(f"\n{_BAR}\n⚠ CODE UPDATE SKIPPED — Git could not count the commits on '{branch}' that are "
              f"not on {target_ref}")
    else:
        print(f"\n{_BAR}\n⚠ CODE UPDATE SKIPPED — '{branch}' carries {len(carried)} local commit(s) not on "
              f"{target_ref}")
    why = ("updates.carried_commits_policy: refuse" if policy == "refuse" else
           "config.yaml could not be read, so updates.carried_commits_policy counts as refuse")
    drop = "could drop them" if carried is None else "drop them"
    print(f"  Updating would reset '{branch}' to {target_ref} and {drop} ({why}).")
    print("  Nothing was changed: HEAD, the branches and the working tree are as they were.")
    log = _git(git_cmd, cwd, "log", "--oneline", "--no-walk=unsorted", *carried[:20]) if carried else None
    if log is not None and log.returncode == 0:
        print(f"\n  Carried commits{' (first 20)' if len(carried) > 20 else ''}:")
        for line in log.stdout.splitlines():
            print(f"    {line}")
    else:
        print(f"\n  Inspect them with: git -C {cwd} log --oneline {target_ref}..{branch}")
    if rescue_ref:
        print(f"  '{branch}' is also saved as {rescue_ref}.")
    if policy == "unreadable":
        print("\n  Fix config.yaml first (`hermes config check` shows what is wrong).")
    print(
        f"\n  To keep them, rebase them onto the update, then update again\n"
        f"  (commit or stash uncommitted changes first):\n"
        f"    git -C {cwd} rebase {target_ref} {branch} && hermes update\n"
        f"  To let updates reset '{branch}' and drop them (a rescue ref keeps them reachable\n"
        f"  for {_ORPHAN_RESCUE_REF_MAX_AGE_DAYS} days):\n"
        f"    hermes config set updates.carried_commits_policy reset && hermes update\n{_BAR}"
    )


def _refuse_update_over_carried_commits(
    git_cmd: list[str], branch: str, target_ref: str, *, windows_gateway_resume,
    release_checkout: bool = False,
) -> None:
    """``sys.exit(1)`` before anything moves when landing on local *branch* would reset it to
    *target_ref* and drop commits carried on it, or ones Git cannot rule out. Only a readable
    ``updates.carried_commits_policy: reset`` lets the update proceed to that reset."""
    from hermes_cli.update_cmd import _m, _record_update_skip
    cwd = _m().PROJECT_ROOT
    branch_ref = "HEAD" if release_checkout and branch == "HEAD" else f"refs/heads/{branch}"
    if _git(git_cmd, cwd, "rev-parse", "--verify", "--quiet", branch_ref).returncode == 1:
        return  # no local branch yet: the checkout creates it from the target
    if not release_checkout and _git(git_cmd, cwd, "merge-base", "--is-ancestor", target_ref, branch_ref).returncode == 0:
        return  # the target adds nothing, so the update never moves the branch
    policy = _carried_commits_policy()
    if policy == "reset":
        return
    carried = _carried_commits(git_cmd, cwd, branch_ref, target_ref)
    if carried == []:
        return
    head = _git(git_cmd, cwd, "rev-parse", "--verify", "--quiet", branch_ref).stdout.strip()
    rescue_ref = None
    if head:
        kind = "diverged" if _git(git_cmd, cwd, "merge-base", target_ref, branch_ref).returncode == 0 else "orphan"
        rescue_ref = _rescue_ref_name(branch, head, kind)
        if _git(git_cmd, cwd, "update-ref", rescue_ref, head).returncode != 0:
            rescue_ref = None
        _prune_orphan_rescue_refs(git_cmd, cwd, branch)
    _print_carried_commits_refusal(git_cmd, cwd, branch, target_ref, carried, policy, rescue_ref)
    count = "an unknown number of" if carried is None else str(len(carried))
    _record_update_skip(
        "code_update", f"carried local commits: {count} on {branch} not on {target_ref} "
        f"(updates.carried_commits_policy: {policy})")
    print()
    print(f"⚠ Update finished — code update SKIPPED: carried local commits{_branch_head_suffix(git_cmd, cwd)}")
    _m()._resume_windows_gateways_after_update(windows_gateway_resume)
    sys.exit(1)
