"""Real maintenance orchestration and marker fault contracts with authority OFF."""
from argparse import Namespace
from pathlib import Path
import errno
import os
import stat
import threading
from unittest.mock import Mock

import pytest

from tests.hermes_cli import test_retention_runtime as fixtures
from tests.hermes_cli.test_retention_runtime import task, git, forbidden
from hermes_cli import worktree_ops as wt, worktree_gc as gc
from hermes_cli import kanban_bundle_preserve as bp
from hermes_cli import retention_markers as markers

board, tree = fixtures.board, fixtures.tree


def test_cli_tui_and_cron_maintenance_are_local_audits(board, tree, monkeypatch):
    import atexit
    import cli
    from hermes_cli import main_tui_launch as tui
    from cron import scheduler
    repo, path = tree
    before = git(repo, 'worktree', 'list', '--porcelain')
    info = {'path': str(path), 'branch': 'wt/fixture', 'repo_root': str(repo)}
    calls, callbacks = [], []
    # Drive each launcher's maintenance callback synchronously. Requested tree
    # creation is stubbed independently of the actual read-only audit implementation.
    class InlineThread:
        def __init__(self, target, args=(), name='', **kwargs):
            self.target, self.args, self.name = target, args, name
        def start(self):
            calls.append(self.name)
            if self.name != 'tool-prewarm':
                self.target(*self.args)
        def join(self, **kwargs):
            pass
    monkeypatch.setattr(threading, 'Thread', InlineThread)
    monkeypatch.setattr(cli, '_git_repo_root', lambda: str(repo))
    monkeypatch.setattr(cli, '_setup_worktree', lambda **kw: info)
    monkeypatch.setattr(atexit, 'register', lambda callback, *args: callbacks.append((callback, args)))
    pack = Mock(side_effect=AssertionError('pack maintenance on audit path'))
    monkeypatch.setattr(cli, '_maintain_pack_health', pack)
    monkeypatch.setattr(wt, '_maintain_pack_health', pack)
    for name in ('_deepen_shallow_repo', '_fetch_remote_branch_heads', '_save_worktree_merge_cache',
                 '_classify_prune_candidates', '_reclaim_orphan_install_states'):
        monkeypatch.setattr(wt, name, Mock(side_effect=AssertionError(name)))
    with forbidden(monkeypatch):
        join = cli._start_worktree_setup(False, False, True, False)
        assert join() == info
        for callback, args in callbacks:
            callback(*args)
        assert tui._setup_tui_worktree() == info
        monkeypatch.setattr(scheduler, '_last_worktree_maintenance_at', None)
        monkeypatch.setattr(scheduler, '_worktree_maintenance_repos', lambda: [str(repo)])
        scheduler._maybe_run_worktree_maintenance()
        assert all(record.verdict == 'keep' for record in gc.audit_worktrees(str(repo), with_sizes=False))
    pack.assert_not_called()
    assert 'pack-maintenance' not in calls
    assert 'worktree-prune' in calls and 'cron-worktree-prune' in calls
    assert git(repo, 'worktree', 'list', '--porcelain') == before


def test_direct_pack_helper_never_prunes_registration(board, tmp_path, monkeypatch):
    # Existing standalone repack policy is outside this change; isolate it and
    # prove that even the pack-sprawl branch no longer prunes registration.
    from hermes_cli import gitlock
    pack = tmp_path / 'repo/.git/objects/pack'
    pack.mkdir(parents=True)
    for index in range(wt._PACK_SPRAWL_THRESHOLD):
        (pack / f'{index}.pack').touch()
    monkeypatch.setattr(wt, '_claim_repack_slot', lambda *a: True)
    monkeypatch.setattr(gitlock, 'clear_stale_tmp_packs', lambda *a: None)
    repack = Mock()
    monkeypatch.setattr(wt, '_run_bounded_repack', repack)
    git_call = Mock(side_effect=AssertionError('worktree prune attempted'))
    monkeypatch.setattr(wt, '_git', git_call)
    wt._maintain_pack_health(str(tmp_path / 'repo'))
    repack.assert_called_once()
    git_call.assert_not_called()


