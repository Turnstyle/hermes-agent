"""carry t_2e0ceb41: ``message_agent`` on opted-in gateway surfaces (Slack, Meet bridge, ...).

Contract:
- A profile lists gateway platforms in its OWN config.yaml under
  ``bot_mode.message_agent_platforms``; a session on that platform gets the tool.
- Default (no key) is unchanged: only the canonical Bot Chat gets it.
- cli/tui/cron/subagent/batch cannot be opted in; dispatched kanban workers send one-way queued messages.
- The install must still be Bot-Mode-managed and the protocol switch on.
- Sender identity is the profile's own (home-derived), and the fleet message drain
  stays Bot-Chat-only.
"""

import json
import textwrap
from pathlib import Path

import pytest

from tools import bot_mode_dm, bot_mode_probe


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    bot_mode_probe._reset_cache_for_tests()
    yield
    bot_mode_probe._reset_cache_for_tests()


def _install(tmp_path, *, platforms=None, managed=True) -> Path:
    """<root>/profiles/{cos,coordinator}; returns cos's home."""
    root = tmp_path / ".hermes"
    for name in ("cos", "coordinator"):
        d = root / "profiles" / name
        d.mkdir(parents=True, exist_ok=True)
        if managed:
            (d / "profile.yaml").write_text(
                textwrap.dedent("""\
                    description: teammate
                    ui_meta:
                      hermes-bots:
                        shape: cloud
                """),
                encoding="utf-8",
            )
    cos = root / "profiles" / "cos"
    if platforms is not None:
        body = "bot_mode:\n  message_agent_platforms: " + json.dumps(platforms) + "\n"
        (cos / "config.yaml").write_text(body, encoding="utf-8")
    return cos


class _FakeDB:
    def __init__(self, home: Path, title: str):
        self.db_path = str(home / "state.db")
        self._title = title

    def get_session_title(self, _sid):
        return self._title


class _FakeAgent:
    def __init__(self, home: Path, *, platform: str, title: str = "Slack thread"):
        self._session_db = _FakeDB(home, title)
        self.session_id = "sess-1"
        self._session_title_hint = None
        self._bot_mode_protocol = True
        self.platform = platform
        self.tools: list = []
        self.valid_tool_names: set = set()


def _names(agent):
    return [t["function"]["name"] for t in agent.tools]


# ── injection gate ───────────────────────────────────────────────────────────


def test_opted_in_slack_session_gets_tool(tmp_path):
    agent = _FakeAgent(_install(tmp_path, platforms=["slack"]), platform="slack")
    assert bot_mode_dm.message_agent_authorized(agent) is True
    assert bot_mode_dm.ensure_message_agent_tool(agent) is True
    assert _names(agent) == ["message_agent"]
    assert "message_agent" in agent.valid_tool_names
    # idempotent / byte-stable
    assert bot_mode_dm.ensure_message_agent_tool(agent) is True
    assert len(agent.tools) == 1


def test_default_is_unchanged_without_opt_in(tmp_path):
    agent = _FakeAgent(_install(tmp_path), platform="slack")
    assert bot_mode_dm.ensure_message_agent_tool(agent) is False
    assert agent.tools == []


def test_other_platform_not_opted_in(tmp_path):
    agent = _FakeAgent(_install(tmp_path, platforms=["slack"]), platform="telegram")
    assert bot_mode_dm.ensure_message_agent_tool(agent) is False


def test_string_value_and_case_accepted(tmp_path):
    agent = _FakeAgent(_install(tmp_path, platforms="Slack"), platform="slack")
    assert bot_mode_dm.message_agent_authorized(agent) is True


@pytest.mark.parametrize("platform", ["cli", "tui", "cron", "subagent", "batch", ""])
def test_never_list_cannot_be_opted_in(tmp_path, platform):
    home = _install(tmp_path, platforms=[platform or "x", "cli", "tui", "cron", "subagent", "batch"])
    agent = _FakeAgent(home, platform=platform)
    assert bot_mode_dm.message_agent_authorized(agent) is False
    assert "cli" not in bot_mode_dm.message_agent_surfaces(home)


def test_kanban_worker_authorized_without_opt_in(tmp_path):
    agent = _FakeAgent(_install(tmp_path), platform="kanban")
    agent.task_id = "t_test123"
    assert bot_mode_dm.message_agent_authorized(agent) is True
    assert bot_mode_dm.ensure_message_agent_tool(agent) is True
    assert _names(agent) == ["message_agent"]
    assert "message_agent" in agent.valid_tool_names


