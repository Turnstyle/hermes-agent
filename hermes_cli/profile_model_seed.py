"""Copy the model, its selected custom endpoint and the explicit text auxiliary policy (with the
endpoints that policy names) into a new profile, and merge those endpoints over same-identity
entries the new profile already has."""

from hermes_cli.config import _expand_env_vars, find_provider_entry
from hermes_cli.config_providers import _normalize_custom_provider_entry
from hermes_cli.providers import custom_provider_aliases
from hermes_cli.runtime_provider_custom import (
    _get_named_custom_provider,
    _normalize_custom_provider_name,
    _shadowed_by_builtin,
)


def _requested_provider(model_cfg) -> str | None:
    provider = model_cfg.get("provider") if isinstance(model_cfg, dict) else None
    # Runtime expands ``model.provider: ${VAR}`` before resolving; select with the same value.
    return str(_expand_env_vars(provider)) if isinstance(provider, str) else None


# Fields name lookups match on. Runtime sees them ``${VAR}``-expanded (load_config expands the
# whole file), so matching expands only these; copied entries stay raw and secrets stay refs.
_ROUTING_FIELDS = ("name", "provider_key", "base_url", "baseUrl", "url", "api", "enabled")


def _expand_routing_fields(entry):
    if not isinstance(entry, dict):
        return entry
    return {k: _expand_env_vars(v) if k in _ROUTING_FIELDS else v for k, v in entry.items()}


def _routing_view(cfg: dict) -> dict:
    """*cfg* with endpoint routing fields expanded; same keys and list order, so a key or index
    found here addresses the raw entry in *cfg*."""
    view = dict(cfg)
    providers = cfg.get("providers")
    if isinstance(providers, dict):
        view["providers"] = {k: _expand_routing_fields(v) for k, v in providers.items()}
    custom = cfg.get("custom_providers")
    if isinstance(custom, list):
        view["custom_providers"] = [_expand_routing_fields(e) for e in custom]
    return view


def _resolve(provider: str, view: dict):
    """Runtime named-endpoint lookup on a routing *view* without reading any credential."""
    return _get_named_custom_provider(provider, config=view, resolve_credentials=False)


def _source_of(cfg: dict, resolved: dict):
    """Where in *cfg* the resolver found *resolved*: ``("providers", key)`` or
    ``("custom_providers", index)``. Keyed results carry ``provider_key``; legacy results do not
    (their normalizer runs without one), so they are located by normalized name and URL."""
    key, entry = find_provider_entry(cfg.get("providers"), resolved.get("provider_key"))
    if entry is not None:
        return "providers", key
    custom = cfg.get("custom_providers")
    for index, entry in enumerate(custom if isinstance(custom, list) else ()):
        normalized = _normalize_custom_provider_entry(entry)
        if normalized and (normalized["name"], normalized["base_url"]) == (
            resolved["name"],
            resolved["base_url"],
        ):
            return "custom_providers", index
    return None


# Text side-LLM tasks whose explicit policy (route, reasoning effort, speed tier) a fresh profile
# takes with the model, so its review/curator/MoA/compression calls run where the launch profile's
# do. Non-text slots (vision, voice) and delegation caps stay per-profile.
_SEEDED_AUX_TASKS = (
    "review", "background_review", "curator", "moa_reference", "moa_aggregator", "compression",
    "approval", "mcp", "title_generation", "memory_query_rewrite", "triage_specifier",
    "kanban_decomposer", "profile_describer", "goal_judge", "group_router", "monitor",
)


def _explicit_aux_policy(block) -> bool:
    """True when an ``auxiliary.<task>`` block sets a route, an effort or request fields; a block
    left at ``auto`` / ``""`` inherits the main model anyway and is not seeded."""
    if not isinstance(block, dict):
        return False
    provider = str(block.get("provider") or "").strip().lower()
    return bool(
        provider not in ("", "auto")
        or str(block.get("model") or "").strip()
        or str(block.get("base_url") or "").strip()
        or block.get("reasoning_effort") not in (None, "")
        or block.get("extra_body")
    )


def _seed_endpoint(seed: dict, source_cfg: dict, view: dict, selected: dict) -> None:
    """Add the raw ``providers:`` / ``custom_providers:`` entry *selected* came from to *seed*."""
    found = _source_of(view, selected)
    if found is None:
        return
    section, where = found
    if section == "providers":
        seed.setdefault("providers", {})[where] = source_cfg["providers"][where]
    elif (entry := source_cfg["custom_providers"][where]) not in seed.setdefault("custom_providers", []):
        seed["custom_providers"].append(entry)


def _exact_tuning(source_cfg: dict, provider: str):
    """The exact ``providers.<built-in>`` entry a built-in *provider* reads for its tuning, else None.
    Bare ``custom`` runs as provider ``custom`` and reads ``providers.custom`` tuning too."""
    requested = _normalize_custom_provider_name(provider)
    providers = source_cfg.get("providers")
    exact = providers.get(provider) if isinstance(providers, dict) else None
    if (
        isinstance(exact, dict)
        and requested not in {"", "auto"}
        and (requested == "custom" or _shadowed_by_builtin(requested))
    ):
        return exact
    return None


