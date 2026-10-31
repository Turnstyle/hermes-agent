"""Offline config transform and apply safety contracts; no live writes."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("context7_helper_config_flags.py")
spec = importlib.util.spec_from_file_location("flags", SCRIPT)
flags = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flags)


class FlagConfigContract(unittest.TestCase):
    def anchored(self, ttl='1800', allow='true', binary=None):
        return ('unrelated: &values [one, two]\ncopy: *values\n'
                'secrets:\n  onepassword:\n    binary_path: ' + (binary or flags.HELPER)
                + '\n    allow_unattended_helper: ' + allow + '\n'
                + ('' if ttl is None else '    cache_ttl_seconds: ' + ttl + '\n'))

    def test_anchored_helper_noop_preserves_bytes_and_creates_no_backup(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as temp:
            root = Path(temp)
            for name in ('cc-carter-producer', 'shld-cc-script-carter',
                         'shld-cc-script-lead', 'shld-cc-script-pappy'):
                path = root / name / 'config.yaml'
                path.parent.mkdir()
                for ttl in ('1800', '900'):
                    with self.subTest(profile=name, ttl=ttl):
                        raw = self.anchored(ttl).encode()
                        path.write_bytes(raw)
                        item = flags.plan(path)
                        self.assertEqual(item['status'], 'already_set')
                        self.assertEqual(item['settings'], [])
                        self.assertEqual(item['new'], raw)
                        flags.apply([item], root / 'backup', {})
                        self.assertFalse((root / 'backup').exists())
                        self.assertEqual(path.read_bytes(), raw)

    def test_anchored_needing_write_or_ambiguous_values_refused(self):
        cases = [self.anchored(ttl) for ttl in
                 (None, '86400', '1801', '1800.0', '"1800"', 'true', 'null')]
        cases += [self.anchored(allow=value) for value in
                  ('false', '"true"', '1', 'yes', 'True', 'null')]
        cases += [self.anchored().replace('    allow_unattended_helper: true\n', ''),
                  'flag: &flag true\n' + self.anchored(allow='*flag'),
                  self.anchored() + '    cache_ttl_seconds: 1800\n',
                  self.anchored() + '    <<: {}\n',
                  self.anchored(binary='/wrong/binary')]
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as temp:
            path = Path(temp) / 'shld-coordinator' / 'config.yaml'
            path.parent.mkdir()
            for raw in cases:
                with self.subTest(case=cases.index(raw)):
                    path.write_text(raw + 'mcp_servers:\n  context7:\n    enabled: true\n')
                    with self.assertRaises(ValueError):
                        flags.plan(path)

    def test_anchored_context7_noop_requires_literal_enabled(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as temp:
            path = Path(temp) / 'shld-coordinator' / 'config.yaml'
            path.parent.mkdir()
            for enabled in ('true', 'false', 'yes', '"true"'):
                path.write_text(self.anchored() +
                                'mcp_servers:\n  context7:\n    enabled: ' + enabled + '\n')
                if enabled == 'true':
                    self.assertEqual(flags.plan(path)['status'], 'already_set')
                else:
                    with self.assertRaises(ValueError):
                        flags.plan(path)

    def test_anchored_noop_still_refuses_unsafe_paths(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as temp:
            root = Path(temp)
            real = root / 'real'
            real.mkdir()
            config = real / 'config.yaml'
            config.write_text(self.anchored())
            link = root / 'link.yaml'
            link.symlink_to(config)
            parent_link = root / 'parent-link'
            parent_link.symlink_to(real, target_is_directory=True)
            for path in (link, parent_link / 'config.yaml'):
                with self.assertRaises(ValueError):
                    flags.plan(path)
            os.link(config, root / 'hard-link.yaml')
            with self.assertRaises(ValueError):
                flags.plan(config)

    def test_scoped_backed_up_atomic_apply_is_idempotent(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temp:
            root = Path(temp)
            profiles = root / "profiles"
            selected = profiles / "selected" / "config.yaml"
            selected.parent.mkdir(parents=True)
            before = ("# preserve comment\nmodel: unchanged\nsecrets:\n  onepassword:\n"
                      "    enabled: true\n    binary_path: " + flags.HELPER +
                      "\n    cache_ttl_seconds: 1800\n    mapping:\n      CONTEXT7_API_KEY: op://test/item/field\n")
            selected.write_text(before)
            other = profiles / "other" / "config.yaml"
            other.parent.mkdir()
            other.write_text(before.replace(flags.HELPER, "/opt/homebrew/bin/op"))
            other_before = other.read_bytes()
            args = [sys.executable, str(SCRIPT), "--profiles-root", str(profiles)]
            dry = json.loads(subprocess.check_output(args, text=True, timeout=15))
            self.assertEqual((dry["matched_count"], dry["changed_count"]), (1, 1))
            self.assertEqual(selected.read_text(), before)
            backup = root / "backup"
            applied = json.loads(subprocess.check_output(args + ["--apply", "--backup-dir", str(backup)], text=True, timeout=15))
            self.assertEqual((backup / "selected-config.yaml").read_text(), before)
            self.assertEqual(other.read_bytes(), other_before)
            self.assertEqual(applied["profiles"][0]["after_sha256"], flags.sha(selected.read_bytes()))
            self.assertEqual(json.loads(subprocess.check_output(args, text=True, timeout=15))["changed_count"], 0)
            self.assertIn("# preserve comment", selected.read_text())


if __name__ == "__main__":
    unittest.main()