def test_direct_install_wrapper_does_not_import_collector(board, tmp_path, monkeypatch):
    import builtins
    original = builtins.__import__
    attempted = []
    def guard(name, *args, **kw):
        if name in {'pm.install_states', 'pm.environments'}:
            attempted.append(name)
            raise AssertionError('collector import attempted')
        return original(name, *args, **kw)
    monkeypatch.setattr(builtins, '__import__', guard)
    wt._reclaim_orphan_install_states()
    wt._prune_stale_worktrees(str(tmp_path / 'missing-repo'))
    assert attempted == []


@pytest.mark.parametrize('value,expected', [('true', True), ('false', False), (None, None), ('', None), ('corrupt', None)])
def test_shallow_unknown_is_not_complete_history(board, monkeypatch, value, expected):
    monkeypatch.setattr(wt, '_git_out', lambda *a, **kw: value)
    assert wt._repo_is_shallow('fixture') is expected


@pytest.mark.parametrize('mode', [stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, 0])
def test_special_marker_taxonomy_never_opens_payload(board, tmp_path, monkeypatch, mode):
    root = tmp_path / 'workspace'
    tid = task(board, root, status='done', result='accepted.patch')
    real_stat, real_open = os.stat, os.open
    def marker_stat(path, *args, **kw):
        if str(path) == 'KEEP-BUNDLE':
            return os.stat_result((mode | 0o600, 12, 1, 1, 0, 0, 0, 0, 0, 0))
        return real_stat(path, *args, **kw)
    def marker_open(path, *args, **kw):
        assert str(path) != 'KEEP-BUNDLE', 'special file opened'
        return real_open(path, *args, **kw)
    monkeypatch.setattr(os, 'stat', marker_stat)
    monkeypatch.setattr(os, 'open', marker_open)
    assert markers.inspect_keep_bundle(root).status is markers.MarkerStatus.UNSAFE
    with pytest.raises(markers.RetainAll):
        bp.preserve_bundle(board, tid, root)
    assert (root / 'accepted.patch').read_bytes() == b'accepted bytes\n'


@pytest.mark.parametrize('change', ['marker-replaced', 'marker-disappears', 'ancestor-replaced', 'unsupported'])
def test_marker_identity_and_unsupported_semantics_retain(board, tmp_path, monkeypatch, change):
    root = tmp_path / 'workspace'
    tid = task(board, root, status='done', result='accepted.patch')
    marker = root / 'KEEP-BUNDLE'; marker.touch()
    real_stat = os.stat
    calls = 0
    def changed_stat(path, *args, **kw):
        nonlocal calls
        if str(path) == 'KEEP-BUNDLE':
            calls += 1
            if calls % 2 == 0:
                if change == 'marker-disappears':
                    raise FileNotFoundError(errno.ENOENT, 'injected disappearance')
                if change == 'marker-replaced':
                    old = real_stat(path, *args, **kw)
                    # A different no-follow inode invalidates the open descriptor.
                    return os.stat_result((old.st_mode, old.st_ino+1, old.st_dev, old.st_nlink,
                                           old.st_uid, old.st_gid, old.st_size, old.st_atime,
                                           old.st_mtime, old.st_ctime))
        return real_stat(path, *args, **kw)
    if change == 'unsupported':
        monkeypatch.delattr(os, 'O_NOFOLLOW')
    elif change == 'ancestor-replaced':
        monkeypatch.setattr(markers, '_chain_stable', lambda *a: False)
    else:
        monkeypatch.setattr(os, 'stat', changed_stat)
    assert markers.inspect_keep_bundle(root).status is markers.MarkerStatus.UNSAFE
    with pytest.raises(markers.RetainAll):
        bp.preserve_bundle(board, tid, root)


def test_absence_requires_verified_parent(board, tmp_path):
    assert markers.inspect_keep_bundle(tmp_path / 'missing/workspace').status is markers.MarkerStatus.UNSAFE
    alias = tmp_path / 'alias'; alias.symlink_to(tmp_path, target_is_directory=True)
    assert markers.inspect_keep_bundle(alias).status is markers.MarkerStatus.UNSAFE


