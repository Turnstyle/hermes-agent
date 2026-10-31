"""Tests for the specifier module + `hermes kanban specify` CLI surface.

The auxiliary LLM client is mocked — these tests don't hit any network or
real provider. They exercise the prompt plumbing, response parsing, DB
writes, and CLI flag surface.
"""

from __future__ import annotations

import argparse
import json as jsonlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_specify as spec


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_aux_response(content: str):
    """Build a minimal object shaped like an OpenAI chat.completions result.

    The specifier only reads ``resp.choices[0].message.content``, so we
    avoid importing the openai SDK and build the tree with MagicMock.
    """
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _mock_client_returning(content: str):
    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=_fake_aux_response(content))
    return client


def _patch_aux_client(content: str, *, model: str = "test-model"):
    """Patch call_llm at its source module — specify_task now routes through
    it (#35566) instead of building a raw client. Returns (patcher, mock) so
    callers can still assert on the call.
    """
    mock_fn = MagicMock(return_value=_fake_aux_response(content))
    return patch("agent.auxiliary_client.call_llm", mock_fn), mock_fn


# ---------------------------------------------------------------------------
# JSON extraction helpers & compatibility
# ---------------------------------------------------------------------------

def test_extract_json_blob_handles_fenced_and_structured_sections():
    structured_body = (
        "**Purpose**\n"
        "Ensure receipt drain verifies existing documents before marking 409 as sent.\n\n"
        "**Work**\n"
        "- Fetch the existing document on 409 and compare fields.\n"
        "- Add negative and positive unit tests.\n\n"
        "**Owner**\n"
        "tb-decider\n\n"
        "**Done when** — Acceptance criteria:\n"
        "- Negative 409 mismatch fails the run.\n"
        "- Exact 409 match counts as sent.\n\n"
        "**Blocker**\n"
        "unknown\n\n"
        "**Proof**\n"
        "scripts/run_tests.sh tests/hermes_cli/test_kanban_specify.py\n\n"
        "**Supporting details**\n"
        "- File: receipt_outbox.py:266-271\n"
        "- Follow-up from: t_516b84b1 (SHA dc778ca4)"
    )
    fenced = "```json\n" + jsonlib.dumps({"title": "Verify 409 GET match", "body": structured_body}) + "\n```"
    parsed = spec._extract_json_blob(fenced)
    assert parsed is not None
    title, body = spec._title_body(parsed)
    assert title == "Verify 409 GET match"
    assert body == structured_body


# ---------------------------------------------------------------------------
# specify_task (module-level entry point)
# ---------------------------------------------------------------------------

