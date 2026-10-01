"""carry t_2e0ceb41: ``message_agent`` on opted-in gateway surfaces (Slack, Meet bridge, ...).

Contract:
- A profile lists gateway platforms in its OWN config.yaml under
  ``bot_mode.message_agent_platforms``; a session on that platform gets the tool.
- Default (no key) is unchanged: only the canonical Bot Chat gets it.
- cli/tui/cron/subagent/kanban/batch can never be opted in.
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


@pytest.mark.parametrize("platform", ["cli", "tui", "cron", "subagent", "kanban", "batch", ""])
def test_never_list_cannot_be_opted_in(tmp_path, platform):
    home = _install(tmp_path, platforms=[platform or "x", "cli", "tui", "cron", "subagent", "kanban", "batch"])
    agent = _FakeAgent(home, platform=platform)
    assert bot_mode_dm.message_agent_authorized(agent) is False
    assert "cli" not in bot_mode_dm.message_agent_surfaces(home)


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
