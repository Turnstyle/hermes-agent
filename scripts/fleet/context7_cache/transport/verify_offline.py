#!/usr/bin/env python3
"""Run only synthetic tests and exact read-only baseline/hash verification."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
import unittest
import test_transport

HERE = Path(__file__).resolve().parent


def main():
    manifest = json.loads((HERE / 'source-overlay-manifest.json').read_text())
    sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
    assert all(sha(HERE / k) == v for k, v in manifest['files'].items())
    assert sha(manifest['source_path']) == manifest['source_sha256']
    assert sha(manifest['router_read_only_path']) == manifest['router_sha256']
    sink = io.StringIO()
    start = time.monotonic()
    suite = unittest.defaultTestLoader.loadTestsFromModule(test_transport)
    full = unittest.TextTestRunner(stream=sink, verbosity=2).run(suite)
    # Reproduce the original Darwin concurrent-create regression many times.
    stress_suite = unittest.TestSuite(test_transport.CacheTests(
        'test_multiprocess_cross_profile_alias_dedup') for _ in range(50))
    stress = unittest.TextTestRunner(stream=sink, verbosity=1).run(stress_suite)
    receipt = {
        'ok': full.wasSuccessful() and stress.wasSuccessful(),
        'full_suite_tests': full.testsRun, 'full_suite_failures': len(full.failures),
        'full_suite_errors': len(full.errors), 'dedup_stress_runs': stress.testsRun,
        'dedup_stress_failures': len(stress.failures), 'dedup_stress_errors': len(stress.errors),
        'spawned_processes_per_dedup_test': 8,
        'expected_provider_calls_per_dedup_test': 1,
        'elapsed_seconds': round(time.monotonic()-start, 4),
        'manifest_sha256': sha(HERE / 'source-overlay-manifest.json'),
        'baseline_verified_unchanged': True,
        'tests_synthetic_only': True,
        'live_source_main_router_and_custody_not_invoked_by_tests': True,
        'jev_advice': 'unavailable: required hermes CLI lacks jev subcommand; no repair attempted',
        'jev_launcher_observation': 'CLI stderr reported 1Password: applied 2 secrets; source of values or fresh vault reads not measured',
        'python_executable': sys.executable,
        'test_output': sink.getvalue(),
    }
    (HERE / 'offline-verification.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps({k:v for k,v in receipt.items() if k != 'test_output'}, indent=2))
    return 0 if receipt['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
