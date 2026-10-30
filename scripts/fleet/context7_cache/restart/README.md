# Exact-source coordinated restart overlay — review only

## Outcome and boundaries

One executable-source line changes, in the installed adapter's existing `run_restart(log)`:

```diff
-    cmd = [hermes_bin(), "-p", "default", "gateway", "restart"]
+    cmd = [hermes_bin(), "-p", "default", "gateway", "restart", "--yes"]
```

This directory is an overlay/evidence package, **not a replacement branch module or an installed adapter**. No live source was edited, no coordinator `request`, `run`, `status`, `release`, gateway operation, service-manager operation, message, email, HarmonyDM, board write, key/vault/environment/auth read, installation, commit, push or merge was performed. Only source files and repository status were read; the tests supply a fabricated, explicitly synthetic environment dictionary instead of reading the process environment. Reported old PID5719 and failed request `r-20261030-152121-60b0` are incident inputs, not independently checked live state.

## Exact provenance

- Installed source: `/Users/sheldon/.hermes/fleet-node-state/u3g/g3f/gateway_coordinated_restart.py`.
- Before SHA-256: `df56ebff6a48c22b1a698d491da4195e2fe54d2d19f8df0133b31654eac3d3a8`.
- Resulting full-source SHA-256: `0f6cd7718abbb0f9dd11e4cdb4c69f03a2b28f96339280ae786b14a993cda77a`.
- `manifest.json` pins the full source, resulting source, patch, extracted fixture and source evidence. `artifact-hashes.json` inventories the package (excluding itself).
- `gateway_coordinated_restart.patch` is a conventional unified diff against that exact source; no base carry git history is assumed. `build_overlay.py` refuses source drift and writes artifacts only here; it never applies the patch.
- `source-evidence.json` preserves exact excerpts, line ranges and full-file hashes for the installed coordinator, live release parser/guard/dispatch, startup secret-loading boundary and launcher chain.

## Underlying CLI semantics

All release paths below are relative to `/Users/sheldon/.hermes/releases/stable-v0.22.5-carry-d4c5aec662c8/candidate/`.

- `hermes_cli/subcommands/gateway.py:17–21,146–154`: restart accepts `-y`, `--yes`, `--interrupt`, all stored as `args.interrupt`. It explicitly consents to interrupting live turns, cron jobs and background delegations; it is **not** a guarantee of a graceful completion or recovery.
- `hermes_cli/gateway_lifecycle_guards.py:97–129`: active-work refusal is checked; `interrupt=True` prints a warning and proceeds. Without it, TTY users are prompted, while non-TTY execution exits 1. `hermes_cli/gateway.py:5465–5468` calls that guard for restart.
- Parser `--force` is a different flag: it permits a separate gateway for a profile already served by the default multiplexer. This overlay never adds it. Nor does it add `--all`, `--detach`, a named-profile target, a bare-restart retry or a new fallback.
- Coordinator `hermes_bin():593–602` selects `HERMES_BIN`, PATH, the local launcher, then the older checkout. Those environment values were **not read**. Source inspection of `/Users/sheldon/.local/bin/hermes` shows an old-directory launcher which actually pins the live v0.22.5 candidate in `sys.path` and imports its `hermes_cli.main`; the old directory name does not establish old code. Deputy must check the executable actually selected at deployment without exposing secrets.

## Existing coordinated window and native safety

The normal flow still acquires owned dispatch pauses and drain markers, checks work/deadline, performs its switch callback/gates, snapshots pre-restart state, invokes the same function, verifies health, reports resume status and releases its own markers. See adapter `1780–1798`, `1815–1830`, `1900–1925`. The retained flow still invokes the same function after callback, auxiliary fence, pause and manifest checks (`2482–2499`). There is no new entry point or shortcut into either flow.

Everything outside `run_restart` is byte-identical in the in-memory result; verification also checks the outside-function AST. Within the function, only the argv list changes. Timeout, capture, default-profile targeting, child environment preservation and the three coordinated restart/drain flags remain unchanged. Child/native board safety is not reimplemented or weakened.

**Pre-existing residual:** adapter `1504–1518` can use launchctl/systemctl if the result contains `Restart budget blocked` and the coordinator request has its own `force` field. That existing coordinator-force mechanism is unchanged, distinct from CLI `--force`, and exercised only with mocks. This package does not authorize it, request it, or claim that the original adapter forbids all service-manager fallbacks.

