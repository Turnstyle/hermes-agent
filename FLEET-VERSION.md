# Fleet Hermes union rc1

Source release candidate. The specified regression gate and scratch smoke pass. Fleet rollout still has prerequisites listed below.

Branch: `fleet/0.21.5-union`. Tag: `fleet/0.21.5-union-rc1`.
Fleet fork: `https://github.com/Turnstyle/hermes-agent.git`.

The base is official `35ad70c035dec839846056073ad81a9bdceffca4`. It includes v0.21.5, tag `v2026.9.24`, commit `f97608f178d1ffeca59860195ab7da295f7c8e5f`. The starting fleet tree was Sheldon `786a302d54527d7010f12a8aa783efbc58cac034`. Official was merged in `010082027bb8477edfcc0424228afd3b575776fa`. This keeps the later base already used by TurnerBook. Source: those Git objects and the U2 release-check receipt.

## What is in

The final classification has 223 rows: 122 KEEP-FIX, 60 MOVE-OUT and 41 DROP. All 182 retained rows remain in this release. MOVE-OUT marks work to extract later; it does not mean the feature has already moved. The complete list, sources and reasons are in [FINAL-CHANGES.tsv](docs/fleet-reports/FINAL-CHANGES.tsv). The retained list is [CARRIED-STILL.md](CARRIED-STILL.md). Row-level coverage and tested blob identities are in [coverage.tsv](docs/fleet-reports/coverage.tsv) and [coverage-source.json](docs/fleet-reports/coverage-source.json).

The carried behavior covers Bot Chat delivery and reply receipts, queue ownership, bounded tool waits, worker cleanup, Kanban ownership and lifecycle checks, restart accounting, source launchers, profile isolation, Desktop relay state and vault binding. Max, Snowdrop and TurnerBook changes supplement the Sheldon tree. The 6 October TurnerBook recovery patch checks canonical ownership before PID probes and recovery writes. Rosie contributed no additional retained source change. Sources: the classified list and its machine column.

## What is out

The 41 DROP replay units are listed with reasons in [DROP-LIST.md](docs/fleet-reports/DROP-LIST.md). The parent-completion exceptions and host-owner API-key fallback were removed. Completion with an open parent is refused. A profile no longer accepts another profile's API key through that fallback. Sources: commits `39398911ce33f2c7fa1ea5faad215c8b29de5bbf` and `48ea15361f1cf11f81bb8e741ccab4ce51d96e51`.

Some DROP commits remain in Git ancestry because their later accepted replacements are kept. They are not independent replay units. Reversing those history steps would remove accepted behavior. The named cases and keeper tests are in [drop-kept.md](docs/fleet-reports/drop-kept.md).

## Test results

Tested source: `9cfe45f5623b9f305a923ef4eecd5d195e07375c`. The release documentation commit does not change runtime or tests. Runtime source is unchanged from the full union run at `bf0bb184da125a6099a75c90418d4aeb4a72b1ec`; affected test fixtures were rerun after repair. Source: U2 candidate-ancestry receipt and [C2.20-fixes.md](docs/fleet-reports/C2.20-fixes.md).

| Check | Passed | Failed | Skipped | Result |
|---|---:|---:|---:|---|
| Sheldon baseline, 332 selected paths | 3,893 | 66 | 33 | 282 passing files, 16 failing files, 34 absent paths |
| Union, latest result per selected path | 4,268 | 42 | 62 | 315 passing files, 11 failing files, 6 explained absent paths |
| New failing cases versus baseline | — | 0 | — | PASS under C2.20 |
| Supplemental log isolation | 2 | 0 | 0 | PASS |
| Four focused Desktop test files | 227 | 0 | 0 | PASS |
| Desktop TypeScript check | — | 0 | — | PASS |
| Scratch gateway and /health | — | 0 | — | running at tested SHA; HTTP 200 |
| Scratch Kanban create and dispatch --dry-run | — | 0 | — | both exit 0 |
| Scratch Bot Chat | — | 0 | — | HTTP 200 with the local fixture answer |

