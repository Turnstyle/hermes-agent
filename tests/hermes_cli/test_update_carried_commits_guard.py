"""`hermes update` must not silently drop commits carried on the update target branch.

Field incident (2026-09-25): the desktop hand-off ran ``hermes update --yes --gateway --keep-stash
--branch main`` on an install whose ``main`` carried 13 local commits. The fast-forward failed, the
same-branch reconcile parked HEAD behind a rescue ref and reset --hard to ``origin/main``, the dirty
files stayed parked in the autostash, the gateway restarted on code without the carried fixes, and
the update exited 0.

``updates.carried_commits_policy: refuse`` (the default) stops before the reset: HEAD, branch and
working tree stay as they were, the rescue ref is still written, the carried commits are named, and
the update exits 1. Only a config.yaml read successfully by this run can say ``reset``. Orphan
divergence (no common ancestor) and plain fast-forwards are unaffected. The reset itself is covered
in test_update_diverged_rescue_ref.py.
"""

from __future__ import annotations

import pytest

from hermes_cli import main as hermes_main, update_cmd, update_receipt
from hermes_cli.config import load_config
from hermes_cli.config_read_errors import FailedConfigRead
from hermes_constants import get_hermes_home
from tests.hermes_cli.test_update_target_identity import git, update_tree  # noqa: F401


def _commit(repo, name, text, message=None):
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", message or f"add {name}")
    return git(repo, "rev-parse", "HEAD")


def _carry(t, count=2):
    """``main`` at the installed commit plus *count* commits that exist only in this checkout."""
    git(t.clone, "checkout", "-q", "main")
    for n in range(1, count + 1):
        _commit(t.clone, f"carried-{n}.txt", f"carried {n}\n")


def _hand_off(t, monkeypatch):
    """The desktop hand-off: ``hermes update --yes --keep-stash --branch main``."""
    t.args.branch, t.args.channel, t.args.keep_stash = "main", "main", True
    monkeypatch.setattr(hermes_main, "_sync_with_upstream_if_needed", update_cmd._sync_with_upstream_if_needed)
    monkeypatch.setattr(update_cmd, "_UPDATE_CRITICAL_MODULES", ())


def _set_policy(policy):
    (get_hermes_home() / "config.yaml").write_text(
        f"updates:\n  carried_commits_policy: {policy}\n", encoding="utf-8")


def _state(t):
    """Everything a refused update must leave untouched: branch, HEAD, index + worktree, stash list."""
    return (git(t.clone, "branch", "--show-current"), git(t.clone, "rev-parse", "HEAD"),
            git(t.clone, "status", "--porcelain=v1", "--untracked-files=all"),
            git(t.clone, "stash", "list"))


def _rescue_refs(t):
    out = git(t.clone, "for-each-ref", "--format=%(refname) %(objectname)", "refs/hermes-update-backups/")
    return dict(line.split() for line in out.splitlines() if line.strip())


def _refused(t):
    with pytest.raises(SystemExit) as refused:
        hermes_main.cmd_update(t.args)
    assert refused.value.code == 1
    assert t.requests == [], "a refused update must not arm the completion phase"


def test_carried_commits_refuse_the_update_before_anything_moves(update_tree, monkeypatch, capsys):
    t = update_tree
    _carry(t)
    (t.clone / "content.txt").write_text("edited and staged\n", encoding="utf-8")
    git(t.clone, "add", "content.txt")
    (t.clone / "carried-1.txt").write_text("edited, not staged\n", encoding="utf-8")
    (t.clone / "scratch.txt").write_text("untracked\n", encoding="utf-8")
    before = _state(t)
    carried_log = git(t.clone, "log", "--oneline", "main", "^" + t.base).splitlines()
    assert len(carried_log) == 2
    _hand_off(t, monkeypatch)

    _refused(t)

    assert _state(t) == before, "nothing may move when the carried commits are refused"
    out = capsys.readouterr().out
    assert "code update SKIPPED: carried local commits" in out
    assert "2 local commit(s)" in out
    for line in carried_log:
        assert line in out
    assert "hermes config set updates.carried_commits_policy reset" in out
    refs = [ref for ref, sha in _rescue_refs(t).items() if sha == before[1]]
    assert len(refs) == 1 and refs[0] in out, "the rescue ref is still written and named"
    receipt = update_receipt.read_latest_receipt()
    assert receipt["outcome"] == "failed"
    assert any(skip["name"] == "code_update" and "carried" in skip["reason"] for skip in receipt["skips"])


def test_unreadable_config_refuses_even_when_the_saved_policy_is_reset(update_tree, monkeypatch, capsys):
    """A config.yaml that fails to load serves its last good copy; that copy cannot authorize a reset."""
    t = update_tree
    _carry(t)
    _set_policy("reset")
    load_config()  # the last good read, as an earlier `hermes` run leaves it
    (get_hermes_home() / "config.yaml").write_text("updates: [carried_commits_policy\n", encoding="utf-8")
    fallback = load_config()
    assert isinstance(fallback, FailedConfigRead)
    assert fallback["updates"]["carried_commits_policy"] == "reset"
    before = _state(t)
    _hand_off(t, monkeypatch)

    _refused(t)

    assert _state(t) == before
    assert "code update SKIPPED: carried local commits" in capsys.readouterr().out


@pytest.mark.parametrize("shape,policy,rescued", [
    ("carried", "reset", True),           # opted in: the reset, old HEAD behind a rescue ref
    ("behind", None, False),              # nothing carried: plain fast-forward
    ("orphan", None, True),               # no common ancestor: the orphan reset under the default policy
])
def test_update_still_lands_when_nothing_is_carried_or_reset_is_chosen(
        update_tree, monkeypatch, capsys, shape, policy, rescued):
    t = update_tree
    if shape == "carried":
        _carry(t)
    elif shape == "behind":
        git(t.clone, "checkout", "-q", "main")
    else:
        git(t.clone, "checkout", "-q", "--orphan", "fresh")
        _commit(t.clone, "orphan.txt", "orphan\n")
        git(t.clone, "branch", "-D", "main")
        git(t.clone, "branch", "-m", "main")
    if policy:
        _set_policy(policy)
    pre = git(t.clone, "rev-parse", "HEAD")
    _hand_off(t, monkeypatch)

    hermes_main.cmd_update(t.args)

    assert git(t.clone, "rev-parse", "HEAD") == t.newer
    assert git(t.clone, "branch", "--show-current") == "main"
    assert [sha for sha in _rescue_refs(t).values() if sha == pre] == ([pre] if rescued else [])
    assert "code update SKIPPED" not in capsys.readouterr().out
