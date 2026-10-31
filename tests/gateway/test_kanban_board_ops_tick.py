"""Board Ops gateway tick on the v0.22.5 delivery seams (fleet carry K01).

The passive exception notice goes through ``BaseAdapter.send(chat_id, content, ...)``
(positional, like the Kanban notifier) inside the recipient profile's
``_async_profile_runtime_scope``; the outcome lands in the durable receipt and is
never retried. No model turn, no secrets: the scope and adapter are fakes.
"""
from __future__ import annotations

import asyncio
import contextlib
import types
from contextlib import contextmanager

import pytest
import hermes_yaml as yaml

import gateway.run as gateway_run
from gateway import kanban_board_ops as gw
from gateway import kanban_watchers_notifier as notifier
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_board_ops as ops
from hermes_cli.kanban_db_notify import add_notify_sub


@contextmanager
def profile(root, name):
    token = set_hermes_home_override(root / "profiles" / name)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


class FakeAdapter:
    def __init__(self, success=True):
        self.calls, self.success = [], success

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.calls.append((chat_id, content, metadata))
        return types.SimpleNamespace(success=self.success)


@pytest.fixture
def escalation(tmp_path, monkeypatch):
    root = tmp_path / "fixture-node"
    for name in ("conductor", "ops", "king"):
        (root / "profiles" / name).mkdir(parents=True)
        (root / "profiles" / name / "config.yaml").write_text("{}\n", encoding="utf-8")
    (root / "config.yaml").write_text(yaml.safe_dump({"kanban": {"board_ops": {
        "enabled": True, "node_id": "fixture-node", "boards": ["fixture"],
        "owner_profile": "conductor", "executor_profile": "ops"}}}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "conductor"))
    monkeypatch.setattr(ops, "_now", lambda: 2000)
    kb.create_board("fixture")
    conn = kbc.connect(board="fixture")
    tid = kb.create_task(conn, title="FIXTURE card", tenant="fixture-tenant")
    conn.execute("UPDATE task_events SET created_at=1900 WHERE task_id=?", (tid,))
    add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="fixture-chat", thread_id="7",
                   notifier_profile="king")
    api = ops.BoardOps(conn, "fixture")
    with profile(root, "conductor"):
        api.control(enabled=True, human_attribution="Turner FIXTURE", authority_provenance="FIXTURE")
        api.grant({
            "grant_id": "g", "node_id": "fixture-node", "board": "fixture", "task_ids": [tid],
            "tenant": "fixture-tenant", "operator_profile": "ops", "owner_chain": ["Turner FIXTURE", "conductor"],
            "human_attribution": "Turner FIXTURE", "authority_provenance": "FIXTURE",
            "allowed_operations": ["escalate"], "expires_at": 2500,
            "escalation_recipient": {"profile": "king", "platform": "telegram", "chat_id": "fixture-chat", "thread_id": "7"},
            "review_authors": ["Turner FIXTURE"],
        })
    task = kb.get_task(conn, tid)
    event_id = conn.execute("SELECT MIN(id) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
    with profile(root, "ops"):
        api.request({"correlation": "c1", "grant_id": "g", "node_id": "fixture-node", "board": "fixture",
                     "task_id": tid, "tenant": "fixture-tenant", "operation": "escalate",
                     "task_sha256": kb.compute_task_sha256(task.title, task.body), "event_id": event_id,
                     "payload": {"reason": "FIXTURE judgment required", "jev_status": "not_configured"}})
    # The gateway executor runs as the admitted executor profile (process home).
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "ops"))
    scopes = []

    @contextlib.asynccontextmanager
    async def fake_scope(profile_home):
        scopes.append(str(profile_home))
        yield

    monkeypatch.setattr(gateway_run, "_async_profile_runtime_scope", fake_scope)
    yield root, conn, api, tid, scopes
    conn.close()


@pytest.mark.parametrize("success,state", [(True, "notified"), (False, "delivery_failed")])
def test_tick_sends_passive_notice_positionally_and_records_receipt(escalation, monkeypatch, success, state):
    root, conn, api, tid, scopes = escalation
    adapter = FakeAdapter(success=success)
    monkeypatch.setattr(notifier, "_adapter_for_subscription", lambda runner, platform, sub, profile: adapter)
    asyncio.run(gw.tick(types.SimpleNamespace(), kb))
    assert len(adapter.calls) == 1
    chat_id, text, metadata = adapter.calls[0]
    assert chat_id == "fixture-chat" and metadata == {"thread_id": "7"}
    assert "Board Ops exception c1" in text and "No maker restarted" in text
    assert scopes == [str((root / "profiles" / "king").resolve())]
    with profile(root, "ops"):
        receipt = api.receipt("c1")
    assert receipt["state"] == state
    assert receipt["delivery_result"]["automatic_retry"] is False
    # At-most-once: a second tick neither re-sends nor rewrites the receipt.
    asyncio.run(gw.tick(types.SimpleNamespace(), kb))
    assert len(adapter.calls) == 1


def test_tick_refuses_stateless_recipient_transport(escalation, monkeypatch):
    root, _conn, api, _tid, scopes = escalation
    adapter = FakeAdapter()
    adapter.supports_async_delivery = False
    monkeypatch.setattr(notifier, "_adapter_for_subscription", lambda runner, platform, sub, profile: adapter)
    asyncio.run(gw.tick(types.SimpleNamespace(), kb))
    assert adapter.calls == [] and scopes == []
    with profile(root, "ops"):
        assert api.receipt("c1")["state"] == "delivery_failed"


