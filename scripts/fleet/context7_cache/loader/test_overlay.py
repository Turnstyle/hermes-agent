"""Offline AST-isolated exact-source behavior tests: no package startup/imports."""
import ast
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import typing
import unittest
import build_overlay as build


def isolated(text, namespace):
    tree = ast.parse(text)
    tree.body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
    # Preserve deferred annotations without importing any Hermes package.
    tree.body.insert(0, ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0))
    exec(compile(ast.fix_missing_locations(tree), '<offline-source>', 'exec'), namespace)
    return namespace


class Result:
    def __init__(self):
        self.warnings = []
        self.skipped = []
        self.applied = []
    def fail(self, *args):
        self.error = args[0]
        return self


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.clock = SimpleNamespace(now=10000.0)
        clock = SimpleNamespace(time=lambda: self.clock.now)
        c = dict(vars(typing), dataclass=dataclass, hashlib=hashlib, json=json,
                 time=clock, Path=Path, secure_parent_dir=lambda p: None,
                 atomic_json_write=lambda *a, **k: self.fail('unexpected disk write'))
        self.cache = isolated((build.LIVE / 'agent/secret_sources/_cache.py').read_text(), c)
        class Errors:
            def __getattr__(self, name):
                return name
        n = dict(vars(typing), Path=Path, time=clock, logging=SimpleNamespace(getLogger=lambda n: None),
                 __name__='offline_onepassword', SecretCache=c['SecretCache'], CachedFetch=c['CachedFetch'],
                 _fingerprint=c['fingerprint'], SecretSource=object, ErrorKind=Errors(), FetchResult=Result,
                 get_source_environment=lambda: {}, is_valid_env_name=lambda name: name.isidentifier(),
                 coerce_float=lambda v, default: float(v), os=SimpleNamespace(environ={}))
        self.n = isolated(build.overlay((build.LIVE / 'agent/secret_sources/onepassword.py').read_text()), n)
        self.store = self.n['_STORE']
        self.disk = None
        self.writes = 0
        self.store.disk.read = lambda *args: self.disk if self.disk and self.disk.is_fresh(args[1]) else None
        def write(key, entry, ttl, home):
            self.assertLessEqual(ttl, 1740)
            self.writes += 1
            self.disk = entry
        self.store.disk.write = write
        self.calls = 0
        def read(*args, **kwargs):
            self.calls += 1
            return 'synthetic-value-' + str(self.calls)
        self.n['_run_op_read'] = read
        self.n['find_op'] = lambda p='': Path(p or '/fake/op')

    def fetch(self, **kwargs):
        return self.n['fetch_onepassword_secrets'](references={'FAKE_KEY': 'op://fake/item/field'}, **kwargs)

    def test_bounds(self):
        for value, expected in [(86400,1740),(1801,1740),(1740,1740),(300,300),(0,0),(-1,0),('86400',1740),(float('inf'),0),(float('-inf'),0),(float('nan'),0),('bad',0),(None,0)]:
            with self.subTest(value=value):
                self.assertEqual(self.n['_bounded_cache_ttl'](value), expected)

    def test_memory_and_disk_expire_without_age_reset(self):
        first = self.fetch(cache_ttl_seconds=86400)
        self.clock.now += 1739
        self.assertEqual(self.fetch(cache_ttl_seconds=86400), first)
        self.store.memory.clear()
        self.assertEqual(self.fetch(cache_ttl_seconds=86400), first)
        self.assertEqual(self.disk.fetched_at, 10000)
        self.clock.now += 1
        self.assertNotEqual(self.fetch(cache_ttl_seconds=86400), first)
        self.assertEqual(self.calls, 2)

    def test_exact_helper_bypasses_both_layers_even_with_old_entries(self):
        self.fetch(cache_ttl_seconds=86400)
        self.calls = 0
        self.writes = 0
        for selector in ({'binary_path':build.HELPER}, {'binary':Path(build.HELPER)}):
            self.fetch(cache_ttl_seconds=86400, **selector)
            self.clock.now += 1739
            self.fetch(cache_ttl_seconds=86400, **selector)
        self.assertEqual(self.calls, 4)
        self.assertEqual(self.writes, 0)

    def test_similar_path_not_exempt_and_cache_optout(self):
        self.fetch(binary_path=build.HELPER+'.other')
        self.fetch(binary_path=build.HELPER+'.other')
        self.assertEqual(self.calls, 1)
        self.fetch(use_cache=False)
        self.assertEqual(self.calls, 2)

    def test_disabled_ttl_never_stores(self):
        for ttl in [0, -1, float('inf'), float('nan')]:
            self.fetch(cache_ttl_seconds=ttl)
            self.fetch(cache_ttl_seconds=ttl)
        self.assertEqual(self.calls, 8)
        self.assertEqual(self.writes, 0)
        self.assertFalse(self.store.memory)

    def test_slow_provider_origin_and_future_cache_fail_closed(self):
        def slow(*args, **kwargs):
            self.calls += 1
            self.clock.now += 100
            return 'synthetic-slow'
        self.n['_run_op_read'] = slow
        self.fetch(cache_ttl_seconds=1800)
        self.assertEqual(self.disk.fetched_at, 10000)
        self.clock.now = 11740
        self.fetch(cache_ttl_seconds=1800)
        self.assertEqual(self.calls, 2)
        self.disk.fetched_at = self.clock.now + 1
        self.fetch(cache_ttl_seconds=1800)
        self.assertEqual(self.calls, 3)
        self.n['_run_op_read'] = lambda *a, **kw: (setattr(self.clock, 'now', self.clock.now + 1740) or 'synthetic-too-old')
        self.assertEqual(self.fetch(use_cache=False)[0], {})

    def test_legacy_cache_namespace_invalidated(self):
        self.fetch()
        self.assertTrue(all(key[-1].startswith('age-budget-v2:') for key in self.store.memory))

    def test_public_wrappers_propagate_exact_pin(self):
        kwargs = dict(binary_path=build.HELPER, cache_ttl_seconds=86400)
        self.n['apply_onepassword_secrets'](enabled=True, env={'FAKE_KEY':'op://fake/item/field'}, **kwargs)
        self.n['OnePasswordSource'].token_env = lambda self, cfg: 'FAKE_TOKEN'
        self.n['OnePasswordSource']().fetch(dict(env={'FAKE_KEY':'op://fake/item/field'}, **kwargs), Path('/synthetic/home'))
        self.assertEqual(self.calls, 2)
        self.assertEqual(self.writes, 0)

    def test_guard_and_snapshot_residual_are_unchanged(self):
        text = (build.LIVE / 'hermes_cli/env_loader.py').read_text()
        self.assertEqual(build.sha(text.encode()), build.ENV_SHA)
        self.assertIn('source.get("allow_unattended_helper") is True', text)
        self.assertIn('if home_key in _APPLIED_HOMES:', text)
        self.assertIn('return get_secret_source_values(home)', text)

    def test_env_snapshot_has_no_ttl_residual(self):
        tree = ast.parse((build.LIVE / 'hermes_cli/env_loader.py').read_text())
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'get_secret_source_values']
        home = str(Path('/synthetic/home').resolve())
        scope = dict(Path=Path, os=SimpleNamespace(PathLike=object), _SECRET_SOURCE_VALUES_BY_HOME={home:{'FAKE_KEY':'synthetic-old'}})
        module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)] + functions, type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), '<isolated-snapshot>', 'exec'), scope)
        self.clock.now += 100000
        self.assertEqual(scope['get_secret_source_values'](home), {'FAKE_KEY':'synthetic-old'})

    def test_hash_mismatch_and_anchor_fail_closed(self):
        with self.assertRaises(ValueError):
            build.overlay('unrelated source')
        manifest = json.loads((build.OUT / 'source_manifest.json').read_text())
        for relative, digest in manifest['source_sha256'].items():
            self.assertEqual(build.sha((build.LIVE / relative).read_bytes()), digest)
        self.assertEqual(build.sha((build.OUT/'onepassword.candidate.py').read_bytes()), manifest['candidate_sha256'])

if __name__ == '__main__':
    unittest.main(verbosity=2)
