"""Exercise the actual candidate startup selector without importing secret loaders.

Run with --source PATH. No credentials are read and no external commands run.
The candidate source is AST-selected, not a duplicate implementation fixture.
"""
import argparse
import ast
import contextlib
import io
from pathlib import Path
import threading
from collections.abc import Mapping
import unittest


class StartupHelperContract(unittest.TestCase):
    source_path = None

    def setUp(self):
        tree = ast.parse(self.source_path.read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                        and n.name == "_startup_secret_sources")
        self.ns = {
            "Mapping": Mapping, "os": __import__("os"), "sys": __import__("sys"),
            "_startup_onepassword_skip_reason": lambda: "launchd job",
            "_SECRET_SOURCE_CACHE_LOCK": threading.RLock(),
            "_ONEPASSWORD_SKIP_LOGGED": False,
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(self.source_path), "exec"), self.ns)

    def select(self, source, environ=None):
        cfg = {"onepassword": source, "command": {"enabled": True}}
        with contextlib.redirect_stderr(io.StringIO()):
            result = self.ns["_startup_secret_sources"](cfg, environ={} if environ is None else environ)
        self.assertEqual(result["command"], cfg["command"])
        self.assertEqual(cfg["onepassword"], source)  # caller config is not mutated
        return result

    def test_explicit_helper_survives_unattended_start(self):
        source = {"enabled": True, "allow_unattended_helper": True,
                  "binary_path": "/approved/helper", "cache_ttl_seconds": 86400}
        self.assertEqual(self.select(source)["onepassword"], source)

    def test_default_and_invalid_assertions_remain_protected(self):
        for flag in (None, False, "true", 1):
            with self.subTest(flag=flag):
                source = {"enabled": True, "binary_path": "/approved/helper"}
                if flag is not None:
                    source["allow_unattended_helper"] = flag
                self.assertNotIn("onepassword", self.select(source))

    def test_missing_or_blank_binary_path_remains_protected(self):
        for binary in (None, "", "   "):
            with self.subTest(binary=binary):
                source = {"enabled": True, "allow_unattended_helper": True}
                if binary is not None:
                    source["binary_path"] = binary
                self.assertNotIn("onepassword", self.select(source))

    def test_service_account_path_still_survives(self):
        self.assertIn("onepassword", self.select({"enabled": True},
                      {"OP_SERVICE_ACCOUNT_TOKEN": "non-secret-test-placeholder"}))

    def test_interactive_path_unchanged(self):
        self.ns["_startup_onepassword_skip_reason"] = lambda: ""
        self.assertIn("onepassword", self.select({"enabled": True}))

    def test_disabled_source_unchanged(self):
        self.assertIn("onepassword", self.select({"enabled": False}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    args, remaining = parser.parse_known_args()
    StartupHelperContract.source_path = args.source
    unittest.main(argv=[__file__, *remaining])
