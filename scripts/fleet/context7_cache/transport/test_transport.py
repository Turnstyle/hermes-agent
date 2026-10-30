#!/usr/bin/env python3
"""All values/providers are synthetic. Never run live key_op main/router/custody."""
import ast
import base64
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import plistlib
import stat
import tempfile
import time
import unittest
from unittest import mock
import secret_value_cache as c

REF = 'op://keys-router/jtp-bots-suite Context7 API Key/credential'
LEGACY = next(iter(c.LEGACY_REFS))
SCRATCH = Path('/Users/sheldon/.hermes/profiles/shld-mm-hermes-agent/cache/scratch/context7-cache-correction-20261030/transport')
HERE = Path(__file__).resolve().parent


def worker(path, ref, count, start, results):
    os.environ['HERMES_PROFILE'] = 'synthetic-profile-' + str(os.getpid())
    start.wait(5)
    def provider(canonical):
        with count.get_lock():
            count.value += 1
        time.sleep(0.08)
        return b'synthetic-key-only'
    try:
        results.put(c.get(ref, provider, _directory=path).decode())
    except Exception:
        import traceback
        results.put(traceback.format_exc())


class CacheTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'cache'
        self.now = [10000.0, 1000.0]
        self.events = []
        self.calls = 0

    def provider(self, ref):
        self.calls += 1
        return b'synthetic-value'

    def get(self, ref=REF, **kw):
        return c.get(ref, kw.pop('provider', self.provider), _directory=self.path,
                     _clock=lambda: tuple(self.now), evidence=self.events.append, **kw)

    def record(self):
        return self.path / (c.identity(REF) + '.json')

    def mutate(self, **kw):
        r = json.loads(self.record().read_text())
        r.update(kw)
        self.record().write_text(json.dumps(r))

    def test_hit_miss_metadata_and_modes(self):
        self.assertEqual(self.get(), self.get())
        self.assertEqual(self.calls, 1)
        self.assertEqual([e['event'] for e in self.events], ['miss', 'provider_read', 'hit'])
        for e in self.events:
            self.assertEqual(set(e), {'event', 'ref_id'})
            self.assertNotIn('synthetic', json.dumps(e))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o700)
        for p in self.path.iterdir():
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertFalse(list(self.path.glob('*.tmp')))

    def test_multiprocess_cross_profile_alias_dedup(self):
        ctx = multiprocessing.get_context('spawn')
        count, start, results = ctx.Value('i', 0), ctx.Event(), ctx.Queue()
        refs = [LEGACY, c.canonical(LEGACY)] * 4
        processes = [ctx.Process(target=worker, args=(str(self.path), ref, count, start, results)) for ref in refs]
        for p in processes:
            p.start()
        start.set()
        for p in processes:
            p.join(15)
            self.assertEqual(p.exitcode, 0)
        self.assertEqual([results.get(timeout=2) for _ in refs], ['synthetic-key-only'] * len(refs))
        self.assertEqual(count.value, 1)
        self.assertEqual(len(list(self.path.glob('*.json'))), 1)
        results.close()

    def test_exact_ttl_boundary_and_cleanup_without_read(self):
        self.get(ttl=10)
        self.now[:] = [10009.0, 1009.0]
        self.get()
        self.assertEqual(self.calls, 1)
        self.now[:] = [10010.0, 1010.0]
        self.assertEqual(c.cleanup(_directory=self.path, _clock=lambda: tuple(self.now)), 1)
        self.assertFalse(self.record().exists())
        self.get()
        self.assertEqual(self.calls, 2)

    def test_max_ttl(self):
        for ttl in (0, -1, 1801, True, float('nan'), float('inf'), '30'):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                self.get(ttl=ttl)
        self.get(ttl=1800)
        self.now[:] = [11800, 2800]
        self.get()
        self.assertEqual(self.calls, 2)

    def test_clock_rollback_and_forward(self):
        for wall, mono in ((9999, 1001), (10001, 999), (10020, 1001), (20000, 1001)):
            with self.subTest(wall=wall, mono=mono):
                self.now[:] = [10000, 1000]
                self.get()
                before = self.calls
                self.now[:] = [wall, mono]
                self.get()
                self.assertEqual(self.calls, before + 1)
                self.record().unlink()

    def test_invalid_records_refetch(self):
        cases = [dict(v=True), dict(v=2), dict(ref=LEGACY), dict(created=True), dict(created=float('nan')),
                 dict(ttl=1801), dict(ttl=-1), dict(expires=10001), dict(value='%%%'),
                 dict(value=base64.b64encode(b'ops_not-allowed').decode()), dict(extra='unexpected'),
                 dict(value=base64.b64encode(b'x\ny').decode())]
        for change in cases:
            with self.subTest(change=change):
                self.now[:] = [10000, 1000]
                self.get()
                self.mutate(**change)
                before = self.calls
                self.get()
                self.assertEqual(self.calls, before + 1)
        for raw in ('{}', '{bad', '[]', 'x' * 90001, '{"v":1,"v":1}'):
            self.record().write_text(raw)
            before = self.calls
            self.get()
            self.assertEqual(self.calls, before + 1)

    def test_provider_errors_not_cached_no_stale(self):
        self.get(ttl=1)
        self.now[:] = [10001, 1001]
        def fail(ref):
            raise RuntimeError('synthetic provider failure')
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                self.get(provider=fail)
            self.assertFalse(self.record().exists())
        self.get()
        self.assertEqual(self.calls, 2)

    def test_bad_values_auth_tokens_never_written(self):
        for value in (b'', b'x\ny', b'x\ry', b'x\x00y', b'ops_synthetic', b'OP_SESSION_x',
                      b'op://a/b/c', b'Bearer x', b'\xff', b'x' * 65537, 'not-bytes'):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                self.get(provider=lambda r: value)
            self.assertFalse(self.record().exists())

    def test_nonallowlisted_ref_no_provider(self):
        for ref in ('op://jtp-bots-suite/arbitrary/credential', 'op://keys-router/arbitrary/credential', 'ops_x', None):
            with self.assertRaises(c.CacheSecurityError):
                self.get(ref)
        self.assertEqual(self.calls, 0)
        self.assertFalse(self.path.exists())

    def test_symlink_leaf_and_parent(self):
        target = Path(self.tmp.name) / 'target'
        target.mkdir(mode=0o700)
        self.path.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            self.get()
        self.path.unlink()
        parent = Path(self.tmp.name) / 'alias'
        parent.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            c.get(REF, self.provider, _directory=parent / 'cache')
        self.assertEqual(self.calls, 0)

    def test_insecure_dir_permissions(self):
        self.path.mkdir(mode=0o755)
        with self.assertRaises(c.CacheSecurityError):
            self.get()
        self.assertEqual(self.calls, 0)

    def test_symlink_files_hardlink_and_permissions(self):
        self.get()
        original = self.record().read_bytes()
        self.record().chmod(0o644)
        with self.assertRaises(c.CacheSecurityError):
            self.get()
        self.record().chmod(0o600)
        other = Path(self.tmp.name) / 'other'
        os.link(self.record(), other)
        with self.assertRaises(c.CacheSecurityError):
            self.get()
        other.unlink()
        self.record().unlink()
        other.write_bytes(original)
        self.record().symlink_to(other)
        with self.assertRaises(OSError):
            self.get()
        self.record().unlink()
        lock = self.path / (c.identity(REF) + '.lock')
        lock.unlink()
        lock.symlink_to(other)
        with self.assertRaises(OSError):
            self.get()
        self.assertEqual(self.calls, 1)

    def test_nonowner_rejected(self):
        self.get()
        real = os.fstat
        def fake(fd):
            s = real(fd)
            fields = list(s)
            fields[4] = os.getuid() + 1
            return os.stat_result(fields)
        with mock.patch.object(c.os, 'fstat', side_effect=fake), self.assertRaises(c.CacheSecurityError):
            self.get()
        self.assertEqual(self.calls, 1)

    def test_atomic_failure_no_cache_or_temp(self):
        with mock.patch.object(c.os, 'replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                self.get()
        self.assertFalse(self.record().exists())
        self.assertFalse(list(self.path.glob('*.tmp')))
        self.get()
        self.assertEqual(self.calls, 2)

    def test_slow_provider_not_cached(self):
        def slow(ref):
            self.now[:] = [11800, 2800]
            return b'synthetic'
        with self.assertRaises(ValueError):
            self.get(provider=slow)
        self.assertFalse(self.record().exists())

    def test_candidate_guards_and_fake_provider(self):
        spec = importlib.util.spec_from_file_location('candidate_test', HERE / 'key_op.candidate.py')
        candidate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(candidate)
        self.assertEqual(candidate.LEGACY_REFS, c.LEGACY_REFS)
        self.assertEqual(candidate.CURRENT_REFS, c.ALLOWED_REFS)
        calls = []
        real_get = c.get
        def isolated(ref, provider):
            return real_get(ref, provider, _directory=self.path)
        with mock.patch.object(c, 'get', side_effect=isolated), mock.patch.object(candidate, '_current_provider_read', side_effect=lambda r, m: calls.append(r) or b'synthetic-overlay'):
            self.assertEqual(candidate.resolve(LEGACY, object()), b'synthetic-overlay')
            self.assertEqual(candidate.resolve(c.canonical(LEGACY), object()), b'synthetic-overlay')
        self.assertEqual(calls, [c.canonical(LEGACY)])
        # Static guards: no live auth function/main/router call is made.
        source = (HERE / 'key_op.candidate.py').read_text()
        for guard in ("OP_LOAD_DESKTOP_APP_SETTINGS='false'", "OP_BIOMETRIC_UNLOCK_ENABLED='false'", "env=custody_env(module)", 'start_new_session=True'):
            self.assertIn(guard, source)

    def test_cleanup_abandoned_temps_without_provider(self):
        self.get()
        temp = self.path / (c.identity(REF) + '.' + 'a' * 24 + '.tmp')
        temp.write_bytes(b'synthetic-crash-left-value')
        temp.chmod(0o600)
        unrelated = self.path / 'unrelated.tmp'
        unrelated.write_text('synthetic')
        self.assertEqual(c.cleanup(_directory=self.path, _clock=lambda: tuple(self.now)), 1)
        self.assertFalse(temp.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(self.record().exists())
        self.assertEqual(self.calls, 1)

    def test_per_ref_lock_names_are_distinct(self):
        self.get()
        self.get(c.canonical(LEGACY))
        self.assertEqual(self.calls, 2)
        self.assertEqual(len(list(self.path.glob('*.lock'))), 2)
        self.assertEqual(len(list(self.path.glob('*.json'))), 2)

    def test_cleanup_plist(self):
        p = plistlib.loads((HERE / 'cleanup.launchd.plist').read_bytes())
        self.assertEqual(p['StartInterval'], 30)
        self.assertTrue(p['RunAtLoad'])
        self.assertEqual(p['ProgramArguments'][-1], '--cleanup')
        self.assertFalse('EnvironmentVariables' in p)


if __name__ == '__main__':
    unittest.main(verbosity=2)
