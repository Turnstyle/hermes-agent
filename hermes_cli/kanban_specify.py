"""Kanban triage specifier — flesh out a one-liner into a real spec.

``hermes kanban specify [task_id | --all]`` asks the auxiliary LLM for a
tightened title + concrete body for a Triage task, then flips it
``triage -> todo`` via ``kanban_db.specify_triage_task``.

Mirrors ``hermes_cli/goals.py``: same aux-client pattern, same "empty config
=> skip, don't crash" tolerance. One shot, no retry loop. JSON mode is not
requested (works on providers without it); the parse is lenient and falls
back to "whole reply is the body" so a malformed reply never strands a task.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

from utils import env_int

HERMES_KANBAN_SPECIFY_MAX_TOKENS = max(1500, env_int("HERMES_KANBAN_SPECIFY_MAX_TOKENS", 6000))

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are the Kanban triage specifier for the Hermes Agent board.
A user dropped a rough idea into the Triage column. Your job is to turn it
into a concrete, readable, actionable task spec that an autonomous worker or
human operator can pick up and execute without further clarification.

Output a single JSON object with exactly two keys:

  {
    "title": "<tightened task title, <= 80 chars, imperative voice>",
    "body":  "<multi-line spec, see structure below>"
  }

The body MUST include these sections in this order, separated by a blank line
(\\n\\n), using these exact bold markdown headings:

  **Purpose** — 1-2 plain-language sentences on why this task exists and the
      user- or system-facing outcome.
  **Work** — 2-5 concise plain-language bullets on what the worker will do.
  **Owner** — the actual assigned owner from Current owner (assignee).
      That assignment takes precedence over names in the body. If unassigned
      or unknown, write "unknown"; do not turn a mentioned person or profile
      into an assignment.
  **Done when** — Acceptance criteria:
      checklist of concrete, verifiable conditions that prove completion
      (keep "Acceptance criteria:" on the heading line for downstream
      extractors).
  **Blocker** — any current blocker, hold, or prerequisite preventing work;
      if none is stated or known, write "unknown" (never invent a blocker).
  **Proof** — the concrete verification command, test run, or readback
      receipt required to prove the work succeeded; if not known from the
      input, write "unknown" (never fabricate proof).
  **Supporting details** — exact technical identifiers preserved verbatim
      from the input (file paths, function/symbol names, task/card IDs,
      profile names, config keys, CLI commands/flags, commit SHAs, error
      codes) plus any explicit out-of-scope boundaries (omit out-of-scope
      notes if none are obvious; never invent scope creep).

Rules:
  - Write the card face (Purpose, Work, Owner, Done when, Blocker, Proof) in
    concise plain language with proper spaces (a blank line between every
    section, and a space after every bullet marker, colon, and sentence
    period). Keep dense technical identifiers in Supporting details instead
    of cramming them into the prose sections.
  - Preserve every exact technical identifier from the input verbatim in
    Supporting details — never alter, re-case, or drop paths, IDs, profile
    names, config keys, SHAs, or commands.
  - Owner must come from the actual assignment. Unknown owner, blocker, or
    proof must be stated explicitly as "unknown", never invented.
  - Keep the tightened title close in meaning to the original idea — do
    NOT invent a different project.
  - If the original idea is already detailed, preserve its substance and
    exact technical identifiers while reformatting into the sections above.
  - Never add invented requirements the user didn't hint at.
  - No preamble, no closing remarks, no code fences around the JSON.
  - Output only the JSON object and nothing else.
"""


_USER_TEMPLATE = """Task id: {task_id}
Current title: {title}
Current owner (assignee): {owner}
Current body:
{body}
"""


@dataclass
class SpecifyOutcome:
    """Result of specifying a single triage task."""

    task_id: str
    ok: bool
    reason: str = ""
    new_title: Optional[str] = None
    kept_spec: bool = False
    sha256: Optional[str] = None
    hash_encoding: Optional[str] = None
    status_after: Optional[str] = None
    committed: bool = False
    recompute_error: Optional[str] = None


