"""Offline-only adversarial config contracts. All writes use TMPDIR fixtures."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / 'context7_helper_config_flags.py'
spec = importlib.util.spec_from_file_location('flags', SCRIPT)
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)


def fixture(binary=f.HELPER, ttl='86400', flag='false', newline='\n'):
    return ('# π preserve\nmodel: unchanged  # exact\nsecrets:\n  onepassword:\n'
            '    binary_path: ' + binary + '\n    allow_unattended_helper: ' + flag +
            '\n    cache_ttl_seconds: ' + ttl + ' # ttl\n    enabled: true\n'
            'unrelated: {enabled: false, count: 12}\n').replace('\n', newline)


class Contracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'profiles'
        self.root.mkdir()

    def put(self, name='helper', text=None):
        p = self.root / name / 'config.yaml'
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes((fixture() if text is None else text).encode())
        return p

    def test_helper_exact_byte_delta(self):
        p = self.put()
        before = p.read_bytes()
        item = f.plan(p)
        self.assertEqual(item['new'], before.replace(b'false\n    cache', b'true\n    cache').replace(b'86400 # ttl', b'1800 # ttl'))
        self.assertEqual(p.read_bytes(), before)

    def test_helper_missing_optin_and_ttl(self):
        p = self.put(text='secrets:\n  onepassword:\n    binary_path: ' + f.HELPER + '\n')
        result = f.yaml.safe_load(f.plan(p)['new'])['secrets']['onepassword']
        self.assertIs(result['allow_unattended_helper'], True)
        self.assertEqual(result['cache_ttl_seconds'], 1800)

    def test_helper_always_exact_ttl(self):
        for ttl in ['0', '1799', '1800', '1801', '86400', '"1800"']:
            with self.subTest(ttl=ttl):
                p = self.put(text=fixture(ttl=ttl))
                result = f.yaml.safe_load(f.plan(p)['new'])['secrets']['onepassword']
                self.assertEqual(result['cache_ttl_seconds'], 1800)
                self.assertIs(result['allow_unattended_helper'], True)

    def test_ordinary_ttl_cap_no_optin(self):
        p = self.put('shld-keys-router', fixture('/opt/homebrew/bin/op'))
        result = f.yaml.safe_load(f.plan(p)['new'])['secrets']['onepassword']
        self.assertEqual(result['cache_ttl_seconds'], 1800)
        self.assertIs(result['allow_unattended_helper'], False)

    def test_ordinary_low_ttl_and_unpinned_binary_untouched(self):
        for ttl in ['0', '1799', '1800', '"86400"', 'true', 'null']:
            with self.subTest(ttl=ttl):
                p = self.put(text=fixture(f.HELPER + '.other', ttl=ttl))
                self.assertIsNone(f.plan(p))

    def test_only_two_mcp_enablements(self):
        for name in [*f.CONTEXT7_PROFILES, 'shld-cndr', 'other']:
            p = self.put(name, fixture('/usr/bin/op', ttl='1800') + 'mcp_servers:\n  context7:\n    enabled: "false" # keep\n    timeout: 20\n')
            item = f.plan(p)
            if name in f.CONTEXT7_PROFILES:
                self.assertIn(b'enabled: true # keep', item['new'])
                self.assertIn(b'timeout: 20', item['new'])
            else:
                self.assertIsNone(item)

    def test_mcp_missing_parents(self):
        for text in ['model: same\n', 'model: same\nmcp_servers:\n  other:\n    enabled: false\n', 'mcp_servers:\n  context7:\n    timeout: 30\n']:
            p = self.put('shld-coordinator', text)
            result = f.yaml.safe_load(f.plan(p)['new'])
            self.assertIs(result['mcp_servers']['context7']['enabled'], True)

    def test_crlf_unicode_preserved(self):
        p = self.put(text=fixture(newline='\r\n'))
        self.assertEqual(f.plan(p)['new'].count(b'\r\n'), p.read_bytes().count(b'\r\n'))

    def test_duplicate_yaml_anywhere_refused(self):
        for extra in ['x: 1\nx: 2\n', 'x: {a: 1, a: 2}\n', 'x:\n  - {a: 1, a: 2}\n']:
            p = self.put(text=fixture() + extra)
            with self.assertRaises(ValueError):
                f.plan(p)

    def test_alias_merge_tags_and_bad_shapes_refused(self):
        for text in ['x: &a {v: 1}\ny: *a\n', 'x: {<<: {v: 1}}\n', '!!python/object:evil {}', '[]', 'secrets: nope', 'secrets: {onepassword: []}', 'mcp_servers: false']:
            p = self.put('shld-coordinator', text)
            with self.subTest(text=text), self.assertRaises((ValueError, f.yaml.YAMLError)):
                f.plan(p)

    def test_symlink_file_profile_and_root_refused(self):
        p = self.put()
        target = self.base / 'target.yaml'
        p.rename(target)
        p.symlink_to(target)
        with self.assertRaises(ValueError): f.run(self.root)
        p.unlink()
        p.write_text(fixture())
        alias = self.root / 'alias'
        alias.symlink_to(p.parent, target_is_directory=True)
        with self.assertRaises(ValueError): f.run(self.root)
        alias.unlink()
        linked = self.base / 'linked'
        linked.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError): f.run(linked)

    def test_hardlink_refused(self):
        p = self.put()
        os.link(p, self.base / 'hardlink')
        with self.assertRaises(ValueError): f.plan(p)

    def test_exact_one_level_discovery_no_env(self):
        self.put()
        nested = self.root / 'nested' / 'deep'
        nested.mkdir(parents=True)
        (nested / 'config.yaml').write_text('invalid: [')
        (self.root / 'helper' / '.env').symlink_to(self.base / 'missing')
        self.assertEqual(f.run(self.root)['scanned_count'], 1)

    def test_dryrun_apply_backup_mode_and_idempotence(self):
        p = self.put()
        p.chmod(0o640)
        before = p.read_bytes()
        dry = f.run(self.root)
        self.assertEqual(dry, f.run(self.root))
        self.assertEqual(before, p.read_bytes())
        backups = self.base / 'backups'
        f.run(self.root, True, backups)
        self.assertEqual((backups / 'helper-config.yaml').read_bytes(), before)
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(backups.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((backups / 'helper-config.yaml').stat().st_mode), 0o600)
        self.assertEqual(f.run(self.root)['changed_count'], 0)
        f.run(self.root, True, backups)  # no-op doesn't touch existing backup
        self.assertFalse((self.root / '.context7-config.lock').exists())

    def test_backup_directory_collision_and_symlink(self):
        p = self.put()
        before = p.read_bytes()
        backups = self.base / 'backups'
        backups.mkdir()
        with self.assertRaises(FileExistsError): f.run(self.root, True, backups)
        backups.rmdir()
        backups.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError): f.run(self.root, True, backups)
        self.assertEqual(p.read_bytes(), before)

    def test_existing_writer_lock_refused_without_removal(self):
        self.put()
        lock = self.root / '.context7-config.lock'
        lock.write_text('other writer')
        with self.assertRaises(FileExistsError): f.run(self.root, True, self.base / 'backup')
        self.assertEqual(lock.read_text(), 'other writer')

    def test_concurrent_writer_identity_conflict(self):
        p = self.put()
        item = f.plan(p)
        replacement = self.base / 'replacement'
        replacement.write_bytes(item['raw'])
        replacement.replace(p)
        with self.assertRaises(ValueError): f.apply([item], self.base / 'backup', {})
        self.assertEqual(p.read_bytes(), item['raw'])

    def test_backup_failure_before_any_replace(self):
        a, b = self.put('a'), self.put('b')
        original = f.exclusive_file
        def fail_second(path, data):
            if path.name == 'b-config.yaml': raise OSError('injected full disk')
            return original(path, data)
        with patch.object(f, 'exclusive_file', side_effect=fail_second):
            with self.assertRaises(OSError): f.run(self.root, True, self.base / 'backup')
        self.assertEqual(a.read_text(), fixture())
        self.assertEqual(b.read_text(), fixture())
        self.assertFalse(list(self.root.glob('*/.context7-*')))

    def test_precreate_failure_before_any_replace(self):
        p = self.put()
        with patch.object(f.tempfile, 'mkstemp', side_effect=OSError('injected')):
            with self.assertRaises(OSError): f.run(self.root, True, self.base / 'backup')
        self.assertEqual(p.read_text(), fixture())

    def test_replace_failure_preserves_original_and_backup(self):
        p = self.put()
        with patch.object(f.os, 'replace', side_effect=OSError('injected')):
            with self.assertRaises(OSError): f.run(self.root, True, self.base / 'backup')
        self.assertEqual(p.read_text(), fixture())
        self.assertEqual((self.base / 'backup' / 'helper-config.yaml').read_text(), fixture())
        self.assertFalse(list(self.root.glob('*/.context7-*')))

    def test_literal_true_normalizes_yes(self):
        p = self.put('shld-coordinator', fixture(flag='YES') + 'mcp_servers:\n  context7:\n    enabled: on\n')
        new = f.plan(p)['new']
        self.assertIn(b'allow_unattended_helper: true', new)
        self.assertIn(b'enabled: true', new)
        self.assertNotIn(b'enabled: on', new)

    def test_unscoped_anchors_are_skipped_not_edited(self):
        p = self.put(text='x: &a {value: 1}\ny: *a\n')
        self.assertIsNone(f.plan(p))

    def test_scoped_anchor_refused(self):
        p = self.put(text=fixture() + 'x: &a {value: 1}\ny: *a\n')
        with self.assertRaises(ValueError): f.plan(p)

    def test_flow_existing_scalars_and_missing_field_refusal(self):
        p = self.put(text='secrets: {onepassword: {binary_path: ' + f.HELPER + ', cache_ttl_seconds: 86400, allow_unattended_helper: false}}\n')
        self.assertIn(b'cache_ttl_seconds: 1800', f.plan(p)['new'])
        p.write_text('secrets: {onepassword: {binary_path: ' + f.HELPER + '}}\n')
        with self.assertRaises(ValueError): f.plan(p)

    def test_concurrent_change_during_precreation(self):
        p = self.put()
        original = f.exclusive_file
        def alter(path, data):
            original(path, data)
            if path.name == 'manifest.json': p.write_text(fixture() + '# other writer\n')
        with patch.object(f, 'exclusive_file', side_effect=alter):
            with self.assertRaises(ValueError): f.run(self.root, True, self.base / 'backup')
        self.assertTrue(p.read_text().endswith('# other writer\n'))
        self.assertFalse(list(self.root.glob('*/.context7-*')))

    def test_all_backups_exist_before_first_replace(self):
        self.put('a')
        self.put('b')
        original = f.os.replace
        backup = self.base / 'backup'
        def verify_then_replace(src, dst):
            self.assertEqual(len(list(backup.glob('*-config.yaml'))), 2)
            self.assertTrue((backup / 'manifest.json').exists())
            return original(src, dst)
        with patch.object(f.os, 'replace', side_effect=verify_then_replace):
            f.run(self.root, True, backup)

    def test_cli_apply_requires_backup(self):
        self.put()
        proc = subprocess.run([sys.executable, str(SCRIPT), '--profiles-root', str(self.root), '--apply'], capture_output=True)
        self.assertEqual(proc.returncode, 2)

    def test_cli_error_redacts_yaml_source(self):
        self.put(text='PRIVATE_SENTINEL: [\n')
        proc = subprocess.run([sys.executable, str(SCRIPT), '--profiles-root', str(self.root)], capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn('PRIVATE_SENTINEL', proc.stderr + proc.stdout)

    def test_cli_manifest_no_source_values(self):
        p = self.put(text=fixture() + 'private_value: PRIVATE_SENTINEL\n')
        proc = subprocess.run([sys.executable, str(SCRIPT), '--profiles-root', str(self.root)], capture_output=True, text=True, check=True)
        self.assertNotIn('PRIVATE_SENTINEL', proc.stdout)
        self.assertNotIn(f.HELPER, proc.stdout)
        self.assertEqual(json.loads(proc.stdout)['changed_count'], 1)
        self.assertIn('PRIVATE_SENTINEL', p.read_text())


if __name__ == '__main__':
    unittest.main()
