"""carry t_2e0ceb41 (drain part): the fleet message drain stays strict Bot Chat even when a
Slack/Meet session carries message_agent via bot_mode.message_agent_platforms.

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


# ── drain gate ──


def test_drain_gate_uses_strict_bot_chat(tmp_path):
    """A Slack session carrying message_agent is authorized for the tool, never Bot Chat."""
    agent = _FakeAgent(_install(tmp_path, platforms=["slack"]), platform="slack")
    assert bot_mode_dm.message_agent_authorized(agent) is True
    assert bot_mode_dm.is_canonical_bot_chat(agent) is False


@pytest.mark.parametrize("platform", ["cli", "tui", "cron", "subagent", "batch", "kanban"])
def test_internal_surfaces_can_never_be_opted_in(tmp_path, platform):
    home = _install(tmp_path, platforms=[platform])
    assert platform not in bot_mode_dm.message_agent_surfaces(home)
    agent = _FakeAgent(home, platform=platform)
    assert bot_mode_dm._surface_authorized(agent) is False


def test_default_install_keeps_message_agent_bot_chat_only(tmp_path):
    agent = _FakeAgent(_install(tmp_path), platform="slack")
    assert bot_mode_dm.message_agent_authorized(agent) is False
    assert bot_mode_dm.ensure_message_agent_tool(agent) is False and _names(agent) == []


def test_unmanaged_install_never_authorizes_a_surface(tmp_path):
    agent = _FakeAgent(_install(tmp_path, platforms=["slack"], managed=False), platform="slack")
    assert bot_mode_dm.message_agent_authorized(agent) is False


def test_surface_answer_is_stable_for_the_session(tmp_path):
    """Prompt-cache safety: a config edit mid-session never changes this session's tool list."""
    home = _install(tmp_path, platforms=["slack"])
    agent = _FakeAgent(home, platform="slack")
    assert bot_mode_dm.ensure_message_agent_tool(agent) is True
    (home / "config.yaml").write_text("bot_mode:\n  message_agent_platforms: []\n", encoding="utf-8")
    bot_mode_probe._reset_cache_for_tests()
    assert bot_mode_dm._surface_authorized(agent) is True
    assert _names(agent) == ["message_agent"]


def test_fleet_drain_admits_only_into_the_canonical_bot_chat(tmp_path, monkeypatch):
    """The drain is profile-scoped at the authority: the door targets the discovered canonical
    Bot Chat, never the messaging session (``sess-1``) that sent or carried the message."""
    from tests.tools.test_fleet_message_admission import FakeDoor
    from tests.tools.test_fleet_message_drain import NOW, MemoryStore, msg
    from tools import bot_live_delivery as live
    from tools import fleet_message_admission as fma
    from tools import fleet_message_drain as fmd

    home = _install(tmp_path, platforms=["slack"])
    door = FakeDoor()
    monkeypatch.setattr(live, "find_canonical_live_owner", lambda h: dict(
        profile_home=str(Path(h).resolve()), session_id="bot-chat-tip", canonical=True,
        lease_id="a", live_session_id="bot-chat-tip"))
    monkeypatch.setattr(live, "authority_delivery", door)
    store = MemoryStore()
    store.seed("for-cos", msg(5, to="cos"))
    config = fmd.DrainConfig(target="emulator", emulator_host="127.0.0.1:1")
    assert fma.drain_once(home, config=config, store=store, now=NOW) == "admitted"
    (_, params), = door.calls
    assert params["session_id"] == "bot-chat-tip" and "sess-1" not in json.dumps(params)
