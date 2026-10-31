"""Native exit regression and disposable-process integration on Linux/macOS.

Can run with unittest directly on an isolated machine, without pytest fixtures.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
import psutil

from hermes_cli.backend_retirement import RetirementFence
from hermes_cli.reader_release import ReaderExit

ROOT = Path(__file__).resolve().parents[2]


class ReaderExitContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.source = self.home / "source"
        self.source.mkdir()
        self.env = patch.dict(os.environ, HERMES_HOME=str(self.home))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.idle = patch("hermes_cli.web_server_idle_proof.idle_proof", return_value={"idle": True})
        self.probe = self.idle.start()
        self.addCleanup(self.idle.stop)
        self.server = SimpleNamespace(should_exit=False)
        self.owner = ReaderExit(self.server, 12345, "desktop-serve")
        self.fence = RetirementFence()
        self.body = {"expected": self.owner.identity.copy(), "sources": [str(self.source)],
                     "exclude_pids": [2552538]}

    def permit(self):
        permit = self.owner.prepare(self.body, self.fence)
        self.assertTrue(permit["ok"])
        self.body["token"] = permit["token"]
        self.assertTrue(self.fence.commit(permit["token"])["ok"])

    def test_busy_refused_and_no_reopen_after_commit(self):
        self.assertTrue(self.fence.acquire())
        self.assertFalse(self.owner.prepare(self.body, self.fence)["ok"])
        self.fence.release()
        self.permit()
        self.assertFalse(self.fence.acquire())
        self.assertFalse(self.fence.cancel(self.body["token"])["ok"])
        self.assertTrue(self.owner.exit(self.body, self.fence)["ok"])
        self.assertTrue(self.server.should_exit)

    def test_every_generation_binding_and_parent_exclusion(self):
        for key, value in self.owner.identity.items():
            with self.subTest(key=key):
                body = copy.deepcopy(self.body)
                body["expected"][key] = str(value) + "-changed"
                self.assertFalse(self.owner.prepare(body, self.fence)["ok"])
        self.body["exclude_pids"].append(os.getpid())
        self.assertFalse(self.owner.prepare(self.body, self.fence)["ok"])
        self.assertFalse(self.server.should_exit)

    def test_swap_after_prepare_and_foreign_ticket_refused(self):
        self.permit()
        other = ReaderExit(SimpleNamespace(should_exit=False), 12345, "desktop-serve")
        self.assertFalse(other.exit(self.body, self.fence)["ok"])
        changed = copy.deepcopy(self.body)
        changed["token"] = "wrong"
        self.assertFalse(self.owner.exit(changed, self.fence)["ok"])
        self.body["expected"]["nonce"] = "replaced"
        self.assertFalse(self.owner.exit(self.body, self.fence)["ok"])
        self.assertFalse(self.server.should_exit)

    def test_source_swap_and_outside_home(self):
        self.permit()
        self.source.rename(self.home / "old")
        self.source.mkdir()
        self.assertFalse(self.owner.exit(self.body, self.fence)["ok"])
        self.assertFalse(self.server.should_exit)
        self.body["sources"] = [str(self.home.parent)]
        self.assertFalse(self.owner.prepare(self.body, self.fence)["ok"])

    def test_unrecorded_generation_cannot_fetch_native_credentials(self):
        from hermes_cli.reader_release import release
        from gateway import host_rendezvous
        from types import SimpleNamespace
        # One singleton record cannot authorize a different Desktop pool child.
        for record in (None, SimpleNamespace(pid=99999999)):
            with self.subTest(record=record), \
                    patch.object(host_rendezvous, "read_record", return_value=record), \
                    patch.object(host_rendezvous, "read_token") as credential:
                with self.assertRaises(ValueError):
                    release(self.body)
                credential.assert_not_called()

    def test_drain_recheck_and_unknown_probe_fail_closed(self):
        self.permit()
        for value in (False, None):
            self.probe.return_value = {"idle": value}
            self.assertFalse(self.owner.exit(self.body, self.fence)["ok"])
        self.probe.side_effect = RuntimeError("unavailable")
        self.assertFalse(self.owner.exit(self.body, self.fence)["ok"])
        self.assertFalse(self.server.should_exit)
        self.assertFalse(self.fence.acquire())

    def test_legacy_semantics_and_cancel_retry(self):
        old = self.fence.prepare()["token"]
        self.assertTrue(self.fence.commit(old)["ok"])
        self.assertFalse(self.owner.prepare(self.body, self.fence)["ok"])
        self.assertFalse(self.server.should_exit)
        self.fence = RetirementFence()
        first = self.owner.prepare(self.body, self.fence)["token"]
        self.assertTrue(self.fence.cancel(first)["ok"])
        second = self.owner.prepare(self.body, self.fence)["token"]
        self.assertNotEqual(first, second)


class ReaderExitProcess(unittest.TestCase):
    def test_native_authenticated_operator_releases_actual_fd_and_mcp(self):
        self.process_case(False)

    def test_grace_timeout_never_escalates(self):
        self.process_case(True)

    def process_case(self, held):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            source = home / "source"
            source.mkdir()
            (source / "reader.txt").write_text("disposable")
            env = {**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": str(ROOT),
                   "HERMES_PM_NO_AUTO_INSTALL": "1",
                   "HERMES_GATEWAY_LOCK_DIR": str(home / "gateway-locks")}
            # A native CLI resolves inherited profile selectors before HOME.
            # This disposable home must never select or touch the caller's profile.
            env.pop("HERMES_PROFILE", None)
            fixture = ROOT / "tests/hermes_cli/reader_release_fixture.py"
            with open(home / "fixture.log", "w+") as log:
                proc = subprocess.Popen([sys.executable, str(fixture)], cwd=ROOT, env=env,
                                        stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 45
                    while not (home / "manifest.json").exists():
                        if proc.poll() is not None or time.monotonic() > deadline:
                            log.seek(0)
                            self.fail("fixture startup failed: " + log.read())
                        time.sleep(.05)
                    manifest = json.loads((home / "manifest.json").read_text())
                    url = f'http://127.0.0.1:{manifest["expected"]["port"]}/api/health/retirement'
                    headers = {"Authorization": "Bearer disposable-only-not-a-production-credential"}
                    # Native public boundary rejects missing credentials and malformed identity.
                    with httpx.Client(trust_env=False) as client:
                        self.assertEqual(client.post(url, json={"action": "identity"}).status_code, 401)
                        self.assertEqual(client.post(url, headers=headers, json={"action": "identity"}).json()["identity"], manifest["expected"])
                        changed = copy.deepcopy(manifest)
                        changed["expected"]["nonce"] = "replaced"
                        self.assertFalse(client.post(url, headers=headers,
                            json={**changed, "action": "prepare-exit"}).json()["ok"])
                        (home / "busy").touch()
                        self.assertFalse(client.post(url, headers=headers,
                            json={**manifest, "action": "prepare-exit"}).json()["ok"])
                        (home / "busy").unlink()
                    parent_files = psutil.Process(proc.pid).open_files()
                    self.assertTrue(any(str(source) in f.path for f in parent_files))
                    mcp_pid = int((home / "mcp.pid").read_text())
                    self.assertTrue(any(str(source) in f.path for f in psutil.Process(mcp_pid).open_files()))
                    # Exercise the real documented CLI and native profile credential loader.
                    if held:
                        (home / "hold-shutdown").touch()
                        command = [sys.executable, "-m", "hermes_cli.reader_release",
                            "--manifest", str(home / "manifest.json"), "--timeout", "0.5"]
                    else:
                        command = [sys.executable, "-m", "hermes_cli.main", "serve",
                            "--retire-exact", str(home / "manifest.json")]
                    operator = subprocess.run(command, cwd=ROOT, env=env,
                        capture_output=True, text=True, timeout=45)
                    if held:
                        self.assertEqual(operator.returncode, 1, operator.stdout + operator.stderr)
                        self.assertIsNone(proc.poll(), "timeout must not force exit")
                        self.assertFalse((home / "fds.closed").exists())
                        (home / "hold-shutdown").unlink()
                    else:
                        self.assertEqual(operator.returncode, 0, operator.stdout + operator.stderr)
                        self.assertIn('"generation_exited": true', operator.stdout)
                    self.assertNotIn("disposable-only", operator.stdout + operator.stderr)
                    self.assertEqual(proc.wait(timeout=20), 0)
                    self.assertFalse(psutil.pid_exists(mcp_pid))
                    self.assertTrue((home / "fds.closed").exists())
                    self.assertTrue((home / "mcp.closed").exists())
                    # No source inode remains open in either process. Reopen SQLite
                    # exclusively and rename the source after both exit normally.
                    import sqlite3
                    with sqlite3.connect(source / "state.db") as db:
                        db.execute("begin exclusive")
                    source.rename(home / "released")
                finally:
                    # Failure cleanup is cooperative via the native HTTP action only.
                    # No signals, even to the disposable reader.
                    if proc.poll() is None and (home / "manifest.json").exists():
                        (home / "hold-shutdown").unlink(missing_ok=True)
                        (home / "busy").unlink(missing_ok=True)
                        subprocess.run([sys.executable, "-m", "hermes_cli.reader_release",
                            "--manifest", str(home / "manifest.json")], cwd=ROOT, env=env,
                            capture_output=True, timeout=40)
                        proc.wait(timeout=20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
