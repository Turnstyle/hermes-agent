"""Two lifecycle invariants, using real SQLite and a local GitHub HTTP contract."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


@pytest.fixture
def github(tmp_path, monkeypatch):
    state = {"conclusion": "success", "head": "a" * 40, "reads": 0, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            sha = state["head"]
            if self.path == "/graphql":
                value = {"data": {"repository": {"pullRequest": {
                    "headRefOid": sha, "baseRefName": "main", "state": "OPEN",
                    "baseRef": {"branchProtectionRule": {"requiredStatusChecks": [
                        {"context": "required", "app": {"databaseId": 1}}]}}}}}}
            elif "/rules/branches/" in self.path:
                value = [[]]
            elif "/check-runs" in self.path:
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
            elif "/statuses" in self.path:
                value = [[]]
            elif "/pulls/" in self.path:
                value = {"head": {"sha": sha}, "base": {"ref": "main"}, "state": "open"}
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport sys,urllib.request\n"
                  f"u='http://127.0.0.1:{server.server_port}/'+sys.argv[2]\n"
                  "print(urllib.request.urlopen(u).read().decode())\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.platforms("linux")
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


@pytest.mark.platforms("linux")
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


# --- #122689: acceptance must read the repo as the ASSIGNEE profile's gh login ---

@pytest.mark.platforms("posix")
def test_acceptance_runs_gh_as_the_assignee_profile(tmp_path, monkeypatch):
    """The gh child env carries the assignee's own GH credentials (its .env),
    never the ambient/launch residue, and an invisible repo is classified
    `auth` naming the repository — not a retryable `infra` failure."""
    from pathlib import Path

    launch_home = tmp_path / "home"
    launch_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    assignee_home = launch_home / "profiles" / "b"
    assignee_home.mkdir(parents=True)
    (assignee_home / ".env").write_text("GH_TOKEN=b-token\n", encoding="utf-8")
    # Ambient residue that must NOT decide the login.
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/nonexistent/launch/gh")

    env_dump = tmp_path / "gh_env.json"
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport json, os\n"
                  f"json.dump(dict(os.environ), open({str(env_dump)!r}, 'w'))\n"
                  "print(json.dumps({'data': {'repository': None}}))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-b", completion_contract="acme/repo", assignee="b")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        assert kb.get_task(conn, tid).status != "done"
        receipts = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
        assert receipts[-1]["classification"] == "auth"
        assert "acme/repo" in receipts[-1]["detail"]
    captured = json.loads(env_dump.read_text())
    assert captured["GH_TOKEN"] == "b-token"
    assert captured.get("GH_CONFIG_DIR") != "/nonexistent/launch/gh"
    assert "credentials" in (kb.get_task(conn, tid).last_failure_error or "")


@pytest.mark.platforms("posix")
def test_assignee_without_own_gh_login_never_falls_through_to_ambient_login(tmp_path, monkeypatch):
    """An assignee profile with no GH_TOKEN/GH_CONFIG_DIR of its own must not inherit the
    launch user's ~/.config/gh (HOME/XDG_CONFIG_HOME stay the launch process's): gh is pinned
    to a profile-owned config dir, its 'not logged in' exit is classified `auth` naming the profile."""
    launch_home = tmp_path / "home"
    launch_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    assignee_home = launch_home / "profiles" / "b"
    assignee_home.mkdir(parents=True)
    (assignee_home / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/nonexistent/launch/gh")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "launch-xdg"))

    env_dump = tmp_path / "gh_env.json"
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    # Real gh: GH_CONFIG_DIR wins; a config dir without hosts.yml means "not logged in" (exit 4).
    gh.write_text(f"#!{sys.executable}\nimport json, os, pathlib, sys\n"
                  f"pathlib.Path({str(env_dump)!r}).write_text(json.dumps(dict(os.environ)), encoding='utf-8')\n"
                  "if 'GH_CONFIG_DIR' in os.environ and not os.path.exists(os.environ['GH_CONFIG_DIR']):\n"
                  "    sys.exit(4)\n"
                  "print(json.dumps({'data': {'repository': None}}))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-b", completion_contract="acme/repo", assignee="b")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "auth"
    assert "'b'" in receipt["detail"] and "no login" in receipt["detail"]
    captured = json.loads(env_dump.read_text(encoding="utf-8-sig"))
    assert captured["GH_CONFIG_DIR"] == str(assignee_home / "gh")
    assert "GH_TOKEN" not in captured and "GITHUB_TOKEN" not in captured


def test_assigned_card_with_unresolvable_profile_is_auth_not_ambient(tmp_path, monkeypatch):
    """A card assigned to a profile that no longer exists must not run gh as the completing
    process's ambient login: classification `auth` naming the profile, gh never invoked."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))  # any gh spawn would fail as infra
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-ghost", completion_contract="acme/repo", assignee="ghost")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "auth"
    assert "'ghost'" in receipt["detail"] and "cannot be resolved" in receipt["detail"]


