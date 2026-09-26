"""Write/read guards and the availability gate for the Supermemory provider.

Covers three failure modes found in a pilot review:
1. built-in memory mirroring (``on_memory_write``) ignored ``auto_capture`` and container approval;
2. a container described as read-only was writable through the tool handler;
3. a key inherited from the environment kept the provider live after the key helper reported down.
All data here is synthetic; the client is a fake, so nothing leaves the process.
"""

import hashlib
import json
import logging
import os

import pytest

import plugins.memory.supermemory as sm
from plugins.memory.supermemory import SupermemoryMemoryProvider

PROOF_ENV = "SUPERMEMORY_AVAILABILITY_PROOF"
KEY = "synthetic-test-key-0001"


class FakeClient:
    def __init__(self, api_key, timeout, container_tag, search_mode="hybrid", base_url="", tunnel=None, socket_pin=None):
        self.api_key, self.container_tag = api_key, container_tag
        self.add_calls, self.search_calls, self.profile_calls = [], [], []
        self.search_results = [{"id": "r1", "memory": "synthetic result", "similarity": 0.9}]
        self.forgotten_ids, self.forget_queries = [], []

    def add_memory(self, content, metadata=None, *, entity_context="", container_tag=None, custom_id=None):
        self.add_calls.append({"content": content, "container_tag": container_tag, "metadata": metadata})
        return {"id": "mem_synthetic"}

    def search_memories(self, query, *, limit=5, container_tag=None, search_mode=None):
        self.search_calls.append(container_tag)
        return self.search_results

    def get_profile(self, query=None, *, container_tag=None):
        self.profile_calls.append(container_tag)
        return {"static": ["synthetic static"], "dynamic": [], "search_results": []}

    def forget_memory(self, memory_id, *, container_tag=None):
        self.forgotten_ids.append((memory_id, container_tag))

    def forget_by_query(self, query, *, container_tag=None):
        self.forget_queries.append((query, container_tag))
        return {"success": True, "message": "Forgot"}


def _fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _valid_proof(key: str = KEY, pid: int | None = None) -> str:
    return f"v1:{os.getpid() if pid is None else pid}:{_fingerprint(key)}"


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("SUPERMEMORY_API_KEY", KEY)
    monkeypatch.delenv(PROOF_ENV, raising=False)
    monkeypatch.delenv("SUPERMEMORY_CONTAINER_TAG", raising=False)
    monkeypatch.setattr("plugins.memory.supermemory._SupermemoryClient", FakeClient)
    monkeypatch.setattr(sm, "_dropped_key_reason", "", raising=False)  # process-level record of a key drop
    return tmp_path


def _provider(home, config: dict, identity: str = "tb_king") -> SupermemoryMemoryProvider:
    (home / "supermemory.json").write_text(json.dumps(config), encoding="utf-8")
    p = SupermemoryMemoryProvider()
    p.initialize("session-1", hermes_home=str(home), platform="cli", agent_identity=identity)
    return p


def _mirror(p: SupermemoryMemoryProvider, content: str = "synthetic built-in memory fact") -> None:
    p.on_memory_write("add", "memory", content)
    if p._write_thread is not None:
        p._write_thread.join(timeout=5)


PILOT = {
    "container_tag": "hermes_{identity}",
    "enable_custom_container_tags": True,
    "custom_containers": ["hermes_fleet_pilot"],
    "containers": {
        "hermes_{identity}": {"read": True, "write": True},
        "hermes_fleet_pilot": {"read": True, "write": False},
    },
}


# ---- Finding 1: built-in memory mirroring honors auto_capture and write approval ----------------


def test_mirroring_skipped_when_auto_capture_false(home):
    p = _provider(home, {"auto_capture": False})
    _mirror(p)
    assert p._client.add_calls == []


def test_mirroring_skipped_when_primary_container_not_write_approved(home):
    p = _provider(home, {**PILOT, "auto_capture": True,
                         "containers": {**PILOT["containers"], "hermes_{identity}": {"read": True, "write": False}}})
    _mirror(p)
    assert p._client.add_calls == []


def test_mirroring_skipped_when_permission_map_omits_primary(home):
    p = _provider(home, {**PILOT, "auto_capture": True,
                         "containers": {"hermes_fleet_pilot": {"read": True, "write": False}}})
    _mirror(p)
    assert p._client.add_calls == []


