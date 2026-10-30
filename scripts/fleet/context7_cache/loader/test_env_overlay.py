"""Offline tests: exact-live AST, fake providers/environment, controllable monotonic clock."""
import ast
import codecs
import io
import itertools
import json
import logging
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace, ModuleType
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
import build_env_overlay as build


class EnvOverlayTests(unittest.TestCase):
    def setUp(self):
        self.now = 10000.0
        self.calls = []
        self.config_reads = 0
        self.config = {'fake': {'enabled': True}}
        self.failed = False
        self.raise_fetch = False
        self.delay = 0
        self.version = 1
        self.home = Path('/synthetic/home-a')
        self.env = {'HERMES_DELEGATED_CHILD_CONTEXT': '/synthetic/parent', 'AMBIENT_KEY': 'must-not-leak'}
        self.n = dict(Path=Path, os=SimpleNamespace(environ=self.env), sys=SimpleNamespace(stderr=io.StringIO()),
                      codecs=codecs, io=io, itertools=itertools, threading=threading, time=time, logging=logging,
                      Mapping=dict, __name__='offline_env_loader')
        self.before = (build.LIVE / 'hermes_cli/env_loader.py').read_text()
        tree = ast.parse(build.overlay(self.before))
        tree.body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
        tree.body.insert(0, ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0))
        exec(compile(ast.fix_missing_locations(tree), '<offline-env-loader>', 'exec'), self.n)
        self.n['_SECRET_SOURCE_CLOCK'] = lambda: self.now
        self.n['_load_secrets_config'] = self.load_config
        self.original_skip_reason = self.n['_startup_onepassword_skip_reason']
        self.n['_startup_onepassword_skip_reason'] = lambda: 'delegated child'
        self.modules = {}
        def module(name, **attrs):
            m = ModuleType(name)
            m.__dict__.update(attrs)
            self.modules[name] = m
            return m
        module('agent', __path__=[])
        module('agent.safe_worker_policy', worker_config_snapshot=lambda: None)
        module('agent.secret_scope', _is_global_env=lambda name: name in {'PATH'}, load_env_file=lambda p: {}, is_multiplex_active=lambda: False)
        module('agent.secret_sources', __path__=[])
        module('agent.secret_sources.registry', apply_all=self.apply, enabled_source_names=lambda *a: {'fake'})
        module('hermes_cli', __path__=[], _early_recovery=SimpleNamespace(_should_skip_external_secret_sources=lambda: False))
        module('hermes_constants', get_process_hermes_home=lambda: self.home, get_hermes_home_override=lambda: None,
               get_hermes_home=lambda: self.home)
        self.patcher = patch.dict(sys.modules, self.modules)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def load_config(self, home):
        self.config_reads += 1
        return self.config

    def apply(self, cfg, home, environ=None):
        self.calls.append(str(home))
        if self.delay:
            time.sleep(self.delay)
        if self.raise_fetch:
            raise RuntimeError('synthetic failure')
        env = self.env if environ is None else environ
        value = str(home) + '-v' + str(self.version)
        provenance = {}
        skipped = []
        if not self.failed:
            if 'FAKE_KEY' in env:
                skipped = ['FAKE_KEY']
            else:
                env['FAKE_KEY'] = value
                provenance = {'FAKE_KEY': SimpleNamespace(source='fake', authoritative=True)}
        src = SimpleNamespace(name='fake', label='Fake', skipped_existing=skipped, applied=list(provenance),
                              result=SimpleNamespace(ok=not self.failed, error='offline' if self.failed else '', warnings=[], error_kind=None))
        return SimpleNamespace(sources=[src] if cfg else [], provenance=provenance, applied_any=bool(provenance), conflicts=[])

    def hydrate(self, home=None):
        return self.n['hydrate_profile_secret_sources'](home or self.home)

    def global_apply(self, home=None):
        self.n['_apply_external_secret_sources'](home or self.home)

    def snapshot(self, home=None):
        return self.n['get_secret_source_values'](home or self.home)

    def test_hydration_boundary_no_extra_reads_and_copy(self):
        first = self.hydrate()
        first['FAKE_KEY'] = 'mutated'
        self.now += 59.999
        self.assertNotEqual(self.hydrate(), first)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.config_reads, 1)
        self.version = 2
        self.now = 10060
        self.assertIn('-v2', self.hydrate()['FAKE_KEY'])
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn('FAKE_KEY', self.env)

    def test_snapshot_expires_without_fetch_or_ambient_fallback(self):
        self.hydrate()
        self.env['FAKE_KEY'] = 'ambient-wrong-profile'
        self.now += 60
        self.assertEqual(self.snapshot(), {})
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.n['_APPLIED_HOMES'])
        self.assertFalse(self.n['_SECRET_SOURCE_RESTORE_BY_HOME'])

    def test_global_refresh_cannot_resnapshot_own_old_nonoverride_write(self):
        self.global_apply()
        self.now += 59
        self.global_apply()
        self.assertEqual((len(self.calls), self.config_reads), (1, 1))
        self.version = 2
        self.now += 1
        self.global_apply()
        self.assertIn('-v2', self.env['FAKE_KEY'])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.snapshot()['FAKE_KEY'], self.env['FAKE_KEY'])

    def test_failed_refresh_drops_expired_value_and_snapshot(self):
        self.global_apply()
        self.now += 60
        self.failed = True
        self.global_apply()
        self.assertNotIn('FAKE_KEY', self.env)
        self.assertEqual(self.snapshot(), {})
        self.global_apply()
        self.assertEqual(len(self.calls), 2)  # global error guard remains bounded
        self.now += 60
        self.failed = False
        self.version = 3
        self.global_apply()
        self.assertIn('-v3', self.env['FAKE_KEY'])

    def test_exception_refresh_has_no_stale_snapshot(self):
        self.global_apply()
        self.now += 60
        self.raise_fetch = True
        self.global_apply()
        self.assertEqual(self.snapshot(), {})
        self.assertNotIn('FAKE_KEY', self.env)

    def test_disabled_source_expires_and_revokes(self):
        self.global_apply()
        self.config = {}
        self.now += 60
        self.global_apply()
        self.assertEqual(self.snapshot(), {})
        self.assertNotIn('FAKE_KEY', self.env)
        self.assertEqual(len(self.calls), 1)

    def test_per_home_age_and_reset_isolation(self):
        self.hydrate()
        b = Path('/synthetic/home-b')
        self.now += 30
        other = self.hydrate(b)
        self.now += 30
        self.assertEqual(self.snapshot(), {})
        self.assertEqual(self.snapshot(b), other)
        self.n['reset_secret_source_cache'](self.home)
        self.assertEqual(self.snapshot(b), other)
        self.n['reset_secret_source_cache']()
        self.assertEqual(self.snapshot(b), {})
        self.assertFalse(self.n['_SECRET_SOURCE_FETCHED_AT'])

    def test_clock_rollback_fails_closed(self):
        self.hydrate()
        self.now -= 1
        self.assertEqual(self.snapshot(), {})

    def test_concurrent_hydrate_single_fetch_per_window(self):
        self.delay = .01
        for _ in range(2):
            with ThreadPoolExecutor(max_workers=8) as pool:
                values = list(pool.map(lambda _: self.hydrate(), range(16)))
            self.assertTrue(all(v == values[0] for v in values))
            self.now += 60
        self.assertEqual((len(self.calls), self.config_reads), (2, 2))

    def test_concurrent_global_single_fetch_per_window(self):
        self.delay = .01
        for _ in range(2):
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _: self.global_apply(), range(16)))
            self.now += 60
        self.assertEqual((len(self.calls), self.config_reads), (2, 2))

    def test_slow_fetch_does_not_restart_age(self):
        registry = sys.modules['agent.secret_sources.registry']
        def slow(*args, **kwargs):
            report = self.apply(*args, **kwargs)
            self.now += 60
            return report
        registry.apply_all = slow
        self.assertEqual(self.hydrate(), {})
        self.global_apply()
        self.assertEqual(self.snapshot(), {})
        self.assertNotIn('FAKE_KEY', self.env)

    def test_expiry_peels_own_secret_even_when_dotenv_owns_name(self):
        self.global_apply()
        key = str(self.home.resolve())
        source, value, _ = self.n['_SECRET_SOURCE_WRITES_BY_HOME'][key]['FAKE_KEY']
        self.n['_SECRET_SOURCE_WRITES_BY_HOME'][key]['FAKE_KEY'] = (source, value, '__MANAGED_PLACEHOLDER__')
        sys.modules['agent.secret_scope'].load_env_file = lambda p: {'FAKE_KEY': '__MANAGED_PLACEHOLDER__'}
        self.now += 60
        self.assertEqual(self.snapshot(), {})
        self.assertEqual(self.env['FAKE_KEY'], '__MANAGED_PLACEHOLDER__')
        self.assertNotIn(key, self.n['_SECRET_SOURCE_WRITES_BY_HOME'])

    def test_external_replacement_is_not_revoked(self):
        self.global_apply()
        self.env['FAKE_KEY'] = 'new-shell-owner'
        self.now += 60
        self.assertEqual(self.snapshot(), {})
        self.assertEqual(self.env['FAKE_KEY'], 'new-shell-owner')

    def test_dotenv_restore_checks_age_even_when_external_fetch_disabled(self):
        # Exercise the actual load orchestration, not a hand-written restore loop.
        self.global_apply()
        self.n['_apply_managed_env'] = lambda **kw: None
        self.n['_reapply_terminal_config_bridge'] = lambda p: None
        self.env['FAKE_KEY'] = 'placeholder'
        self.n['load_hermes_dotenv'](hermes_home=self.home, load_external_secrets=False)
        self.assertIn('-v1', self.env['FAKE_KEY'])
        self.now += 60
        self.env['FAKE_KEY'] = 'placeholder'
        self.n['load_hermes_dotenv'](hermes_home=self.home, load_external_secrets=False)
        self.assertEqual(self.env['FAKE_KEY'], 'placeholder')
        self.assertEqual(len(self.calls), 1)

    def test_literal_optin_and_profile_token_context(self):
        guard = self.n['_startup_secret_sources']
        for value in ['true', 1, False, None]:
            cfg = {'onepassword': dict(enabled=True, binary_path='/fake/helper', allow_unattended_helper=value)}
            self.assertNotIn('onepassword', guard(cfg, environ={}))
        cfg['onepassword']['allow_unattended_helper'] = True
        self.assertIs(guard(cfg, environ={}), cfg)
        cfg['onepassword']['allow_unattended_helper'] = False
        self.env['OP_SERVICE_ACCOUNT_TOKEN'] = 'ambient-wrong'
        self.assertNotIn('onepassword', guard(cfg, environ={}))
        self.assertIs(guard(cfg, environ={'OP_SERVICE_ACCOUNT_TOKEN':'synthetic-local'}), cfg)
        self.assertEqual(self.env['HERMES_DELEGATED_CHILD_CONTEXT'], '/synthetic/parent')

    def test_preserved_functions_byte_equivalent_ast(self):
        original = ast.parse(self.before)
        candidate = ast.parse(build.overlay(self.before))
        old = {n.name: ast.dump(n) for n in original.body if isinstance(n, ast.FunctionDef)}
        new = {n.name: ast.dump(n) for n in candidate.body if isinstance(n, ast.FunctionDef)}
        for name in ['_startup_onepassword_skip_reason', '_startup_secret_sources', '_stdin_is_tty',
                     '_parse_dotenv_file', '_dotenv_lookup_env', '_resolve_dotenv_assignments',
                     '_load_dotenv_with_fallback', '_process_hermes_home', '_load_secrets_config']:
            self.assertEqual(old[name], new[name], name)
        self.assertIn('HERMES_DELEGATED_CHILD_CONTEXT', self.env)

    def test_desktop_and_delegated_guards_execute_unchanged(self):
        delegated = SimpleNamespace(value=False)
        modules = {
            'agent.delegation_context': SimpleNamespace(is_delegated_child_process_context=lambda: delegated.value),
            'gateway': ModuleType('gateway'),
            'gateway.session_context': SimpleNamespace(get_session_env=lambda name, default='': default),
            'hermes_cli._parser': SimpleNamespace(command_argv=lambda args: args),
        }
        self.n['sys'].argv = ['hermes', 'dashboard']
        self.env['HERMES_DESKTOP'] = '1'
        with patch.dict(sys.modules, modules):
            self.assertEqual(self.original_skip_reason(), 'Desktop backend')
            delegated.value = True
            self.assertEqual(self.original_skip_reason(), 'delegated child')
            delegated.value = False
            self.n['sys'].argv = ['hermes', 'gateway', 'run']
            self.env['HERMES_GATEWAY_DETACHED'] = '1'
            self.assertEqual(self.original_skip_reason(), 'detached gateway daemon')

    def test_dotenv_self_reference_baseline_remains_idempotent(self):
        # Exercise the unchanged loader publication/baseline code with a fake
        # parser: no dotenv install and no filesystem or Hermes bootstrap needed.
        class Atom:
            def resolve(self, env):
                return '/prefix:' + env.get('PATH', '')
        with patch.dict(sys.modules, {'dotenv': ModuleType('dotenv'),
                                    'dotenv.variables': SimpleNamespace(parse_variables=lambda value: [Atom()])}):
            self.n['_parse_dotenv_file'] = lambda path: [('PATH', '/prefix:${PATH}')]
            self.env['PATH'] = '/base'
            for _ in range(5):
                self.n['_load_dotenv_with_fallback'](Path('/synthetic/.env'), override=True)
                self.assertEqual(self.env['PATH'], '/prefix:/base')

    def test_reset_waits_for_inflight_publication(self):
        entered, release = threading.Event(), threading.Event()
        registry = sys.modules['agent.secret_sources.registry']
        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise RuntimeError('test release timed out')
            return self.apply(*args, **kwargs)
        registry.apply_all = blocked
        with ThreadPoolExecutor(max_workers=2) as pool:
            fetch = pool.submit(self.hydrate)
            self.assertTrue(entered.wait(2))
            reset = pool.submit(self.n['reset_secret_source_cache'], self.home)
            release.set()
            self.assertTrue(fetch.result(timeout=2))
            reset.result(timeout=2)
        self.assertEqual(self.snapshot(), {})
        self.assertFalse(self.n['_APPLIED_HOMES'])

    def test_generated_artifact_hashes(self):
        manifest = json.loads((build.OUT / 'env_loader_manifest.json').read_text())
        self.assertEqual(build.sha(self.before.encode()), manifest['source_sha256'])
        self.assertEqual(build.sha((build.OUT/'env_loader.candidate.py').read_bytes()), manifest['candidate_sha256'])
        self.assertEqual(build.sha((build.OUT/'env_loader.patch').read_bytes()), manifest['patch_sha256'])
        self.assertEqual(build.overlay(self.before), (build.OUT/'env_loader.candidate.py').read_text())


if __name__ == '__main__':
    unittest.main(verbosity=2)