@pytest.mark.parametrize("paginate, wrapped, expected", [(False, False, []), (True, False, [[]]), (True, True, [[]])])
def test_rules_plan_403_is_empty_rules(monkeypatch, paginate, wrapped, expected):
    import subprocess
    from hermes_cli import kanban_pr_acceptance as acceptance

    body = {"message": "Upgrade to GitHub Pro or make this repository public to enable this feature.",
            "documentation_url": "https://docs.github.com/rest/repos/rules#get-rules-for-a-branch", "status": "403"}
    def denied(command, **kwargs):
        raise subprocess.CalledProcessError(1, command,
            output=json.dumps([body] if wrapped else body),
            stderr="gh: feature unavailable (HTTP 403) sensitive-do-not-store")

    monkeypatch.setattr(acceptance.subprocess, "run", denied)
    assert acceptance._api("repos/acme/repo/rules/branches/main?per_page=100", paginate=paginate) == expected


@pytest.fixture
def acceptance_api(monkeypatch):
    import subprocess
    from hermes_cli import kanban_pr_acceptance as acceptance

    sha = "a" * 40
    run = {"id": 42, "name": "ci", "head_sha": sha, "app": {"id": 1},
           "status": "completed", "conclusion": "success"}
    state = {"required": [], "rules": [[]], "runs": [{"total_count": 1, "check_runs": [run]}],
             "statuses": [[]], "current": {"head": {"sha": sha}, "base": {"ref": "main"}, "state": "open"},
             "requests": []}

    def api(command, **kwargs):
        endpoint = command[2]
        state["requests"].append(endpoint)
        if endpoint == "graphql":
            value = {"data": {"repository": {"pullRequest": {
                "headRefOid": sha, "baseRefName": "main", "state": "OPEN",
                "baseRef": {"branchProtectionRule": {"requiredStatusChecks": state["required"]}}}}}}
        elif "/rules/branches/" in endpoint:
            if state.get("plan_denial"):
                raise subprocess.CalledProcessError(1, command, output=json.dumps({
                    "message": "Upgrade to GitHub Pro or make this repository public to enable this feature.", "status": "403"}),
                    stderr="gh: unavailable (HTTP 403) sensitive-do-not-store")
            value = state["rules"]
        elif "/check-runs?" in endpoint:
            assert endpoint == f"repos/acme/repo/commits/{sha}/check-runs?per_page=100&filter=latest"
            value = state["runs"]
        elif "/statuses?" in endpoint:
            assert endpoint == f"repos/acme/repo/commits/{sha}/statuses?per_page=100"
            value = state["statuses"]
        elif endpoint == "repos/acme/repo/pulls/7":
            value = state["current"]
        else:
            pytest.fail(f"unexpected endpoint {endpoint}")
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

    monkeypatch.setattr(acceptance.subprocess, "run", api)
    return state


def test_no_required_checks_accepts_observed_success(acceptance_api):
    from hermes_cli.kanban_pr_acceptance import collect_acceptance

    receipt = collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["ok"] is True
    assert receipt["classification"] == "success"
    assert receipt["checks"][0]["id"] == 42
    assert acceptance_api["requests"][-1] == "repos/acme/repo/pulls/7"