def test_kanban_worker_queued_delivery(tmp_path, monkeypatch):
    cos = _install(tmp_path)
    agent = _FakeAgent(cos, platform="kanban")
    agent.task_id = "t_card_abc123"

    queued_calls = []
    def fake_enqueue(*, sender, recipient, body, **kw):
        queued_calls.append({"sender": sender, "recipient": recipient, "body": body})
        return "fm_msg_999"

    comment_calls = []
    def fake_add_comment(conn, tid, author, body):
        comment_calls.append({"tid": tid, "author": author, "body": body})
        return 1

    monkeypatch.setattr("tools.fleet_message_enqueue.enqueue_busy_dm", fake_enqueue)
    from hermes_cli import kanban_db
    monkeypatch.setattr(kanban_db, "add_comment", fake_add_comment)

    out = json.loads(bot_mode_dm.message_agent_tool(target="coordinator", message="need help unblocking", agent=agent))
    assert out["status"] == "queued"
    assert out["message_id"] == "fm_msg_999"
    assert out["reply_route"] == "t_card_abc123"
    assert len(queued_calls) == 1
    assert queued_calls[0]["sender"] == "t_card_abc123"
    assert queued_calls[0]["recipient"] == "coordinator"
    assert "need help unblocking" in queued_calls[0]["body"]
    assert len(comment_calls) == 1
    assert comment_calls[0]["tid"] == "t_card_abc123"
    assert "coordinator" in comment_calls[0]["body"]
    assert "fm_msg_999" in comment_calls[0]["body"]


def test_kanban_worker_missing_card_id_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    agent = _FakeAgent(_install(tmp_path), platform="kanban")
    out = json.loads(bot_mode_dm.message_agent_tool(target="coordinator", message="hello", agent=agent))
    assert "error" in out
    assert "lacks card id" in out["error"]


def test_kanban_worker_system_prompt_gets_roster(tmp_path):
    from agent import system_prompt

    agent = _FakeAgent(_install(tmp_path), platform="kanban")
    agent.task_id = "t_card_123"
    parts = system_prompt._bot_mode_parts(agent)
    assert parts and "message_agent" in parts[0] and "@coordinator" in parts[0]


def test_unmanaged_install_still_refused(tmp_path):
    agent = _FakeAgent(_install(tmp_path, platforms=["slack"], managed=False), platform="slack")
    assert bot_mode_dm.message_agent_authorized(agent) is False


def test_protocol_switch_off_still_refused(tmp_path):
    agent = _FakeAgent(_install(tmp_path, platforms=["slack"]), platform="slack")
    agent._bot_mode_protocol = False
    assert bot_mode_dm.message_agent_authorized(agent) is False


def test_bot_chat_still_works_and_is_canonical(tmp_path):
    agent = _FakeAgent(_install(tmp_path), platform="cli", title="Bot Chat")
    assert bot_mode_dm.is_canonical_bot_chat(agent) is True
    assert bot_mode_dm.message_agent_authorized(agent) is True


def test_slack_session_is_not_canonical_bot_chat(tmp_path):
    """The fleet message drain keys on is_canonical_bot_chat; a Slack session must not drain."""
    agent = _FakeAgent(_install(tmp_path, platforms=["slack"]), platform="slack")
    assert bot_mode_dm.message_agent_authorized(agent) is True
    assert bot_mode_dm.is_canonical_bot_chat(agent) is False


def test_memo_is_session_stable(tmp_path):
    home = _install(tmp_path, platforms=["slack"])
    agent = _FakeAgent(home, platform="slack")
    assert bot_mode_dm.message_agent_authorized(agent) is True
    (home / "config.yaml").write_text("bot_mode: {}\n", encoding="utf-8")
    # config change applies to the NEXT session, not mid-session (prompt-cache stability)
    assert bot_mode_dm.message_agent_authorized(agent) is True
    fresh = _FakeAgent(home, platform="slack")
    assert bot_mode_dm.message_agent_authorized(fresh) is False


