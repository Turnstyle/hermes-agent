"""Tests for the decomposer module + `hermes kanban decompose` CLI surface.

The auxiliary LLM client is mocked — no network calls. Tests exercise the
prompt plumbing, response parsing, DB writes (via the real DB helper),
and the assignee-fallback logic.
"""

from __future__ import annotations

import json as jsonlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose as decomp


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_aux_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _mock_client_returning(content: str):
    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=_fake_aux_response(content))
    return client


def _patch_aux_client(content: str, *, model: str = "test-model"):
    # decompose_task now routes through call_llm (see #35566) — mock it at
    # the source module so task config, extra_body, and retries stay out of
    # unit-test scope.
    return patch(
        "agent.auxiliary_client.call_llm",
        return_value=_fake_aux_response(content),
    )


def _patch_extra_body():
    # No-op shim retained for call-site compatibility: extra_body plumbing
    # now lives inside call_llm, which _patch_aux_client already mocks.
    return patch("agent.auxiliary_client.get_auxiliary_extra_body", return_value={})


def _patch_list_profiles(names: list[str]):
    """Pretend the named profiles exist. The decomposer uses
    profiles_mod.list_profiles() to build the roster + valid-set, and
    profiles_mod.profile_exists() to resolve orchestrator/default."""
    from types import SimpleNamespace
    fake_profiles = [
        SimpleNamespace(
            name=n, is_default=(i == 0), description=f"desc for {n}",
            description_auto=False, model="m", provider="p", skill_count=1,
        )
        for i, n in enumerate(names)
    ]
    return [
        patch("hermes_cli.profiles.list_profiles", return_value=fake_profiles),
        patch("hermes_cli.profiles.profile_exists", side_effect=lambda x: x in names),
        patch("hermes_cli.profiles.get_active_profile_name", return_value=names[0] if names else "default"),
    ]


def test_foreign_fleet_mirror_is_not_listed_or_decomposed(kanban_home, monkeypatch):
    monkeypatch.setenv("FLEET_KANBAN_ACTOR_NODE", "node-a")
    with kbc.connect() as conn:
        conn.execute("""CREATE TABLE fleet_kanban_issue_map (
            local_task_id TEXT PRIMARY KEY, issue_id TEXT, raw_title TEXT,
            canonical_body TEXT, source_node TEXT, current_node TEXT,
            source_profile TEXT, deferred_snapshot TEXT, canonical_status TEXT
        )""")
        conn.execute("""CREATE TRIGGER fleet_kanban_task_insert AFTER INSERT ON tasks BEGIN
            INSERT INTO fleet_kanban_issue_map(
                local_task_id, issue_id, raw_title, canonical_body,
                source_node, current_node, source_profile
            ) VALUES(NEW.id, 'fk_' || lower(hex(randomblob(4))), NEW.title,
                     NEW.body, 'node-a', NULL, 'p');
        END""")
        foreign_id = kb.create_task(conn, title="foreign", triage=True)
        local_id = kb.create_task(conn, title="local", triage=True)
        conn.execute(
            "UPDATE fleet_kanban_issue_map SET current_node = 'node-b' WHERE local_task_id = ?",
            (foreign_id,),
        )
        conn.commit()

    listed = decomp.list_triage_ids()
    assert foreign_id not in listed
    assert local_id in listed

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "tasks": [{"title": "local child", "body": "work", "assignee": "orchestrator", "parents": []}],
    })
    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload) as aux:
            foreign = decomp.decompose_task(foreign_id, author="auto-decomposer")
            assert foreign.ok is False
            assert "foreign Fleet mirror" in foreign.reason
            aux.assert_not_called()
            local = decomp.decompose_task(local_id, author="auto-decomposer")
            assert local.ok is True, local.reason
            aux.assert_called_once()
    finally:
        for p in patches:
            p.stop()

    with kbc.connect() as conn:
        assert kb.get_task(conn, foreign_id).status == "triage"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_links WHERE parent_id = ?", (foreign_id,),
        ).fetchone()[0] == 0
        assert len(local.child_ids) == 1
        assert kb.get_task(conn, local.child_ids[0]) is not None


