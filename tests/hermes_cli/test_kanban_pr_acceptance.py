"""Two lifecycle invariants, using real SQLite and a local GitHub HTTP contract."""
import argparse
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


@pytest.fixture
def github(tmp_path, monkeypatch):
    state = {"conclusion": "success", "head": "a" * 40, "reads": 0, "requests": [],
             "pr_state": "OPEN", "classic": {"requiredStatusChecks": [
                 {"context": "required", "app": {"databaseId": 1}}]}, "merge_sha": "c" * 40}

    def response(path):
        state["requests"].append(path)
        sha = state["head"]
        if path == "/graphql":
            value = {"data": {"repository": {"pullRequest": {
                "headRefOid": sha, "baseRefName": "main", "state": state["pr_state"],
                "merged": state["pr_state"] == "MERGED", "mergeCommit": {"oid": state["merge_sha"]},
                "baseRef": None if state.get("base_ref_missing") else
                           {"branchProtectionRule": state["classic"]}}}}}
        elif "/rules/branches/" in path:
            if state.get("rules_network"):
                return -1, ""
            if state.get("rules_error"):
                status, message = state["rules_error"]
                value = {"message": message, "status": str(status)}
                if not state.get("rules_bare"):
                    value = [value]
                return status, state.get("rules_raw", json.dumps(value))
            value = [[]]
        elif "/check-runs" in path:
            run = {"id": 42, "name": "required", "head_sha": sha,
                   "app": {"id": 1}, "status": "in_progress" if state["conclusion"] == "pending" else "completed", "conclusion": state["conclusion"],
                   "html_url": "https://github.com/acme/repo/actions/runs/42"}
            if state.get("stale"):
                run["head_sha"] = "b" * 40
            runs = [] if state.get("missing") else [run]
            value = [{"total_count": 100 + len(runs), "check_runs": [
                {**run, "id": 1000 + i, "name": "optional", "conclusion": "skipped"}
                for i in range(100)]}, {"total_count": 100 + len(runs), "check_runs": runs}]
            if state.get("race"):
                state["race"]()
            if state.get("head_change"):
                state["head"] = "b" * 40
        elif "/statuses" in path:
            value = [[]]
        elif "/pulls/" in path:
            value = {"head": {"sha": state.get("rest_head", sha)}, "base": {"ref": state.get("rest_base", "main")},
                     "state": "closed" if state["pr_state"] != "OPEN" else "open",
                     "merged": state.get("rest_merged", state["pr_state"] == "MERGED"),
                     "merge_commit_sha": state.get("rest_merge_sha", state["merge_sha"])}
        else:
            return 404, json.dumps({"message": "Not Found"})
        return 200, json.dumps(value)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status, body = response(self.path)
            if status == -1:
                self.close_connection = True
                return
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args):
            pass

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError:
        # Some local sandboxes prohibit loopback binds; preserve the gh response
        # contract so the same assertions can still run there.
        server = None
        original_run = subprocess.run

        def local_gh(command, **kwargs):
            if command[:2] != ["gh", "api"]:
                return original_run(command, **kwargs)
            status, body = response("/" + command[2])
            if status == -1:
                raise subprocess.CalledProcessError(1, command, output="", stderr="gh: network error")
            if status != 200:
                raise subprocess.CalledProcessError(1, command, output=body,
                                                    stderr=f"gh: request failed (HTTP {status})")
            return subprocess.CompletedProcess(command, 0, stdout=body, stderr="")

        monkeypatch.setattr(subprocess, "run", local_gh)
    else:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        shim = tmp_path / "bin"
        shim.mkdir()
        gh = shim / "gh"
        gh.write_text(f"#!{sys.executable}\nimport sys,urllib.request,urllib.error\n"
                      f"u='http://127.0.0.1:{server.server_port}/'+sys.argv[2]\n"
                      "try:\n print(urllib.request.urlopen(u).read().decode())\n"
                      "except urllib.error.HTTPError as e:\n"
                      " print(e.read().decode())\n"
                      " print(f'gh: request failed (HTTP {e.code})', file=sys.stderr)\n"
                      " sys.exit(1)\n")
        gh.chmod(0o755)
        monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    try:
        yield state
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
            thread.join()


