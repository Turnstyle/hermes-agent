# Board Ops maker receipt, run2768

VERDICT: PARTIAL SOURCE DELIVERED; FULL TASK ACCEPTANCE NOT PROVEN

Same maker session `01a0fdbc-feef-71c0-bf9a-4c3d6716a9fe`, card `t_1d6d9bdd`.
Parent owns independent Gemini-family review. This continuation finishes docs and
evidence only; the reviewed source/tests remain immutable. No second writer was
created. Parent deliberately interrupted the same CLI for scope corrections;
the later enforced 900-second wall-clock termination preserved source. Neither
event is evidence of an auth failure or successful lifecycle exit.

## Source custody

- Installed-base source: `39e341ef7250ec37fed6e32a1af2916fa0f01696`.
- Reviewed TurnerBook keep-spec delta: `fb7f20603691ccbffa3c4f439b3a5bc3fc8fc092`,
  already carried by parent as `da15360a52d12b55c85074e49cd01ee6b730505b`.
- Parent's immutable Board Ops source commit:
  `be3c6a7dbbfaa7dda103716605d975452d78516a`.
- Branch: `ai/shld-board-ops-t_1d6d9bdd`.
- Workspace: `/Users/sheldon/.hermes/kanban/boards/fleet/workspaces/t_1d6d9bdd/source`.

Current git history confirms the base, carry and source sequence. Source comparison
by parsed function boundaries confirms the original `keep_spec_triage_task` is
byte-identical in the carry and Board Ops commits. Its 101 lines have SHA-256
`5bd146dc1c89bdff0b89f472ed1294529c7e97540f38b1ba73c51bcb7cf51fec`.
The local identity receipt is `.lane-tests/keep-spec-function-identity.json`.
No replacement `_has_unreleased_block_loop` definition or sibling binding remains
in this candidate. Source-purpose comments and existing fences are retained.

The source commit changes exactly these 13 files:

| File | Responsibility |
| --- | --- |
| `hermes_cli/kanban_board_ops.py` | Shared checked control/grant/request/receipt/executor API. |
| `hermes_cli/kanban_board_ops_policy.py` | Runtime identity, owner startup, grant and recipient checks. |
| `hermes_cli/kanban_board_ops_store.py` | Additive records in the canonical board, snapshots and receipts. |
| `hermes_cli/kanban_board_ops_actions.py` | Admitted actions, original keep-spec import, interrupted-candidate evidence. |
| `hermes_cli/kanban_board_ops_transactions.py` | Connection-local database-only savepoint composition and promotion scope. |
| `hermes_cli/kanban_board_ops_cli.py` | Public JSON CLI over the shared API. |
| `gateway/kanban_board_ops.py` | Existing-timer collection and bounded passive exception delivery. |
| `gateway/kanban_watchers.py` | Five-line timer integration before normal dispatch. |
| `hermes_cli/kanban.py` | Import and command registration. |
| `hermes_cli/kanban_parser.py` | Exact public subcommand tree. |
| `hermes_cli/kanban_db.py` | Import and target-only readiness integration; keep-spec unchanged. |
| `hermes_cli/kanban_db_connect.py` | Opt-in connection-local nested transaction composition; write fence retained. |
| `tests/hermes_cli/test_kanban_board_ops.py` | 54 parameterized FIXTURE cases. |

Source visibility is **test +420/-0; product +839/-0**, 0.50 added test lines per
product line. This docs continuation adds only `BOARD_OPS.md`, this report and
the saved checklist `BOARD_OPS_TODO.md`. Scratch logs/homes are excluded from
the scoped docs commit. No old imported carry source is staged by this continuation.

## Tested evidence, all FIXTURES

The maker's saved core log reports `54 passed in 2.78s`. The parent independently
reported `54 passed in 3.26s` against the preserved source before committing the
same 13 files unchanged as `be3c6a7dbb`. The parent's exact command/output is
parent-supplied evidence; it was not rerun by this documentation continuation.

Maker core command, saved in `.lane-tests/board-ops-core.log`:

```sh
PYTHONPATH="$PWD" HERMES_HOME="$PWD/.lane-tests/runner-home" HERMES_DISABLE_LAZY_INSTALLS=1 /Users/sheldon/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/test_kanban_board_ops.py -q
```

