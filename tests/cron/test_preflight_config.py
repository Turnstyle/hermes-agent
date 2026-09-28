"""Cron pre-dispatch configuration validation (T1-26).

A job whose configuration cannot possibly produce a successful run — missing
provider API key, unready attached skill (missing required env), unknown
delivery platform — must be blocked BEFORE any agent machinery is constructed:

  - ``last_status`` becomes ``blocked_config`` (not a generic ``error``),
  - exactly ONE alert is delivered (no re-alert every tick — same
    alert-once spirit as the dead-pin auto-pause in #73506),
  - the agent is NEVER constructed, so no LLM call is burned.

``cron.preflight: false`` in config.yaml restores the old behavior (the run
proceeds to resolution and fails loudly every tick).

Related precedent: #27948 (fail-loud for hidden tools — same fail-before-run
spirit, different check).
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import cron.jobs as cron_jobs
from cron.scheduler import run_job
import cron.scheduler as sched


_RUNTIME = {
    "api_key": "test-key",
    "base_url": "https://example.invalid/v1",
    "provider": "openrouter",
    "api_mode": "chat_completions",
}


def _job(**overrides):
    job = {
        "id": "pf-test",
        "name": "preflight test",
        "prompt": "hello",
        "enabled": True,
        "state": "scheduled",
        "schedule": {"kind": "interval", "minutes": 5, "display": "every 5m"},
        "deliver": "local",
        "model": None,
        "provider": None,
        "base_url": None,
    }
    job.update(overrides)
    return job


class _AuthErrorFactory:
    """Raise a real AuthError from hermes_cli.auth."""

    def __call__(self, **kwargs):
        from hermes_cli.auth import AuthError

        raise AuthError("No API key configured for provider 'openrouter'")


def _run_job_patched(job, tmp_path, *, resolve=None, skill_view=None):
    """Drive run_job with the standard cron-test seams patched.

    Returns (success, output, final_response, error, agent_constructed).
    """
    fake_db = MagicMock()
    patches = [
        patch("cron.scheduler._hermes_home", tmp_path),
        patch("cron.scheduler_delivery._resolve_origin", return_value=None),
        patch("hermes_cli.env_loader.load_hermes_dotenv"),
        patch("hermes_cli.env_loader.reset_secret_source_cache"),
        patch("hermes_state_registry.acquire", return_value=fake_db),
        patch("tools.mcp_tool_discovery.discover_mcp_tools", return_value=[]),
    ]
    if resolve is None:
        patches.append(
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                return_value=dict(_RUNTIME),
            )
        )
    else:
        patches.append(
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                side_effect=resolve,
            )
        )
    if skill_view is not None:
        patches.append(patch("tools.skills_tool.skill_view", side_effect=skill_view))

    with patch("run_agent.AIAgent") as mock_agent_cls:
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "ok"}
        mock_agent_cls.return_value = mock_agent
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            success, output, final_response, error = run_job(job)
        agent_constructed = mock_agent_cls.called
    return success, output, final_response, error, agent_constructed


def _run_mcp_tick(job, tmp_path, *, deliver=None):
    """Run one cron tick with a configured MCP server that discovers no tools."""
    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state_registry.acquire", return_value=MagicMock()), \
         patch("tools.mcp_tool_discovery.discover_mcp_tools", return_value=[]), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider",
               return_value=dict(_RUNTIME)), \
         patch("run_agent.AIAgent") as agent_cls:
        agent_cls.return_value.run_conversation.return_value = {"final_response": "ok"}
        if deliver is not None:
            with patch.object(sched, "_deliver_result", side_effect=deliver):
                assert sched.run_one_job(job) is True
        else:
            assert sched.run_one_job(job) is True
        return agent_cls.called


def _configure_empty_mcp_server(tmp_path):
    from tools.registry import registry

    (tmp_path / "config.yaml").write_text(
        "model:\n  default: test-model\nmcp_servers:\n  notion:\n"
        "    url: https://mcp.invalid\n", encoding="utf-8")
    registry.register_toolset_alias("notion", "mcp-notion")


class TestMissingProviderKeyBlocks:
    def test_missing_key_blocked_config_no_agent(self, tmp_path):
        """Missing provider key (AuthError, no fallback chain) → blocked_config,
        agent never constructed, no LLM run burned."""
        job = _job()
        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            success, output, final_response, error, agent_constructed = \
                _run_job_patched(job, tmp_path, resolve=_AuthErrorFactory())

        assert agent_constructed is False
        assert success is False
        assert error is not None
        assert "[blocked_config]" in error
        assert "blocked" in output.lower() or "BLOCKED" in output

    def test_single_alert_across_two_ticks_and_blocked_status(self, tmp_path):
        """Two ticks of a blocked job through run_one_job deliver exactly ONE
        alert and persist last_status='blocked_config'."""
        job = _job()
        deliveries = []

        def fake_deliver(job, content, adapters=None, loop=None, **kwargs):
            deliveries.append(content)
            return None

        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            fake_db = MagicMock()
            for _tick in range(2):
                fresh = [j for j in cron_jobs.load_jobs() if j["id"] == job["id"]][0]
                with patch("cron.scheduler._hermes_home", tmp_path), \
                     patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
                     patch("hermes_cli.env_loader.load_hermes_dotenv"), \
                     patch("hermes_cli.env_loader.reset_secret_source_cache"), \
                     patch("hermes_state_registry.acquire", return_value=fake_db), \
                     patch("tools.mcp_tool_discovery.discover_mcp_tools", return_value=[]), \
                     patch("hermes_cli.runtime_provider.resolve_runtime_provider",
                           side_effect=_AuthErrorFactory()), \
                     patch.object(sched, "_deliver_result", side_effect=fake_deliver), \
                     patch("run_agent.AIAgent") as mock_agent_cls:
                    ok = sched.run_one_job(fresh)
                    assert ok is True
                    assert mock_agent_cls.called is False

            stored = [j for j in cron_jobs.load_jobs() if j["id"] == job["id"]][0]

        assert stored["last_status"] == "blocked_config"
        assert len(deliveries) == 1, (
            f"expected exactly one alert across two ticks, got {len(deliveries)}: "
            f"{deliveries!r}"
        )

    def test_pinned_local_block_alert_reaches_own_bot_chat_once(self, tmp_path):
        """The one preflight alert uses a real own-profile target and books its delivery."""
        from cron.executions import get_execution

        job = _job(provider="anthropic", model="claude-sonnet-5", deliver="local")
        targets_seen = []
        outcomes = []

        def missing_anthropic(**kwargs):
            assert kwargs.get("requested") == "anthropic"
            from hermes_cli.auth import AuthError
            raise AuthError("no key")

        def capture_targets(delivery_job, content, adapters=None, loop=None, **kwargs):
            assert kwargs["for_failure"] is True
            targets_seen.append(sched._resolve_delivery_targets(delivery_job, for_failure=True))
            assert "did not run" in content.lower()
            return None

        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            for _ in range(2):
                fresh = cron_jobs.get_job(job["id"])
                with patch("cron.scheduler._hermes_home", tmp_path), \
                     patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
                     patch("hermes_cli.env_loader.load_hermes_dotenv"), \
                     patch("hermes_cli.env_loader.reset_secret_source_cache"), \
                     patch("hermes_state_registry.acquire", return_value=MagicMock()), \
                     patch("tools.mcp_tool_discovery.discover_mcp_tools", return_value=[]), \
                     patch("hermes_cli.runtime_provider.resolve_runtime_provider",
                           side_effect=missing_anthropic), \
                     patch.object(sched, "_deliver_result", side_effect=capture_targets), \
                     patch("run_agent.AIAgent") as mock_agent_cls:
                    assert sched.run_one_job(fresh) is True
                    assert mock_agent_cls.called is False
                outcomes.append(get_execution(fresh["execution_id"])["delivery_outcome"])
            stored = cron_jobs.get_job(job["id"])

        assert stored["last_status"] == "blocked_config"
        assert stored["deliver"] == "local"
        assert stored.get("failure_deliver") is None
        assert targets_seen == [[{"platform": "bot-chat", "chat_id": "", "thread_id": None}]]
        assert outcomes == ["delivered", "suppressed"]

    def test_empty_mcp_toolset_local_alert_reaches_own_bot_chat_once(self, tmp_path):
        """Three blocked ticks retain one marker and send one own-profile Bot Chat alert."""
        _configure_empty_mcp_server(tmp_path)
        job = _job(enabled_toolsets=["terminal", "notion"], deliver="local")
        targets_seen = []

        def capture_targets(delivery_job, _content, adapters=None, loop=None, **kwargs):
            assert kwargs["for_failure"] is True
            targets_seen.append(sched._resolve_delivery_targets(delivery_job, for_failure=True))
            return None

        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            for _ in range(3):
                fresh = cron_jobs.get_job(job["id"])
                assert _run_mcp_tick(fresh, tmp_path, deliver=capture_targets) is False
            stored = cron_jobs.get_job(job["id"])

        assert stored["last_status"] == "blocked_config"
        assert stored.get("preflight_alerted") is True
        assert targets_seen == [[{"platform": "bot-chat", "chat_id": "", "thread_id": None}]]

    def test_empty_mcp_toolset_recovery_clears_alert_marker(self, tmp_path):
        """A healthy tick after an MCP block re-arms the alert for a future break."""
        _configure_empty_mcp_server(tmp_path)
        job = _job(enabled_toolsets=["terminal", "notion"])
        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            assert _run_mcp_tick(cron_jobs.get_job(job["id"]), tmp_path) is False
            assert cron_jobs.get_job(job["id"]).get("preflight_alerted") is True

            cron_jobs.update_job(job["id"], {"enabled_toolsets": ["terminal"]})
            assert _run_mcp_tick(cron_jobs.get_job(job["id"]), tmp_path) is True
            assert not cron_jobs.get_job(job["id"]).get("preflight_alerted")

    @pytest.mark.parametrize("local_lane", ["local", None, "", [], "none"])
    def test_blocked_config_local_lanes_resolve_to_own_bot_chat(self, local_lane):
        job = _job(deliver=local_lane)
        delivery_job = sched._blocked_config_delivery_job(job, blocked_config=True)
        assert sched._resolve_delivery_targets(delivery_job, for_failure=True) == [
            {"platform": "bot-chat", "chat_id": "", "thread_id": None}]
        assert job["deliver"] == local_lane
        assert job.get("failure_deliver") is None

    def test_blocked_config_configured_lane_keeps_its_only_target(self):
        job = _job(deliver="telegram")
        with patch("cron.scheduler_delivery._get_home_target_chat_id", return_value="123"), \
             patch("cron.scheduler_delivery._get_home_target_thread_id", return_value=None), \
             patch("cron.scheduler_delivery._is_known_delivery_platform", return_value=True), \
             patch("cron.scheduler_delivery._resolve_origin", return_value=None):
            targets = sched._resolve_delivery_targets(
                sched._blocked_config_delivery_job(job, blocked_config=True), for_failure=True)
        assert len(targets) == 1
        assert targets[0]["platform"] == "telegram"
        assert not any(target["platform"] == "bot-chat" for target in targets)

    def test_blocked_config_local_failure_override_uses_own_bot_chat(self):
        job = _job(deliver="telegram", failure_deliver="local")
        delivery_job = sched._blocked_config_delivery_job(job, blocked_config=True)
        assert sched._resolve_delivery_targets(delivery_job, for_failure=True) == [
            {"platform": "bot-chat", "chat_id": "", "thread_id": None}]
        assert job["deliver"] == "telegram"
        assert job["failure_deliver"] == "local"

    def test_ordinary_local_failure_stays_local(self):
        job = _job(deliver="local")
        delivery_job = sched._blocked_config_delivery_job(job, blocked_config=False)
        assert delivery_job is job
        assert sched._resolve_delivery_targets(delivery_job, for_failure=True) == []

    def test_fallback_chain_rescues_missing_primary_key(self, tmp_path):
        """A configured fallback chain means a missing primary key does NOT
        block — the existing auth-fallback path handles it."""
        (tmp_path / "config.yaml").write_text(
            "fallback_providers:\n"
            "  - provider: openrouter\n"
            "    model: z-ai/glm-5.2\n",
            encoding="utf-8",
        )
        calls = []

        def resolve(**kwargs):
            calls.append(kwargs.get("requested"))
            if kwargs.get("requested") in (None, ""):
                from hermes_cli.auth import AuthError

                raise AuthError("no key")
            return {**_RUNTIME, "provider": "openrouter"}

        job = _job()
        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            success, output, final_response, error, agent_constructed = \
                _run_job_patched(job, tmp_path, resolve=resolve)

        assert agent_constructed is True
        assert success is True
        assert error is None

    def test_global_chain_does_not_rescue_a_pinned_job(self, tmp_path):
        """A pinned job never walks the global chain (#100437), so the chain must not skip the
        missing-key check for it either: block before the agent is built."""
        (tmp_path / "config.yaml").write_text(
            "fallback_providers:\n"
            "  - provider: openrouter\n"
            "    model: z-ai/glm-5.2\n",
            encoding="utf-8",
        )
        calls = []

        def resolve(**kwargs):
            calls.append(kwargs.get("requested"))
            if kwargs.get("requested") == "anthropic":
                from hermes_cli.auth import AuthError

                raise AuthError("no key")
            return {**_RUNTIME, "provider": kwargs.get("requested")}

        job = _job(provider="anthropic", model="claude-sonnet-5")
        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            success, output, final_response, error, agent_constructed = \
                _run_job_patched(job, tmp_path, resolve=resolve)

        assert agent_constructed is False
        assert success is False
        assert "provider credential missing" in (error or "")
        assert "openrouter" not in calls


class TestHealthyJobUnaffected:
    def test_healthy_job_runs_normally(self, tmp_path):
        job = _job()
        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            success, output, final_response, error, agent_constructed = \
                _run_job_patched(job, tmp_path)

        assert success is True
        assert error is None
        assert final_response == "ok"
        assert agent_constructed is True

    def test_recovery_clears_alert_marker(self, tmp_path):
        """After a blocked tick, a healthy tick clears the alert-dedup marker
        so a FUTURE config break re-alerts."""
        job = _job()
        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            # Tick 1: blocked.
            _run_job_patched(job, tmp_path, resolve=_AuthErrorFactory())
            stored = [j for j in cron_jobs.load_jobs() if j["id"] == job["id"]][0]
            assert stored.get("preflight_alerted")
            # Tick 2: key restored → healthy run clears the marker.
            fresh = [j for j in cron_jobs.load_jobs() if j["id"] == job["id"]][0]
            success, *_rest, agent_constructed = _run_job_patched(fresh, tmp_path)
            assert success is True
            assert agent_constructed is True
            stored = [j for j in cron_jobs.load_jobs() if j["id"] == job["id"]][0]
            assert not stored.get("preflight_alerted")

    def test_fail_open_validator_keeps_alert_marker(self, tmp_path):
        job = _job(preflight_alerted=True)
        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            with patch("cron.scheduler._preflight_job_config", side_effect=RuntimeError("probe failed")):
                success, *_rest, agent_constructed = _run_job_patched(job, tmp_path)
            assert cron_jobs.get_job(job["id"]).get("preflight_alerted") is True
        assert success is True
        assert agent_constructed is True


class TestOptOut:
    def test_preflight_false_keeps_existing_alert_marker(self, tmp_path):
        (tmp_path / "config.yaml").write_text("cron:\n  preflight: false\n", encoding="utf-8")
        job = _job(preflight_alerted=True)
        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            success, *_rest, agent_constructed = _run_job_patched(job, tmp_path)
            assert cron_jobs.get_job(job["id"]).get("preflight_alerted") is True
        assert success is True
        assert agent_constructed is True

    def test_preflight_false_restores_old_behavior(self, tmp_path):
        """cron.preflight: false → job proceeds to resolution and fails the
        old way (error status, re-alerts every tick, no blocked_config)."""
        (tmp_path / "config.yaml").write_text(
            "cron:\n  preflight: false\n", encoding="utf-8"
        )
        job = _job()
        deliveries = []

        def fake_deliver(job, content, adapters=None, loop=None, **kwargs):
            deliveries.append(content)
            return None

        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            fake_db = MagicMock()
            for _tick in range(2):
                fresh = [j for j in cron_jobs.load_jobs() if j["id"] == job["id"]][0]
                with patch("cron.scheduler._hermes_home", tmp_path), \
                     patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
                     patch("hermes_cli.env_loader.load_hermes_dotenv"), \
                     patch("hermes_cli.env_loader.reset_secret_source_cache"), \
                     patch("hermes_state_registry.acquire", return_value=fake_db), \
                     patch("tools.mcp_tool_discovery.discover_mcp_tools", return_value=[]), \
                     patch("hermes_cli.runtime_provider.resolve_runtime_provider",
                           side_effect=_AuthErrorFactory()), \
                     patch.object(sched, "_deliver_result", side_effect=fake_deliver), \
                     patch("run_agent.AIAgent") as mock_agent_cls:
                    sched.run_one_job(fresh)
                    assert mock_agent_cls.called is False

            stored = [j for j in cron_jobs.load_jobs() if j["id"] == job["id"]][0]

        assert stored["last_status"] == "error"
        assert len(deliveries) == 2  # old behavior: alert every tick


class TestSkillReadiness:
    def test_unready_skill_blocks(self, tmp_path):
        """An attached skill whose readiness_status is setup_needed (missing
        required env) blocks the run before the agent is constructed."""
        payload = json.dumps(
            {
                "success": True,
                "content": "# needy skill\nbody",
                "readiness_status": "setup_needed",
                "setup_needed": True,
                "missing_required_environment_variables": ["NEEDY_API_KEY"],
                "missing_required_commands": [],
            }
        )

        def fake_skill_view(name, *args, **kwargs):
            return payload

        job = _job(skills=["needy-skill"])
        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            success, output, final_response, error, agent_constructed = \
                _run_job_patched(job, tmp_path, skill_view=fake_skill_view)

        assert agent_constructed is False
        assert success is False
        assert error is not None and "[blocked_config]" in error
        assert "NEEDY_API_KEY" in f"{error} {output}"

    def test_ready_skill_runs(self, tmp_path):
        payload = json.dumps(
            {
                "success": True,
                "content": "# ready skill\nbody",
                "readiness_status": "available",
                "setup_needed": False,
                "missing_required_environment_variables": [],
            }
        )

        def fake_skill_view(name, *args, **kwargs):
            return payload

        job = _job(skills=["ready-skill"])
        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            success, output, final_response, error, agent_constructed = \
                _run_job_patched(job, tmp_path, skill_view=fake_skill_view)

        assert success is True
        assert agent_constructed is True


class TestDeliveryPlatform:
    def test_unknown_delivery_platform_blocks(self, tmp_path):
        job = _job(deliver="notaplatform")
        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            with patch("cron.scheduler_delivery._is_known_delivery_platform",
                       return_value=False):
                success, output, final_response, error, agent_constructed = \
                    _run_job_patched(job, tmp_path)

        assert agent_constructed is False
        assert success is False
        assert error is not None and "[blocked_config]" in error
        assert "notaplatform" in f"{error} {output}"

    def test_local_delivery_never_touches_gateway_config(self, tmp_path):
        """deliver=local jobs must not load gateway config in preflight."""
        job = _job(deliver="local")
        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            with patch("gateway.config.load_gateway_config",
                       side_effect=AssertionError("gateway config loaded")):
                success, *_rest, agent_constructed = _run_job_patched(job, tmp_path)

        assert success is True
        assert agent_constructed is True