def _truncate(text: str, limit: int) -> str:
    # Plain length clamp for LLM prompt fields; these never reach a terminal, so no escape stripping here.
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _extract_json_blob(raw: str, fence_re: re.Pattern = _FENCE_RE) -> Optional[dict]:
    """Lenient JSON object extraction: strip code fences, take the first ``{``
    to the last ``}``. None if nothing parses to a dict."""
    if not raw:
        return None
    stripped = fence_re.sub("", raw.strip())
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    try:
        val = json.loads(stripped[first : last + 1])
    except (ValueError, json.JSONDecodeError):
        return None
    return val if isinstance(val, dict) else None


def _nonblank(v) -> Optional[str]:
    return v if isinstance(v, str) and v.strip() else None


def _title_body(parsed: dict) -> tuple[Optional[str], Optional[str]]:
    """``(title, body)`` from an LLM reply: title stripped, body verbatim,
    either None when missing/blank."""
    title = _nonblank(parsed.get("title"))
    return (title.strip() if title else None), _nonblank(parsed.get("body"))


def _profile_author(default: str = "specifier") -> str:
    """Same identity contract as ``hermes_cli.kanban._profile_author``; ``$USER`` as the last
    resort for a human running the CLI outside any profile."""
    from hermes_cli.profiles import current_profile_name
    return current_profile_name() or os.environ.get("USER") or default


def _load_triage_task(task_id: str) -> tuple[Optional[kb.Task], str]:
    """``(task, "")`` when the task exists and is in triage, else ``(None, reason)``."""
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    if task is None:
        return None, "unknown task id"
    if task.status != "triage":
        return None, f"task is not in triage (status={task.status!r})"
    return task, ""


def _task_prompt_fields(task: kb.Task) -> dict[str, str]:
    """Bounded ``task_id``/``title``/``owner``/``body`` for the user prompt templates."""
    owner = _truncate((task.assignee or "").strip(), 200)
    return {
        "task_id": task.id,
        "title": _truncate(task.title or "", 400),
        "owner": owner or "unknown",
        "body": _truncate(task.body or "(no body)", 4000),
    }


def _call_aux(verb: str, task_id: str, *, aux_task: str, system: str, user: str,
              max_tokens: int, timeout: int, log: logging.Logger = logger) -> tuple[Optional[str], str]:
    """One auxiliary LLM call; ``(reply_text, "")`` or ``(None, reason)``.

    ``call_llm`` applies all ``auxiliary.<aux_task>.*`` config (provider/model/
    base_url, extra_body, reasoning_effort, retries). Imported lazily so a
    missing aux client degrades to a skip instead of an import-time crash.
    """
    try:
        from agent.auxiliary_client import call_llm
    except Exception as exc:  # pragma: no cover — import smoke test
        log.debug("%s: auxiliary client import failed: %s", verb, exc)
        return None, "auxiliary client unavailable"
    # Specify/decompose run outside any agent turn (CLI, dashboard route, gateway watcher), so no
    # conversation affinity scope is bound and the relay-affinity headers (x-opencode-session, the
    # OpenRouter/Portal sticky key) are omitted — the OpenCode Go relay rejects that with 400
    # MissingSessionID (#112043). Declare a per-task scope, but only when none is already bound so an
    # in-turn caller keeps its conversation's key.
    from agent.portal_tags import get_affinity_scope, reset_affinity_scope, set_affinity_scope
    affinity_token = None if get_affinity_scope() else set_affinity_scope(f"kanban:{task_id}")
    try:
        # Route through call_llm so auxiliary.triage_specifier.* config (provider/model/base_url,
        # extra_body, reasoning_effort, retries) all apply — the direct-create path dropped extra_body
        # (#35566).
        resp = call_llm(
            task=aux_task,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.3,
            max_tokens=max_tokens,
            timeout=timeout,
        )
    except Exception as exc:
        suffix = " — skipping" if verb == "specify" else ""
        log.info("%s: API call failed for %s (%s)%s", verb, task_id, exc, suffix)
        return None, f"LLM error: {type(exc).__name__}"
    finally:
        if affinity_token is not None:
            reset_affinity_scope(affinity_token)
    try:
        return resp.choices[0].message.content or "", ""
    except Exception:
        return "", ""


