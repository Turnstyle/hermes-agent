"""Exact delegation-rule injection contract (Item U / Conductor c62920)."""

from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.prompt_builder import KANBAN_GUIDANCE
from agent.system_prompt import build_system_prompt


RULE = (
    "Use 1 to 10 delegate_task sub-agents and don't do the work inside your main model. "
    "Finish the whole outcome in THIS turn. Don't end your turn while helpers are still "
    "working; collect their results. If a helper fails, send another. Stop only when done "
    "and proved, or at a blocker only Turner can clear (name it in one line)."
)


def test_guidance_preserves_immutable_prefix_and_appends_exact_rule_once():
    # Item U explicitly makes this existing prefix byte-immutable.
    prefix = KANBAN_GUIDANCE.removesuffix("\n\n" + RULE)

    assert KANBAN_GUIDANCE.count(RULE) == 1
    assert len(prefix) == 7458
    assert sha256(prefix.encode("utf-8")).hexdigest() == (
        "8857144c259dc6a13d0bd7bc4f236371fc4cd50ed1e32b64a2964b6b1466e090"
    )
    assert KANBAN_GUIDANCE == prefix + "\n\n" + RULE


def _agent(home, worker=True):
    return SimpleNamespace(
        load_soul_identity=True, skip_context_files=True,
        valid_tool_names={"kanban_show"},
        _kanban_worker_guidance=KANBAN_GUIDANCE if worker else "",
        _session_db=SimpleNamespace(db_path=str(home / "state.db")),
        _task_completion_guidance=False, _parallel_tool_call_guidance=False,
        _tool_use_enforcement=False, _execution_guidance=False,
        _environment_probe=False, _bot_mode_protocol=False,
        _plugin_system_prompt_sections_snapshot=(),
        _memory_store=None, _memory_manager=None,
        model="", provider="", platform="", pass_session_id=False, session_id="",
        _emit_diagnostic_status=lambda *_args: None,
    )


@pytest.mark.parametrize("soul_rule", [None, "", RULE, RULE[:-1], RULE.replace("1 to 10", "1–10")],
                         ids=["no-soul", "plain-soul", "exact-soul", "partial-soul", "near-soul"])
def test_worker_injects_only_missing_exact_rule(tmp_path, monkeypatch, soul_rule):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    soul = None if soul_rule is None else f"SOUL sentinel before.\n\n{soul_rule}\n\nSOUL sentinel after."
    if soul is not None:
        (tmp_path / "SOUL.md").write_text(soul, encoding="utf-8")

    prompt = build_system_prompt(_agent(tmp_path))

    assert prompt.count(RULE) == 1
    old_guidance = KANBAN_GUIDANCE.removesuffix("\n\n" + RULE)
    assert old_guidance in prompt
    if soul is not None:
        assert soul in prompt  # Never strip or rewrite SOUL to dedupe.
    if soul_rule != RULE:
        assert KANBAN_GUIDANCE in prompt
    else:
        assert KANBAN_GUIDANCE not in prompt


def test_nonworker_does_not_acquire_kanban_rule(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)

    prompt = build_system_prompt(_agent(tmp_path, worker=False))

    assert RULE not in prompt
    assert KANBAN_GUIDANCE not in prompt


@pytest.mark.parametrize("source", [
    "caller", "project", "memory", "user", "external-memory", "plugin",
    "autoload", "skills", "platform", "environment",
])
@pytest.mark.parametrize("exact", [True, False], ids=["exact", "near"])
def test_dedupe_checks_other_assembled_content(tmp_path, monkeypatch, source, exact):
    from agent import system_prompt
    from hermes_cli.plugins import RenderedPluginSystemPromptSection

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    text = RULE if exact else RULE.replace("1 to 10", "1–10")
    block = f"{source} before.\n\n{text}\n\n{source} after."
    agent = _agent(tmp_path)
    caller = None
    if source == "caller":
        caller = block
    elif source == "project":
        agent.skip_context_files = False
        (tmp_path / "AGENTS.md").write_text(block, encoding="utf-8")
    elif source in ("memory", "user"):
        agent._memory_enabled = source == "memory"
        agent._user_profile_enabled = source == "user"
        agent._memory_store = SimpleNamespace(format_for_system_prompt=lambda kind: block)
    elif source == "external-memory":
        # No injection toolset gate is configured on this minimal agent.
        agent._memory_manager = SimpleNamespace(build_system_prompt=lambda: block)
    elif source == "plugin":
        agent._plugin_system_prompt_sections_snapshot = (
            RenderedPluginSystemPromptSection(
                id="rule-test", content=block, position="after_memory", plugin="test"),
        )
    elif source == "autoload":
        agent.skip_context_files = False
        agent.valid_tool_names.add("skill_view")
        agent._auto_load_skills_resolved = True
        agent._auto_load_skills_result = (block, [], [])
    elif source == "skills":
        # Supply the index at its I/O boundary, not a mocked assembled prompt.
        monkeypatch.setattr(system_prompt, "_skills_prompt", lambda _agent: block)
    elif source == "platform":
        agent.platform = "tui"
        agent._platform_hint_overrides = {"tui": {"replace": block}}
    elif source == "environment":
        monkeypatch.setattr(system_prompt._pb, "build_environment_hints", lambda: block)

    prompt = build_system_prompt(agent, system_message=caller)

    assert block in prompt
    assert prompt.count(RULE) == 1
    assert KANBAN_GUIDANCE.removesuffix("\n\n" + RULE) in prompt
    assert (KANBAN_GUIDANCE in prompt) is not exact
    assert agent._kanban_worker_guidance == KANBAN_GUIDANCE
    assert KANBAN_GUIDANCE.endswith("\n\n" + RULE)


def test_profile_a_b_a_keeps_identity_guidance_and_cache_isolated(tmp_path, monkeypatch):
    from agent.secret_scope import set_multiplex_active
    from agent.system_prompt import reconstruct_static_prefix
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "ambient"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    homes = {name: tmp_path / ".hermes" / "profiles" / name for name in ("A", "B")}
    souls = {"A": f"Profile A before.\n\n{RULE}\n\nProfile A after.",
             "B": "Profile B ordinary SOUL."}
    for name, home in homes.items():
        home.mkdir(parents=True)
        (home / "SOUL.md").write_text(souls[name], encoding="utf-8")
    agents = {name: _agent(home) for name, home in homes.items()}
    prompts = []
    set_multiplex_active(True)
    try:
        for name in ("A", "B", "A"):
            token = set_hermes_home_override(homes[name])
            try:
                agent = agents[name]
                prompt = build_system_prompt(agent)
                assert prompt.count(RULE) == 1
                assert souls[name] in prompt
                assert souls["B" if name == "A" else "A"] not in prompt
                assert (KANBAN_GUIDANCE in prompt) is (name == "B")
                assert prompt.startswith(agent._cached_system_prompt_static)
                agent._cached_system_prompt = prompt
                agent._cached_system_prompt_static = None
                agent._use_prompt_caching = True
                reconstruct_static_prefix(agent)
                assert agent._cached_system_prompt_static
                assert prompt.startswith(agent._cached_system_prompt_static)
                assert agent._kanban_worker_guidance == KANBAN_GUIDANCE
                assert (homes[name] / "SOUL.md").read_text(encoding="utf-8") == souls[name]
                prompts.append(prompt)
            finally:
                reset_hermes_home_override(token)
    finally:
        set_multiplex_active(False)
    assert prompts[0] == prompts[2]
    assert prompts[0] != prompts[1]
