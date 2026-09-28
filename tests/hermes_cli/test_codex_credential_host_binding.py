"""Carry-only regression for model-aware Codex pool selection.

The source host-binding suite belongs to e87f673faa and is not in this carry.
"""

import json
import os
from pathlib import Path

import yaml


def test_codex_fallback_resolves_pooled_credential_benched_for_other_model(monkeypatch):
    home = Path(os.environ["HERMES_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("HERMES_CODEX_BASE_URL", raising=False)
    (home / "config.yaml").write_text(yaml.safe_dump({
        "model": {"provider": "openai-codex", "default": "gpt-5.5"},
    }))
    (home / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {},
        "credential_pool": {"openai-codex": [{
            "id": "gw", "label": "gateway", "auth_type": "api_key", "priority": 0,
            "source": "manual", "access_token": "dummy-gateway-pool-key",
            "base_url": "https://chatgpt.com/backend-api/codex",
        }]},
    }))

    from agent.credential_pool import load_pool
    from agent.auxiliary_client import resolve_provider_client

    pool = load_pool("openai-codex")
    pool.mark_exhausted_and_rotate(
        status_code=400, failure_reason="model_entitlement",
        model="claude-opus-5-5", credential_id="gw",
    )
    client, model = resolve_provider_client(
        "openai-codex", model="gpt-6-sol-900k", raw_codex=True)
    assert client is not None
    assert client.api_key == "dummy-gateway-pool-key"
    assert model == "gpt-6-sol-900k"
    benched_client, _ = resolve_provider_client(
        "openai-codex", model="claude-opus-5-5", raw_codex=True)
    assert benched_client is None