def test_specify_task_happy_path(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rough", triage=True)

    content = jsonlib.dumps({
        "title": "Refined rough",
        "body": "**Goal**\nA concrete goal.",
    })
    p, _ = _patch_aux_client(content)
    with p:
        outcome = spec.specify_task(tid, author="ace")

    assert outcome.ok is True
    assert outcome.task_id == tid
    assert outcome.new_title == "Refined rough"

    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
    # Parent-free → recompute_ready promotes to ready.
    assert task.status == "ready"
    assert task.title == "Refined rough"
    assert "**Goal**" in (task.body or "")


def test_specify_task_wires_repaired_prompt_and_known_owner_to_call_llm(kanban_home):
    """specify_task delivers the repaired section contract in the system prompt
    and carries the task's actual assignee + verbatim technical identifiers in
    the user prompt sent to call_llm, without mutating task assignment."""
    repo_root = Path(__file__).resolve().parents[2]
    assert Path(spec.__file__).resolve().is_relative_to(repo_root)

    raw_body = (
        "Fix 409 handling in ~/.hermes/profiles/tb-decider/plugins/jev/receipt_outbox.py:266-271 "
        "from card t_516b84b1 (commit dc778ca4) and check delegation.reasoning_effort."
    )
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Jev receipt drain 409 check",
            body=raw_body,
            assignee="tb-decider",
            triage=True,
        )

    repaired_body = (
        "**Purpose**\n"
        "Verify existing documents on HTTP 409 before marking receipts as sent.\n\n"
        "**Work**\n"
        "- Perform an exact GET when the drain receives a 409 response.\n"
        "- Compare document fields and types before marking sent.\n\n"
        "**Owner**\n"
        "tb-decider\n\n"
        "**Done when** — Acceptance criteria:\n"
        "- A 409 with a mismatched or missing GET fails the run.\n"
        "- A 409 with an exact GET match counts as sent.\n\n"
        "**Blocker**\n"
        "unknown\n\n"
        "**Proof**\n"
        "unknown\n\n"
        "**Supporting details**\n"
        "- Path: ~/.hermes/profiles/tb-decider/plugins/jev/receipt_outbox.py:266-271\n"
        "- Card: t_516b84b1\n"
        "- Commit: dc778ca4\n"
        "- Config key: delegation.reasoning_effort"
    )
    content = jsonlib.dumps({
        "title": "Verify GET match on 409 in Jev receipt drain",
        "body": repaired_body,
    })
    p, mock_llm = _patch_aux_client(content)
    with p:
        outcome = spec.specify_task(tid, author="specifier-test")

    assert outcome.ok is True
    assert mock_llm.call_count == 1
    call_kwargs = mock_llm.call_args.kwargs
    assert call_kwargs["task"] == "triage_specifier"
    messages = call_kwargs["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    sys_msg = messages[0]["content"]
    user_msg = messages[1]["content"]

    # Verify required headings reach call_llm in exact order.
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

    # Verify actual owner and exact technical identifiers reach call_llm in user prompt.
    assert f"Task id: {tid}" in user_msg
    assert "Current owner (assignee): tb-decider" in user_msg
    for ident in (
        "~/.hermes/profiles/tb-decider/plugins/jev/receipt_outbox.py:266-271",
        "t_516b84b1",
        "dc778ca4",
        "delegation.reasoning_effort",
    ):
        assert ident in user_msg

    # Verify DB state preserves assignee and stores the structured body.
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status == "ready"
    assert task.assignee == "tb-decider"
    assert task.body == repaired_body

    # Verify downstream acceptance-criteria extractor compatibility (coordinator.py pattern).
    import re
    m = re.search(r"acceptance(?:\s+criteria)?[:\s]+(.*?)(?=\n\n|\Z)", task.body, re.IGNORECASE | re.DOTALL)
    assert m is not None
    extracted = re.sub(r"\s+", " ", m.group(1)).strip().lower()
    assert extracted == (
        "- a 409 with a mismatched or missing get fails the run. "
        "- a 409 with an exact get match counts as sent."
    )


def test_specify_task_passes_explicit_unknown_when_owner_unassigned(kanban_home):
    """When a triage card has no assignee, specify_task passes explicit
    'Current owner (assignee): unknown' in the user prompt and leaves
    task.assignee as None."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="unassigned rough idea", body="check status", triage=True)

    content = jsonlib.dumps({
        "title": "Check service status",
        "body": (
            "**Purpose**\nCheck service status.\n\n"
            "**Work**\n- Run status check.\n\n"
            "**Owner**\nunknown\n\n"
            "**Done when** — Acceptance criteria:\n- Status is recorded.\n\n"
            "**Blocker**\nunknown\n\n"
            "**Proof**\nunknown\n\n"
            "**Supporting details**\nNone."
        ),
    })
    p, mock_llm = _patch_aux_client(content)
    with p:
        outcome = spec.specify_task(tid, author="ace")

    assert outcome.ok is True
    user_msg = mock_llm.call_args.kwargs["messages"][1]["content"]
    assert "Current owner (assignee): unknown" in user_msg

    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.assignee is None
    assert "**Owner**\nunknown" in (task.body or "")


def test_specify_task_non_json_fallback_still_promotes(kanban_home):
    """Lenient parse contract: a plain-text reply without JSON still promotes
    the task using the raw text as the body so a task is never stranded."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="keep original title", triage=True)

    plain_reply = "**Purpose**\nPlain reply without JSON wrapper."
    p, _ = _patch_aux_client(plain_reply)
    with p:
        outcome = spec.specify_task(tid, author="ace")

    assert outcome.ok is True
    assert outcome.new_title is None
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status == "ready"
    assert task.title == "keep original title"
    assert task.body == plain_reply


# ---------------------------------------------------------------------------
# CLI wiring — argparse + _cmd_specify
# ---------------------------------------------------------------------------

def _run_cli(*argv: str) -> int:
    """Invoke the `hermes kanban …` argparse surface directly."""
    root = argparse.ArgumentParser()
    subp = root.add_subparsers(dest="cmd")
    kanban_cli.build_parser(subp)
    ns = root.parse_args(["kanban", *argv])
    return kanban_cli.kanban_command(ns)


def test_cli_specify_single_task_delivers_prompt_and_owner(kanban_home, capsys):
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn,
            title="update config on tb-core-cos",
            body="Set delegation.model=gpt-6.1-sol in /Users/turner/.hermes/profiles/tb-core-cos/config.yaml",
            assignee="tb-core-cos",
            triage=True,
        )

    content = jsonlib.dumps({"title": "Configure tb-core-cos delegation model", "body": "**Purpose**\nConfigure model."})
    p, mock_llm = _patch_aux_client(content)
    with p:
        rc = _run_cli("specify", tid, "--json")
    assert rc == 0
    payload = jsonlib.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True
    assert payload["task_id"] == tid

    user_msg = mock_llm.call_args.kwargs["messages"][1]["content"]
    assert "Current owner (assignee): tb-core-cos" in user_msg
    assert "/Users/turner/.hermes/profiles/tb-core-cos/config.yaml" in user_msg
    assert "delegation.model=gpt-6.1-sol" in user_msg


def test_cli_specify_tenant_filter(kanban_home, capsys):
    with kbc.connect() as conn:
        outside = kb.create_task(conn, title="outside", triage=True)
        inside = kb.create_task(
            conn, title="inside", triage=True, tenant="proj-a",
        )

    content = jsonlib.dumps({"title": "spec", "body": "body"})
    p, _ = _patch_aux_client(content)
    with p:
        rc = _run_cli("specify", "--all", "--tenant", "proj-a", "--json")
    assert rc == 0
    lines = [
        jsonlib.loads(l)
        for l in capsys.readouterr().out.strip().splitlines()
        if l
    ]
    ids = {row["task_id"] for row in lines}
    assert ids == {inside}

    # The outside task stays in triage.
    with kbc.connect() as conn:
        assert kb.get_task(conn, outside).status == "triage"
        # The inside task was promoted.
        assert kb.get_task(conn, inside).status in {"todo", "ready"}


