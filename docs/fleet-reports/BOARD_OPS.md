# Node-local Board Ops

This source candidate implements run2768 for the Hermes runtime. It supports
bounded operations on an existing canonical Kanban board, independently of a
Conductor chat turn. It does not create a task registry, bot, scheduled model
turn, or inference client. Its scope does not redefine my new ‘Conductive’ Standard Schema.

The reviewed source snapshot is `be3c6a7dbbfaa7dda103716605d975452d78516a`.
Owner adoption and live acceptance remain pending. The original keep-spec carry
prerequisite is missing in this snapshot; `keep_spec` conservatively refuses it.
The original source owner must provide that closure before release acceptance.

## Owner startup and responsibilities

These are adoption instructions for the owning Conductor, not actions performed
by this maker. Existing local profiles and the canonical board must already exist.
The authority file is `<kanban_home>/config.yaml`, ordinarily the node-root
configuration, not the operator's profile configuration. If a deployment has an
existing `HERMES_KANBAN_HOME` override, the owner must verify that exact root.
Do not clear worker or delegated-child markers to run these commands.

In the node-root configuration context, the owner uses the supported settings
command. Replace example identities with canonical existing local profile IDs.
Use the owner-approved node ID and explicit board list.

```sh
hermes -p default config set kanban.board_ops '{"enabled":true,"node_id":"node-example","boards":["fleet"],"owner_profile":"node-conductor","executor_profile":"board-ops"}'
```

This command writes the node-root file only when the default profile actually
resolves to `kanban_home`. Verify that topology before adoption. Managed config
restrictions still apply. Configuration alone does not grant operations.

```sh
hermes -p node-conductor kanban --board fleet board-ops control --human Turner --provenance run2768
```

Owner admission persists the node, board, owner home, executor home, human
attribution and authority provenance. An existing authority anchor cannot be
replaced by changing configuration and re-enabling control. The node Conductor
alone issues and revokes grants and disables control. The named operator submits
exact requests under its own runtime profile. The admitted executor consumes
those requests; it may be the operator or an existing gateway profile. The
Conductor remains accountable for exceptions and recovery.

The gateway must actually run under the configured executor home. The existing
embedded dispatcher must be enabled and own its singleton lock. No new service
is started by Board Ops. An owner may adopt an already served role/profile later;
installation and restart remain separate owner-authorized deployment work.

Before granting, verify an existing local recipient profile and a matching task
subscription for every granted task. An owner-created passive subscription can use:

```sh
hermes -p node-conductor kanban --board fleet notify-subscribe t_example --platform telegram --chat-id example-chat --notifier-profile king --delivery-mode notify
```

For a thread, supply `--thread-id` and the existing platform routing anchors.
Subscription existence is local route evidence. It does not prove that a served
adapter or recipient is currently available. The gateway checks the actual adapter
at delivery time.

## Grant contract

`grant.json` must contain exactly these input fields. This illustrative expiry
must be replaced with an integer Unix timestamp strictly after issuance and no
more than 86,400 seconds later. At most 64 exact task IDs are allowed. All must
exist in this board and have the exact tenant string, or all have tenant `null`.

```json
{
  "grant_id": "node-example-ops-001",
  "node_id": "node-example",
  "board": "fleet",
  "task_ids": ["t_example"],
  "tenant": "example-tenant",
  "operator_profile": "board-ops",
  "owner_chain": ["Turner", "CoS", "node-conductor"],
  "human_attribution": "Turner",
  "authority_provenance": "run2768",
  "allowed_operations": ["record_input", "inspect_wait", "interrupted_cli", "escalate"],
  "expires_at": 1790967600,
  "escalation_recipient": {
    "profile": "king",
    "platform": "telegram",
    "chat_id": "example-chat",
    "thread_id": ""
  },
  "review_authors": ["Turner"]
}
```

The owner chain starts with the human attribution and ends with the admitted
Conductor. Names and provenance are owner attestations, not cryptographic human
authentication. The API derives and stores operator/owner homes, issuance time,
nondelegable actions, revocation state and a content digest. Do not provide those
derived fields as input. Grants are immutable; a reused grant ID cannot extend
or replace authority. Only a fresh owner-issued grant can admit a later scope.

```sh
hermes -p node-conductor kanban --board fleet board-ops grant grant.json
hermes -p node-conductor kanban --board fleet board-ops revoke node-example-ops-001
```

## Request contract and operations

`request.json` must contain exactly these fields. Obtain the exact task title,
body and durable triggering event ID through the supported read command:

```sh
hermes -p board-ops kanban --board fleet show t_example --json
```

Compute `task_sha256` as SHA-256 of UTF-8 compact JSON `[title,body]`, with
`ensure_ascii=False`, separators `(',', ':')`, and no trailing newline. Preserve
whitespace and Unicode; a null body differs from an empty body. The hash below is
only an illustrative placeholder and must be replaced with the actual digest.

```json
{
  "correlation": "node-example-t_example-event-123-input-001",
  "grant_id": "node-example-ops-001",
  "node_id": "node-example",
  "board": "fleet",
  "task_id": "t_example",
  "tenant": "example-tenant",
  "operation": "record_input",
  "task_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "event_id": 123,
  "payload": {"text": "Owner-provided input for the existing task."}
}
```

