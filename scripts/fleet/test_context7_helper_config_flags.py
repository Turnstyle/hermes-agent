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
