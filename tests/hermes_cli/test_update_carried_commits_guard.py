"""`hermes update` must not silently drop commits carried on the update target branch.

Field incident (2026-09-25): the desktop hand-off ran ``hermes update --yes --gateway --keep-stash
--branch main`` on an install whose ``main`` carried 13 local commits. The fast-forward failed, the
same-branch reconcile parked HEAD behind a rescue ref and reset --hard to ``origin/main``, the dirty
files stayed parked in the autostash, the gateway restarted on code without the carried fixes, and
the update exited 0.

``updates.carried_commits_policy: refuse`` (the default) stops the update before it touches the
checkout: HEAD, branches, index and working tree (npm lockfile edits included) stay as they were,
a rescue ref is still written, the carried commits are named, and the update exits 1. Only a
config.yaml read successfully by this run can say ``reset``. Carried commits are the ones
``origin/<branch>`` has never pointed at or reached: an orphan history counts, a commit Git cannot
count refuses, while shallow-graft artifacts and upstream force-pushes (commits origin itself once
served) do not block the update. The reset itself is covered in test_update_diverged_rescue_ref.py.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from types import SimpleNamespace

from hermes_cli import banner
from hermes_cli import main as hermes_main, update_cmd, update_cmd_carried as update_carried, update_receipt
from hermes_cli.config import load_config
from hermes_cli.config_read_errors import FailedConfigRead
from hermes_cli.update_inventory import UpdatePlan
from hermes_constants import get_hermes_home


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout.strip()


@pytest.fixture
def update_tree(tmp_path, monkeypatch):
    """A real origin + install clone parked on ``retained-branch``; origin/main moves base -> wanted -> newer.

    0.21.5 backport of the fixture in ``test_update_target_identity.py`` (not present on this line): the
    run stops at the post-swap hand-off, the point where the pulled code would take over, and records it
    as the completion request.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "git-config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.delenv("HERMES_UPDATE_HANDOFF_PID", raising=False)
    monkeypatch.delenv("HERMES_UPDATE_REEXEC", raising=False)
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "user.name", "Fixture")
    git(origin, "config", "user.email", "fixture@example.invalid")
    (origin / "content.txt").write_text("base\n", encoding="utf-8")
    (origin / ".gitignore").write_text(".bytecode-fingerprint\n", encoding="utf-8")
    git(origin, "add", "content.txt", ".gitignore")
    git(origin, "-c", "commit.gpgsign=false", "commit", "-qm", "base")
    base = git(origin, "rev-parse", "HEAD")
    clone = tmp_path / "install"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    git(clone, "config", "user.name", "Fixture")
    git(clone, "config", "user.email", "fixture@example.invalid")
    git(clone, "checkout", "-qb", "retained-branch")
    (origin / "content.txt").write_text("release\n", encoding="utf-8")
    git(origin, "-c", "commit.gpgsign=false", "commit", "-qam", "release")
    wanted = git(origin, "rev-parse", "HEAD")
    (origin / "content.txt").write_text("unreleased-main\n", encoding="utf-8")
    git(origin, "-c", "commit.gpgsign=false", "commit", "-qam", "unreleased")
    newer = git(origin, "rev-parse", "HEAD")

    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", clone)
    monkeypatch.setattr(update_receipt, "_code_identity", lambda **_: {"commit": base})
    monkeypatch.setattr(hermes_main, "_run_pre_update_backup", lambda *_: None)
    monkeypatch.setattr(hermes_main, "_pause_windows_gateways_for_update", lambda: None)
    resumed = []
    monkeypatch.setattr(hermes_main, "_resume_windows_gateways_after_update", lambda state: resumed.append(state))
    monkeypatch.setattr(hermes_main, "_install_hangup_protection", lambda **_: {"installed": False})
    monkeypatch.setattr(hermes_main, "_finalize_update_output", lambda *_: None)
    monkeypatch.setattr(hermes_main, "_is_windows", lambda: False)
    # A file:// origin reads as a fork; the fork/upstream sync is not what these tests exercise.
    monkeypatch.setattr(update_cmd, "_is_fork", lambda *_a, **_k: False)

    def inventory():
        sha = git(clone, "rev-parse", "HEAD") if (clone / ".git").exists() else base
        return UpdatePlan(install_method="git", expected_sha=sha, profiles=["default"])

    monkeypatch.setattr("hermes_cli.update_inventory.collect_runtime_inventory", inventory)

    requests = []

    def complete(request):
        requests.append(request)
        return {"exit_code": 0, "receipt": None}

    monkeypatch.setattr(update_cmd, "run_completion", complete)
    args = SimpleNamespace(branch=None, yes=True, force=True, force_venv=True, check=False, plan=False,
                           gateway=False, keep_stash=False, channel='main', install_id=False, set_channel=None)
    return SimpleNamespace(origin=origin, clone=clone, base=base, wanted=wanted, newer=newer,
                           args=args, resumed=resumed, requests=requests)


def _commit(repo, name, text, message=None):
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", message or f"add {name}")
    return git(repo, "rev-parse", "HEAD")