The original event must belong to this task and cannot be a `board_ops_*` event.
Its stored timestamp determines deadlines; requests cannot supply a new timestamp
or actor identity. Correlations are board-unique and at most 128 characters.
Identical repeats return the existing receipt while current authorization remains
valid. A conflicting reuse refuses. JSON files are limited to 64 KiB; payload
string values together are limited to 8,192 characters.

| Operation | Exact payload | Effect |
| --- | --- | --- |
| `record_input` | `{"text":"..."}` | Adds an attributed comment and receipt. Releases no hold. |
| `inspect_wait` | `{}` | Observes a verified live current run without redispatch. Otherwise records an exception. |
| `interrupted_cli` | `{"candidate_path":"...","candidate_sha256":"..."}` | Reads a candidate of at most 1 MiB under the saved workspace, records candidate/dead-PID evidence and escalates. Never resumes, kills or deletes files. |
| `escalate` | `{"reason":"...","jev_status":"not_configured"}` | Records a finite exception with no inference. Status may also be `not_requested`, `unavailable`, or `timed_out`; none grants authority. |
| `keep_spec` | `{"review_author":"Turner"}` | Once the original carry prerequisite exists, invokes that unchanged keep-spec function with the exact digest and owner-admitted reviewer. Current snapshot refuses the missing prerequisite. |

```sh
hermes -p board-ops kanban --board fleet board-ops request request.json
hermes -p board-ops kanban --board fleet board-ops list
hermes -p board-ops kanban --board fleet board-ops receipt node-example-t_example-event-123-input-001
hermes -p board-ops kanban --board fleet board-ops tick
```

`tick` is admitted-executor-only and processes pending requests once. It does not
send notifications; the gateway delivers exceptions. `request` returning `pending`
means durable intake, not action success. `list` and `receipt` filter operator
records to that runtime profile's grants; owner/executor can inspect all records.

## Execution, deadlines and preserved fences

The existing dispatcher timer calls Board Ops before normal dispatch. Default-off
boards have no admission or grants. Healthy unchanged state creates no model/Jev
calls or request actions. Only typed durable requests execute; comment prose does
not. No model polling or separate timer/service is introduced.

At action time, one checked SQLite transaction revalidates grant digest, expiry,
revocation, control revision, node/board/task/tenant, exact text, the full task/run
snapshot, dependencies, local Fleet ownership/verified lease and ESTOP. An action
and its receipt commit together. Nested database-only transitions use savepoints;
readiness composition is restricted to the requested task. Restart resumes pending
rows and does not replay committed actions. The cursor and per-request states are
durable in the same canonical board.

Routine operators cannot manage grants/control or edit models, config, persona,
locks, assignees or dependencies; they cannot archive/delete, restart/install/kill,
complete cards, release holds or dispatch makers. Worker and delegated-child
contexts are denied. Keep-spec refuses current runs/claims, failure counters and
block recurrences. Existing keep-spec holds and Fleet write triggers remain in
force. Genuine dependencies, title/body, checkpoints, model overrides, links,
leases and ownership fences are not reset to make a card eligible. The live SAME
Rescue `block_loop_detected` hold still requires its owning Conductor's explicit
resolution; an ordinary operator cannot clear it.

The action budget is 300 seconds from the original durable event. A request first
processed at age 300 seconds or later becomes an exception rather than a late
action. Exceptions are due immediately; the escalation receipt records whether
the 600-second original-event budget was met. Each pass handles at most 32 requests
and 32 exception claims. Sending has a 10-second timeout per notice.

These thresholds are not an unconditional response-time guarantee. The shared
dispatcher has configurable intervals and other tick work; this snapshot does not
cap either interval or total tick duration. Owners must prove the 300/600-second
bounds on each admitted node under load with its actual timer, queue and transport.
A stopped gateway, missing executor, backlog or unavailable route can miss them.
No missed deadline is converted into success.

Exception delivery is one passive transport attempt per correlation, using the
existing profile-owned adapter resolver. It never wakes a model. A transport
`success=True` produces `notified`, which does not prove human read/action. Missing
routes become `recipient_unavailable` or `delivery_failed`. A crash after claim
without readback becomes `delivery_unknown` after 30 seconds when next processed;
there is no automatic resend. Stateless routes without passive outbound delivery
are refused. Recovery/escalation remains owner work.

## Revoke, rollback and source persistence

For immediate owner control rollback, use the admitted owner runtime:

```sh
hermes -p node-conductor kanban --board fleet board-ops control --disable --human Turner --provenance run2768-rollback
hermes -p node-conductor kanban --board fleet board-ops list
```

Disabling increments the control revision and revokes all existing grants.
Re-enabling does not revive them. To prevent future admission, the owner can also
use `hermes -p default config set kanban.board_ops.enabled false` in the verified
node-root context. Changing that config flag alone does not disable an already
persisted control row; use the supported control command first. Disabled control
still allows the admitted executor to settle refused pending requests and drain
exception receipts. It does not reverse already committed actions or recalled
transport deliveries.

Retain `kanban_board_ops_control`, `kanban_board_ops_grants`,
`kanban_board_ops_requests`, and the canonical task events. Do not drop tables,
clear receipts/fences or rewrite tasks as rollback. A later owner source rollback
must preserve these durable records and follow normal deployment custody.

This candidate is a local source change, not an upstream merge or installed
feature. Before any future Hermes update, save the exact source/docs commits,
their hashes and an exported diff in owner-controlled storage. The update path
may replace local source. Reapply only reviewed saved commits through normal
source custody and prove the installed SHA afterward. No upstream push, merge,
installation or restart is claimed here.