def test_bound_home_override_wins(tmp_path):
    """The gateway shares ONE launch state.db and binds the profile home per turn."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    cos = _install(tmp_path, platforms=["slack"])
    launch_root = cos.parent.parent  # db_path parent would be the default profile
    agent = _FakeAgent(launch_root, platform="slack")
    assert bot_mode_dm.message_agent_authorized(agent) is False  # default profile did not opt in
    token = set_hermes_home_override(str(cos))
    try:
        assert bot_mode_dm.message_agent_authorized(agent) is True
    finally:
        reset_hermes_home_override(token)


# ── dispatch: sender identity ────────────────────────────────────────────────


def test_dispatch_from_slack_uses_own_sender_identity(tmp_path, monkeypatch):
    cos = _install(tmp_path, platforms=["slack"])
    agent = _FakeAgent(cos, platform="slack")
    seen = {}

    def fake_start(argv, content, label, **kw):
        seen.update(argv=argv, content=content, label=label, author=kw.get("author"))
        return json.dumps({"status": "queued", "delivery_id": "x", "to": label})

    monkeypatch.setattr(bot_mode_dm, "_start_delivery", fake_start)
    out = json.loads(bot_mode_dm.message_agent_tool(target="coordinator", message="need fact X", agent=agent))
    assert out["status"] == "queued"
    assert seen["author"]["id"] == "bot:cos"
    assert "(@cos)" in seen["content"] and seen["content"].endswith("need fact X")
    assert "-p" in seen["argv"] and seen["argv"][seen["argv"].index("-p") + 1] == "coordinator"


def test_dispatch_refused_from_unopted_surface(tmp_path):
    agent = _FakeAgent(_install(tmp_path), platform="slack")
    out = json.loads(bot_mode_dm.message_agent_tool(target="coordinator", message="hi", agent=agent))
    assert "error" in out and "message_agent_platforms" in out["error"]


# ── prompt: roster for opted-in surfaces, no timeless flag ───────────────────


def test_prompt_roster_section_for_opted_in_surface(tmp_path):
    from agent import system_prompt

    agent = _FakeAgent(_install(tmp_path, platforms=["slack"]), platform="slack")
    parts = system_prompt._bot_mode_parts(agent)
    assert parts and "message_agent" in parts[0] and "@coordinator" in parts[0]
    assert not getattr(agent, "_bot_chat_timeless_prompt", False)


def test_prompt_no_roster_without_opt_in(tmp_path):
    from agent import system_prompt

    agent = _FakeAgent(_install(tmp_path), platform="slack")
    assert system_prompt._bot_mode_parts(agent) == []

@pytest.mark.parametrize("platform", ["cron", "subagent", "batch", "tui"])
def test_inherited_card_environment_does_not_authorize_other_runners(tmp_path, monkeypatch, platform):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_origin")
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
    agent = _FakeAgent(_install(tmp_path), platform=platform)
    agent.task_id = "t_origin"
    assert bot_mode_dm.message_agent_authorized(agent) is False


def test_card_id_without_dispatcher_source_does_not_authorize_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_origin")
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    agent = _FakeAgent(_install(tmp_path), platform="cli")
    agent.task_id = "t_origin"
    assert bot_mode_dm.message_agent_authorized(agent) is False


def test_dispatcher_source_authorizes_actual_cli_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_origin")
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
    agent = _FakeAgent(_install(tmp_path), platform="cli")
    assert bot_mode_dm.message_agent_authorized(agent) is True


def test_worker_queue_payload_is_accepted_by_real_drain(tmp_path, monkeypatch):
    from tools import fleet_message_enqueue as enqueue, fleet_message_drain as drain
    from tests.tools.test_fleet_message_drain import MemoryStore
    from hermes_cli import kanban_db

    home = _install(tmp_path)
    agent = _FakeAgent(home, platform="kanban")
    agent.task_id = "t_origin_card"
    board_path = str(tmp_path / "origin-board.db")
    monkeypatch.setenv("HERMES_KANBAN_DB", board_path)
    store = MemoryStore()

    def writer(paths, collection, set_by):
        assert collection == "fleet_messages_v1"
        doc = json.loads(Path(paths[0]).read_text())
        assert set_by == "t_origin_card"
        store.seed(doc["message_id"], doc)
        return 0

    monkeypatch.setattr(enqueue, "_default_writer", writer)
    monkeypatch.setattr(kanban_db, "add_comment", lambda *args, **kwargs: 1)
    result = json.loads(bot_mode_dm.message_agent_tool(target="coordinator", message="help with this card", agent=agent))
    assert result["status"] == "queued" and result["reply_relayed"] is False
    assert result["delivery_owner_verified"] is False
    assert "not confirmed delivery" in result["warning"]
    assert drain.claim_next(store, "another-profile") is None
    claimed = drain.claim_next(store, "coordinator")
    assert claimed is not None and claimed.doc_id == result["message_id"]
    assert claimed.fields["from"] == "t_origin_card"
    text, author, metadata = drain.render_input(claimed)
    assert "help with this card" in text and board_path in text and str(home) in text
    assert "Reply route hint: card t_origin_card" in text
    assert author["id"] == "bot:t_origin_card"
    assert drain.claim_next(store, "coordinator") is None


def test_worker_queue_error_does_not_expose_adapter_payload(tmp_path, monkeypatch, caplog):
    from tools import fleet_message_enqueue as enqueue

    agent = _FakeAgent(_install(tmp_path), platform="kanban")
    agent.task_id = "t_origin_card"
    def writer(*args, **kwargs):
        raise RuntimeError("synthetic-sensitive-adapter-payload")
    monkeypatch.setattr(enqueue, "_default_writer", writer)
    output = bot_mode_dm.message_agent_tool(target="coordinator", message="help", agent=agent)
    assert "FleetEnqueueError" in output and "Next step" in output
    assert "synthetic-sensitive-adapter-payload" not in output + caplog.text

def test_worker_known_local_drain_is_named_without_fleet_delivery_claim(tmp_path, monkeypatch):
    from tools import fleet_message_enqueue as enqueue
    from hermes_cli import kanban_db

    home = _install(tmp_path)
    recipient = home.parent / "coordinator"
    (recipient / "config.yaml").write_text("fleet_messages:\n  drain_on_turn_end: true\n  target: live\n")
    agent = _FakeAgent(home, platform="kanban")
    agent.task_id = "t_origin"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    monkeypatch.setattr(enqueue, "_default_writer", lambda *args, **kwargs: 0)
    monkeypatch.setattr(kanban_db, "add_comment", lambda *args, **kwargs: 1)
    result = json.loads(bot_mode_dm.message_agent_tool(target="coordinator", message="help", agent=agent))
    assert result["status"] == "queued"
    assert result["delivery_owner_verified"] is True and result["warning"] is None
    assert result["reply_relayed"] is False