def _keep_spec_lane(tmp_path, monkeypatch):
    """A triage card with a reviewed spec, an unrelated promotable todo card, and a
    keep_spec request admitted for the executor; returns the executor-profile env."""
    root = tmp_path / "fixture-node"
    for name in ("conductor", "ops", "king"):
        (root / "profiles" / name).mkdir(parents=True)
        (root / "profiles" / name / "config.yaml").write_text("{}\n", encoding="utf-8")
    (root / "config.yaml").write_text(yaml.safe_dump({"kanban": {"board_ops": {
        "enabled": True, "node_id": "fixture-node", "boards": ["fixture"],
        "owner_profile": "conductor", "executor_profile": "ops"}}}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "conductor"))
    monkeypatch.setattr(ops, "_now", lambda: 2000)
    kb.create_board("fixture")
    conn = kbc.connect(board="fixture")
    tid = kb.create_task(conn, title="Reviewed spec", body="Keep these bytes\n", tenant="fixture-tenant", triage=True)
    unrelated = kb.create_task(conn, title="unrelated candidate", tenant="fixture-tenant")
    conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (unrelated,))
    conn.execute("UPDATE task_events SET created_at=1900 WHERE task_id=?", (tid,))
    conn.commit()
    add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="fixture-chat", notifier_profile="king")
    api = ops.BoardOps(conn, "fixture")
    with profile(root, "conductor"):
        api.control(enabled=True, human_attribution="Turner FIXTURE", authority_provenance="FIXTURE")
        api.grant({
            "grant_id": "g", "node_id": "fixture-node", "board": "fixture", "task_ids": [tid],
            "tenant": "fixture-tenant", "operator_profile": "ops", "owner_chain": ["Turner FIXTURE", "conductor"],
            "human_attribution": "Turner FIXTURE", "authority_provenance": "FIXTURE",
            "allowed_operations": ["keep_spec"], "expires_at": 2500,
            "escalation_recipient": {"profile": "king", "platform": "telegram", "chat_id": "fixture-chat", "thread_id": ""},
            "review_authors": ["Turner FIXTURE"],
        })
    task = kb.get_task(conn, tid)
    event_id = conn.execute("SELECT MIN(id) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
    with profile(root, "ops"):
        api.request({"correlation": "ks", "grant_id": "g", "node_id": "fixture-node", "board": "fixture",
                     "task_id": tid, "tenant": "fixture-tenant", "operation": "keep_spec",
                     "task_sha256": kb.compute_task_sha256(task.title, task.body), "event_id": event_id,
                     "payload": {"review_author": "Turner FIXTURE"}})
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "ops"))
    return root, conn, api, tid, unrelated


def test_watcher_tick_applies_keep_spec_without_side_effects_or_unrelated_promotion(tmp_path, monkeypatch):
    import subprocess
    from hermes_cli import kanban_board_ops_transactions as txn

    root, conn, api, tid, unrelated = _keep_spec_lane(tmp_path, monkeypatch)
    effects = []
    monkeypatch.setattr(kb, "_fire_task_hook", lambda *a, **k: effects.append(("hook", a)))
    monkeypatch.setattr(kb, "notify_task_updated", lambda *a, **k: effects.append(("notify", a)))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: effects.append(("popen", a)) or pytest.fail("process effect"))
    adapter = FakeAdapter()
    monkeypatch.setattr(notifier, "_adapter_for_subscription", lambda runner, platform, sub, profile: adapter)

    asyncio.run(gw.tick(types.SimpleNamespace(), kb))  # the dispatcher-timer entry point

    with profile(root, "ops"):
        receipt = api.receipt("ks")
    assert receipt["state"] == "applied"
    assert receipt["receipt"]["facts"]["status_after"] == "ready"
    target = kb.get_task(conn, tid)
    assert (target.status, target.title, target.body) == ("ready", "Reviewed spec", "Keep these bytes\n")
    # Composition limits promotion to the admitted card; the unrelated candidate waits for
    # the ordinary dispatcher pass.
    assert kb.get_task(conn, unrelated).status == "todo"
    assert effects == [] and adapter.calls == []
    # Composition is connection-local and released: nesting is not globally enabled.
    assert txn._scope.get() is None
    with kbc.write_txn(conn):
        with pytest.raises(RuntimeError, match="already inside a transaction"):
            kb.recompute_ready(conn)
    assert kb.recompute_ready(conn) == 1
    assert kb.get_task(conn, unrelated).status == "ready"


def test_watcher_tick_never_raises_when_receipt_readback_fails(escalation, monkeypatch):
    root, _conn, api, _tid, _scopes = escalation
    adapter = FakeAdapter()
    monkeypatch.setattr(notifier, "_adapter_for_subscription", lambda runner, platform, sub, profile: adapter)

    real_open, real_collect = gw.open_board_ops, gw._collect
    collected = {"done": False}

    def collect_then_break(kb_module):
        result = real_collect(kb_module)
        collected["done"] = True
        return result

    def open_failing_after_collect(board):
        if collected["done"]:
            raise OSError("FIXTURE board unavailable at readback")
        return real_open(board)

    monkeypatch.setattr(gw, "_collect", collect_then_break)
    monkeypatch.setattr(gw, "open_board_ops", open_failing_after_collect)
    asyncio.run(gw.tick(types.SimpleNamespace(), kb))  # must not raise
    assert len(adapter.calls) == 1
    with profile(root, "ops"):
        # Single attempt is durable as 'delivering'; it can only become delivery_unknown.
        assert api.receipt("c1")["state"] == "delivering"