def _seed_model_endpoint(seed: dict, source_cfg: dict, view: dict, provider: str) -> None:
    selected = _resolve(provider, view)
    if selected is not None:
        _seed_endpoint(seed, source_cfg, view, selected)
    elif (exact := _exact_tuning(source_cfg, provider)) is not None:
        seed["providers"] = {provider: exact}


def _seed_aux_policy(seed: dict, source_cfg: dict, view: dict) -> None:
    """Explicit text ``auxiliary.<task>`` blocks, as written, plus the named endpoints they route to and
    a built-in provider's exact ``providers.<built-in>`` tuning (a ``base_url`` pin needs no entry).
    Entries already seeded (the model's) are kept, never replaced."""
    aux = source_cfg.get("auxiliary")
    if not isinstance(aux, dict):
        return
    policy = {task: aux[task] for task in _SEEDED_AUX_TASKS if _explicit_aux_policy(aux.get(task))}
    if not policy:
        return
    seed["auxiliary"] = policy
    for block in policy.values():
        provider = _requested_provider(block)
        if provider and provider.strip().lower() not in ("auto", "main"):
            selected = _resolve(provider, view)
            if selected is not None:
                _seed_endpoint(seed, source_cfg, view, selected)
            elif (exact := _exact_tuning(source_cfg, provider)) is not None:
                seed.setdefault("providers", {}).setdefault(provider, exact)


def launch_model_seed(source_cfg: dict) -> dict:
    """The config a fresh profile needs to run the launch profile's model: its ``model`` block plus
    the endpoint definition it points at. ``model.provider: custom:lab`` alone is "Unknown provider"
    on the first turn. The selected ``providers:`` or ``custom_providers:`` entry is copied as
    written (``${VAR}`` refs stay refs), and an exact ``providers.<built-in>`` / ``providers.custom``
    key travels for its tuning even without a URL. The launch profile's explicit text auxiliary
    policy travels with the endpoints it names (``_SEEDED_AUX_TASKS``). ``{}`` when the launch
    profile has no model."""
    model_cfg = source_cfg.get("model")
    if not model_cfg:
        return {}
    seed = {"model": model_cfg}
    # Resolve and locate on ONE view so the found key/index addresses the raw entry.
    view = _routing_view(source_cfg)
    provider = _requested_provider(model_cfg)
    if provider is not None:
        _seed_model_endpoint(seed, source_cfg, view, provider)
    _seed_aux_policy(seed, source_cfg, view)
    return seed


def _aliases(entry, key: str = "") -> frozenset[str]:
    """Every identity name-based lookups (runtime, ``/model`` pinning, pickers) accept for *entry*."""
    if not isinstance(entry, dict):
        return frozenset()
    entry = _expand_routing_fields(entry)
    key = key or str(entry.get("provider_key") or "")
    return custom_provider_aliases(str(entry.get("name") or key), key)


def merge_endpoint_seed(dst_cfg: dict, seed: dict) -> dict:
    """Return a copy of *dst_cfg* with the seeded endpoint written first; a same-key destination
    entry is replaced whole. When the seed is an endpoint (it resolves), destination entries that
    claim any of its identities are dropped in both sections: Hermes has several name lookups with
    different skip rules (URL validation, ``enabled``, keyed-first), so only removing every
    same-identity entry guarantees each of them lands on the inherited endpoint. A URL-less tuning
    seed (``providers.custom``) claims no identity and replaces only its own key. Dropped entries
    include URL-less or disabled ones sharing a name (e.g. ``providers.openrouter`` tuning under a
    custom endpoint named OpenRouter); this only runs for a new profile without a model. Seeded
    ``auxiliary`` blocks fill only the tasks the destination does not set."""
    seed_providers = seed.get("providers") or {}
    seed_custom = seed.get("custom_providers") or []
    provider = _requested_provider(seed.get("model"))
    claimed: frozenset[str] = frozenset()
    if provider and _resolve(provider, _routing_view(seed)) is not None:
        claimed = frozenset().union(
            *(_aliases(e, str(k)) for k, e in seed_providers.items()),
            *(_aliases(e) for e in seed_custom),
        )

    merged = dict(dst_cfg)
    dst_providers = dst_cfg.get("providers")
    if isinstance(dst_providers, dict) or seed_providers:
        kept = {
            k: v for k, v in (dst_providers if isinstance(dst_providers, dict) else {}).items()
            if k not in seed_providers and not claimed & _aliases(v, str(k))
        }
        merged["providers"] = {**seed_providers, **kept}
    dst_custom = dst_cfg.get("custom_providers")
    if isinstance(dst_custom, list) or seed_custom:
        merged["custom_providers"] = seed_custom + [
            e for e in (dst_custom if isinstance(dst_custom, list) else [])
            if not claimed & _aliases(e)
        ]
    seed_aux = seed.get("auxiliary") or {}
    if seed_aux:
        dst_aux = dst_cfg.get("auxiliary")
        merged["auxiliary"] = {**seed_aux, **(dst_aux if isinstance(dst_aux, dict) else {})}
    return merged
