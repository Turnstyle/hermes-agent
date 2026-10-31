# Context7 config correction — offline artifact, not deployment

This correction supersedes the daily-TTL and ordinary-op exclusion instructions
in `../../context7-unattended-helper.md`. That historical document and its old
single-case config test remain outside this task's editing scope. In particular,
the old test's expectation that an ordinary op TTL86400 is excluded is now
intentionally obsolete. Use this directory's suite for the corrected policy.

## Approved delta

- Exact pinned helper `binary_path` gets literal `allow_unattended_helper: true`
  and `cache_ttl_seconds: 1800`, including absent settings and lower helper TTLs.
- Ordinary `secrets.onepassword.cache_ttl_seconds` numeric values above1800 are
  capped at1800. This explicitly includes the keys-router's ordinary op config.
  Ordinary opt-in/binary/enabled/auth fields are not changed. Missing, string,
  boolean, and already-low ordinary TTLs are not interpreted/coerced.
- Only `shld-coordinator` and `shld-host-diagnostics` are targeted to receive
  literal `mcp_servers.context7.enabled: true`. Existing unrelated Context7
  enablements are preserved, not disabled. “Exactly two” describes this delta,
  not a claim that the fleet has only two enabled Context7 servers.
- All other semantic content is compared against an independently deep-copied
  expected config. Source-mark edits preserve untouched bytes/comments, Unicode,
  CRLFs, source ordering, and unrelated inline mappings. No dump/reformat pass.
  Literal YAML forms such as YES/on are normalized for the approved true fields.
- The PR9 explicit helper startup opt-in and default desktop guard are unchanged.
  No env_loader replacement, model/fallback, native board/delegated-child guard,
  auth setting, command, vault, or credential change is part of this artifact.

## Writer guarantees and boundaries

Dryrun reads only exact one-level `profiles-root/*/config.yaml`. It does not read
.env files, invoke Hermes, op, the helper, or any provider, and emits only profile
identifiers, counts, setting names and hashes. YAML parser text is suppressed on
CLI failure because it can contain source values.

Apply requires an explicit backup directory and an exclusive root writer lock.
It refuses an existing lock rather than clearing it. It rejects symlink roots,
profile directories, configs and backup paths, non-regular/hardlinked configs,
duplicate/merge keys and invalid mapping shapes. Anchored scoped configs fail
closed; unrelated anchored configs are inventoried/skipped without edits. Flow
mappings allow existing scalar replacement but missing-field insertion requires
review; empty mappings requiring insertion also fail closed.

The backup directory must be fresh and is created exclusively (0700); backup
files and the prepared manifest use O_EXCL/O_NOFOLLOW (0600). Every backup is
byte-verified/fsynced, and every same-directory replacement is precreated,
verified and fsynced before the first replacement. Original permission bits and
ownership are preserved. File identity/stat metadata and bytes are checked at
planning, before staging, after all staging, and immediately before each
`os.replace`; readback and directory fsync follow each replacement. Staged
files and the owned writer lock are removed on failure. Backups remain for
recovery and are never overwritten.

**Limits:** the lock serializes cooperating invocations, not arbitrary editors.
There is no portable compare-and-swap against a hostile writer between final
check and replacement. Profile/backup ancestors must be operator-controlled and
other writers quiesced for deployment. Atomicity is per file, not an all-profile
transaction; later replacement failure can leave earlier profiles replaced.
The durable manifest is a **prepared plan**, not an apply-success receipt. A
failed run must be reconciled against current hashes and cold backups; never
blindly replay it or automatically remove stale locks. ACLs/xattrs/timestamps
are not claimed preserved by this config-only writer.

## Reproduce offline tests

Use installed Python with PyYAML; no installation is needed. This run discovered
`/Users/sheldon/.hermes/webui-venv/bin/python` (Python3.14 / installed PyYAML).
The system Python and base standalone runtime lacked PyYAML.

From the worktree root:

```sh
TMPDIR=/Users/sheldon/.hermes/profiles/shld-mm-hermes-agent/cache/scratch/context7-cache-correction-20261030/config \
  /Users/sheldon/.hermes/webui-venv/bin/python -B \
  scripts/fleet/context7_cache/config/test_config.py -v
```

