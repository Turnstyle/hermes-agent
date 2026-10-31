"""``model_foreign_to_provider`` names only a KNOWN vendor mismatch on a single-vendor provider.

A model persisted for a session can be stale after the session moved provider (a ``claude-…`` id on
an ``openai-codex`` session). Callers may discard such a model, so the helper must never call an
aggregator's, a custom endpoint's or a multi-vendor reseller's model "foreign", nor an id the vendor
classifier cannot place: absence of evidence is not a mismatch.
"""

from __future__ import annotations

import pytest

from hermes_cli.models_detect import current_provider_owns_vendor, model_foreign_to_provider


@pytest.mark.parametrize("model,provider", [
    ("claude-opus-4.7", "openai-codex"),
    ("gpt-5.4", "anthropic"),
    ("grok-4.6", "openai-codex"),
])
def test_known_other_vendor_on_single_vendor_provider_is_foreign(model, provider):
    assert model_foreign_to_provider(model, provider) is True
    assert current_provider_owns_vendor(model, provider) is False


@pytest.mark.parametrize("model,provider", [
    ("gpt-5.4", "openai-codex"),             # the provider's own vendor
    ("claude-opus-4.7", "anthropic"),
    ("claude-opus-4.7", "openrouter"),       # aggregator: every vendor is native
    ("claude-opus-4.7", "custom"),           # custom endpoint
    ("claude-opus-4.7", "custom:my-proxy"),
    ("deepseek-v4-pro", "bedrock"),          # multi-vendor / unclassifiable catalog
    ("my-local-finetune", "openai-codex"),   # id the classifier cannot place
    ("", "openai-codex"),
    ("claude-opus-4.7", ""),
    ("claude-opus-4.7", "auto"),
])
def test_unknown_or_owned_pairs_are_never_foreign(model, provider):
    assert model_foreign_to_provider(model, provider) is False


@pytest.mark.parametrize("model,provider", [("gpt-5.4", "openai-codex"), ("claude-opus-4.7", "anthropic")])
def test_ownership_and_foreignness_are_exclusive_for_classified_ids(model, provider):
    assert current_provider_owns_vendor(model, provider) is True
    assert model_foreign_to_provider(model, provider) is False
