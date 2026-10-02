"""Routine routing contracts. All faults and registry proofs here are fixtures.

Protects ordinary preference recovery and exact/policy/quality isolation at the
public CLI and real dispatch/init boundary. Existing override tests cannot prove
eligibility or constrained runtime fallback; no test-only product seam is used.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as dispatch
from hermes_cli.kanban_model_selection import eligible_routes, select_model, set_policy, fallback_allowed


def policy(profile="worker"):
    stamp = datetime.now(timezone.utc).isoformat()
    routes = [{"provider": "fixture-a", "model": "routine-old"},
              {"provider": "fixture-b", "model": "quality-new"}]
    return {
        "node": "fixture-node", "profile": profile, "task_class": "coding",
        "required_capabilities": ["tools"], "minimum_quality": 0.8,
        "checkpoint_ref": "fixture-checkpoint-7",
        "advisory": {"ok": False, "error": "not_configured", "execution_authorized": False},
        "snapshot": {
            "fleet_profile_config_v1": {"node": "fixture-node", "profile": profile,
                "model_chain": routes, "credential_provider_names": ["fixture-a", "fixture-b"],
                "observed_at": stamp},
            "model_capabilities": [{**r, "node": "fixture-node", "profile": profile, "capabilities": ["tools"],
                "quality": {"coding": 0.9}, "uncertain": False,
                "proof": {"authenticated": True, "usable": True, "observed_at": stamp}}
                for r in routes],
        },
    }


def cli(home, *args):
    # Use the actual public parser/dispatch without loading live profiles/plugins.
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "HERMES_HOME": str(home / "hermes"),
           "TMPDIR": str(home), "LANG": "C.UTF-8", "TZ": "UTC", "PYTHONUTF8": "1"}
    return subprocess.run([sys.executable, "-m", "hermes_cli.main", "kanban", *args],
                          cwd=Path(__file__).resolve().parents[2], env=env,
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("fault, expected", [
    ("instruction_mismatch", "quality-new"), ("renamed", "quality-new"),
    ("missing_model", "quality-new"), ("quota", "quality-new"),
    ("timeout", "quality-new"), ("outage", "quality-new"),
    ("unknown_auth", "quality-new"), ("quality", None),
    ("capability", None), ("registry_unavailable", None),
    ("jev_unavailable_uncertain", None), ("security_hold", None),
    ("eval_lock", None), ("stale", None), ("future", None),
    ("wrong_node", None), ("wrong_profile", None), ("duplicate_proof", None),
    ("unauthorized_provider", None), ("missing_quality", None),
])
def test_public_selection_preserves_contract_and_never_releases_work(tmp_path, monkeypatch, fault, expected):
    home = tmp_path / "operator"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HERMES_HOME", str(home / "hermes"))
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Fixture only: instruction prefers missing-model",
            body="Routine preference only. Keep checkpoint 7 and output contract.",
            assignee="worker", initial_status="blocked" if fault == "security_hold" else "running",
            model_override="routine-old", provider_override="fixture-a")
        before = kb.get_task(conn, tid)
    p = policy()
    a, b = p["snapshot"]["model_capabilities"]
    a["proof"]["usable"] = False
    if fault in {"missing_model", "renamed"}:
        p["snapshot"]["model_capabilities"].remove(a)
    if fault in {"quota", "timeout", "outage"}:
        p["failed_routes"] = [{"provider": "fixture-a", "model": "routine-old",
            "observed_at": datetime.now(timezone.utc).isoformat(), "kind": fault}]
    if fault == "unknown_auth":
        a["proof"]["authenticated"] = "UNKNOWN"
    if fault == "wrong_node":
        b["node"] = "other-node"
    if fault == "wrong_profile":
        b["profile"] = "other-profile"
    if fault == "duplicate_proof":
        p["snapshot"]["model_capabilities"].append(deepcopy(b))
    if fault == "unauthorized_provider":
        p["snapshot"]["fleet_profile_config_v1"]["credential_provider_names"] = ["fixture-a"]
    if fault == "missing_quality":
        b["quality"] = {}
    if fault == "quality":
        b["quality"]["coding"] = 0.7
    if fault == "capability":
        b["capabilities"] = []
    if fault == "registry_unavailable":
        p["snapshot"]["model_capabilities"] = []
    if fault == "jev_unavailable_uncertain":
        b["uncertain"] = True
    if fault == "eval_lock":
        p["exact_reason"] = "Reproduce fixture evaluation identity"
    if fault in {"stale", "future"}:
        b["proof"]["observed_at"] = (datetime.now(timezone.utc) +
            timedelta(seconds=-901 if fault == "stale" else 60)).isoformat()
    f = tmp_path / "fixture-policy.json"
    f.write_text(json.dumps(p), encoding="utf-8")
    preview = cli(home, "select-model", tid, "--policy-file", str(f))
    assert preview.returncode == (0 if expected else 2), preview.stdout + preview.stderr
    result = json.loads(preview.stdout)
    assert (result["selected"]["model"] if result["selected"] else None) == expected
    assert result["owner"] == "worker"
    assert result["checkpoint_ref"] == "fixture-checkpoint-7"
    assert result["model_output"] == result["accepted"] == "unobserved"
    applied = cli(home, "select-model", tid, "--policy-file", str(f), "--apply")
    assert applied.returncode == (0 if expected else 2), applied.stdout + applied.stderr
    with kbc.connect_closing() as conn:
        after = kb.get_task(conn, tid)
    for field in ("id", "assignee", "body", "status", "claim_lock", "current_run_id",
                  "workspace_path", "completion_contract", "result", "model_override", "provider_override"):
        assert getattr(after, field) == getattr(before, field)
    assert after.model_policy == (p if expected else None)


def test_dispatch_runtime_scope_and_bounded_fallback(tmp_path, monkeypatch):
    from agent.agent_init import _init_fallback_chain
    from agent.agent_runtime_helpers import apply_fallback_chain_to_agent
    from agent.chat_completion_helpers import try_activate_fallback
    for profile in ("worker-a", "worker-b", "worker-a"):
        home = tmp_path / profile
        home.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("HERMES_HOME", str(home / "hermes"))
        monkeypatch.setattr(Path, "home", lambda: home)
        kb.init_db()
        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title="fixture routine preference", assignee=profile,
                model_override="routine-old", provider_override="fixture-a")
            p = policy(profile)
            set_policy(conn, tid, p)
            task = kb.get_task(conn, tid)
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        command = dispatch._worker_argv(task, profile, None)
        assert command[max(i for i, arg in enumerate(command) if arg == "-m") + 1] == "routine-old"
        agent = SimpleNamespace(model="routine-old", provider="fixture-a", quiet_mode=True,
                                _credential_pool=None, _credential_pool_entry_id=None)
        _init_fallback_chain(agent, [{"provider": "unapproved", "model": "low-quality"}])
        assert agent._fallback_chain == [{"provider": "fixture-b", "model": "quality-new"}]
        assert not apply_fallback_chain_to_agent(agent, [{"provider": "unapproved", "model": "low-quality"}])
        assert agent._model_selection["accepted"] == "unobserved"
        assert fallback_allowed(agent, "fixture-b", "quality-new")
        # Proof expires between dispatch and failure: do not resolve credentials
        # or attempt a provider. Existing fallback walks each candidate once.
        agent._routine_model_policy = deepcopy(p)
        agent._routine_model_policy["snapshot"]["model_capabilities"][1]["proof"]["usable"] = False
        agent.base_url = "https://fixture.invalid"
        agent._fallback_chain_exhausted = True
        agent._unavailable_fallback_keys = set()
        assert not fallback_allowed(agent, "fixture-b", "quality-new")
        assert not try_activate_fallback(agent)
        assert agent._fallback_index == 1
        assert not try_activate_fallback(agent)
        assert agent._fallback_index == 1
        with kbc.connect_closing() as conn:
            exact = kb.create_task(conn, title="fixture eval", assignee=profile,
                model_override="routine-old", provider_override="fixture-a",
                exact_model_reason="Exact fixture eval identity")
        monkeypatch.setenv("HERMES_KANBAN_TASK", exact)
        _init_fallback_chain(agent, [{"provider": "fixture-b", "model": "quality-new"}])
        assert agent._fallback_chain == []
        assert agent._model_selection["exact_reason"] == "Exact fixture eval identity"
        assert not fallback_allowed(agent, "fixture-b", "quality-new")
        with kbc.connect_closing() as conn:
            with pytest.raises(ValueError, match="exact-model policy"):
                kb.set_model_override(conn, exact, "other", provider="fixture-b")
            kb.block_task(conn, exact, reason="fixture security hold")
            assert select_model(kb.get_task(conn, exact))["selected"] is None
        with pytest.raises(ValueError, match="does not match"):
            _init_fallback_chain(agent, [])
    for malformed in ({}, []):
        with pytest.raises(ValueError):
            select_model(task, malformed)
    # Known quota remains excluded until newer proof; a policy hold stays held.
    p = policy()
    stamp = p["snapshot"]["model_capabilities"][0]["proof"]["observed_at"]
    p["failed_routes"] = [{"provider": "fixture-a", "model": "routine-old", "observed_at": stamp, "kind": "quota"}]
    assert eligible_routes(p) == [{"provider": "fixture-b", "model": "quality-new"}]
    p["failed_routes"][0]["observed_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    assert len(eligible_routes(p)) == 2
    p["failed_routes"][0]["kind"] = "policy"
    assert eligible_routes(p) == [{"provider": "fixture-b", "model": "quality-new"}]
