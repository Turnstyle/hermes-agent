"""Same-account credential pool + 429: fall back fast instead of sleeping 600s per step.

Live case (Sheldon, 2026-09-28): an anthropic pool with two entries on ONE account
(env OAuth token + claude_code) answered every call with an account-level 429 and a
600s Retry-After. ``_pool_may_recover_from_rate_limit`` said "rotation may recover"
(2 entries, one available), so the eager fallback never fired and each retry slept
600s before the cross-family fallback chain was ever tried.

Pins:
  (a) same-account 2-entry pool, both 429 -> fallback activates, no backoff >= 60s;
  (b) distinct entries where rotation succeeds -> no fallback;
  (c) no fallback chain -> pre-fix retry-same-then-rotate behaviour unchanged.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent

ACCOUNT_429 = (
    "Error code: 429 - {'type': 'error', 'error': {'type': 'rate_limit_error', 'message': "
    "\"This request would exceed your account's rate limit. Please try again later.\"}}"
)


def _tool_defs():
    return [{"type": "function", "function": {
        "name": "web_search", "description": "web_search tool",
        "parameters": {"type": "object", "properties": {}},
    }}]


@pytest.fixture()
def agent():
    with (
        patch("model_tools.get_tool_definitions", return_value=_tool_defs()),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
        )
    a.client = MagicMock()
    a._cached_system_prompt = "You are helpful."
    a._use_prompt_caching = False
    a.compression_enabled = False
    a.save_trajectories = False
    return a


def _ok(content="done"):
    msg = SimpleNamespace(content=content, tool_calls=None, reasoning=None,
                          reasoning_content=None, reasoning_details=None, role="assistant")
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                           model="test/model", usage=None)


def _account_429():
    exc = Exception(ACCOUNT_429)
    exc.status_code = 429
    exc.response = SimpleNamespace(headers={"retry-after": "600"})
    return exc


class _Entry:
    def __init__(self, id_):
        self.id = id_
        self.priority = 0
        self.runtime_api_key = f"key-{id_}"


class _Pool:
    """Two-entry pool stand-in: rotation from A goes to B, from B nowhere."""

    provider = ""

    def __init__(self):
        self._entries = [_Entry("aaa111"), _Entry("bbb222")]
        self.exhausted = set()
        self.rotations = []

    def entries(self):
        return list(self._entries)

    def has_available(self, **_kw):
        return any(e.id not in self.exhausted for e in self._entries)

    def has_credentials(self):
        return True

    def current(self):
        return None

    def mark_exhausted_and_rotate(self, *, credential_id=None, api_key_hint=None, **_kw):
        failed = credential_id or next(
            (e.id for e in self._entries if e.runtime_api_key == api_key_hint), None)
        if failed:
            self.exhausted.add(failed)
        self.rotations.append(failed)
        return next((e for e in self._entries if e.id not in self.exhausted), None)


def _wire(agent, pool, fallback_chain):
    agent._credential_pool = pool
    agent._credential_pool_entry_id = "aaa111"
    agent.api_key = "key-aaa111"

    def _swap(entry):
        agent._credential_pool_entry_id = entry.id
        agent.api_key = entry.runtime_api_key
        return True

    agent._swap_credential = _swap
    agent._fallback_chain = fallback_chain
    agent._fallback_index = 0
    agent._fallback_activated = False


def _run(agent, fallback_calls):
    sleeps = []

    def _record_sleep(_agent, wait_time, _retry, **_kw):
        sleeps.append(wait_time)
        return None

    def _fake_fallback(*_a, **_kw):
        if agent._fallback_index >= len(agent._fallback_chain):
            return False
        fallback_calls.append(agent._fallback_chain[agent._fallback_index])
        agent._fallback_index += 1
        agent._fallback_activated = True
        agent._credential_pool = None  # fallback provider has its own credentials
        return True

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent, "_try_activate_fallback", side_effect=_fake_fallback),
        patch("agent.turn_api_error.interruptible_backoff_sleep", side_effect=_record_sleep),
    ):
        result = agent.run_conversation("hello")
    return result, sleeps


def test_same_account_pool_both_429_falls_back_without_long_sleep(agent):
    """(a) Both same-account entries 429 -> the fallback chain takes the turn, no 600s wait."""
    pool = _Pool()
    _wire(agent, pool, [{"provider": "openai-codex", "model": "gpt-6-sol"}])
    agent.client.chat.completions.create.side_effect = [_account_429(), _account_429(), _ok("fallback")]
    fallback_calls = []

    result, sleeps = _run(agent, fallback_calls)

    assert result["completed"] is True
    assert result["final_response"] == "fallback"
    assert fallback_calls == [{"provider": "openai-codex", "model": "gpt-6-sol"}]
    assert pool.rotations[:1] == ["aaa111"]  # one cheap rotation still happened
    assert all(s < 60 for s in sleeps), sleeps
    assert agent.client.chat.completions.create.call_count == 3


def test_account_limit_after_one_rotation_skips_second_rotation(agent):
    """(a') Rotation lands on a sibling that 429s with the account message: fall back at once,
    even though the pool still reports an available entry."""
    pool = _Pool()
    pool._entries.append(_Entry("ccc333"))  # a third entry the pool would happily rotate to
    _wire(agent, pool, [{"provider": "openai-codex", "model": "gpt-6-sol"}])
    agent.client.chat.completions.create.side_effect = [_account_429(), _account_429(), _ok("fallback")]
    fallback_calls = []

    result, sleeps = _run(agent, fallback_calls)

    assert result["final_response"] == "fallback"
    assert len(fallback_calls) == 1
    assert pool.rotations == ["aaa111"]  # no second rotation onto ccc333
    assert all(s < 60 for s in sleeps), sleeps


def test_distinct_entries_rotation_succeeds_no_fallback(agent):
    """(b) Different accounts: the rotated entry answers, the fallback chain stays untouched."""
    pool = _Pool()
    _wire(agent, pool, [{"provider": "openai-codex", "model": "gpt-6-sol"}])
    agent.client.chat.completions.create.side_effect = [_account_429(), _ok("rotated")]
    fallback_calls = []

    result, sleeps = _run(agent, fallback_calls)

    assert result["completed"] is True
    assert result["final_response"] == "rotated"
    assert fallback_calls == []
    assert agent._fallback_index == 0
    assert pool.rotations == ["aaa111"]
    assert agent._credential_pool_entry_id == "bbb222"
    assert sleeps == []


def test_no_fallback_chain_keeps_retry_same_then_rotate(agent):
    """(c) No fallback chain: first 429 retries the same entry after the provider's backoff
    (old behaviour), the second 429 rotates."""
    pool = _Pool()
    _wire(agent, pool, [])
    agent.client.chat.completions.create.side_effect = [_account_429(), _account_429(), _ok("rotated")]
    fallback_calls = []

    result, sleeps = _run(agent, fallback_calls)

    assert result["final_response"] == "rotated"
    assert fallback_calls == []
    assert sleeps == [600]  # Retry-After still honoured when there is nowhere else to go
    assert pool.rotations == ["aaa111"]
