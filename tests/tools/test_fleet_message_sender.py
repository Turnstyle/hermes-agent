"""G15 sender (``message_agent``) on the native door: a definite no-send to an opted-in teammate is
durably queued; an uncertain outcome never is. Real ``bot_mode_dm`` and ``bot_live_delivery`` code;
fakes only for the authority RPC/discovery, the background runner and the registry writer."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.tools.test_bot_mode_dm import _FakeAgent, _capture_spawn, _managed_home
from tools import bot_live_delivery as live
from tools import bot_mode_dm, bot_mode_probe
from tools import fleet_message_spool as spool

OPT_IN = "fleet_messages:\n  drain_on_turn_end: true\n  target: emulator\n  emulator_host: 127.0.0.1:1\n"


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    bot_mode_probe._reset_cache_for_tests()
    yield
    bot_mode_probe._reset_cache_for_tests()


@pytest.fixture
def install(tmp_path, monkeypatch):
    home = _managed_home(tmp_path)
    researcher = home / "profiles" / "researcher"
    monkeypatch.setattr(bot_mode_dm, "_dm_dir", lambda: tmp_path)
    spawned = _capture_spawn(monkeypatch)
    return home, researcher, spawned, tmp_path


def _not_ready(monkeypatch):
    def unavailable(h):
        raise ValueError("profile authority is not ready")
    monkeypatch.setattr(live, "find_canonical_live_owner", unavailable)


def _send(home, **kw):
    return json.loads(bot_mode_dm.message_agent_tool(target="researcher", message="status?",
                                                     agent=_FakeAgent(home, **kw)))


def test_definite_no_send_to_an_opted_in_teammate_is_durably_queued(install, monkeypatch):
    home, researcher, spawned, dm_dir = install
    (researcher / "config.yaml").write_text(OPT_IN, encoding="utf-8")
    _not_ready(monkeypatch)
    result = _send(home)
    assert result["status"] == "queued" and result["reason"] == "target_busy"
    assert result["refusal_reason"] == "runtime_unavailable" and result["queue_admission"] == "enabled"
    assert result["reply_relayed"] is False and "Do NOT resend" in result["detail"]
    envelope = spool.read_envelope(researcher, result["message_id"])
    assert envelope["recipient"] == "researcher" and envelope["sender"] == "default"
    assert envelope["body"].startswith("Message from ") and envelope["body"].endswith("status?")
    assert spawned == []  # no runner, no CLI child
    assert list(dm_dir.glob("*.live.json")) == [] and list(dm_dir.glob("dm-*.txt")) == []


@pytest.mark.parametrize("config, admission", [(None, "missing_config"), ("model: {}\n", "missing_opt_in"),
                                               ("fleet_messages:\n  drain_on_turn_end: false\n", "disabled")])
def test_not_opted_in_teammate_gets_the_typed_refusal(install, monkeypatch, config, admission):
    home, researcher, spawned, _ = install
    if config is not None:
        (researcher / "config.yaml").write_text(config, encoding="utf-8")
    _not_ready(monkeypatch)
    result = _send(home)
    assert result["reason"] == "runtime_unavailable" and "status" not in result
    assert result["queue_admission"] == admission and "NOT sent" in result["error"]
    assert not spool.spool_dir(researcher).exists()


def test_uncertain_admission_is_never_queued(install, monkeypatch):
    home, researcher, _, _ = install
    (researcher / "config.yaml").write_text(OPT_IN, encoding="utf-8")
    monkeypatch.setattr(live, "find_canonical_live_owner", lambda h: dict(
        profile_home=str(Path(h).resolve()), session_id="bot", canonical=True, lease_id="a", live_session_id="bot"))

    def lost(home, params):
        raise ConnectionError("socket closed after the RPC went out")

    monkeypatch.setattr(live, "authority_delivery", lost)
    result = _send(home)
    assert result["status"] == "ambiguous" and "Do not resend" in result["error"]
    assert not spool.spool_dir(researcher).exists()  # a second copy could run the message twice


def test_writer_held_chat_is_settled_then_queued(install, monkeypatch):
    from hermes_cli.gateway_client import GatewayRPCError
    from tools.bot_failure_reasons import HELD_NOT_SENT

    home, researcher, _, _ = install
    (researcher / "config.yaml").write_text(OPT_IN, encoding="utf-8")
    monkeypatch.setattr(live, "find_canonical_live_owner", lambda h: dict(
        profile_home=str(Path(h).resolve()), session_id="bot", canonical=True, lease_id="a", live_session_id="bot"))
    settled = []

    def held(home, params):
        raise GatewayRPCError(HELD_NOT_SENT, data={"admitted": False})

    def settle(home, params):
        settled.append(params["id"])
        return dict(status="failed", refused=True, reason=HELD_NOT_SENT, delivery_id=params["id"],
                    profile_home=str(Path(home).resolve()), session_id="bot", message=params["message"],
                    reply="", error="Settled as not sent")

    monkeypatch.setattr(live, "authority_delivery", held)
    monkeypatch.setattr(live, "authority_settle", settle)
    result = _send(home)
    assert len(settled) == 1  # the owner settled the original id as never sent before we queued
    assert result["status"] == "queued" and result["refusal_reason"] == HELD_NOT_SENT


def test_kanban_worker_message_is_one_way_queued_with_its_card(install, monkeypatch):
    home, researcher, spawned, _ = install
    (researcher / "config.yaml").write_text(OPT_IN, encoding="utf-8")
    agent = _FakeAgent(home, title="Kanban task")
    agent.platform = "kanban"
    agent.task_id = "t_card"
    assert bot_mode_dm.message_agent_authorized(agent)
    monkeypatch.setattr(live, "authority_delivery", lambda *a: pytest.fail("a worker never dials a Bot Chat turn"))
    result = json.loads(bot_mode_dm.message_agent_tool(target="researcher", message="done", agent=agent))
    assert result["status"] == "queued" and result["reply_route"] == "t_card"
    assert result["delivery_owner_verified"] is True and result["reply_relayed"] is False
    envelope = spool.read_envelope(researcher, result["message_id"])
    assert envelope["sender"] == "t_card" and "[card t_card;" in envelope["body"]
    assert spawned == []