@pytest.mark.parametrize("endpoint,code,http,body,query", [
    ("repos/acme/repo/rules/branches/main", 1, 403, {"message": "Resource not accessible by integration"}, None),
    ("repos/acme/repo/rules/branches/main", 1, 403, {"message": "API rate limit exceeded"}, None),
    ("repos/acme/repo/rules/branches/main", 1, 403, {"message": "Permission denied"}, None),
    ("repos/acme/repo/rules/branches/main", 1, 401, "plan", None),
    ("repos/acme/repo/rules/branches/main", 1, 404, "plan", None),
    ("repos/acme/repo/rules/branches/main", 4, 403, "plan", None),
    ("repos/acme/repo/pulls/7", 1, 403, "plan", None),
    ("repos/acme/repo/commits/aaa/check-runs", 1, 403, "plan", None),
    ("graphql", 1, 403, "plan", None),
    ("repos/acme/repo/rules/branches/main", 1, 403, "plan", "mutation {}"),
    ("repos/acme/repo/rules/branches/main", 1, 403, {"message": "Upgrade to GitHub Pro or make this repository public to enable this feature.", "status": "401"}, None),
    ("repos/acme/repo/rules/branches/main", 1, 403, [], None),
    ("repos/acme/repo/rules/branches/main", 1, 403, "invalid-json", None),
    ("repos/acme/repo/rules/branches/main", 1, 403, {"message": "Upgrade to GitHub Pro or make this repository public, permission denied"}, None),
])
@pytest.mark.parametrize("paginate, wrapped", [(False, False), (True, False), (True, True)])
def test_rules_plan_exception_does_not_bypass_auth(monkeypatch, endpoint, code, http, body, query, paginate, wrapped):
    import subprocess
    from hermes_cli import kanban_pr_acceptance as acceptance

    if body == "plan":
        body = {"message": "Upgrade to GitHub Pro or make this repository public to enable this feature."}
    def denied(command, **kwargs):
        raise subprocess.CalledProcessError(code, command,
            output="not json" if body == "invalid-json" else json.dumps([body] if wrapped else body),
            stderr=f"gh: denied (HTTP {http}) sensitive-do-not-store")
    monkeypatch.setattr(acceptance.subprocess, "run", denied)
    with pytest.raises(acceptance._GateAuthError) as error:
        acceptance._api(endpoint, query=query, paginate=paginate)
    assert "sensitive-do-not-store" not in str(error.value)


@pytest.mark.parametrize("paginate, shape", [(False, "singleton"), (True, "multiple"), (True, "null"), (True, "string"), (True, "nested")])
def test_rules_plan_array_shapes_fail_closed(monkeypatch, paginate, shape):
    import subprocess
    from hermes_cli import kanban_pr_acceptance as acceptance

    body = {"message": "Upgrade to GitHub Pro or make this repository public to enable this feature.", "status": "403"}
    evidence = {"singleton": [body], "multiple": [body, body], "null": [None],
                "string": [body["message"]], "nested": [[body]]}[shape]
    def denied(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, output=json.dumps(evidence),
                                            stderr="gh: unavailable (HTTP 403)")
    monkeypatch.setattr(acceptance.subprocess, "run", denied)
    with pytest.raises(acceptance._GateAuthError):
        acceptance._api("repos/acme/repo/rules/branches/main?per_page=100", paginate=paginate)


