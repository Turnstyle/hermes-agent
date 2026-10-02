"""Metadata-only routine selection on the existing Kanban and fallback paths.

Snapshots are owner exports, not another registry or an authentication probe.
The export contract is documented in the routine-model-selection source skill.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path


MAX_PROOF_AGE_SECONDS = 900


def _instant(value):
    if not isinstance(value, str):
        raise ValueError("proof timestamps must be UTC strings")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("proof timestamps must be UTC")
    return parsed.timestamp()


def _fresh(value, now):
    try:
        age = now - _instant(value)
        return 0 <= age <= MAX_PROOF_AGE_SECONDS
    except (TypeError, ValueError):
        return False


def validate_policy(policy):
    if not isinstance(policy, dict):
        raise ValueError("model policy must be an object")
    allowed = {"node", "profile", "task_class", "required_capabilities", "minimum_quality",
               "exact_reason", "snapshot", "advisory", "failed_routes", "checkpoint_ref"}
    if set(policy) - allowed:
        raise ValueError("unknown model policy fields")
    if policy.get("exact_reason") is not None:
        if not isinstance(policy["exact_reason"], str) or not policy["exact_reason"].strip():
            raise ValueError("an exact-model constraint needs a non-empty reason")
    if "snapshot" in policy:
        for key in ("node", "profile", "task_class"):
            if not isinstance(policy.get(key), str) or not policy[key].strip():
                raise ValueError(f"selection requires {key}")
        caps = policy.get("required_capabilities")
        if not isinstance(caps, list) or any(not isinstance(c, str) or not c for c in caps):
            raise ValueError("required_capabilities must be a list of names")
        score = policy.get("minimum_quality")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("minimum_quality must be in [0, 1]")
        snapshot = policy["snapshot"]
        if not isinstance(snapshot, dict) or set(snapshot) != {"model_capabilities", "fleet_profile_config_v1"}:
            raise ValueError("snapshot requires only the two canonical collections")
        if not isinstance(snapshot["model_capabilities"], list) or not isinstance(snapshot["fleet_profile_config_v1"], dict):
            raise ValueError("invalid canonical snapshot shape")
        if not isinstance(policy.get("failed_routes", []), list):
            raise ValueError("failed_routes must be a list")
    elif not policy.get("exact_reason"):
        raise ValueError("routine selection requires an owner-exported snapshot")
    # Only the documented metadata contract can be persisted. Credential values
    # and opaque provider errors are never accepted at this boundary.
    _validate_snapshot_fields(policy)
    return policy


def _validate_snapshot_fields(policy):
    snapshot = policy.get("snapshot", {})
    config = snapshot.get("fleet_profile_config_v1", {})
    if not isinstance(config, dict) or set(config) - {"node", "profile", "model_chain", "credential_provider_names", "observed_at"}:
        raise ValueError("unknown profile snapshot fields")
    chain = config.get("model_chain", [])
    if not isinstance(chain, list) or len(chain) > 32:
        raise ValueError("model_chain must have at most 32 routes")
    for route in chain:
        if not isinstance(route, dict) or set(route) != {"provider", "model"}:
            raise ValueError("model_chain entries require provider and model only")
        if any(not isinstance(v, str) or not v.strip() for v in route.values()):
            raise ValueError("model_chain route names must be non-empty strings")
    if len(snapshot.get("model_capabilities", [])) > 128:
        raise ValueError("capability snapshot must have at most 128 records")
    for route in snapshot.get("model_capabilities", []):
        if not isinstance(route, dict) or set(route) - {"node", "profile", "provider", "model", "capabilities", "quality", "proof", "uncertain"}:
            raise ValueError("unknown capability snapshot fields")
        proof = route.get("proof", {})
        if not isinstance(proof, dict) or set(proof) - {"authenticated", "usable", "observed_at"}:
            raise ValueError("unknown route proof fields")
    for failed in policy.get("failed_routes", []):
        if not isinstance(failed, dict) or set(failed) != {"provider", "model", "observed_at", "kind"}:
            raise ValueError("failure metadata requires route, timestamp, and kind")
        _instant(failed["observed_at"])
        if failed["kind"] not in {"quota", "auth", "policy", "timeout", "outage", "missing_model"}:
            raise ValueError("unknown failure kind")
    advisory = policy.get("advisory", {})
    if not isinstance(advisory, dict) or set(advisory) - {"ok", "error", "message", "execution_authorized", "observed_at", "eligible_routes"}:
        raise ValueError("unknown advisory fields")


def eligible_routes(policy, *, now=None):
    """Require every mandatory fact; advice never grants permission or quality."""
    validate_policy(policy)
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    snapshot = policy.get("snapshot")
    if snapshot is None:
        return []
    config = snapshot["fleet_profile_config_v1"]
    if (config.get("node"), config.get("profile")) != (policy["node"], policy["profile"]):
        return []
    if not _fresh(config.get("observed_at"), now):
        return []
    providers = config.get("credential_provider_names", [])
    if not isinstance(providers, list) or any(not isinstance(p, str) for p in providers):
        raise ValueError("credential_provider_names must contain names only")
    chain = config.get("model_chain", [])
    if not isinstance(chain, list) or len(chain) > 32:
        raise ValueError("model_chain must have at most 32 routes")
    accepted = []
    for route in chain:
        if route["provider"] not in providers or route in accepted:
            continue
        matches = [m for m in snapshot["model_capabilities"]
                   if (m.get("node"), m.get("profile"), m.get("provider"), m.get("model")) ==
                   (policy["node"], policy["profile"], route["provider"], route["model"])]
        # Ambiguous exports cannot prove which record is authoritative.
        if len(matches) != 1:
            continue
        meta = matches[0]
        proof = meta.get("proof", {})
        if proof.get("authenticated") is not True or proof.get("usable") is not True or not _fresh(proof.get("observed_at"), now):
            continue
        caps = meta.get("capabilities", [])
        if not isinstance(caps, list) or not set(policy["required_capabilities"]).issubset(caps):
            continue
        quality = meta.get("quality", {})
        score = quality.get(policy["task_class"]) if isinstance(quality, dict) else None
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not policy["minimum_quality"] <= score <= 1:
            continue
        failures = [f for f in policy.get("failed_routes", []) if
                    (f["provider"], f["model"]) == (route["provider"], route["model"])]
        # A policy denial is an owner hold, not something a successful ping lifts.
        if any(f["kind"] == "policy" or _instant(f["observed_at"]) >= _instant(proof["observed_at"]) for f in failures):
            continue
        if meta.get("uncertain") is not False:
            advisory = policy.get("advisory", {})
            if (advisory.get("ok") is not True or not _fresh(advisory.get("observed_at"), now)
                    or route not in advisory.get("eligible_routes", [])):
                continue
        accepted.append(dict(route))
    return accepted


def select_model(task, policy=None, *, now=None):
    """Preview only. Selection does not claim, retry, release, or accept work."""
    policy = task.model_policy if policy is None else policy
    if policy is not None:
        validate_policy(policy)
    result = {"task_id": task.id, "owner": task.assignee,
              "checkpoint_ref": (policy or {}).get("checkpoint_ref"),
              "completion_contract": task.completion_contract,
              "selected": None, "model_output": "unobserved", "accepted": "unobserved"}
    if task.status not in {"ready", "running", "todo", "triage"}:
        return {**result, "reason": "task_hold_or_terminal_state"}
    if policy is None:
        return {**result, "selected": {"model": task.model_override, "provider": task.provider_override},
                "reason": "routine_preference_existing_profile_fallback"}
    if policy.get("profile", task.assignee) != task.assignee:
        raise ValueError("policy profile must match the task owner")
    exact = policy.get("exact_reason")
    if exact and (not task.model_override or not task.provider_override):
        raise ValueError("exact-model constraints require model and provider")
    routes = eligible_routes(policy, now=now)
    preferred = {"provider": task.provider_override, "model": task.model_override}
    if exact:
        if "snapshot" not in policy or preferred in routes:
            return {**result, "selected": preferred, "reason": "exact_model_required", "exact_reason": exact}
        return {**result, "reason": "exact_model_unavailable"}
    match = next((r for r in routes if r["model"] == task.model_override and
                  (not task.provider_override or r["provider"] == task.provider_override)), None)
    return {**result, "selected": match or (routes[0] if routes else None),
            "reason": "eligible_preference" if match else "eligible_ordered_alternative" if routes else "no_verified_eligible_route"}


def set_policy(conn, task_id, policy):
    from hermes_cli import kanban_db as kb
    validate_policy(policy)
    with kb.write_txn(conn):
        task = kb.get_task(conn, task_id)
        if task is None:
            raise ValueError("unknown task")
        decision = select_model(task, policy)
        if decision["selected"] is None:
            raise ValueError(decision["reason"])
        conn.execute("UPDATE tasks SET model_policy = ? WHERE id = ?", (json.dumps(policy), task_id))
        kb._append_event(conn, task_id, "model_selection_policy_set", decision)
    kb.notify_task_updated(conn, task_id, ("model_policy",))
    return decision


def selection_command(args):
    """Existing Kanban CLI extension, read-only unless the operator passes --apply."""
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    try:
        policy = json.loads(Path(args.policy_file).read_text(encoding="utf-8"))
        with kbc.connect_closing() as conn:
            task = kb.get_task(conn, args.task_id)
            if task is None:
                raise ValueError("unknown task")
            result = set_policy(conn, task.id, policy) if args.apply else select_model(task, policy)
        print(json.dumps(result, indent=2))
        return 0 if result["selected"] else 2
    except (OSError, ValueError, TypeError, KeyError) as exc:
        # Do not echo input text or opaque metadata into logs.
        print(json.dumps({"selected": None, "reason": "invalid_or_unavailable_selection_metadata",
                          "error_type": type(exc).__name__}))
        return 2


def owned_worker_policy():
    from agent.delegation_context import owned_kanban_task
    task_id = owned_kanban_task()
    if not task_id:
        return None
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    if task is None:
        raise ValueError("owned worker task is missing")
    return task


def bind_worker_selection(agent):
    task = owned_worker_policy()
    if task is None or task.model_policy is None:
        return
    decision = select_model(task)
    if decision["selected"] != {"provider": agent.provider, "model": agent.model}:
        raise ValueError("worker route does not match verified card selection")
    agent._routine_model_policy = task.model_policy
    agent._model_selection = decision
    agent._fallback_chain_pinned = True
    routes = [] if task.model_policy.get("exact_reason") else eligible_routes(task.model_policy)
    agent._fallback_chain = [r for r in routes if r != decision["selected"]]
    agent._fallback_index = 0
    agent._fallback_model = agent._fallback_chain[0] if agent._fallback_chain else None


def fallback_allowed(agent, provider, model):
    policy = getattr(agent, "_routine_model_policy", None)
    if policy is None:
        return True
    if policy.get("exact_reason"):
        return False
    return {"provider": provider, "model": model} in eligible_routes(policy)
