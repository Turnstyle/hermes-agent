"""Distinguish unavailable selection evidence from violated task constraints."""
import math


class SelectionEvidenceUnavailable(ValueError):
    """No worker ran; use existing infrastructure accounting and cooldown."""


def evidence_unavailable(policy, preferred, *, now):
    from hermes_cli.kanban_model_selection import _fresh
    snapshot = policy.get('snapshot')
    if snapshot is None:
        return False
    config = snapshot['fleet_profile_config_v1']
    if (config.get('node'), config.get('profile')) != (policy['node'], policy['profile']):
        return False
    records = snapshot['model_capabilities']
    candidates = [r for r in config.get('model_chain', [])
                  if (not policy.get('exact_reason') or r == preferred)
                  and r['provider'] in config.get('credential_provider_names', [])]
    for route in candidates:
        # A known owner denial or exhausted route is not an evidence outage.
        if any(f['provider'] == route['provider'] and f['model'] == route['model']
               for f in policy.get('failed_routes', [])):
            continue
        matches = [r for r in records if (r.get('node'), r.get('profile'), r.get('provider'), r.get('model')) ==
                   (policy['node'], policy['profile'], route['provider'], route['model'])]
        if not matches:
            return True
        if len(matches) != 1:
            continue
        meta = matches[0]
        caps = meta.get('capabilities', [])
        quality = meta.get('quality', {})
        score = quality.get(policy['task_class']) if isinstance(quality, dict) else None
        if not isinstance(caps, list) or not set(policy['required_capabilities']).issubset(caps):
            continue
        if score is None and policy['minimum_quality'] == 0:
            pass
        elif isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not policy['minimum_quality'] <= score <= 1:
            continue
        proof = meta.get('proof', {})
        if not _fresh(config.get('observed_at'), now) or not _fresh(proof.get('observed_at'), now):
            return True
        if proof.get('authenticated') is not True or proof.get('usable') is not True:
            return True
        advisory = policy.get('advisory', {})
        if meta.get('uncertain') is not False and (advisory.get('ok') is not True or not _fresh(advisory.get('observed_at'), now)):
            return True
    return False


def require_selection(decision):
    if decision['selected'] is not None:
        return
    error = f"model selection unavailable: {decision['reason']}"
    if decision.get('evidence_unavailable'):
        raise SelectionEvidenceUnavailable(error)
    raise ValueError(error)