def _carry(t, count=2, *, lockfile=False):
    """``main`` at the installed commit plus *count* commits that exist only in this checkout."""
    git(t.clone, "checkout", "-q", "main")
    for n in range(1, count + 1):
        _commit(t.clone, f"carried-{n}.txt", f"carried {n}\n")
    if lockfile:
        _commit(t.clone, "package-lock.json", '{"lockfileVersion": 3}\n', "carry a lockfile")


@pytest.mark.parametrize('detached', [False, True])
@pytest.mark.parametrize('release', ['base', 'newer'])
def test_release_checkout_preserves_carried_head_and_dirty_files(update_tree, detached, release):
    t = update_tree
    _carry(t)
    git(t.clone, 'fetch', '-q', 'origin')
    if detached:
        git(t.clone, 'checkout', '-q', '--detach')
    (t.clone / 'content.txt').write_text('uncommitted work\n', encoding='utf-8')
    before = _state(t)
    with pytest.raises(SystemExit) as refused:
        update_cmd._prepare_checkout_for_update(
            ['git'], 'main', 'HEAD' if detached else 'main',
            is_fork=False, assume_yes=True, gateway_mode=False, gw_input_fn=None,
            switch_branch=False, target_ref=getattr(t, release), _windows_gateway_resume=None,
        )
    assert refused.value.code == 1
    assert _state(t) == before
    assert (t.clone / 'carried-1.txt').read_text() == 'carried 1\n'


def _hand_off(t, monkeypatch):
    """The desktop hand-off: ``hermes update --yes --keep-stash --branch main``."""
    t.args.branch, t.args.keep_stash = "main", True
    # A shallow checkout asks GitHub for the behind count; the fixture is offline.
    monkeypatch.setattr(banner, "_github_compare_behind", lambda *_a, **_k: None)


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


def _fail_git(monkeypatch, failing):
    """Make every ``git <failing>`` call fail as Git would on a damaged repository."""
    run = subprocess.run

    def fault(command, *args, **kwargs):
        if failing(command):
            return subprocess.CompletedProcess(command, 128, stdout="", stderr="fatal: fixture refusal")
        return run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fault)


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


def test_refused_update_keeps_an_unstaged_lockfile_edit(update_tree, monkeypatch, capsys):
    """The update's npm lockfile cleanup must not run before the refusal decision."""
    t = update_tree
    _carry(t, lockfile=True)
    edited = '{"lockfileVersion": 3, "local": "edit"}\n'
    (t.clone / "package-lock.json").write_text(edited, encoding="utf-8")
    before = _state(t)
    _hand_off(t, monkeypatch)

    _refused(t)

    assert (t.clone / "package-lock.json").read_text(encoding="utf-8") == edited
    assert _state(t) == before
    assert "code update SKIPPED: carried local commits" in capsys.readouterr().out


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


@pytest.mark.parametrize("shape", ["orphan", "merge-base-fails", "count-fails"])
def test_carried_commits_git_cannot_rule_out_refuse(update_tree, monkeypatch, capsys, shape):
    """No common ancestor, a failing merge-base and a failing count are never "nothing carried"."""
    t = update_tree
    if shape == "orphan":
        git(t.clone, "checkout", "-q", "--orphan", "fresh")
        _commit(t.clone, "orphan.txt", "orphan\n")
        git(t.clone, "branch", "-D", "main")
        git(t.clone, "branch", "-m", "main")
    else:
        _carry(t)
    before = _state(t)
    _hand_off(t, monkeypatch)
    if shape == "merge-base-fails":
        _fail_git(monkeypatch, lambda command: "merge-base" in command)
    if shape == "count-fails":
        # Only the update's own behind count (HEAD..origin/main) still answers.
        _fail_git(monkeypatch, lambda command: "rev-list" in command and not any(
            str(arg).startswith("HEAD..") for arg in command))

    _refused(t)

    assert _state(t) == before
    assert "code update SKIPPED: carried local commits" in capsys.readouterr().out


@pytest.mark.parametrize("damage", ["tip-object-missing", "tip-not-a-commit"])
def test_a_branch_tip_git_cannot_read_as_a_commit_refuses(update_tree, monkeypatch, capsys, damage):
    """A branch ref whose tip is not a readable commit is not "nothing carried": the count skipped a
    missing tip and never walks a non-commit one, so both read as zero. Called directly because a
    full update stops at the fetch, which also trips over the bad ref; that is not the guard."""
    t = update_tree
    _carry(t)
    git(t.clone, "fetch", "-q", "origin")
    tip = git(t.clone, "rev-parse", "main")
    if damage == "tip-object-missing":
        (t.clone / ".git" / "objects" / tip[:2] / tip[2:]).unlink()  # the older carried commit survives
    else:
        tip = git(t.clone, "hash-object", "-w", "carried-1.txt")
        (t.clone / ".git" / "refs" / "heads" / "main").write_text(f"{tip}\n", encoding="utf-8")

    with pytest.raises(SystemExit) as refused:
        update_carried._refuse_update_over_carried_commits(
            update_cmd._base_git_cmd(), "main", "origin/main", windows_gateway_resume=None)

    assert refused.value.code == 1
    assert git(t.clone, "rev-parse", "--verify", "refs/heads/main") == tip
    assert "code update SKIPPED: carried local commits" in capsys.readouterr().out