The provided runtime `TMPDIR` and normal pytest setup were used for this core
run. Existing conftest selects per-test scratch homes and guards production DBs;
it has protected automatic relocation when needed. No guards, native home
identity or conftest were modified. All authored source and logs remain here.

The existing keep-spec baseline reports **15 failed, 10 passed in 1.84s**, saved
in `.lane-tests/keep-spec-baseline.log`. Failures identify the undefined original
`_has_unreleased_block_loop`, not Board Ops replacement logic. That historical
baseline command used the subsequently withdrawn explicit basetemp override:

```sh
PYTHONPATH="$PWD" HERMES_HOME="$PWD/.lane-tests/runner-home" HERMES_DISABLE_LAZY_INSTALLS=1 /Users/sheldon/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/test_kanban_specify_keep_spec.py -q --basetemp=/private/tmp/board-ops-keep-spec-base
```

This records execution provenance, not a recommended command. Parent corrected
that override; no further outside-workspace fixture override or deletion was
performed. Future proving runs use the same command without `--basetemp`, with
the provided `TMPDIR`. An earlier attempted workspace basetemp triggered
conftest's automatic relocation and a sandbox `PermissionError` at
`/var/tmp/hermes-pytest/...`; that was setup refusal, not product test evidence.

Current `git diff --check` passes. No proving regression was repeated in this
short docs-only continuation. No full suite, live production board, installed
gateway or deployed response-time acceptance was exercised or claimed.

| Contract | Actual FIXTURE evidence and limit |
| --- | --- |
| Default off / owner admission | Missing root authority and operator admission refuse; operator profile config cannot bootstrap node authority. |
| Worker/child denial | Wrong runtime profile, in-process delegated child, process-child marker and worker task pin deny requests and management. |
| Scope / forbidden operations | Wrong node, board, task, tenant, grant and forged actor fields refuse. Nondelegable operation rows refuse. |
| Grant/action freshness | Expiry, revocation, disabled control and tampered grant digest prevent pending actions. Text, tenant, status, run, dependency and hold changes invalidate snapshots. |
| Atomicity / recovery | Trigger-induced receipt failure rolls back the input comment and receipt; reopening board connections recovers pending work without repeating committed comments. |
| Correlation | Duplicate exact request returns its record; conflicting payload refuses without another action. |
| Task preservation | Authorized input comment keeps the task/run/dependency snapshot intact; scoped readiness leaves an unrelated task untouched. |
| Timing / recipient | Original timestamp deadline refuses late action; a single exception claim and simulated delivery readback record timing. Missing local recipient/subscription refuses. |
| Interrupted CLI | Real short-lived local FIXTURE child exits without a kill; saved candidate and checkpoint bytes survive. Dead-PID evidence is not successful CLI/session completion. |
| Healthy maker / ESTOP | Current process/run fixture is observed without redispatch; ESTOP remains present and prevents action. |
| Finite Jev handling | `not_configured`, `unavailable`, `timed_out` exception fixtures terminate without retries or inference. Receipt counters are assertions, not live inference traces. |
| Public commands | Parser/request/tick/receipt path is exercised through the shared API. |
| Keep-spec prerequisite | New test proves conservative refusal and unchanged task state when the original helper is absent. Existing keep-spec failures remain visible. |

The operator-context fixture executes without calling an owning Conductor chat.
It proves separation of API responsibilities, **not a fully exercised busy or
unavailable Conductor gateway fixture**. No gateway adapter send, busy-session
guard integration, live recipient action, Fleet-installed lease/trigger matrix,
or multi-process crash campaign is proved by these 54 tests. Connection reopening
and an injected transaction failure are narrower than real process-crash proof.

Test-audit authoring record: the tests protect authorization, durable atomicity,
state preservation and finite lifecycle outcomes; credible failures include
forged scope, expired grants, duplicate writes and lost transaction rollback;
existing tests do not exercise this new supported API; production entry points
are used without test-only product flags or helper substitution. No tests were
deleted. Parent remains the non-maker checker.

## Trust boundary and actual behavior

Actor identity is the resolved current Hermes runtime profile home, including
the existing context-local home override. Requests cannot supply `actor_home`.
Worker task/run pins and delegated-child process/context markers deny Board Ops.
Owner authority is anchored by node-root config and then the persisted board
control row. Operator config alone cannot replace it. Each grant names at most
64 tasks on one exact board/node/tenant, canonical local operator and recipient
profiles, owner chain, human attribution/provenance, operations and admitted
review authors. Expiry is at most 24 hours. Grants cannot be edited or extended;
owner disable revokes all grants and invalidates pending control revisions.