Observed: **29 tests passed**, covering helper/ordinary TTL boundaries, missing
fields, literal flags, exactly-two delta, source-byte/semantic preservation,
CRLF/Unicode, scoped anchors and unrelated aliases, duplicate/merge keys/tags,
flow mappings, invalid shapes, file/directory/root symlinks, hardlinks,
exact-depth config discovery, no env-file access, dryrun identity, fake apply,
backup bytes/modes, no-op repeat apply, existing backup/lock refusal, concurrent
identity/byte mutation, all backups before first replace, precreation/full-disk
injection, replacement failure cleanup and CLI secret-value redaction.

The unchanged startup test AST-extracted the actual installed release function,
without importing the secret loader or invoking providers:

```sh
/Users/sheldon/.hermes/webui-venv/bin/python -B \
  scripts/fleet/test_context7_unattended_helper.py \
  --source /Users/sheldon/.hermes/releases/stable-v0.22.5-carry-d4c5aec662c8/candidate/hermes_cli/env_loader.py -v
```

Observed: **6 tests passed**, including invalid/no assertion and blank helper
path protection, explicit opt-in, service-account, interactive and disabled
source behavior. Config code is not imported by those AST tests.

## Fresh live INVENTORY DRYRUN ONLY

Saved evidence directory (own profile scratch):

`/Users/sheldon/.hermes/profiles/shld-mm-hermes-agent/cache/scratch/context7-cache-correction-20261030/config/`

- `live-inventory-dryrun.json`: 95 exact configs scanned;79 matched;3 planned
  changes. Two consecutive dryruns produced identical manifests, without writes.
- `inventory-settings.json`: UTC capture metadata plus safe per-profile hashes
  and selected settings. Fresh inventory found **all78 helpers already TTL1800**,
  contrary to task's historical78 TTL86400 context. No live config apply occurred
  here, and this task does not attribute the preexisting correction to itself.
- Remaining planned changes: coordinator/host-diagnostics Context7 false→true,
  and ordinary keys-router TTL86400→1800.
- Other existing literal-enabled Context7 profiles are unrelated and preserved.
- `offline-tests.txt`, `startup-guard-tests.txt`: real passing execution output.

The first inventory attempt correctly failed on four unrelated anchored configs;
planner scope handling was corrected and a regression test added before the
successful inventory. The initial fixture test invocation inherited a symlinked
system TMPDIR and failed safely; fixtures were cleaned, then all successful
fake apply tests explicitly used the own-profile scratch path shown above.

## Jev advisory blocker and unexpected native startup behavior

The required native command was attempted once:

```sh
hermes -p shld-mm-hermes-agent jev evaluate --file <scratch>/jev-request.json
```

The request has24 keyed varied questions (noul/score/choice), descriptions for
choice labels, six ordered score criteria, no noul criteria, and30343 canonical
state bytes (below32768). Its state includes actual source/tests/results,
constraints, contrary evidence, options and proposed report. Request/state/raw
result are saved in the evidence directory.

**Installed CLI lacks `jev`: exit2, no JSON response and no usable signal.** The
required raw_decode extraction therefore had no opening JSON object, no `ok`,
answer IDs or confidence to validate. No repairs/install/provider fallback were
attempted; absent or below0.50 confidence is no signal. See `jev-status.json`.

**Important exception:** before rejecting `jev`, native Hermes startup printed
`1Password: applied 2 secrets` (no values). The task did not explicitly invoke op
or inspect those values, but the requested native CLI unexpectedly ran its own
secret bootstrap. Therefore do not claim the entire execution was free of all
credential-provider activity. No more native CLI calls were made. This is a
reported scope exception, not evidence of config application or deployment.

## Handoff

Source/tests/documentation only. No live config edit, deployment, gateway
restart, board/message write, commit, push, merge, binding change or self-review
approval was performed. Independent review and eventual deployment remain with
the authorized Marshal/deploy-deputy path; saved dryrun is not live adoption.
