"""`hermes update` must not silently drop commits carried on the update target branch.

Card t_0382ebfc (2026-09-25): the desktop hand-off ran ``hermes update --yes --gateway --keep-stash
--branch main`` on an install whose ``main`` carried 13 local commits. The fast-forward failed, the
same-branch reconcile parked HEAD behind a rescue ref and reset --hard to ``origin/main``, the dirty
files stayed parked in the autostash, the gateway restarted on code without the carried fixes, and
the update exited 0.

``updates.carried_commits_policy: refuse`` (the default) stops before the reset: HEAD, branch and
working tree stay as they were, the rescue ref is still written, the carried commits are named, and
the update exits 1. ``reset`` keeps the old behaviour; orphan divergence (no common ancestor) and
plain fast-forwards are unaffected. The reset itself is covered in test_update_diverged_rescue_ref.py.
"""

from __future__ import annotations

import json
import re
import subprocess

import pytest

from hermes_cli import update_cmd, update_receipt
from hermes_constants import get_hermes_home


GIT = ["git"]


def _git(repo, *args):
    return subprocess.run(
        GIT + list(args), cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def _commit(repo, name, text):
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


def _checkout(tmp_path, monkeypatch, shape):
    """A clone of ``origin`` (shared commit + one upstream-only commit), sitting on ``main`` at the
    shared commit plus whatever *shape* adds: ``carried`` = two local commits (diverged), ``behind`` =
    nothing (fast-forward possible), ``orphan`` = an unrelated root commit (no common ancestor)."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "git-config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
        subprocess.run(GIT + ["config", "--global", key, value], check=True)
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _commit(origin, "shared.txt", "shared\n")
    _commit(origin, "upstream-only.txt", "upstream\n")
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(origin), str(checkout))
    _git(checkout, "reset", "-q", "--hard", "HEAD~1")
    if shape == "carried":
        _commit(checkout, "carried-1.txt", "carried one\n")
        _commit(checkout, "carried-2.txt", "carried two\n")
    elif shape == "orphan":
        _git(checkout, "checkout", "-q", "--orphan", "fresh")
        _commit(checkout, "orphan.txt", "orphan\n")
        _git(checkout, "branch", "-D", "main")
        _git(checkout, "branch", "-m", "main")
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", checkout)
    return checkout


def _set_policy(policy):
    (get_hermes_home() / "config.yaml").write_text(
        f"updates:\n  carried_commits_policy: {policy}\n", encoding="utf-8")


def _pull(checkout, auto_stash_ref=None, *, keep_stash=False):
    return update_cmd._pull_updates(
        GIT, "main", auto_stash_ref, prompt_for_restore=False, gw_input_fn=None,
        discard_local_changes=False, keep_stash=keep_stash)


def _state(checkout):
    """Everything a refused update must leave untouched: branch, HEAD, index + worktree status."""
    return (_git(checkout, "branch", "--show-current"), _git(checkout, "rev-parse", "HEAD"),
            _git(checkout, "status", "--porcelain=v1", "--untracked-files=all"))


def _rescue_refs(checkout):
    out = _git(checkout, "for-each-ref", "--format=%(refname) %(objectname)", "refs/hermes-update-backups/")
    return dict(line.split() for line in out.splitlines() if line.strip())


def test_carried_commits_refuse_the_reset_by_default(tmp_path, monkeypatch, capsys):
    checkout = _checkout(tmp_path, monkeypatch, "carried")
    before = _state(checkout)
    carried_log = _git(checkout, "log", "--oneline", "origin/main..HEAD").splitlines()
    assert len(carried_log) == 2
    monkeypatch.setattr(update_receipt, "_code_identity", lambda **_: {})

    with update_receipt.update_receipt_scope():
        update_receipt.begin_update_receipt()
        with pytest.raises(SystemExit) as refused:
            _pull(checkout)
        receipt_path = update_receipt.finalize_pending_update_receipt(exit_code=refused.value.code)

    assert refused.value.code == 1
    assert _state(checkout) == before, "nothing may move when the carried commits are refused"
    out = capsys.readouterr().out
    assert "code update SKIPPED: carried local commits" in out
    assert re.search(r"\b2\b[^\n]*commit", out), "the block must name how many commits are carried"
    for line in carried_log:
        assert line in out
    assert "hermes config set updates.carried_commits_policy reset" in out
    refs = [ref for ref, sha in _rescue_refs(checkout).items() if sha == before[1]]
    assert len(refs) == 1 and refs[0] in out, "the rescue ref is still written and named"

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["outcome"] == "failed"
    assert any(skip["name"] == "code_update" and "carried" in skip["reason"] for skip in receipt["skips"])


def test_refused_update_puts_keep_stash_changes_back(tmp_path, monkeypatch, capsys):
    """Nothing landed, so --keep-stash has nothing to protect: the dirty tree comes back as it was."""
    checkout = _checkout(tmp_path, monkeypatch, "carried")
    (checkout / "shared.txt").write_text("edited and staged\n", encoding="utf-8")
    _git(checkout, "add", "shared.txt")
    (checkout / "carried-1.txt").write_text("edited, not staged\n", encoding="utf-8")
    (checkout / "scratch.txt").write_text("untracked\n", encoding="utf-8")
    before = _state(checkout)
    stash_ref = update_cmd._stash_local_changes_if_needed(GIT, checkout)
    assert stash_ref and _git(checkout, "status", "--porcelain") == ""

    with pytest.raises(SystemExit) as refused:
        _pull(checkout, stash_ref, keep_stash=True)

    assert refused.value.code == 1
    assert _state(checkout) == before
    assert (checkout / "shared.txt").read_text(encoding="utf-8") == "edited and staged\n"
    assert (checkout / "carried-1.txt").read_text(encoding="utf-8") == "edited, not staged\n"
    assert (checkout / "scratch.txt").read_text(encoding="utf-8") == "untracked\n"
    assert _git(checkout, "stash", "list") == "", "the restored autostash must not stay parked"
    assert "code update SKIPPED: carried local commits" in capsys.readouterr().out


@pytest.mark.parametrize("shape,policy,rescued", [
    ("carried", "reset", True),    # opted in: today's reset, old HEAD behind a rescue ref
    ("behind", None, False),       # nothing carried: plain fast-forward under the default policy
    ("orphan", None, True),        # no common ancestor: today's orphan reset under the default policy
])
def test_update_still_lands_when_nothing_is_refused(tmp_path, monkeypatch, capsys, shape, policy, rescued):
    checkout = _checkout(tmp_path, monkeypatch, shape)
    if policy:
        _set_policy(policy)
    pre = _git(checkout, "rev-parse", "HEAD")

    _pull(checkout)

    assert _git(checkout, "rev-parse", "HEAD") == _git(checkout, "rev-parse", "origin/main")
    assert _git(checkout, "branch", "--show-current") == "main"
    assert [sha for sha in _rescue_refs(checkout).values() if sha == pre] == ([pre] if rescued else [])
    assert "code update SKIPPED" not in capsys.readouterr().out