def test_mirroring_writes_when_auto_capture_on_and_primary_approved(home):
    p = _provider(home, {**PILOT, "auto_capture": True})
    _mirror(p)
    assert [c["content"] for c in p._client.add_calls] == ["synthetic built-in memory fact"]
    assert p._container_tag == "hermes_tb_king"


def test_turn_capture_skipped_when_primary_container_not_write_approved(home):
    p = _provider(home, {**PILOT, "auto_capture": True,
                         "containers": {"hermes_{identity}": {"read": True, "write": False}}})
    p.sync_turn("synthetic user turn", "synthetic assistant turn", session_id="session-1")
    assert p._client.add_calls == []


def test_retry_flush_uses_the_same_capture_gate(home):
    """Defence in depth: even a pending turn (normally impossible here) is not flushed to an unapproved container."""
    p = _provider(home, {**PILOT, "auto_capture": True,
                         "containers": {"hermes_{identity}": {"read": True, "write": False}}})
    p._pending_turns = [{"user": "synthetic", "assistant": "synthetic", "session_id": "session-1"}]
    p.on_session_end([])
    p.shutdown()
    assert p._client.add_calls == []


# ---- Finding 2: per-container operation permissions --------------------------------------------


def _call(p, tool, **args):
    return json.loads(p.handle_tool_call(tool, args))


def test_store_refused_on_read_only_container(home, caplog):
    p = _provider(home, PILOT)
    with caplog.at_level(logging.WARNING, logger="plugins.memory.supermemory"):
        out = _call(p, "supermemory-save", content="synthetic fact", container_tag="hermes_fleet_pilot")
    assert "error" in out and "hermes_fleet_pilot" in out["error"] and "write" in out["error"]
    assert p._client.add_calls == []
    assert any("hermes_fleet_pilot" in r.getMessage() for r in caplog.records)


def test_forget_by_id_refused_on_read_only_container(home):
    p = _provider(home, PILOT)
    out = _call(p, "supermemory_forget", id="doc_1", container_tag="hermes_fleet_pilot")
    assert "error" in out
    assert p._client.forgotten_ids == []


def test_forget_by_query_refused_on_read_only_container(home):
    p = _provider(home, PILOT)
    out = _call(p, "supermemory_forget", query="synthetic", container_tag="hermes_fleet_pilot")
    assert "error" in out
    assert p._client.forget_queries == []


def test_search_allowed_on_read_only_container(home):
    p = _provider(home, PILOT)
    out = _call(p, "supermemory-search", query="synthetic", container_tag="hermes_fleet_pilot")
    assert out["count"] == 1
    assert p._client.search_calls == ["hermes_fleet_pilot"]


def test_search_filters_quarantined_document_id(home):
    p = _provider(home, {**PILOT, "quarantined_memory_ids": {"hermes_fleet_pilot": ["r1"]}})
    p._client.search_results.append({"id": "r2", "memory": "approved cited result", "similarity": 0.8})
    out = _call(p, "supermemory-search", query="synthetic", container_tag="hermes_fleet_pilot")
    assert out["count"] == 1
    assert [item["id"] for item in out["results"]] == ["r2"]
    assert "synthetic result" not in json.dumps(out)


def test_search_filters_quarantined_parent_custom_id(home):
    custom_id = "session_2026-09-24_b4"
    p = _provider(home, {**PILOT, "quarantined_memory_ids": {"hermes_fleet_pilot": [custom_id]}})
    p._client.search_results[0]["documents"] = [{"id": custom_id}]
    p._client.search_results.append({"id": "r2", "memory": "approved cited result", "similarity": 0.8,
                                     "documents": [{"id": "approved_custom_id"}]})
    out = _call(p, "supermemory-search", query="synthetic", container_tag="hermes_fleet_pilot")
    assert [item["id"] for item in out["results"]] == ["r2"]
    assert "synthetic result" not in json.dumps(out)


def test_real_memory_field_mapper_preserves_nested_documents():
    item = type("SdkResult", (), {
        "id": "chunk-1", "memory": "synthetic", "similarity": 0.9,
        "metadata": {}, "documents": [type("Document", (), {"id": "parent-custom-id"})()],
    })()
    mapped = sm._memory_fields(item, "id", "memory", "similarity", "metadata", "documents")
    assert mapped["id"] == "chunk-1"
    assert mapped["documents"][0].id == "parent-custom-id"