def specify_task(
    task_id: str,
    *,
    author: Optional[str] = None,
    timeout: Optional[int] = None,
) -> SpecifyOutcome:
    """Specify one triage task and promote it to ``todo``. Expected failures
    (not in triage, no aux client, API error, malformed reply) surface as
    ``ok=False`` so an ``--all`` sweep continues."""
    task, reason = _load_triage_task(task_id)
    if task is None:
        return SpecifyOutcome(task_id, False, reason)

    raw, reason = _call_aux(
        "specify", task_id, aux_task="triage_specifier", system=_SYSTEM_PROMPT,
        user=_USER_TEMPLATE.format(**_task_prompt_fields(task)),
        max_tokens=HERMES_KANBAN_SPECIFY_MAX_TOKENS, timeout=timeout or 120,
    )
    if raw is None:
        return SpecifyOutcome(task_id, False, reason)
    raw = raw.strip()

    parsed = _extract_json_blob(raw)
    if parsed is None:
        # Whole reply becomes the body; the user can edit afterward.
        if not raw:
            return SpecifyOutcome(task_id, False, "LLM returned an empty response")
        new_title, new_body = None, raw
    else:
        new_title, new_body = _title_body(parsed)
        if new_body is None and new_title is None:
            return SpecifyOutcome(task_id, False, "LLM response missing title and body")

    with kbc.connect_closing() as conn:
        ok = kb.specify_triage_task(
            conn,
            task_id,
            title=new_title,
            body=new_body,
            author=author or _profile_author(),
        )
    if not ok:
        # Race: promoted/archived between our read and the write.
        return SpecifyOutcome(task_id, False, "task moved out of triage before promotion")
    return SpecifyOutcome(task_id, True, "specified", new_title=new_title)


def keep_spec_task(
    task_id: str,
    *,
    expect_sha256: str,
    author: str,
) -> SpecifyOutcome:
    """Specify one triage task by releasing its exact reviewed spec verbatim.

    Why this path exists and guarantees:
    - Moving a card out of triage must not rewrite instructions a human already
      reviewed: model-backed specify regenerates title/body, silently replacing
      human edits; this path bypasses the LLM and releases the reviewed text.
    - Binds release to exact reviewed bytes via expect_sha256; refuses post-review edits.
    - Preserves exact bytes: whitespace, trailing newlines, Unicode (ensure_ascii=False),
      and NULL vs "" are never normalized.
    - Hash and triage status are verified in the same write transaction as the
      status change, so a concurrent edit cannot slip between check and write.
    - Active holds are refused, never cleared.
    """
    if not author or not author.strip():
        return SpecifyOutcome(
            task_id=task_id,
            ok=False,
            reason="author cannot be blank",
            kept_spec=False,
            committed=False,
        )

    clean_author = author.strip()
    with kbc.connect_closing() as conn:
        ok, reason, computed_sha256, status_after, committed, recompute_err = kb.keep_spec_triage_task(
            conn,
            task_id,
            expected_sha256=expect_sha256,
            author=clean_author,
        )

    return SpecifyOutcome(
        task_id=task_id,
        ok=ok,
        reason=reason,
        kept_spec=ok,
        sha256=computed_sha256,
        hash_encoding=kb.HASH_ENCODING,
        status_after=status_after,
        committed=committed,
        recompute_error=recompute_err,
    )


def list_triage_ids(*, tenant: Optional[str] = None) -> list[str]:
    """Task ids in the triage column; ``tenant`` narrows the sweep."""
    with kbc.connect_closing() as conn:
        tasks = kb.list_tasks(conn, status="triage", tenant=tenant, include_archived=False)
    return [t.id for t in tasks]
