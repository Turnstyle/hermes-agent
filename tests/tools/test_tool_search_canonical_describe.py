"""``tool_describe`` serves the session's canonical tool contract; the provider wire keeps its sanitizer.

The Tool Search bridge read its catalog from the provider wire definitions, and the wire
sanitizer drops top-level combinators (``allOf`` and friends) that strict backends reject in a
tool declaration. A deferred plugin tool whose contract lives in a top-level ``allOf`` was
therefore described without it: the model was told any provider would do, and only the raw
pre-dispatch validation caught the call. The fixture is an installed plugin tool's schema, used
here as inert data behind a fake handler (the autouse conftest fixture supplies a temporary
HERMES_HOME).
"""
import copy
import json
from pathlib import Path

import pytest
from jsonschema.validators import validator_for

import model_tools
from tools.registry import invalidate_check_fn_cache, registry
from tools.schema_sanitizer import sanitize_tool_schemas

INSTALLED = json.loads((Path(__file__).parents[1] / "fixtures" / "tool_describe_installed_schema.json")
                       .read_text(encoding="utf-8"))
NAME = INSTALLED["name"]
TOOLSET = "fake_keys_router"  # a plugin toolset: neither core nor a GUI surface, so the tool defers
PEER, PEER_TOOLSET = "fake_peer_lookup", "fake_peer"
PEER_SCHEMA = {"name": PEER, "description": "Look up a peer record by id.",
               "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}}
IN_SESSION = {"enabled_toolsets": [TOOLSET]}
VALID = {"action": "subscription_account_models", "provider": "antigravity"}
OVERRIDDEN = "Keys router operation, as the registry's dynamic schema overrides describe it."
HINT = " Rewritten for this session."


class _InstalledTool:
    """The installed schema registered as a fake plugin tool, with its handler and registry dispatch spied."""

    def __init__(self, monkeypatch):
        self.calls, self.dispatched, self.available = [], [], True
        real_dispatch = registry.dispatch

        def recording_dispatch(name, args, **kwargs):
            self.dispatched.append(name)
            return real_dispatch(name, args, **kwargs)

        monkeypatch.setattr(registry, "dispatch", recording_dispatch)

    def handler(self, args, **kwargs):
        self.calls.append(dict(args))
        return json.dumps({"ok": True})

    def register(self, *, available=True, peer_schema=PEER_SCHEMA, **extra):
        self.available = available
        # A copy, so a consumer mutating the served schema cannot also rewrite what the asserts compare.
        registry.register(name=NAME, toolset=TOOLSET, schema=copy.deepcopy(INSTALLED), handler=self.handler,
                          check_fn=lambda: self.available, **extra)
        registry.register(name=PEER, toolset=PEER_TOOLSET, schema=peer_schema,
                          handler=lambda args, **kwargs: json.dumps({"peer": True}))
        return self


@pytest.fixture
def installed(monkeypatch):
    tool = _InstalledTool(monkeypatch)
    yield tool
    for name in (NAME, PEER):
        registry.deregister(name)


def _bridge(tool, args, **session):
    return json.loads(model_tools.handle_function_call(tool, args, **session))


def _wire(**session):
    """The session's provider wire definitions, un-collapsed (``/tools`` reads the same list)."""
    defs = model_tools.get_tool_definitions(**session, quiet_mode=True, skip_tool_search_assembly=True)
    return {td["function"]["name"]: td["function"] for td in defs}


def _sanitized(function):
    return sanitize_tool_schemas([{"type": "function", "function": function}])[0]["function"]


@pytest.mark.parametrize("arguments, admitted", [
    pytest.param(VALID, True, id="antigravity"),
    pytest.param({**VALID, "provider": "xai"}, False, id="wrong_provider"),
    pytest.param({"action": "subscription_account_models"}, False, id="absent_provider"),
    pytest.param({**VALID, "row": "fake-row"}, False, id="declared_extra_field"),
    pytest.param({**VALID, "bogus": True}, False, id="undeclared_extra_field"),
])
def test_tool_describe_promises_exactly_what_tool_call_enforces(installed, arguments, admitted):
    installed.register()
    wire_before = _wire(**IN_SESSION)[NAME]
    described = _bridge("tool_describe", {"names": [NAME]}, **IN_SESSION)["tools"][NAME]
    wire_after = _wire(**IN_SESSION)[NAME]

    # The whole installed contract: the ordered action enum and every top-level allOf
    # conditional (subscription_account_models: action + provider, provider antigravity, no other key).
    assert described == {"description": INSTALLED["description"], "parameters": INSTALLED["parameters"]}
    # The wire is still exactly the sanitizer's output, before and after a describe: the two forms
    # never share a memo entry.
    assert wire_before == wire_after == _sanitized(INSTALLED)
    # What describe promises is what dispatch enforces.
    assert validator_for(described["parameters"])(described["parameters"]).is_valid(arguments) is admitted

    result = _bridge("tool_call", {"calls": [{"name": NAME, "arguments": arguments}]}, **IN_SESSION)
    if admitted:
        assert result == {"ok": True}
        assert installed.calls == [arguments] and installed.dispatched == [NAME]
    else:
        assert "NOT invoked" in result["error"]
        assert installed.calls == [] and installed.dispatched == []


def _schema_override(tool, monkeypatch):
    tool.register(dynamic_schema_overrides=lambda: {"description": OVERRIDDEN})


def _dynamic_rewriter(tool, monkeypatch):
    tool.register()
    monkeypatch.setitem(model_tools._DYNAMIC_SCHEMA_REWRITERS, NAME, lambda td, available: {
        **td, "function": {**td["function"], "description": td["function"]["description"] + HINT}})


def _recovered(tool, monkeypatch):
    tool.register(available=False)
    assert _bridge("tool_describe", {"names": [NAME]}, **IN_SESSION)["not_found"] == [NAME]
    tool.available = True
    invalidate_check_fn_cache()  # `hermes tools enable`, an MCP reprobe: a new cache epoch


def _malformed_sibling(tool, monkeypatch):
    # Unsanitized now, so a plugin's malformed parameters reach the catalog as registered.
    tool.register(peer_schema={**PEER_SCHEMA, "parameters": {"type": "object", "properties": "id"}})


def _registered(tool, monkeypatch):
    tool.register()


def _unavailable(tool, monkeypatch):
    tool.register(available=False)


@pytest.mark.parametrize("setup, session, description", [
    pytest.param(_schema_override, IN_SESSION, OVERRIDDEN, id="schema_override"),
    pytest.param(_dynamic_rewriter, IN_SESSION, INSTALLED["description"] + HINT, id="dynamic_rewriter"),
    pytest.param(_recovered, IN_SESSION, INSTALLED["description"], id="recovered_on_new_epoch"),
    pytest.param(_malformed_sibling, {"enabled_toolsets": [TOOLSET, PEER_TOOLSET]}, INSTALLED["description"],
                 id="malformed_sibling_schema"),
    pytest.param(_registered, {"enabled_toolsets": [PEER_TOOLSET]}, None, id="out_of_session"),
    pytest.param(_registered, {"enabled_toolsets": [TOOLSET, PEER_TOOLSET], "disabled_toolsets": [TOOLSET]},
                 None, id="disabled_toolset"),
    pytest.param(_unavailable, IN_SESSION, None, id="unavailable"),
])
def test_bridge_catalog_is_the_sessions_catalog_without_the_wire_sanitizer(
        installed, monkeypatch, setup, session, description):
    setup(installed, monkeypatch)
    wire = _wire(**session)
    search = _bridge("tool_search", {"queries": [NAME]}, **session)
    described = _bridge("tool_describe", {"names": [NAME]}, **session)
    called = _bridge("tool_call", {"calls": [{"name": NAME, "arguments": VALID}]}, **session)

    if description is None:  # outside the session's grant or availability: no door reaches it
        assert NAME not in wire and NAME not in search["tools"]
        assert described["tools"] == {} and described["not_found"] == [NAME]
        assert "not available in this session" in called["error"]
        assert installed.calls == [] and installed.dispatched == []
        return
    # Same selection, availability, overrides and rewrites on both forms; only the wire is sanitized.
    assert wire[NAME] == _sanitized({**INSTALLED, "description": description})
    assert NAME in search["results"][0]["matches"] and search["tools"][NAME]["source"] == "plugin"
    assert described["tools"][NAME] == {"description": description, "parameters": INSTALLED["parameters"]}
    assert called == {"ok": True}
    assert installed.calls == [VALID] and installed.dispatched == [NAME]