def test_parked_checkout_is_not_switched_onto_a_branch_that_carries_commits(update_tree, monkeypatch, capsys):
    t = update_tree
    _carry(t)
    git(t.clone, "checkout", "-q", "retained-branch")
    before = _state(t)
    _hand_off(t, monkeypatch)

    _refused(t)

    assert _state(t) == before, "the refusal comes before the switch to main"
    assert git(t.clone, "rev-parse", "main") != t.newer
    assert "code update SKIPPED: carried local commits" in capsys.readouterr().out


def _shallow_install_after_check(t):
    """A legacy installer checkout (``clone --depth 1``) after ``hermes update --check`` fetched
    origin/main at depth 1: the graft hides that the installed commit is origin/main's ancestor."""
    git(t.origin, "reset", "-q", "--hard", t.base)
    git(t.clone.parent, "-c", "advice.detachedHead=false", "clone", "-q", "--depth", "1",
        t.origin.as_uri(), "shallow")
    git(t.origin, "reset", "-q", "--hard", t.newer)
    git(t.clone.parent / "shallow", "fetch", "-q", "--depth", "1", "origin", "main")
    shutil.rmtree(t.clone)
    (t.clone.parent / "shallow").rename(t.clone)
    assert git(t.clone, "rev-parse", "--is-shallow-repository") == "true"
    assert git(t.clone, "rev-list", "--count", "origin/main..HEAD") == "1", "the graft makes HEAD look carried"


def _upstream_force_push(t):
    """The checkout sits where origin/main used to be; upstream then rewrote main without it."""
    git(t.clone, "checkout", "-q", "main")
    git(t.clone, "fetch", "-q", "origin", "main")
    git(t.clone, "merge", "-q", "--ff-only", "origin/main")
    git(t.origin, "reset", "-q", "--hard", t.wanted)
    t.newer = _commit(t.origin, "content.txt", "rewritten upstream\n", "rewritten upstream")


def _origin_history_lost(t):
    """``main`` is behind; origin/main once held a commit upstream then force-pushed away, and that
    commit's object is gone (only the reflog still names it)."""
    git(t.clone, "checkout", "-q", "main")
    dropped = _commit(t.origin, "dropped.txt", "dropped\n", "dropped upstream")
    git(t.clone, "fetch", "-q", "origin")
    git(t.origin, "reset", "-q", "--hard", t.newer)
    git(t.clone, "fetch", "-q", "origin")
    (t.clone / ".git" / "objects" / dropped[:2] / dropped[2:]).unlink()


@pytest.mark.parametrize("shape,policy,rescued", [
    ("carried", "reset", True),           # opted in: the reset, old HEAD behind a rescue ref
    ("behind", None, False),              # nothing carried: plain fast-forward
    ("origin-history-lost", None, False), # an unreadable old origin value only drops an exclusion
    ("shallow-after-check", None, True),  # graft artifact, nothing carried: the orphan reset
    ("force-push", None, True),           # origin itself served the dropped commit
])
def test_update_still_lands_when_nothing_is_carried_or_reset_is_chosen(
        update_tree, monkeypatch, capsys, shape, policy, rescued):
    t = update_tree
    if shape == "carried":
        _carry(t)
    elif shape == "behind":
        git(t.clone, "checkout", "-q", "main")
    elif shape == "origin-history-lost":
        _origin_history_lost(t)
    elif shape == "shallow-after-check":
        _shallow_install_after_check(t)
    else:
        _upstream_force_push(t)
    if policy:
        _set_policy(policy)
    pre = git(t.clone, "rev-parse", "HEAD")
    _hand_off(t, monkeypatch)

    hermes_main.cmd_update(t.args)

    assert git(t.clone, "rev-parse", "HEAD") == t.newer
    assert git(t.clone, "branch", "--show-current") == "main"
    assert [sha for sha in _rescue_refs(t).values() if sha == pre] == ([pre] if rescued else [])
    assert "code update SKIPPED" not in capsys.readouterr().out


def test_parked_checkout_with_only_lockfile_churn_still_switches(update_tree, monkeypatch, capsys):
    """npm lockfile churn is discarded as machine dirt, so it must not read as a dirty parked tree."""
    t = update_tree
    _commit(t.clone, "package-lock.json", '{"lockfileVersion": 3}\n')
    (t.clone / "package-lock.json").write_text('{"lockfileVersion": 3, "npm": "churn"}\n', encoding="utf-8")
    _hand_off(t, monkeypatch)

    hermes_main.cmd_update(t.args)

    assert git(t.clone, "branch", "--show-current") == "main"
    assert git(t.clone, "rev-parse", "HEAD") == t.newer
    assert "code update SKIPPED" not in capsys.readouterr().out