def test_decompose_with_fanout_creates_children(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ship a feature", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": "look it up", "assignee": "researcher", "parents": []},
            {"title": "build", "body": "code it", "assignee": "engineer", "parents": [0]},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "researcher", "engineer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    assert outcome.child_ids and len(outcome.child_ids) == 2

    with kbc.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert root.status == "todo"
    assert c0.status == "ready"
    assert c1.status == "todo"
    assert c0.assignee == "researcher"
    assert c1.assignee == "engineer"


def test_decompose_fanout_children_inherit_root_assignee_when_unrouted(kanban_home):
    """Unrouted children fall back to the ROOT task's assignee, not
    the decomposer's active profile (#114294). The active profile here is ``private``
    (an incognito profile with no credentials), so the old fallback spawned
    workers that deadlocked on capability blockers."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ship it", assignee="zdr", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": "look it up", "assignee": "made_up", "parents": []},
            {"title": "build", "body": "code it", "assignee": None, "parents": [0]},
        ],
    })

    # get_active_profile_name() is mocked to names[0] = "private" — the
    # global default chain would resolve there without kanban.default_assignee.
    patches = _patch_list_profiles(["private", "zdr"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.config.load_config_readonly",
            return_value={},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kbc.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert c0.assignee == "zdr"
    assert c1.assignee == "zdr"
    # Same class for the root: no ``orchestrator_profile`` must not hand the
    # orchestration card to the dispatcher's own (here: incognito) profile.
    assert root.assignee == "zdr"


def test_decompose_explicit_default_assignee_wins_over_root_assignee(kanban_home):
    """An explicitly configured ``kanban.default_assignee`` stays
    authoritative for unroutable children; the root task's assignee only
    fills in when no explicit default is set (explicit config → card
    assignee → active profile)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ship it", assignee="engineer", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": "look it up", "assignee": "made_up", "parents": []},
            {"title": "build", "body": "code it", "assignee": None, "parents": [0]},
        ],
    })

    patches = _patch_list_profiles(["engineer", "docs", "private"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.config.load_config_readonly",
            return_value={"kanban": {"default_assignee": "docs"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kbc.connect() as conn:
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert c0.assignee == "docs"
    assert c1.assignee == "docs"


def test_decompose_fanout_false_invalid_llm_assignee_uses_default(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="route me safely", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Route to fallback.",
        "assignee": "made_up",
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.config.load_config_readonly",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "fallback"


def test_load_routing_falls_back_to_defaults_when_config_unreadable(kanban_home, monkeypatch):
    """decompose_task promises ok=False on expected failures; a config read that raises (missing
    profile home, HomeInitializationError) must not escape _load_routing as an exception."""
    from hermes_cli import config as config_mod

    def _boom():
        raise FileNotFoundError("profile home is gone")

    monkeypatch.setattr(config_mod, "load_config_readonly", _boom)
    routing = decomp._load_routing()
    assert routing.default_assignee == "default" and routing.auto_promote is True


def test_decompose_returns_false_when_task_not_triage(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x")  # ready, not triage

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()
    assert outcome.ok is False


def test_decompose_wires_repaired_prompt_and_known_owner_to_call_llm(kanban_home):
    """decompose_task delivers the repaired section contract in the system prompt
    and carries the task's actual assignee + verbatim technical identifiers +
    roster in the user prompt sent to call_llm."""
    repo_root = Path(__file__).resolve().parents[2]
    assert Path(decomp.__file__).resolve().is_relative_to(repo_root)

    raw_body = (
        "Audit 409 handling in ~/.hermes/profiles/tb-decider/plugins/jev/receipt_outbox.py:266-271 "
        "from t_516b84b1 (commit dc778ca4) and verify delegation.reasoning_effort."
    )
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Audit and test 409 receipt drain",
            body=raw_body,
            assignee="tb-cndr",
            triage=True,
        )

    child_body_0 = (
        "**Purpose**\n"
        "Confirm the 409 receipt drain gap with an offline reproduction test.\n\n"
        "**Work**\n"
        "- Inspect the 409 branch in the receipt drain.\n"
        "- Add an offline reproduction test.\n\n"
        "**Owner**\n"
        "researcher\n\n"
        "**Done when** — Acceptance criteria:\n"
        "- Offline reproduction test fails on unpatched 409 mismatch.\n\n"
        "**Blocker**\n"
        "unknown\n\n"
        "**Proof**\n"
        "unknown\n\n"
        "**Supporting details**\n"
        "- Path: ~/.hermes/profiles/tb-decider/plugins/jev/receipt_outbox.py:266-271\n"
        "- Card: t_516b84b1 (SHA dc778ca4)"
    )
    child_body_1 = (
        "**Purpose**\n"
        "Require an exact GET match before counting a 409 receipt response as sent.\n\n"
        "**Work**\n"
        "- Fetch the existing document on 409 and compare fields.\n"
        "- Run negative and positive unit tests.\n\n"
        "**Owner**\n"
        "engineer\n\n"
        "**Done when** — Acceptance criteria:\n"
        "- Mismatched or missing GET on 409 fails the run.\n"
        "- Exact GET match on 409 counts as sent.\n\n"
        "**Blocker**\n"
        "Wait for child task 0 reproduction test.\n\n"
        "**Proof**\n"
        "unknown\n\n"
        "**Supporting details**\n"
        "- Path: ~/.hermes/profiles/tb-decider/plugins/jev/receipt_outbox.py:266-271\n"
        "- Config key: delegation.reasoning_effort"
    )
    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "Split reproduction test from drain patch.",
        "tasks": [
            {"title": "Reproduce 409 drain gap", "body": child_body_0, "assignee": "researcher", "parents": []},
            {"title": "Patch 409 GET verification", "body": child_body_1, "assignee": "engineer", "parents": [0]},
        ],
    })

    mock_llm = MagicMock(return_value=_fake_aux_response(llm_payload))
    patches = _patch_list_profiles(["tb-cndr", "researcher", "engineer"])
    for p in patches:
        p.start()
    try:
        with patch("agent.auxiliary_client.call_llm", mock_llm), _patch_extra_body(), patch(
            "hermes_cli.config.load_config_readonly",
            return_value={},
        ):
            outcome = decomp.decompose_task(tid, author="decomposer-test")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    assert mock_llm.call_count == 1

    call_kwargs = mock_llm.call_args.kwargs
    assert call_kwargs["task"] == "kanban_decomposer"
    messages = call_kwargs["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    sys_msg = messages[0]["content"]
    user_msg = messages[1]["content"]

    required_headings = [
        "**Purpose**",
        "**Work**",
        "**Owner**",
        "**Done when** — Acceptance criteria:",
        "**Blocker**",
        "**Proof**",
        "**Supporting details**",
    ]
    positions = [sys_msg.index(h) for h in required_headings]
    assert positions == sorted(positions)
    assert "plain language" in sys_msg
    assert '"unknown"' in sys_msg
    assert "Do not inherit the root's" in sys_msg
    assert 'For "fanout": false, keep Current owner' in sys_msg
    assert 'or default_assignee when "assignee" is null' in sys_msg

    assert f"Task id: {tid}" in user_msg
    assert "Current owner (assignee): tb-cndr" in user_msg
    assert "Default assignee (used when no profile fits a task): tb-cndr" in user_msg
    for ident in (
        "~/.hermes/profiles/tb-decider/plugins/jev/receipt_outbox.py:266-271",
        "t_516b84b1",
        "dc778ca4",
        "delegation.reasoning_effort",
    ):
        assert ident in user_msg

    with kbc.connect() as conn:
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert c0.body == child_body_0
    assert c1.body == child_body_1

    import re
    m = re.search(r"acceptance(?:\s+criteria)?[:\s]+(.*?)(?=\n\n|\Z)", c1.body, re.IGNORECASE | re.DOTALL)
    assert m is not None
    extracted = re.sub(r"\s+", " ", m.group(1)).strip().lower()
    assert extracted == (
        "- mismatched or missing get on 409 fails the run. "
        "- exact get match on 409 counts as sent."
    )


def test_decompose_passes_explicit_unknown_when_owner_unassigned_via_cli(kanban_home, capsys):
    """When an unassigned triage card is decomposed through the CLI surface,
    'Current owner (assignee): unknown' is passed explicitly in the user prompt."""
    import argparse
    from hermes_cli import kanban as kanban_cli

    with kbc.connect() as conn:
        tid = kb.create_task(
            conn,
            title="unassigned single task",
            body="Update /Users/turner/.hermes/profiles/tb-core-cos/config.yaml",
            triage=True,
        )

    fenced_payload = "```json\n" + jsonlib.dumps({
        "fanout": False,
        "rationale": "Single config change.",
        "title": "Update tb-core-cos config",
        "body": (
            "**Purpose**\nUpdate delegation config.\n\n"
            "**Work**\n- Set config values.\n\n"
            "**Owner**\nfallback\n\n"
            "**Done when** — Acceptance criteria:\n- Config values match.\n\n"
            "**Blocker**\nunknown\n\n"
            "**Proof**\nunknown\n\n"
            "**Supporting details**\n- /Users/turner/.hermes/profiles/tb-core-cos/config.yaml"
        ),
        "assignee": None,
    }) + "\n```"

    mock_llm = MagicMock(return_value=_fake_aux_response(fenced_payload))
    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with patch("agent.auxiliary_client.call_llm", mock_llm), _patch_extra_body(), patch(
            "hermes_cli.config.load_config_readonly",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            root = argparse.ArgumentParser()
            subp = root.add_subparsers(dest="cmd")
            kanban_cli.build_parser(subp)
            ns = root.parse_args(["kanban", "decompose", tid, "--json"])
            rc = kanban_cli.kanban_command(ns)
    finally:
        for p in patches:
            p.stop()

    assert rc == 0
    payload = jsonlib.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True
    assert payload["fanout"] is False

    user_msg = mock_llm.call_args.kwargs["messages"][1]["content"]
    assert "Current owner (assignee): unknown" in user_msg
    assert "/Users/turner/.hermes/profiles/tb-core-cos/config.yaml" in user_msg

    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.assignee == "fallback"
    assert "**Owner**\nfallback" in (task.body or "")


def test_auto_decompose_dispatcher_caller_delivers_repaired_prompt(kanban_home):
    """Gateway auto-decompose caller path (_decompose_one -> decompose_task)
    delivers the repaired section prompt and actual task owner to call_llm."""
    from gateway.kanban_watchers_dispatcher import _KanbanDispatcher

    with kbc.connect() as conn:
        tid = kb.create_task(
            conn,
            title="dispatcher auto-decompose task",
            body="Check t_19f38859 on tb-decider",
            assignee="tb-decider",
            triage=True,
        )

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "Single verification unit.",
        "title": "Verify t_19f38859 on tb-decider",
        "body": (
            "**Purpose**\nVerify card t_19f38859 on tb-decider.\n\n"
            "**Work**\n- Run verification check.\n\n"
            "**Owner**\ntb-decider\n\n"
            "**Done when** — Acceptance criteria:\n- Verification succeeds.\n\n"
            "**Blocker**\nunknown\n\n"
            "**Proof**\nunknown\n\n"
            "**Supporting details**\n- Card: t_19f38859\n- Profile: tb-decider"
        ),
        "assignee": "tb-decider",
    })

    mock_llm = MagicMock(return_value=_fake_aux_response(llm_payload))
    patches = _patch_list_profiles(["tb-decider"])
    for p in patches:
        p.start()
    try:
        with patch("agent.auxiliary_client.call_llm", mock_llm), _patch_extra_body(), patch(
            "hermes_cli.config.load_config_readonly",
            return_value={},
        ):
            rc = _KanbanDispatcher._decompose_one(decomp, "default", tid)
    finally:
        for p in patches:
            p.stop()

    assert rc == 1
    sys_msg = mock_llm.call_args.kwargs["messages"][0]["content"]
    user_msg = mock_llm.call_args.kwargs["messages"][1]["content"]
    assert "**Done when** — Acceptance criteria:" in sys_msg
    assert "**Supporting details**" in sys_msg
    assert "Current owner (assignee): tb-decider" in user_msg
    assert "t_19f38859" in user_msg