@pytest.mark.parametrize("fault,classification", [
    ("zero", "missing"), ("failure", "failure"), ("pending", "pending"),
    ("skipped", "infra"), ("neutral", "infra"), ("cancelled", "infra"),
    ("timed_out", "infra"), ("action_required", "infra"), ("no_conclusion", "infra"),
    ("stale", "stale"), ("incomplete", "infra"), ("bad_count", "infra"),
    ("bad_run", "infra"), ("bad_status_page", "infra"), ("bad_status", "infra"),
    ("bad_rules", "infra"), ("bad_required", "infra"), ("bad_rule_checks", "infra"),
    ("bad_rule_type", "infra"), ("legacy_failure", "failure"), ("legacy_pending", "pending"),
    ("legacy_masks_run", "failure"), ("run_masks_legacy", "failure"),
    ("other_app_failure", "failure"), ("head_race", "stale"), ("base_race", "stale"),
    ("closed_race", "stale"),
])
def test_no_required_checks_fails_closed(acceptance_api, fault, classification):
    from hermes_cli.kanban_pr_acceptance import collect_acceptance

    state = acceptance_api
    run = state["runs"][0]["check_runs"][0]
    if fault in {"failure", "pending", "skipped", "neutral", "cancelled", "timed_out", "action_required"}:
        run["conclusion"] = fault
        if fault == "pending":
            run["status"] = "in_progress"
    elif fault == "zero":
        state["runs"] = [{"total_count": 0, "check_runs": []}]
    elif fault == "no_conclusion":
        run.pop("conclusion")
    elif fault == "stale":
        run["head_sha"] = "b" * 40
    elif fault == "incomplete":
        state["runs"][0]["total_count"] = 2
    elif fault == "bad_count":
        state["runs"][0]["total_count"] = True
    elif fault == "bad_run":
        run["name"] = ""
    elif fault == "bad_status_page":
        state["statuses"] = [{}]
    elif fault == "bad_status":
        state["statuses"] = [[{"id": 9, "context": "legacy"}]]
    elif fault == "bad_rules":
        state["rules"] = {}
    elif fault == "bad_required":
        state["required"] = {}
    elif fault == "bad_rule_checks":
        state["rules"] = [[{"type": "required_status_checks", "parameters": {"required_status_checks": {}}}]]
    elif fault == "bad_rule_type":
        state["rules"] = [[{"type": None}]]
    elif fault in {"legacy_failure", "legacy_pending", "legacy_masks_run", "run_masks_legacy"}:
        state["statuses"] = [[{"id": 9, "context": "ci", "state": "pending" if fault == "legacy_pending" else "failure"}]]
        if fault == "legacy_masks_run":
            run["conclusion"] = "failure"
            state["statuses"][0][0]["state"] = "success"
    elif fault == "other_app_failure":
        state["runs"][0]["check_runs"].append({**run, "id": 43, "app": {"id": 2}, "conclusion": "failure"})
        state["runs"][0]["total_count"] = 2
    elif fault == "head_race":
        state["current"]["head"]["sha"] = "b" * 40
    elif fault == "base_race":
        state["current"]["base"]["ref"] = "other"
    elif fault == "closed_race":
        state["current"]["state"] = "closed"
    receipt = collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["ok"] is False
    assert receipt["classification"] == classification
    assert "sensitive-do-not-store" not in json.dumps(receipt)


@pytest.mark.parametrize("latest_state,ok", [("success", True), ("failure", False), ("pending", False)])
def test_fallback_uses_highest_status_id_across_pages(acceptance_api, latest_state, ok):
    from hermes_cli.kanban_pr_acceptance import collect_acceptance

    acceptance_api["runs"] = [{"total_count": 0, "check_runs": []}]
    acceptance_api["statuses"] = [[{"id": 9, "context": "legacy", "state": latest_state}],
                                  [{"id": 2, "context": "legacy", "state": "failure" if ok else "success"}]]
    receipt = collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["ok"] is ok
    assert [c["id"] for c in receipt["checks"]] == [9]


@pytest.mark.parametrize("source", ["protection", "rules"])
def test_required_checks_keep_app_pin_and_ignore_optional_failures(acceptance_api, source):
    from hermes_cli.kanban_pr_acceptance import collect_acceptance

    if source == "protection":
        acceptance_api["required"] = [{"context": "ci", "app": {"databaseId": 1}}]
    else:
        acceptance_api["rules"] = [[{"type": "required_status_checks", "parameters": {
            "required_status_checks": [{"context": "ci", "integration_id": 1}]}}]]
    run = acceptance_api["runs"][0]["check_runs"][0]
    acceptance_api["runs"][0].update(total_count=3, check_runs=[run,
        {**run, "id": 43, "app": {"id": 2}, "conclusion": "failure"},
        {**run, "id": 44, "name": "optional", "conclusion": "failure"}])
    receipt = collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["ok"] is True
    assert [c["id"] for c in receipt["checks"]] == [42]
    run["app"]["id"] = 3
    acceptance_api["statuses"] = [[{"id": 99, "context": "ci", "state": "success"}]]
    receipt = collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["ok"] is False
    assert receipt["classification"] == "missing"


def test_plan_denial_reaches_successful_fallback(acceptance_api):
    from hermes_cli.kanban_pr_acceptance import collect_acceptance

    acceptance_api["plan_denial"] = True
    receipt = collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["ok"] is True
    assert receipt["required"] == []