def test_profile_refused_for_container_with_quarantined_document(home):
    p = _provider(home, {**PILOT, "quarantined_memory_ids": {"hermes_fleet_pilot": ["r1"]}})
    out = _call(p, "supermemory-profile", query="synthetic", container_tag="hermes_fleet_pilot")
    assert "error" in out and "quarantined" in out["error"]
    assert p._client.profile_calls == []


def test_malformed_quarantine_entry_fails_closed(home):
    p = _provider(home, {**PILOT, "quarantined_memory_ids": {"hermes_fleet_pilot": "r1"}})
    out = _call(p, "supermemory-search", query="synthetic", container_tag="hermes_fleet_pilot")
    assert out["count"] == 0 and out["results"] == []


def test_forget_by_query_skips_quarantined_match(home):
    p = _provider(home, {"container_tag": "hermes_{identity}",
                         "quarantined_memory_ids": {"hermes_{identity}": ["r1"]}})
    p._client.search_results.append({"id": "r2", "memory": "approved cited result", "similarity": 0.8})
    out = _call(p, "supermemory-forget", query="synthetic")
    assert out == {"success": True, "message": "Forgot the best non-quarantined match.", "id": "r2"}
    assert p._client.forgotten_ids == [("r2", None)]
    assert "synthetic result" not in json.dumps(out)


def test_prefetch_uses_id_bearing_search_when_primary_has_quarantine(home):
    p = _provider(home, {"container_tag": "hermes_{identity}",
                         "quarantined_memory_ids": {"hermes_{identity}": ["r1"]}})
    p._client.search_results.append({"id": "r2", "memory": "approved cited result", "similarity": 0.8})
    p.on_turn_start(1, "synthetic")
    recalled = p.prefetch("synthetic")
    assert "approved cited result" in recalled
    assert "synthetic result" not in recalled
    assert p._client.profile_calls == []


def test_store_allowed_on_write_approved_primary(home):
    p = _provider(home, PILOT)
    out = _call(p, "supermemory_store", content="synthetic fact")
    assert out["saved"] is True
    assert len(p._client.add_calls) == 1


def test_store_refused_on_primary_without_write(home):
    p = _provider(home, {**PILOT, "containers": {"hermes_{identity}": {"read": True, "write": False}}})
    out = _call(p, "supermemory_store", content="synthetic fact")
    assert "error" in out
    assert p._client.add_calls == []


def test_unlisted_container_is_denied_once_a_permission_map_exists(home):
    p = _provider(home, {**PILOT, "custom_containers": ["hermes_fleet_pilot", "scratch"],
                         "containers": {"hermes_{identity}": {"read": True, "write": True}}})
    assert "error" in _call(p, "supermemory_store", content="x", container_tag="scratch")
    assert "error" in _call(p, "supermemory_search", query="x", container_tag="scratch")
    assert "error" in _call(p, "supermemory_profile", container_tag="scratch")
    assert p._client.add_calls == [] and p._client.search_calls == [] and p._client.profile_calls == []


def test_prefetch_skipped_when_primary_not_readable(home):
    p = _provider(home, {**PILOT, "auto_recall": True, "containers": {"hermes_fleet_pilot": {"read": True}}})
    assert p.prefetch("synthetic question") == ""
    assert p._client.profile_calls == []


def test_no_permission_map_keeps_legacy_behavior(home):
    p = _provider(home, {"enable_custom_container_tags": True, "custom_containers": ["other"]})
    out = _call(p, "supermemory_store", content="synthetic fact", container_tag="other")
    assert out["saved"] is True


# ---- Finding 3: availability needs a fresh per-process proof, not key presence -----------------


def test_gate_off_by_default_keeps_key_presence_semantics(home):
    (home / "supermemory.json").write_text("{}", encoding="utf-8")
    assert SupermemoryMemoryProvider().is_available() is True


def test_missing_proof_makes_provider_unavailable_and_drops_inherited_key(home):
    (home / "supermemory.json").write_text(json.dumps({"require_availability_proof": True}), encoding="utf-8")
    p = SupermemoryMemoryProvider()
    assert p.is_available() is False
    assert "SUPERMEMORY_API_KEY" not in os.environ
    assert PROOF_ENV in p.unavailable_reason()