@pytest.mark.linux_only
def test_pr_completion_requires_current_required_evidence(github):
    with connect() as conn:
        for conclusion in ("failure", "pending", "cancelled", "timed_out", "action_required", "neutral", "skipped", None, "success"):
            github.update(conclusion=conclusion, head="a" * 40)
            tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
            ok = kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert ok is (conclusion == "success")
            task = kb.get_task(conn, tid)
            assert (task.status == "done") is ok
            receipts = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
            assert receipts and receipts[-1]["head_sha"] == "a" * 40
            if not ok:
                assert task.status in {"running", "ready", "blocked", "review"}
                assert "retry" in receipts[-1]["recovery"]
                assert receipts[-1]["checks"][0]["id"] == 42
        for fault in ("missing", "stale", "head_change"):
            github.update(conclusion="success", head="a" * 40)
            github[fault] = True
            tid = kb.create_task(conn, title=fault, completion_contract="acme/repo")
            assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).status != "done"
            github.pop(fault)
        # Omission and a sibling repository cannot downgrade the stored declaration.
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, summary="local green")
        assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/other/repo/pull/7"})
        before = len(github["requests"])
        local = kb.create_task(conn, title="local", completion_contract="local-only")
        assert kb.complete_task(conn, local, summary="https://github.com/acme/repo/pull/7 is background context")
        assert len(github["requests"]) == before


@pytest.mark.linux_only
def test_acceptance_receipts_and_terminal_write_share_run_ownership(github):
    with connect() as conn:
        for conclusion in ("success", "failure"):
            tid = kb.create_task(conn, title="race", completion_contract="acme/repo")
            owner = kb.claim_task(conn, tid)
            run_id = owner.current_run_id
            def reclaim():
                with connect() as rival:
                    assert kb.block_task(rival, tid, reason="Reassigned during acceptance")
                    assert kb.unblock_task(rival, tid)
                    github["replacement"] = kb.claim_task(rival, tid).current_run_id
            github.update(conclusion=conclusion, race=reclaim)
            assert not kb.complete_task(conn, tid, result="done", expected_run_id=run_id,
                metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).current_run_id == github["replacement"]
            assert github["replacement"] != run_id
            assert kb.get_task(conn, tid).status != "done"
            assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0] == 0
            github.pop("race")


def _acceptance_events(conn, task_id):
    return [(row["kind"], json.loads(row["payload"])) for row in conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id=? AND kind LIKE 'pr_acceptance%' ORDER BY id", (task_id,))]


@pytest.mark.parametrize("surface", ["db", "cli", "tool", "dashboard"])
@pytest.mark.parametrize("pr_state", ["MERGED", "OPEN"])
def test_plan_limited_rules_merged_fallback_across_surfaces(github, monkeypatch, surface, pr_state):
    github.update(pr_state=pr_state, classic=None, rules_error=(403,
        "Upgrade to GitHub Pro or make this repository public to enable this feature."))
    url = "https://github.com/acme/repo/pull/7"
    metadata = {"published_pr": url, "published_head": "a" * 40}
    with connect() as conn:
        tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
        if surface == "tool":
            owner = kb.claim_task(conn, tid)
            monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
            monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(owner.current_run_id))
            from tools import kanban_tools  # registers the real handler
            from tools.registry import registry
            result = json.loads(registry.dispatch("kanban_complete", {
                "task_id": tid, "result": "published", "metadata": metadata}))
            accepted = result.get("ok") is True
        elif surface == "cli":
            from hermes_cli.kanban import build_parser, kanban_command
            parent = argparse.ArgumentParser()
            build_parser(parent.add_subparsers(dest="command"))
            args = parent.parse_args(["kanban", "complete", tid, "--result", "published",
                                      "--metadata", json.dumps(metadata)])
            accepted = kanban_command(args) == 0
        elif surface == "dashboard":
            from plugins.kanban.dashboard.plugin_api import _STATUS_HANDLERS, UpdateTaskBody
            accepted = _STATUS_HANDLERS["done"](conn, tid, UpdateTaskBody(
                status="done", result="published", metadata=metadata))
        else:
            accepted = kb.complete_task(conn, tid, result="published", metadata=metadata)
        assert accepted is (pr_state == "MERGED")
        assert (kb.get_task(conn, tid).status == "done") is accepted
        events = _acceptance_events(conn, tid)
        receipts = [payload for kind, payload in events if kind == "pr_acceptance"]
        fallbacks = [payload for kind, payload in events if kind == "pr_acceptance_fallback"]
        assert len(receipts) == 1
        assert len(fallbacks) == (1 if accepted else 0)
        if accepted:
            assert receipts[0]["classification"] == "merged_fallback_rules_unavailable"
            assert receipts[0]["head_sha"] == "a" * 40
            assert receipts[0]["merge_sha"] == "c" * 40
            assert receipts[0]["required"] == receipts[0]["checks"] == []
            assert fallbacks[0] == {"pr_url": url, "head_sha": "a" * 40,
                                    "merge_sha": "c" * 40, "reason": "rules_api_plan_limited_403"}
        else:
            assert receipts[0]["classification"] == "missing"
            assert receipts[0]["detail"] == "rules API unavailable; only a MERGED PR at the bound head is accepted"