The suite is not all green: the 42 failed cases were already failing in the baseline. The full list and exact counts are in [test-totals.json](docs/fleet-reports/test-totals.json). Baseline and union logs remain under the build workspace `C2/u2-evidence`. Two class-renamed cases are matched only after identical method AST proof. No timeout or unexplained absent path remains. See [C2.20-fixes.md](docs/fleet-reports/C2.20-fixes.md).

Smoke ran from 2026-10-06 18:28:39 CDT to 18:29:03 CDT. Only the scratch gateway was started and stopped. Its SIGTERM exit 1 is the existing service-manager policy; the log records completed drain, disconnect and database close. Both scratch ports refused connections afterward. Commands and outputs: [smoke.md](docs/fleet-reports/smoke.md), [receipt](docs/fleet-reports/smoke-receipt.json).

The gate covers 332 selected Python test files, with one pytest process at a time, plus focused Desktop tests and type checking. It does not claim the full official upstream suite or a live test on all five machines. Six historical test paths were removed or replaced by the final classification; they are recorded as absent, not passed. Source: U2 test manifest and comparison receipts.

## Still carried and open risks

All 60 MOVE-OUT rows are still carried. Extraction and later official-main merges need their own validation. See [CARRIED-STILL.md](CARRIED-STILL.md).

The live queue-setting prerequisite for C2-143 is not proven on every managed profile. Some profile files lack an explicit queue value, and Rosie root says interrupt. Resolve this before rollout. See [C2-143-config-risk.md](docs/fleet-reports/C2-143-config-risk.md). No live setting was changed by this build.

Trusted Supermemory socket transport cannot be proved in this sandbox because its parent directories fail the production ownership check. Platform skips remain visible. Real provider calls, live vault access, macOS execution and Rosie aarch64 execution were not checked. The scratch smoke uses a local model fixture. The source review is by the builder, with no independent checker, because this task requires working alone. All 12 Jev calls were refused by the tool approval policy; no Jev approval is claimed. The exact questions, refusals and builder decisions are in [JEV-ANSWERS.md](docs/fleet-reports/JEV-ANSWERS.md). The bounded source review is in [codex-union.md](docs/fleet-reports/codex-union.md).

## How a machine moves onto this branch

These are rollout instructions for Lane C7. The U2 build does not execute them.

1. Complete C7's process inventory. Identify every gateway, Desktop backend, dashboard, Web UI and worker that keeps loaded code. Save dirty work. Create a backup branch at the installed HEAD. Record both the checkout and running SHA if they differ.
2. Fetch the rc1 tag from the Turnstyle fork. Verify it against the published FLEET-COMMIT. Stage the switch and the machine's verified dependency command. Use base `35ad70c035`, not the older release commit alone. TurnerBook Git commands require `GIT_NO_LAZY_FETCH=1`.
3. Back up and validate each machine's settings. Keep `updates.parked_branch_strategy: update_in_place`, `updates.carried_commits_policy: refuse`, and update channel `main`. Complete the explicit queue settings and other C7 prerequisites before switching. Never authorize reset as a shortcut around the carried-commit refusal. Source: Lane C7.2 and `hermes_cli/update_cmd.py`.
4. Use the coordinated restart procedure from C7.3 with the staged switch command. Do not perform an uncoordinated gateway restart. Respect the restart budget and the required drain. Restart other long-running clients only as specified in the process inventory.
5. Verify checkout SHA and served `code_sha`, connected platforms, resumed work, a local Bot Chat DM and a cross-machine DM. Follow C7's observation window and hold the next machine until verification passes. Rosie needs its own aarch64 validation under the same procedure.
6. Keep the backup branch and settings backups. If verification fails, use the coordinated rollback procedure and record the result.

Future updates follow the fleet update wrapper and the scratch merge/test/tag process in C8. Do not run a bare update against an unreviewed official target. The carried-commit tests prove refusal before cleanup, including release checkouts; they do not prove that an arbitrary future upstream change will merge cleanly.