def test_down_proof_makes_provider_unavailable_and_drops_inherited_key(home, monkeypatch):
    monkeypatch.setenv(PROOF_ENV, f"down:{os.getpid()}:port_closed")
    (home / "supermemory.json").write_text(json.dumps({"require_availability_proof": True}), encoding="utf-8")
    p = SupermemoryMemoryProvider()
    assert p.is_available() is False
    assert "SUPERMEMORY_API_KEY" not in os.environ
    assert "port_closed" in p.unavailable_reason()


def test_proof_inherited_from_parent_process_is_stale(home, monkeypatch):
    monkeypatch.setenv(PROOF_ENV, _valid_proof(pid=os.getpid() + 1))
    (home / "supermemory.json").write_text(json.dumps({"require_availability_proof": True}), encoding="utf-8")
    assert SupermemoryMemoryProvider().is_available() is False
    assert "SUPERMEMORY_API_KEY" not in os.environ


def test_proof_for_a_different_key_is_rejected(home, monkeypatch):
    monkeypatch.setenv(PROOF_ENV, _valid_proof(key="some-other-synthetic-key"))
    (home / "supermemory.json").write_text(json.dumps({"require_availability_proof": True}), encoding="utf-8")
    assert SupermemoryMemoryProvider().is_available() is False


def test_fresh_proof_for_this_process_and_key_is_accepted(home, monkeypatch):
    monkeypatch.setenv(PROOF_ENV, _valid_proof())
    (home / "supermemory.json").write_text(json.dumps({"require_availability_proof": True}), encoding="utf-8")
    assert SupermemoryMemoryProvider().is_available() is True
    assert os.environ.get("SUPERMEMORY_API_KEY") == KEY


def test_initialize_stays_inert_without_proof_even_if_is_available_was_skipped(home):
    p = _provider(home, {"require_availability_proof": True})
    assert p._client is None and p._active is False
    assert p.system_prompt_block() == ""
    assert "not configured" in p.handle_tool_call("supermemory_search", {"query": "x"})


def test_multiplex_gate_failure_does_not_touch_process_environ(home, monkeypatch):
    monkeypatch.setattr(sm, "is_multiplex_active", lambda: True)
    monkeypatch.setattr(sm, "get_secret", lambda name, default=None: {"SUPERMEMORY_API_KEY": KEY}.get(name, default))
    (home / "supermemory.json").write_text(json.dumps({"require_availability_proof": True}), encoding="utf-8")
    assert SupermemoryMemoryProvider().is_available() is False
    assert os.environ.get("SUPERMEMORY_API_KEY") == KEY


def test_status_reports_gate_reason_without_the_key(home, monkeypatch):
    monkeypatch.setenv(PROOF_ENV, f"down:{os.getpid()}:listener_unverified")
    (home / "supermemory.json").write_text(json.dumps({"require_availability_proof": True}), encoding="utf-8")
    summary = SupermemoryMemoryProvider().get_status_config({})["summary"]
    assert summary.startswith("✗") and "listener_unverified" in summary
    assert KEY not in summary


# ---- A running provider re-checks availability before every client use -------------------------
# A gateway keeps one provider per agent for hours. The start-up proof alone would let it keep sending the
# key after the tunnel is gone, so every client use re-checks, and a failed re-check drops the client and key.

LIVE = {**PILOT, "require_availability_proof": True, "auto_capture": True, "auto_recall": True}
TUNNEL = {"ssh_host": "rosie", "forward": "/tunnel-dir/rosie.sock:127.0.0.1:6768"}
LIVE_TUNNEL = {**LIVE, "tunnel": TUNNEL}
TUNNEL_ARGV = ["/usr/bin/ssh", "-N", "-o", "BatchMode=yes", "-L", TUNNEL["forward"], "rosie"]


def _client_calls(client) -> list:
    return client.add_calls + client.search_calls + client.profile_calls + client.forgotten_ids + client.forget_queries


def _flush(p):
    p._pending_turns = [{"user": "synthetic", "assistant": "synthetic", "session_id": "session-1"}]
    p.on_session_end([])


USES = {
    "search": lambda p: p.handle_tool_call("supermemory-search", {"query": "synthetic"}),
    "profile": lambda p: p.handle_tool_call("supermemory_profile", {}),
    "store": lambda p: p.handle_tool_call("supermemory_store", {"content": "synthetic fact"}),
    "forget": lambda p: p.handle_tool_call("supermemory_forget", {"id": "doc_1"}),
    "prefetch": lambda p: p.prefetch("synthetic question"),
    "capture": lambda p: p.sync_turn("synthetic user", "synthetic assistant", session_id="session-1"),
    "mirror": _mirror,
    "flush": _flush,
}