def test_missing_registration_stays_registered(board, tree, tmp_path, monkeypatch):
    repo, _ = tree
    external = tmp_path / 'external'
    git(repo, 'worktree', 'add', '-b', 'wt/external', str(external))
    external.rename(tmp_path / 'external-retained-by-fixture')
    before = git(repo, 'worktree', 'list', '--porcelain')
    real_git = gc._git
    attempts = []
    def read_only(args, *a, **kw):
        attempts.append(args)
        assert args[:2] == ['worktree', 'list']
        return real_git(args, *a, **kw)
    monkeypatch.setattr(gc, '_git', read_only)
    for _ in range(2):
        assert gc.prune_missing_registrations(str(repo)) == [f'retained registration {external} (destructive_authority_off)']
    assert attempts
    assert git(repo, 'worktree', 'list', '--porcelain') == before
    assert git(repo, 'rev-parse', 'wt/external') == git(repo, 'rev-parse', 'HEAD')


@pytest.mark.parametrize('failure', ['nonzero', 'timeout'])
def test_failed_add_keeps_partial_state_without_retry(board, tree, monkeypatch, failure):
    import subprocess
    from hermes_cli import _subprocess_compat
    repo, path = tree
    original = (path / '.git').read_bytes()
    oid = git(repo, 'rev-parse', 'wt/fixture')
    monkeypatch.setattr(_subprocess_compat, 'noninteractive_repo_git_env', lambda *a, **kw: {})
    calls = []
    def failed_git(args, *a, **kw):
        calls.append(args)
        if failure == 'timeout':
            raise subprocess.TimeoutExpired('fixture git add', 120)
        return subprocess.CompletedProcess(args, 1, '', 'fixture checkout failure')
    monkeypatch.setattr(wt, '_git', failed_git)
    assert wt._worktree_add(str(repo), path, 'wt/fixture', 'origin/main', 'fixture') is None
    assert len(calls) == 1
    assert 'add' in calls[0] and not {'remove', 'prune', 'unlock'}.intersection(calls[0])
    assert (path / '.git').read_bytes() == original
    assert git(repo, 'rev-parse', 'wt/fixture') == oid


def test_clean_zero_commit_cli_exit_and_prune_commands_retain(board, tree, monkeypatch, capsys):
    import cli
    from hermes_cli import worktree_cmd
    repo, path = tree
    before = git(repo, 'worktree', 'list', '--porcelain')
    assert git(path, 'status', '--porcelain') == ''
    real_git = gc._git
    calls = []
    def only_read(args, *a, **kw):
        calls.append(args)
        assert args[:2] in (['worktree', 'list'], ['branch', '--format=%(refname:short)'])
        return real_git(args, *a, **kw)
    monkeypatch.setattr(gc, '_git', only_read)
    release = Mock(side_effect=AssertionError('LSP release during retained exit'))
    monkeypatch.setattr(cli, 'release_lsp_clients', release)
    with forbidden(monkeypatch):
        cli._cleanup_worktree({'path': str(path), 'branch': 'wt/fixture', 'repo_root': str(repo)})
        assert worktree_cmd._prune(gc, str(repo), Namespace(dry_run=False, json=False, older_than=0)) == 0
        cli.HermesCLI._worktree_prune(object(), str(repo), '')
    release.assert_not_called()
    assert calls
    assert '0 objects removed' in capsys.readouterr().out
    assert git(repo, 'worktree', 'list', '--porcelain') == before


def test_direct_worker_cleanup_never_signals(board, tmp_path, monkeypatch):
    import subprocess
    from hermes_cli import kanban_db_workspace as ws
    tid = task(board, tmp_path / 'source', status='done')
    run = Mock(side_effect=AssertionError('tmux inspection or kill attempted'))
    monkeypatch.setattr(subprocess, 'run', run)
    ws._cleanup_worker_tmux(board, tid)
    run.assert_not_called()
