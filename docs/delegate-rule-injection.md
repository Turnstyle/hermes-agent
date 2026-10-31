# First stored Claude user message

The first stored user message on an eligible route contains this exact text:

```text
Use 1 to 10 delegate_task sub-agents and don't do the work inside your main model, and pass this same instruction on to anyone you hand work to.
```

`agent/owner_prompt_policy.py` owns the decision. A route qualifies when its provider,
trimmed and lowercased, is `anthropic`, or its model ID contains `claude`, ignoring case.
A route without a usable model consults the active profile's default pin through the
canonical config loader. An explicit non-Claude model does not inherit a Claude pin.

The turn prologue applies the policy after primary runtime restoration and before
binding the persistence override. Earlier CLI and Desktop writers use the same decision.
The database check counts every stored user row, including typed, inactive and archived
rows. Only the staged row adopted by the current turn is excluded from that check.

The rule is appended after two newlines to string content, or as a final text block to
list content. Existing media blocks stay intact. An exact occurrence anywhere in a
string or a `text`/`input_text` block satisfies the rule; quoted text also qualifies.
The function does not add a second occurrence or remove duplicates the caller supplied.
Empty strings become the rule alone. Unsupported content types pass through the live
turn policy without a mutation.

Both API-facing content and any clean persistence override receive the rule. Existing
image persistence still projects images to screenshot markers rather than storing image
bytes. Text in `input_text` blocks is retained in that projection. The normal API-sidecar
composition happens after injection. The system prompt, tool inventory and delegation
limits do not change.

An admission route can differ from the route restored at turn start. The prologue
reconciles a matching staged first row before sending it. This includes a Desktop row
already written at submit time, under the agent's persistence lock. It does not rewrite
generic history. If an early writer included the rule and the eventual route is
non-Claude, the staged rule is retained to preserve row identity and replay.

## Launch paths

The exercised local entries below reach `AIAgent.run_conversation`, then
`agent/turn_context.py:1044 (build_turn_context)`. Remote authority-owned
admission remains outside the offline proof.
No source-specific rule is added to the generic launch wrappers.

| Source | Owning entry | Offline coverage |
| --- | --- | --- |
| Agent facade | `agent/turn_facade.py:22 (TurnFacadeMixin.run_conversation)` | Full turn, real SessionDB, provider HTTP mock; provider, model and non-Claude selection |
| API server and peer/bot DM | `gateway/platforms/api_server.py:4138 (APIServerAdapter._run_agent)` | Legacy in-process entry with real agent; verified-caller text reaches first SQLite row |
| API runs | `gateway/platforms/api_server_runs.py:891 (_run_agent_sync)` | Actual `_run_agent_sync` executor with real agent and SQLite; HTTP admission and authority branch are not newly driven |
| Native Kanban worker | `gateway/session_kanban.py:376 (_run_task_turns)` | Real task prompt preparation and turn; dispatcher claim acquisition is outside the test |
| CLI Kanban worker | `hermes_cli/cli_single_query.py:183 (_run_quiet_single_query)` | Real quiet driver through its exit, with completion linger suppressed |
| Cron | `cron/scheduler.py:1957 (_run_agent_with_watchdog)` | Real watchdog worker and turn; job claiming/scheduling is outside the test |
| Oneshot | `hermes_cli/oneshot.py:503 (_run_agent)` | Real entry and close; runtime resolution and agent construction supplied offline |
| TUI/Desktop | `tui_gateway/prompt_turn.py:740 (_invoke_agent)` and `_absorb_turn_result` | Real submit row, adoption, turn and result absorption; browser/RPC admission is outside the test |
| Desktop early persist | `tui_gateway/session_workdir.py:800 (_write_submit_user_row)` and `_adopt_submit_user_row` | Real SQLite, expansion, stale input, route reconciliation and identity preservation |
| Desktop queued input | `tui_gateway/session_auto_continue.py:292 (_persist_queued_user_row)` | Real SQLite merge; existing queue/drain suites cover replacement and restart behavior |
| CLI early persist | `hermes_cli/cli_chat_turn_mixin.py:202 (_chat_stage_user_message)` | Real stage and flush followed by a full turn without a duplicate row |
| Messaging gateway | `gateway/run_turn_runner.py:944 (TurnRunner._run_conversation_with_approval)` | Real approval-turn method and bot-author turn; adapter receipt/delivery is outside the test |
| Delegated child | `tools/delegate_tool_child_run.py:879 (_ChildRun.await_child)` | Real child runner and full turn; no nested inference or live child launch |
| ACP | `acp_adapter/server.py:751 (_run_agent_turn)` | Real executor body and turn; editor transport is outside the test |

The legacy Desktop resolvers now live in `tui_gateway/agent_factory.py`. `server.py`
registers those functions against its namespace. Their `default or model` fallback is
patched in the owning sibling and tested through `server._resolve_model` and
`server._config_model_target`; there is no duplicate resolver body to patch in the facade.

`tests/agent/test_owner_prompt_policy.py` ports Rosie's 19 original boundary cases and
12 adversarial intents to real imports. It applies the owner's revised first-turn,
substring-idempotence and warning-only decisions. `test_owner_prompt_policy_sessions.py`
adds actual entry-function tests and independent SQLite reads. These tests deny socket
connections and use mocked HTTP transports; they do not establish live provider behavior.

## Source eligibility matrix

