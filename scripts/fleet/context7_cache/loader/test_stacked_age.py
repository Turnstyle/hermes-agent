"""Synthetic H01 integration: production transport + exact-source loader overlays."""
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import test_env_overlay as env_tests
import test_overlay as op_tests

spec = importlib.util.spec_from_file_location('h01_transport', Path(__file__).resolve().parents[1]/'transport/secret_value_cache.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
REF = 'op://keys-router/jtp-bots-suite Context7 API Key/credential'

class StackedAgeTests(unittest.TestCase):
    def exercise(self, shared, mode, fail=False, disk=False):
        clock = [10000., 1000.]
        version = [1]
        reads = []
        with tempfile.TemporaryDirectory(dir=Path('/Users/sheldon/.hermes/profiles/shld-mm-hermes-agent/cache/scratch')) as tmp:
            cache = Path(tmp)/'cache'
            def provider(ref):
                reads.append(clock[0])
                if fail and version[0] == 2:
                    raise RuntimeError('synthetic unavailable')
                return ('synthetic-version-'+str(version[0])).encode()
            op = op_tests.OverlayTests(); op.setUp()
            # Integration fixture accepts either source policy; red must fail on stale
            # return, not the updated ordinary unit fixture's TTL assertion.
            op.store.disk.write = lambda key, entry, ttl, home: setattr(op, 'disk', entry)
            op.n['_run_op_read'] = lambda *a, **kw: (c.get(REF, provider, _directory=cache, _clock=lambda: tuple(clock)) if shared else provider(REF)).decode()
            def fetch(home):
                op.clock.now = clock[0]
                return op.n['fetch_onepassword_secrets'](references={'FAKE_KEY': REF}, binary_path=op_tests.build.HELPER if shared else '/fake/op', cache_ttl_seconds=1800, home_path=home)
            home = Path('/synthetic/home-a')
            first, warnings = fetch(home)
            self.assertFalse(warnings)
            if disk:
                op.store.memory.clear()  # exercise disk promotion without restarting origin
            env = env_tests.EnvOverlayTests(); env.setUp()
            try:
                def apply(cfg, home, environ=None):
                    values, warnings = fetch(home)
                    target = env.env if environ is None else environ
                    applied = list(values)
                    target.update(values)
                    src = SimpleNamespace(result=SimpleNamespace(ok=not warnings, warnings=warnings, error='', error_kind=None), name='onepassword', label='1Password', skipped_existing=[], applied=applied)
                    return SimpleNamespace(sources=[src], provenance={k:SimpleNamespace(source='onepassword', authoritative=True) for k in values}, applied_any=bool(values), conflicts=[])
                sys.modules['agent.secret_sources.registry'].apply_all = apply
                def observe():
                    if mode == 'routed': return env.hydrate()
                    env.global_apply()
                    return env.snapshot()
                clock[:] = [11739., 2739.]; env.now = clock[0]
                self.assertEqual(observe(), first)
                clock[:] = [11798., 2798.]; env.now = clock[0]
                self.assertEqual(observe(), first)
                self.assertEqual(len(reads), 1, 'outer hit must not read provider')
                version[0] = 2
                clock[:] = [11800., 2800.]; env.now = clock[0]
                if mode == 'dotenv':
                    env.n['_apply_managed_env'] = lambda **kw: None
                    env.n['_reapply_terminal_config_bridge'] = lambda p: None
                    env.env['FAKE_KEY'] = 'placeholder'
                    env.n['load_hermes_dotenv'](hermes_home=env.home, load_external_secrets=False)
                    self.assertEqual(env.env['FAKE_KEY'], 'placeholder')
                    self.assertEqual(env.snapshot(), {})
                else:
                    later = observe()
                    self.assertNotEqual(later.get('FAKE_KEY'), first['FAKE_KEY'], 'H01: original provider age >=1800')
                    self.assertEqual(len(reads), 2)
                    if fail: self.assertNotIn('FAKE_KEY', later)
                    else: self.assertEqual(later['FAKE_KEY'], 'synthetic-version-2')
            finally:
                env.doCleanups()

    def test_late_joining_cache_stack(self):
        for shared in (True, False):
            for mode in ('routed', 'global', 'dotenv'):
                with self.subTest(shared=shared, mode=mode): self.exercise(shared, mode)

    def test_disk_late_joining_cache_stack(self):
        for mode in ('routed', 'global', 'dotenv'):
            with self.subTest(mode=mode): self.exercise(False, mode, disk=True)

    def test_refresh_failure_never_returns_original(self):
        for shared in (True, False):
            for mode in ('routed', 'global'):
                with self.subTest(shared=shared, mode=mode): self.exercise(shared, mode, fail=True)

if __name__ == '__main__': unittest.main(verbosity=2)