def _live_provider(home, monkeypatch, config=LIVE) -> SupermemoryMemoryProvider:
    monkeypatch.setenv(PROOF_ENV, _valid_proof())
    p = _provider(home, config)
    assert p._active and p._client is not None
    return p


@pytest.mark.parametrize("use", sorted(USES))
def test_positive_control_live_provider_reaches_the_client(home, monkeypatch, use):
    p = _live_provider(home, monkeypatch)
    USES[use](p)
    assert _client_calls(p._client)


@pytest.mark.parametrize("use", sorted(USES))
def test_down_after_initialize_blocks_every_client_use_and_drops_the_key(home, monkeypatch, use):
    p = _live_provider(home, monkeypatch)
    client = p._client
    monkeypatch.setenv(PROOF_ENV, f"down:{os.getpid()}:port_closed")
    USES[use](p)
    assert _client_calls(client) == []
    assert p._client is None and p._api_key == "" and p._active is False


def test_tool_call_after_down_is_a_loud_error_and_the_instance_stays_off(home, monkeypatch, caplog):
    p = _live_provider(home, monkeypatch)
    client = p._client
    monkeypatch.setenv(PROOF_ENV, f"down:{os.getpid()}:port_closed")
    with caplog.at_level(logging.WARNING, logger="plugins.memory.supermemory"):
        out = json.loads(p.handle_tool_call("supermemory-search", {"query": "synthetic"}))
    assert "disabled" in out["error"] and "port_closed" in out["error"]
    assert any("disabled" in r.getMessage() and "port_closed" in r.getMessage() for r in caplog.records)
    assert not any(KEY in r.getMessage() for r in caplog.records) and KEY not in out["error"]
    # A dropped key is gone for this instance: a proof that looks good again does not revive it.
    monkeypatch.setenv(PROOF_ENV, _valid_proof())
    assert "error" in json.loads(p.handle_tool_call("supermemory-search", {"query": "synthetic"}))
    assert _client_calls(client) == [] and p._client is None


def test_key_swapped_in_scope_after_initialize_drops_the_client(home, monkeypatch):
    """The instance holds the key the helper vouched for at start; a different key now in scope is not that key."""
    p = _live_provider(home, monkeypatch)
    client = p._client
    monkeypatch.setenv("SUPERMEMORY_API_KEY", "synthetic-other-key-0003")
    monkeypatch.setenv(PROOF_ENV, _valid_proof(key="synthetic-other-key-0003"))
    assert "error" in json.loads(p.handle_tool_call("supermemory-search", {"query": "synthetic"}))
    assert _client_calls(client) == [] and p._api_key == ""


class TunnelOps:
    """Stands in for the helper's SystemOps: the forward's socket and its listener, as lstat, the peer credentials and
    the kernel argv would report them. (test_supermemory_socket_transport.py runs the real ones.)"""

    def __init__(self):
        self.socket, self.argv, self.checks = ("", (1, 100)), list(TUNNEL_ARGV), 0

    def uid(self):
        return os.getuid()

    def check_socket(self, socket_path):
        self.checks += 1
        return self.socket

    def socket_peer(self, socket_path, timeout):
        return 4242, os.getuid()

    def command_argv(self, pid, timeout):
        return self.argv


@pytest.fixture
def tunnel_ops(monkeypatch):
    from plugins.memory.supermemory import tunnel_key_helper
    ops = TunnelOps()
    monkeypatch.setattr(tunnel_key_helper, "SystemOps", lambda: ops)
    return ops


def test_tunnel_is_verified_at_start_and_before_every_use(home, monkeypatch, tunnel_ops):
    """No re-check interval: a use never rides on an earlier check."""
    p = _live_provider(home, monkeypatch, LIVE_TUNNEL)
    assert tunnel_ops.checks == 1
    for _ in range(3):
        USES["search"](p)
    assert tunnel_ops.checks == 4 and len(p._client.search_calls) == 3