@pytest.mark.parametrize("case,classification", [
    ("closed", "infra"), ("different_403", "infra"), ("404", "infra"), ("network", "infra"),
    ("500", "infra"), ("malformed", "infra"), ("classic", "infra"),
    ("rest_head", "stale"), ("published_head", "stale"),
    ("rest_merge", "stale"), ("rest_base", "stale"), ("rest_unmerged", "stale"),
    ("base_ref_missing", "infra"), ("bare", "merged_fallback_rules_unavailable"),
])
def test_plan_limited_rules_refusal_boundaries(github, case, classification):
    github.update(pr_state="MERGED", classic=None, rules_error=(403,
        "Upgrade to GitHub Pro or make this repository public to enable this feature."))
    metadata = {"published_pr": "https://github.com/acme/repo/pull/7", "published_head": "a" * 40}
    if case == "closed":
        github["pr_state"] = "CLOSED"
    elif case == "different_403":
        github["rules_error"] = (403, "Resource not accessible by integration")
    elif case in {"404", "500"}:
        github["rules_error"] = (int(case), "Unavailable")
    elif case == "network":
        github["rules_network"] = True
    elif case == "malformed":
        github["rules_raw"] = "not JSON"
    elif case == "classic":
        github["classic"] = {"requiredStatusChecks": []}
    elif case == "rest_head":
        github["rest_head"] = "b" * 40
    elif case == "published_head":
        metadata["published_head"] = "b" * 40
    elif case == "rest_merge":
        github["rest_merge_sha"] = "d" * 40
    elif case == "rest_base":
        github["rest_base"] = "develop"
    elif case == "rest_unmerged":
        github["rest_merged"] = False
    elif case == "base_ref_missing":
        github["base_ref_missing"] = True
    elif case == "bare":
        github["rules_bare"] = True
    with connect() as conn:
        tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
        accepted = kb.complete_task(conn, tid, result="published", metadata=metadata)
        assert accepted is (case == "bare")
        assert (kb.get_task(conn, tid).status == "done") is accepted
        events = _acceptance_events(conn, tid)
        assert [kind for kind, _ in events].count("pr_acceptance_fallback") == (1 if accepted else 0)
        assert [payload for kind, payload in events if kind == "pr_acceptance"][0]["classification"] == classification


def test_working_rules_keep_required_check_acceptance(github):
    with connect() as conn:
        tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
        assert kb.complete_task(conn, tid, result="published", metadata={
            "published_pr": "https://github.com/acme/repo/pull/7"})
        assert kb.get_task(conn, tid).status == "done"
        events = _acceptance_events(conn, tid)
        assert [kind for kind, _ in events] == ["pr_acceptance"]
        assert events[0][1]["classification"] == "success"
        assert events[0][1]["required"] == [{"context": "required", "app_id": 1}]
