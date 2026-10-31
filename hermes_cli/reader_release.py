"""Exact-generation cooperative backend exit and local credential-bound operator.

No signals, process-table selection, credential arguments or force fallback.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import time
import urllib.request


class ReaderExit:
    def __init__(self, server, port, role):
        import psutil
        import threading
        from hermes_constants import get_hermes_home

        self._lock = threading.Lock()
        self.server = server
        self.identity = {
            "pid": os.getpid(), "start": psutil.Process().create_time(),
            "nonce": secrets.token_urlsafe(32), "home": str(get_hermes_home().resolve()),
            "code_root": str(Path(__file__).resolve().parent.parent),
            "port": port, "role": role,
        }
        self.ticket = None

    def sources(self, paths):
        if not isinstance(paths, list) or not paths or len(paths) != len(set(paths)):
            raise ValueError("nonempty unique source paths required")
        home = Path(self.identity["home"])
        result = []
        for value in paths:
            path = Path(value)
            # Tickets bind directory identity, not just its spelling. A replacement
            # directory or symlink requires a fresh permit and census.
            if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
                raise ValueError("literal nonsymlink source required")
            if path != home and home not in path.parents:
                raise ValueError("source outside runtime home")
            stat = path.stat()
            if not path.is_dir():
                raise ValueError("source directory required")
            result.append([value, stat.st_dev, stat.st_ino])
        return result

    def matches(self, body):
        excluded = body.get("exclude_pids")
        return (body.get("expected") == self.identity
                and isinstance(excluded, list)
                and all(type(pid) is int and pid > 0 for pid in excluded)
                and os.getpid() not in excluded)

    def prepare(self, body, fence):
        with self._lock:
            return self._prepare(body, fence)

    def _prepare(self, body, fence):
        if not self.matches(body):
            return {"ok": False, "reason": "generation-or-exclusion"}
        try:
            sources = self.sources(body.get("sources"))
        except (ValueError, TypeError, OSError):
            return {"ok": False, "reason": "source-binding"}
        if fence.is_committed() and self.ticket is None:
            return {"ok": False, "reason": "legacy-commit-not-exit-ticket"}
        permit = fence.prepare()
        if permit.get("ok"):
            # Cannot repurpose a permit already bound to other sources.
            ticket = (permit["token"], sources, body["exclude_pids"])
            if self.ticket is not None and self.ticket[0] == ticket[0] and self.ticket != ticket:
                return {"ok": False, "reason": "ticket-binding"}
            self.ticket = ticket
        return permit

    def exit(self, body, fence):
        with self._lock:
            return self._exit(body, fence)

    def _exit(self, body, fence):
        if not self.matches(body):
            return {"ok": False, "reason": "generation-or-exclusion"}
        try:
            ticket = (body.get("token"), self.sources(body.get("sources")), body["exclude_pids"])
        except (ValueError, TypeError, OSError):
            return {"ok": False, "reason": "source-binding"}
        if self.ticket is None or ticket != self.ticket:
            return {"ok": False, "reason": "ticket-binding"}
        if not fence.exit_ready(body.get("token")):
            return {"ok": False, "reason": "not-committed-or-not-drained"}
        # This is process-owned: PID reuse/port replacement cannot redirect an
        # old request into a new generation. Uvicorn drains HTTP then lifespan.
        self.server.should_exit = True
        return {"ok": True, "exit_requested": True, "pid": os.getpid()}


def release(manifest, timeout=30):
    """Use only the native home-scoped rendezvous credential loader; never export it."""
    from gateway import host_rendezvous as hosts

    expected = manifest["expected"]
    from hermes_constants import get_hermes_home
    if str(get_hermes_home().resolve()) != expected["home"]:
        raise ValueError("operator profile/home mismatch")
    if expected["role"] != hosts.ROLE_DESKTOP_SERVE:
        raise ValueError("only Desktop-owned readers admitted")
    if expected["pid"] in manifest["exclude_pids"]:
        raise ValueError("protected process")
    record = hosts.read_record(expected["role"])
    if (record is None or record.pid != expected["pid"] or record.port != expected["port"]
            or not hosts.record_token_is_consistent(record)):
        raise ValueError("rendezvous generation mismatch")
    token = hosts.read_token(record.role)
    if not token:
        raise ValueError("native credential unavailable")
    # Fixed loopback transport. No redirects: do not forward credentials to another host.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    url = f"http://127.0.0.1:{record.port}/api/health/retirement"
    def post(action, permit=None):
        body = {**manifest, "action": action}
        if permit:
            body["token"] = permit
        request = urllib.request.Request(url, data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with opener.open(request, timeout=timeout) as response:
            return json.load(response)
    prepared = post("prepare-exit")
    if not prepared.get("ok"):
        raise ValueError("reader busy or binding refused")
    permit = prepared["token"]
    if not post("commit", permit).get("ok"):
        raise ValueError("commit refused")
    if not post("exit", permit).get("ok"):
        raise ValueError("exit refused; committed reader remains fenced")
    import psutil
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            process = psutil.Process(expected["pid"])
            if process.create_time() != expected["start"] or process.status() == psutil.STATUS_ZOMBIE:
                return {"ok": True, "generation_exited": True, "pid": expected["pid"]}
        except psutil.NoSuchProcess:
            return {"ok": True, "generation_exited": True, "pid": expected["pid"]}
        time.sleep(0.05)
    raise TimeoutError("graceful exit timeout; no signal sent; do not purge")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    try:
        print(json.dumps(release(json.loads(Path(args.manifest).read_text()), args.timeout)))
    except Exception:
        # Network errors and manifests can contain sensitive material. Do not
        # print payloads, tokens, headers, or chained exceptions.
        print(json.dumps({"ok": False, "reason": "release-refused-or-unverified"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
