"""Route-owned effort limits must not leak to other models or endpoints."""
import pytest

from hermes_cli.inventory import _apply_capabilities


@pytest.mark.parametrize('model', ['gemini-4-argon', 'gemini-3.8-flash'])
@pytest.mark.parametrize('endpoint', [
    'http://127.0.0.1:8931/v1', 'http://localhost:8931/v1/', 'http://[::1]:8931/v1',
])
def test_local_route_publishes_only_served_levels(model, endpoint):
    rows = [{'slug': 'custom', 'api_url': endpoint, 'models': [model, 'claude-opus-4.6']}]
    _apply_capabilities(rows)
    assert rows[0]['capabilities'][model].get('supported_efforts') == ['low', 'medium', 'high']
    assert 'supported_efforts' not in rows[0]['capabilities']['claude-opus-4.6']
    assert rows[0]['models'] == [model, 'claude-opus-4.6']
    assert rows[0]['api_url'] == endpoint


@pytest.mark.parametrize('endpoint', [
    'https://localhost:8931/v1', 'http://localhost:8932/v1', 'http://localhost/v1',
    'http://localhost:8931/v2', 'http://localhost:8931/v1//', 'http://localhost:8931/v1?',
    'http://localhost:8931/v1#', 'http://localhost:8931/v1?q=1', 'http://localhost:8931/v1#x',
    'http://user@localhost:8931/v1', 'http://localhost.evil:8931/v1', 'http://127.0.0.2:8931/v1',
    ' http://localhost:8931/v1', 'http://localhost:8931/v1 ', 'http://local\thost:8931/v1',
    'http://localhost:bad/v1', 'http://localhost:99999/v1', 'http://[::1:8931/v1',
    'http://localhost:8931/v1\x00', 'http://[::1]evil:8931/v1',
    'http://[::1]localhost:8931/v1', 'http://localhost:8931.0/v1', None,
])
def test_other_or_malformed_routes_remain_unrestricted(endpoint):
    rows = [{'slug': 'custom', 'api_url': endpoint, 'models': ['gemini-4-argon', 'gemini-3.8-flash']}]
    _apply_capabilities(rows)
    assert all('supported_efforts' not in cap for cap in rows[0]['capabilities'].values())


@pytest.mark.parametrize('model', [
    'google/gemini-4-argon', 'gemini-4-argon-fast', 'gemini-4-argon-preview',
    'GEMINI-4-ARGON', 'gemini-3.8-flash-fast', 'claude-opus-4.6', 'unknown',
])
def test_other_model_ids_remain_unrestricted(model):
    rows = [{'slug': 'custom', 'api_url': 'http://localhost:8931/v1', 'models': [model]}]
    _apply_capabilities(rows)
    assert 'supported_efforts' not in rows[0]['capabilities'][model]


def test_picker_payload_carries_route_capability_without_changing_selection(monkeypatch):
    from hermes_cli.inventory import ConfigContext, build_models_payload
    import hermes_cli.inventory as inventory
    import hermes_cli.model_switch as discovery

    endpoint = 'http://localhost:8931/v1'
    monkeypatch.setattr(discovery, 'list_authenticated_providers', lambda **kwargs: [{
        'slug': 'custom', 'name': 'Local', 'api_url': endpoint,
        'models': ['gemini-4-argon', 'gemini-3.8-flash'], 'is_user_defined': True,
    }])
    monkeypatch.setattr(inventory, '_local_runtime_row', lambda ctx: None)
    ctx = ConfigContext('custom', 'gemini-4-argon', endpoint, {}, [])
    payload = build_models_payload(ctx, capabilities=True)
    local = next(row for row in payload['providers'] if row['slug'] == 'custom')
    assert local['capabilities']['gemini-4-argon'].get('supported_efforts') == ['low', 'medium', 'high']
    assert local['capabilities']['gemini-3.8-flash'].get('supported_efforts') == ['low', 'medium', 'high']
    assert (payload['provider'], payload['model']) == ('custom', 'gemini-4-argon')
    assert local['api_url'] == endpoint