The actual-entry SessionDB tests exercise each row below under four routes: provider
`anthropic` with model `opaque`; provider `openrouter` with a Claude model; unset runtime
and stored session model with an Anthropic/Claude profile default; and explicit
`openai-codex/gpt-6.1-sol` despite a Claude profile pin. The first three require the rule; the fourth requires the
original user text unchanged. Every assertion reads the first SQLite user row.

| Entry | Provider | Claude model | Unset + profile default | Non-Claude |
| --- | --- | --- | --- | --- |
| `AIAgent.run_conversation` | Tested | Tested | Tested | Tested |
| API `_run_agent` peer/bot text | Tested | Tested | Tested | Tested |
| API runs `_run_agent_sync` executor | Tested | Tested | Tested | Tested |
| Native Kanban `_run_task_turns` | Tested | Tested | Tested | Tested |
| CLI quiet Kanban worker | Tested | Tested | Tested | Tested |
| Cron watchdog | Tested | Tested | Tested | Tested |
| Oneshot `_run_agent` | Tested | Tested | Tested | Tested |
| Desktop submit + invoke + absorb | Tested | Tested | Tested | Tested |
| CLI stage + flush + full turn | Tested | Tested | Tested | Tested |
| Desktop queue accept + merge | Tested | Tested | Tested | Tested |
| Messaging approval turn | Tested | Tested | Tested | Tested |
| Delegated child runner | Tested | Tested | Tested | Tested |
| ACP executor turn | Tested | Tested | Tested | Tested |

The unset-model cases retain `agent.model=None` and `_primary_runtime=None` through the
actual entry, policy and persistence. The mock HTTP transport accepts that unset model
and returns a valid mock response model; it does not resolve or change the agent route.
SQLite independently confirms the stored session model is NULL before each actual entry.
It stays NULL through full turns; queue acceptance instead persists the resolved Claude
profile model through its existing session-row writer, which is asserted separately.
This proves default-pin
injection decisions under an offline transport seam, not that a real provider accepts a
NULL model request. Oneshot and API agent construction are supplied by fixtures; the
actual turn entry and persistence still execute. Other transport/admission limits below
remain in force.

## Late routes and limits

`build_api_messages` observes the final wire copy after sidecar substitution. A Claude
request whose first user anchor lacks the rule logs a warning once per session and
increments `WIRE_RULE_MISSING_COUNT` per detection. It leaves history and wire content
unchanged. `HERMES_DELEGATE_RULE_STRICT=1` makes that missing-rule condition raise
`RuntimeError`; the default does not block a live turn. This switch is the owner's
explicit diagnostic exception to the normal config-only behavior policy.

Requests without a user anchor, or with unsupported anchor/current-user content, warn
without raising. A compaction summary at or before the anchor makes absence inconclusive.
The observer does not claim to recover a first user row that is no longer on the wire.

Existing sessions are not backfilled. Later turns, resumed history, fallback and voice
route changes do not rewrite historical rows. A stale API sidecar is observed, not silently
repaired. Opaque aliases behind another provider can under-match; unrelated IDs containing
`claude` can over-match. Credential-selected routes and concurrent config changes are not
proven equivalent to an admission-time route decision.

MoA virtual sessions and their inner proposer/aggregator calls remain untouched. The inner
calls do not create stored user sessions and do not pass through this policy. An offline
MoA test verifies both Claude-named slots still run without this rule.

This is a text-presence policy, not proof that a model executes delegates, honors a count,
or propagates the rule. It is not universal enforcement across every Claude inference.
Actual fallback/voice transitions, streamed native-provider lifecycle, the authority-owned
API admission branch and live fleet serving are not established by these tests.

## Foreground snapshot fixture

The foreground notification snapshot fixture retains a real registry reference to its
profile's `state.db` across both timed turns, matching the lifetime held by a built
profile agent. It releases that reference in `finally`, including assertion failures.
The stub agent still has no `_session_db` attribute. Initial SessionDB construction
still runs the deleted-WAL guard, and subsequent registry acquisitions still check
inode replacement. The real child threads, `join(10)`, assertions and parameter cases
are unchanged. Earlier timing failure logs are retained alongside the repaired runs.

## Read-only audit

```sh
python scripts/audit_delegate_rule.py --since 1793386800 /path/to/state.db
python scripts/audit_delegate_rule.py --all-profiles /path/to/hermes
python scripts/audit_delegate_rule.py --all-profiles
```

`--since` is an inclusive Unix epoch against `sessions.started_at`. Explicit database paths
can be combined with `--all-profiles`. The latter includes the root `state.db` and existing
`profiles/*/state.db` files. With no root argument it uses the default Hermes root.

The script opens SQLite with `mode=ro` and `PRAGMA query_only=ON`; it never instantiates
SessionDB or migrates a database. It examines the first inserted `role='user'` row, including
inactive and archived history. It reports exact-rule present/missing totals by profile and
source, non-Claude skips and errors as JSON. A Claude session without a user row counts as
missing. Missing or malformed databases are errors, and the command exits 0. Configuration
errors are reported when a default pin is needed; an absent config supplies no pin.
An Anthropic provider qualifies without reading a default pin.

For an empty session model, the audit reads the sibling `config.yaml` pin. It supports
string, `model.default`, legacy `model.model`, and nested model/provider forms. This is the
current file's pin, not historical route proof or a managed-overlay resolution. A model
stored on the session takes precedence. The script never reads credentials or calls a
provider. It reports text presence, not delegation behavior.