@pytest.mark.parametrize("break_tunnel,reason", [
    (lambda ops: setattr(ops, "socket", ("socket_missing", None)), "socket_missing"),          # tunnel gone
    (lambda ops: setattr(ops, "socket", ("", (1, 101))), "socket_replaced"),                   # another socket there
    (lambda ops: setattr(ops, "argv", ["/usr/bin/python3", "-m", "http.server"]), "listener_unverified"),
    (lambda ops: setattr(ops, "argv", ["/usr/bin/ssh", "-N", "-L", TUNNEL["forward"], "other-host"]),
     "listener_unverified"),
])
def test_tunnel_lost_after_initialize_drops_client_and_key_at_the_next_use(home, monkeypatch, tunnel_ops,
                                                                          break_tunnel, reason):
    p = _live_provider(home, monkeypatch, LIVE_TUNNEL)
    client = p._client
    break_tunnel(tunnel_ops)
    out = json.loads(p.handle_tool_call("supermemory-search", {"query": "synthetic"}))
    assert "disabled" in out["error"] and reason in out["error"]
    assert _client_calls(client) == [] and p._client is None and p._api_key == ""


def test_tunnel_down_at_start_keeps_the_provider_unavailable(home, monkeypatch, tunnel_ops):
    monkeypatch.setenv(PROOF_ENV, _valid_proof())
    tunnel_ops.socket = ("socket_missing", None)
    (home / "supermemory.json").write_text(json.dumps(LIVE_TUNNEL), encoding="utf-8")
    p = SupermemoryMemoryProvider()
    assert p.is_available() is False and "socket_missing" in p.unavailable_reason()
    p.initialize("session-1", hermes_home=str(home), platform="cli", agent_identity="tb_king")
    assert p._client is None and p._active is False


@pytest.mark.parametrize("config", [
    {**LIVE_TUNNEL, "base_url": "http://127.0.0.1:16768"},                   # a TCP route next to the socket
    {**LIVE_TUNNEL, "tunnel": {**TUNNEL, "forward": "127.0.0.1:16768:127.0.0.1:6768"}},   # round 3's TCP forward
    {**LIVE_TUNNEL, "tunnel": {**TUNNEL, "forward": "tunnel-dir/rosie.sock:127.0.0.1:6768"}},  # relative path
    {**LIVE_TUNNEL, "tunnel": {"ssh_host": "rosie"}},                        # malformed: no forward
    {**LIVE_TUNNEL, "tunnel": "rosie"},
])
def test_inconsistent_tunnel_settings_fail_closed(home, monkeypatch, tunnel_ops, config):
    monkeypatch.setenv(PROOF_ENV, _valid_proof())
    (home / "supermemory.json").write_text(json.dumps(config), encoding="utf-8")
    p = SupermemoryMemoryProvider()
    assert p.is_available() is False and "tunnel" in p.unavailable_reason()


def test_reason_survives_the_key_drop_across_calls_and_instances(home, monkeypatch, tunnel_ops):
    """`hermes memory status` asks several provider instances in one process. The first failed gate drops the key; the
    later ones must still name the real cause, not the missing key that drop left behind."""
    monkeypatch.setenv(PROOF_ENV, _valid_proof())
    tunnel_ops.socket = ("socket_missing", None)
    (home / "supermemory.json").write_text(json.dumps(LIVE_TUNNEL), encoding="utf-8")
    assert SupermemoryMemoryProvider().is_available() is False
    assert "SUPERMEMORY_API_KEY" not in os.environ
    assert "socket_missing" in SupermemoryMemoryProvider().unavailable_reason()
    assert "socket_missing" in SupermemoryMemoryProvider().get_status_config({})["summary"]


def test_recheck_reads_a_real_bound_secret_scope(home, monkeypatch):
    """TUI/Desktop bodies bind a secret scope even in a single-profile process, and get_secret serves the scope before
    os.environ. The re-check must see a down proof there (no patched get_secret)."""
    from agent import secret_scope
    monkeypatch.delenv("SUPERMEMORY_API_KEY")
    token = secret_scope.set_secret_scope({"SUPERMEMORY_API_KEY": KEY, PROOF_ENV: _valid_proof()})
    try:
        p = _provider(home, LIVE)
    finally:
        secret_scope.reset_secret_scope(token)
    client = p._client
    assert p._active and client is not None
    token = secret_scope.set_secret_scope({"SUPERMEMORY_API_KEY": KEY, PROOF_ENV: f"down:{os.getpid()}:port_closed"})
    try:
        out = json.loads(p.handle_tool_call("supermemory-search", {"query": "synthetic"}))
    finally:
        secret_scope.reset_secret_scope(token)
    assert "disabled" in out["error"] and "port_closed" in out["error"]
    assert _client_calls(client) == [] and p._client is None and p._api_key == ""