This is cooperative local-process authorization, consistent with existing
Hermes fences. It is not an OS sandbox or a cryptographic delegation system.
An OS user who can rewrite config/SQLite/process environment remains inside the
trust boundary. Human attribution, owner chain and review-author strings are
owner attestations. Existing board SQL and Fleet triggers remain behind the
supported API; no external direct-SQL bypass is offered.

Action-time transactions revalidate grant digest/expiry/revocation, control
revision, exact text, task/run/dependency snapshot, node ownership, available
Fleet lease proof and ESTOP. Input acknowledgment adds a comment only. Wait
inspection never redispatches. Interrupted CLI always escalates and preserves
candidate/checkpoint files; an absent or ambiguous process cannot authorize
resume. Nondelegable operations include grant/control management, model/config/
persona edits, install/restart/kill, archive/delete, dependency removal, lock edits,
completion, hold release, reassign and maker dispatch.

The shared gateway timer handles typed durable requests and due exceptions;
arbitrary card comments do not execute. Healthy idle handling has no inference
client or model/Jev turn. Default-off admission, 32-row batches, a durable cursor,
request terminal states and one correlation claim prevent endless execution
retries. Exception transport is passive, at most one 10-second attempt. The
local subscription and existing profile-owned resolver are checks; adapter
`success=True` is transport-reported delivery, not recipient read/action.
Stateless recipients without passive transport refuse. A crash after claim can
end as `delivery_unknown`; there is no resend or queue-only success claim.

Native Jev result supplied by parent was the real refusal:
`{ok:false,error:not_configured,execution_authorized:false}`.
No Jev judgment was fabricated. No credentials were borrowed or configured;
no new inference spend, model polling or wait loop was introduced. Ambiguous
requests record exceptions rather than using Jev to approve scope.

## Remaining acceptance gaps

**HIGH. Original keep-spec carry prerequisite.** Parent verified the original
helper from `4430234eb7ec67c9bc38185157a123ea24e9db60`; `_newest_event_kind` and
`_has_turner_hold` already exist on Sheldon. Existing tb-cndr/tb-king is providing
the minimal original reviewed closure through peer run
`run_8c28b8c84ef94efd960dafee5cb93bca`. This is parent-reported upstream custody,
not a closure applied or independently fetched here. The snapshot still lacks
the helper and refuses `keep_spec`. No successful reviewed release, exact-text
release preservation or release dependency/hold acceptance is claimed.

**HIGH. Original-event response bounds.** Source checks a 300-second action
deadline and records 600-second escalation compliance. It does not cap the
shared dispatch interval, other tick work, backlog, connection initialization
or total sequential delivery duration. It therefore does not establish a hard
300/600-second service bound. Owner timer/load/transport proof and any necessary
reviewed resolution remain required; frozen source was not expanded here.

**HIGH. Installed SAME Rescue and every admitted node.** Parent reports live
SAME Rescue still has an actual `block_loop_detected` hold. Routine Board Ops
cannot clear it. The owner must resolve the genuine hold, adopt control/grants,
install reviewed source in its authorized window, restart through normal custody
and prove the installed SHA, actual supported action, busy-Conductor independence
and node-specific timing. None was performed by this maker.

**MEDIUM. Transport and broader integration acceptance.** Gateway delivery,
served-profile topology, real Fleet adapter/lease fences, unavailable/busy chat
and crash timing need independent coverage and live evidence. A passive notice
does not wake the operator/King model or prove their response. Automated interrupted
maker recovery and successful session exit are intentionally not authorized by
the available evidence.

Rollback and adoption commands, exact JSON contracts and source-update caveats
are in `BOARD_OPS.md`. Owner supported `control --disable` revokes grants while
retaining tables/events; config-only disable does not change persisted control.
Save exact commits and diff before any Hermes update. No upstream push, merge,
production action, install, restart, profile skill/persona/config/credential
change or model-selection-file change is claimed. The original task is not Done.

Documentation commit custody is reported in the final maker response. Scratch
evidence is retained locally and deliberately excluded from staging.