## Coordination and notice constraints

Permitted here: offline source inspection, patch/hash review, synthetic tests, and this handoff through the existing parent task. No communication endpoint or coordinator request is permitted under this task.

Source `2613–2635` offers:

| Control | What it does | What it does not do |
|---|---|---|
| `--wake-cmd none` | Disables post-restart cut-bot wake route | Does not suppress mid-drain notices |
| Omit `--notify-cmd` | Does not invoke the optional requester outcome route | Does not suppress notices/wakes or local outcome files |
| `--dry-run` | Advertises read/print-only behavior | Not used here; broader imports/reads remain outside this source-only scope |
| Coordinator `--force` | Skips min-gap and can enable existing budget fallback | Is not notice suppression and is not CLI interrupt consent |

There is **no general supported no-notices flag in this inspected parser**. In the normal flow, `1815` loads the API key and `1830` calls `send_notices` for active work. `1409–1430` writes a local notice and may comment on a board card or steer a run. `1944` wakes cut bots, while `1954–1961` optionally invokes the requester route. Omission of notify-cmd plus wake-cmd none is insufficient to satisfy the present no-message/no-board-write boundary. No claim that another mode is notification-free is made. Do not invent an undocumented suppression option, silently remove native coordination, or invoke the coordinator to test it. A later deputy must resolve this constraint under separate explicit operational authority before any request/deployment.

## Verification

From the designated worktree, safe offline commands are:

```sh
python3 -B scripts/fleet/context7_cache/restart/build_overlay.py
python3 -B scripts/fleet/context7_cache/restart/prepare_jev_review.py
python3 -B scripts/fleet/context7_cache/restart/verify.py
```

`verify.py` records genuine unittest output in `test-result.json`. Eight tests extract/execute **only** the fixture's real `run_restart` function using `ast`, never import the adapter, and mock every subprocess operation. Coverage includes exact argv ending in `--yes`, absence of CLI `--force`/`--all`, default scope, inherited child context and timeout, success, nonzero exit/no retry, timeout/no retry, and the unchanged budget fallback on two synthetic platforms. A negative control executes the unpatched function **against mocks only**, simulating the old bare-argv refusal; the patched function passes the same simulator. No bare gateway restart is ever launched.

The CLI test executes the actual parser flag-registration AST against argparse and extracts its actual live-work guard; only its gateway printer import is replaced by mocks. No control socket, runtime state or live-work query runs. Exact full-source hash/diff checks and outside-function AST comparison run before the synthetic suite; source hash is checked again afterward. These checks do not prove live health or interrupted-session recovery.

## Jev advice missing — safely blocked, not a review pass

`jev-request.json` contains 24 keyed varied questions: eight choice (label-description object), eight score (five ordered strings), eight noul (no criteria), each with instructions. The canonical state is 25,054 bytes, under 32,768; it includes 2.55 times the focused proposal/function/CLI evidence state (9,822 bytes), expanding to both coordination windows, notices, provenance and contrary secret-startup evidence. Full request is 32,097 canonical bytes. No long-context mode is needed.

The requested `hermes -p shld-mm-hermes-agent jev evaluate --file ...` was **not invoked**: the exact launcher imports `hermes_cli.main`, whose startup calls `load_hermes_dotenv` before dispatch (`main.py:708–716`). `_early_recovery.py:70–81` exempts update, not Jev, from external secret-source resolution. Invoking it would conflict with the explicit no-secret-loading CLI restriction. No provider/credential was inspected or repaired, no API was called, and no paid result was fabricated. `jev-result.json` is clearly labeled a **local non-invocation receipt**, with `ok:false`, `advice:missing`, and no answers.

If separately authorized later, raw CLI output must be parsed with `json.JSONDecoder().raw_decode(out[out.index('{'):])[0]`; require `ok`, exact question-ID equality and real usage/model evidence. Confidence below 0.50 is no signal; absent confidence is unknown. This task has no Jev verdict.

## Handoff

Overlay and offline verification are complete; deployment and restart are **not authorized or performed**. Deputy alone may later consider deployment, only after exact-source/executable revalidation and explicit resolution of the communication/board-write constraint. Stop on source drift; do not blindly repeat the reported failure. This directory contains no operational request recipe or live patch-applier.
