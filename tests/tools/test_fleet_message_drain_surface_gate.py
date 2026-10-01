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


def test_drain_gate_uses_strict_bot_chat(tmp_path, monkeypatch):
    from tools import fleet_message_drain

    agent = _FakeAgent(_install(tmp_path, platforms=["slack"]), platform="slack")
    monkeypatch.setattr(fleet_message_drain, "drain_config", lambda: object())
    assert fleet_message_drain._api_drain_enabled(agent) is False
    assert fleet_message_drain.drain_agent_turn(agent, tmp_path) is False
