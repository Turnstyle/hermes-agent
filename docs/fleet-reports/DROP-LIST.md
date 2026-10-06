# Dropped changes for fleet/0.21.5-union

The final U1 table replaces the old 34-row grouping in card C3.4. It has 41 DROP rows. A dropped history step can contain assertions or final behavior that U1 expressly keeps in a later named implementation. Those obligations remain. Source: C2/FINAL-CHANGES.tsv and C2/README.md.

## C2-003: fix(supermemory): state what dropping an inherited key does and does not remove

Sources: sheldon:29c9f9b640d2723989cc72d7b3b590b0d1374c95;turnerbook:88c73eb3c40deb4fb322d7a9a99906646432dc64.

Reason: Drop this standalone wording-only history step. Keep accurate guard documentation with the moved Supermemory provider; the guard behavior is not dropped. Sources: research/hermes-patches/MASTER-changes.tsv:4

## C2-006: Let rehomed copies and merged-parent bookkeeping complete.

Sources: sheldon:93ae5fcd9fc361218133554c752a8df342052fde;snowdrop:f72be3954d8cfe77b4c99ea776bb053470daf7c5;turnerbook:559512162503f568b1eb2009a207429652c4d72e.

Reason: Drop the whole open-parent exception chain. C3 found zero completions that needed it. Preserve the separate bookkeeping flag used by the busy-worker logic. Sources: research/hermes-patches/MASTER-changes.tsv:7; research/patch-followup/verdicts-unsure-1.md:16; work/hermes-union/C3/parent-gate-use.md:7

## C2-007: Revert "Route local blocked-config cron alerts to owning Bot Chat"

Sources: max:3e7bc1b92e547ec235f35a0eba5619122335c90e.

Reason: Do not replay this superseded or reverted history step. Preserve the final implementation and its assertions. Sources: research/hermes-patches/MASTER-changes.tsv:8

## C2-009: WIP(checkpoint): Cursor SESSION_NOT_OWNED / relay busy-handoff lane, paused for Desktop relaunch (tb-cndr 2026-09-28)

Sources: turnerbook:d7203ec11dffd7488cfa69180239d4b3822fa0e9.

Reason: Drop the WIP checkpoint commit as a replay unit. Preserve the final relay attestation and quiet-run lease behavior from the later accepted commits. Sources: research/hermes-patches/MASTER-changes.tsv:10

## C2-023: fix(bot-dm): fast-ack a busy target into fleet_messages_v1

Sources: sheldon:cd43f06efa420cebc51cdae5b374b703a185a18e;snowdrop:4c65534015531e2b4b3c592a75470733a1fd4782;turnerbook:6fc991bcccbe0b359a9567f557515f72e0f9bfa8.

Reason: Drop this early fast-ack step as a separate patch. Keep enqueue_busy_dm and the later recipient-admission/create-only behavior from g1#17 and g1#26. Sources: research/hermes-patches/MASTER-changes.tsv:24

## C2-050: Retry busy hosted-room peer runs within exact task identity

Sources: turnerbook:4be0eb7fc1aec743a09128bc9791f097a9cec41d.

Reason: Drop hosted-room busy retries under the Lane C decision. Prior review found no active use and a dependency on another local HTTP busy path. Sources: research/hermes-patches/MASTER-changes.tsv:52; research/patch-followup/verdicts-unsure-4.md:7

## C2-061: fix(bot-chat): re-land Sheldon DM child deadline fix with test-race fix (t_2be7106b)

Sources: turnerbook:e77e718d1b19b84e3080063083f66b090a28037e.

Reason: Drop the duplicate re-land commit. Preserve its test-race correction with the kept DM child deadline series. Sources: research/hermes-patches/MASTER-changes.tsv:64

## C2-064: feat(cron): per-job wake budgets (max_turns/max_tokens) for 0.21.5 (hand-port of 71b1f8bcf)

Sources: snowdrop:93abaf48b0d9dcf9df9b0827b3d25c4519ed653d.

Reason: Drop both per-job cron-budget patches. Prior verdict found only paused users. Use official profile-level agent.max_turns if needed. Sources: research/hermes-patches/MASTER-changes.tsv:67; research/patch-followup/verdicts-unsure-5.md:6

## C2-065: fix(cron): per-job max_turns blocks the reasoning-only grace call

Sources: snowdrop:40c5543d60da1ff7d7585ea796a2c390accf9ff0.

Reason: Drop both per-job cron-budget patches. Prior verdict found only paused users. Use official profile-level agent.max_turns if needed. Sources: research/hermes-patches/MASTER-changes.tsv:68; research/patch-followup/verdicts-unsure-6.md:5

## C2-067: Revert "Clear cron alert marker after all config checks pass"

Sources: max:e065823aee978e676b627d46df2f2dd246f19ec9.

Reason: Do not replay this superseded or reverted history step. Preserve the final implementation and its assertions. Sources: research/hermes-patches/MASTER-changes.tsv:70

## C2-069: fix(desktop): merge a newer transcript and send anyway

Sources: turnerbook:493e2ef5f2689d5a46d0df43b9d67e891dd55c88.

Reason: Drop the stale-transcript workaround that can bypass a competing user-row fork check. Preserve the official refusal contract and the kept transcript-tip fix. Sources: research/hermes-patches/MASTER-changes.tsv:72

## C2-088: Gate kanban completion on a real rehome or a verified merged PR.

Sources: sheldon:f07d34aa12b84b98fe1856f098578dc4d04840a3;snowdrop:829bb3eea9e3e8a11706c247b7be56ca1f36d427;turnerbook:c7af0f7dab086f822924e275c79d18c7c60bab2a.

Reason: Drop the whole open-parent exception chain. C3 found zero completions that needed it. Preserve the separate bookkeeping flag used by the busy-worker logic. Sources: research/hermes-patches/MASTER-changes.tsv:88; work/hermes-union/C3/parent-gate-use.md:7

## C2-102: fix(kanban): preserve Max fleet mirror lifecycle fence recovery

Sources: max:593911c56f5921da8071925f04ab205e23f9a0bb.

Reason: Do not replay Max emergency fence and duplicate tests. Use the canonical Sheldon fence. Preserve the claimed-only capacity assertions in tests/hermes_cli/test_kanban_host_cap_mirror_rows.py in that implementation. Sources: research/hermes-patches/MASTER-changes.tsv:105; research/patch-followup/verdicts-unsure-4.md:42

## C2-107: redtape rank 2: a parent in review or blocked is never released by merged-PR evidence (r2 HIGH)

Sources: sheldon:a695cdba9afbc22ec071980e466c7bee62a5cafa;snowdrop:a4bf52735d042cc4d3261d8974363767b398505e;turnerbook:3675e6b9bf5df0fad7bada86bdfd0ba83da3afbf.

Reason: Drop the whole open-parent exception chain. C3 found zero completions that needed it. Preserve the separate bookkeeping flag used by the busy-worker logic. Sources: research/hermes-patches/MASTER-changes.tsv:110; work/hermes-union/C3/parent-gate-use.md:7

## C2-108: redtape ranks 1-2: allow-list parent statuses; triage/review/running never released by rehome or merged PR (r3 HIGH)

Sources: sheldon:a24d0fb8ef7e5c216aec97e584e500e6be8bfac4;snowdrop:b4f591399876b18fda1db1adf02007ba0ede8971;turnerbook:e5ffd8c1bd609b2b84933105549f05e46e7a813b.

Reason: Drop the whole open-parent exception chain. C3 found zero completions that needed it. Preserve the separate bookkeeping flag used by the busy-worker logic. Sources: research/hermes-patches/MASTER-changes.tsv:111; work/hermes-union/C3/parent-gate-use.md:7

## C2-109: redtape: a parent with open prerequisites is never released by rehome or merged PR (r4 HIGH dependency_wait)

Sources: sheldon:07292b4ac86d854ddec41c6bac25876e729f0562;snowdrop:1e11d496222384582243b2206f5e64fdcb22d6fa;turnerbook:72d33a66b867f7fd3c7e8087af717888d22a0441.

Reason: Drop the whole open-parent exception chain. C3 found zero completions that needed it. Preserve the separate bookkeeping flag used by the busy-worker logic. Sources: research/hermes-patches/MASTER-changes.tsv:112; work/hermes-union/C3/parent-gate-use.md:7

## C2-110: redtape: todo human holds (HOLD FOR TURNER, needs_input loop) are never released by rehome or merged PR (v3 port H1)

Sources: sheldon:7e5befd8f497c4b5a17fef8f078af6aaa131e971.

Reason: Drop the whole open-parent exception chain. C3 found zero completions that needed it. Preserve the separate bookkeeping flag used by the busy-worker logic. Sources: research/hermes-patches/MASTER-changes.tsv:113; work/hermes-union/C3/parent-gate-use.md:7

## C2-111: restart observer: recorded PID must serve this home's profile; restart --all foreground settles claim before run_gateway (r6 HIGH x2)

Sources: turnerbook:b2c473ebd6284ce1335a305091c9549ee8af7ef1.

Reason: Drop as a separate history step. Its custom PID check was superseded. Fold foreground restart-claim settlement into the kept restart-budget series g4#G21-G39. Sources: research/hermes-patches/MASTER-changes.tsv:114; research/patch-followup/verdicts-unsure-2.md:39

## C2-113: test(kanban): prove fleet fence isolates Max mirror and host capacity

Sources: max:20fc61690339d823efde7a72dddd7e953da16666.

Reason: Do not replay Max emergency fence and duplicate tests. Use the canonical Sheldon fence. Preserve the claimed-only capacity assertions in tests/hermes_cli/test_kanban_host_cap_mirror_rows.py in that implementation. Sources: research/hermes-patches/MASTER-changes.tsv:116; research/patch-followup/verdicts-unsure-4.md:42

## C2-119: fix(kanban): require explicit unblock for block-loop hold

Sources: turnerbook:4430234eb7ec67c9bc38185157a123ea24e9db60.

Reason: Replace the TurnerBook hold design with Sheldon K14 plus K11. Audited unblock clears a hold while keeping the card in triage. Sources: research/hermes-patches/MASTER-changes.tsv:122; research/patch-followup/verdicts-unsure-5.md:29

## C2-139: Revert "Keep fallback session model and select Codex pool by model"

Sources: max:d604bb2fc31782920a5bd1843bb3680434f0216f.

Reason: Do not replay this superseded or reverted history step. Preserve the final implementation and its assertions. Sources: research/hermes-patches/MASTER-changes.tsv:142

## C2-141: fix(api-server): accept host key for multiplexed profiles, loudly logged (fleet fail-open, Turner 2026-09-28)

Sources: max:e4110216d71f73290547b2145e717253c6057c3e;sheldon:fe4c39765b969f4d9b899ca3359b956a648db503;snowdrop:285586aa28fcdebcf6b81cdda57199b10ae8ebc8;turnerbook:ab77c59d749f4232d5f67b7d720b01c7cf13b8fa.

Reason: Remove host-key acceptance for every named profile, as Lane C decided. Use each profile own authentication. Prior zero-use logs were not rechecked. Sources: research/hermes-patches/MASTER-changes.tsv:144; research/patch-followup/verdicts-unsure-1.md:75

## C2-143: fix(gateway): an inbound bot message does not interrupt a running turn

Sources: turnerbook:335a3fa138f0614038fece3123f046da4731b13d.

Reason: Drop the interrupt-mode special case only after explicit busy_input_mode=queue is set and proven on every relevant profile. Sources: research/hermes-patches/MASTER-changes.tsv:146

## C2-147: restart --all: tolerate one-arg _restart_all stubs (keeps existing host-verbs test green)

Sources: turnerbook:ced48c1f2da2fe40e02b47d38920502eddea2d62.

Reason: Drop the runtime fallback for obsolete one-argument test stubs when consolidating the restart-budget implementation and tests. Sources: research/hermes-patches/MASTER-changes.tsv:150

## C2-151: restart observer: a recorded PID counts only if the live process is still a gateway (r5 HIGH: PID reuse)

Sources: turnerbook:ddde044a0e6c654fd196ab0acfbfa1a0bb4d9ddc.

Reason: Drop the custom recorded-PID check. Keep G39, which uses gateway.status.live_gateway_pid_for_home. Sources: research/hermes-patches/MASTER-changes.tsv:154

## C2-193: docs(update): describe the round-2 carried-commits semantics

Sources: turnerbook:f853845f09d9bd7b9c2c937ec4cbaa2f8f1ca9cf.

Reason: Drop this separate update-guard history step. Keep its final behavior and all safety assertions in the identical four-node update guard, then move that guard to the fleet update command. Sources: research/hermes-patches/MASTER-changes.tsv:199; work/hermes-union/C2/evidence/update-guard-blobs.json:1

## C2-194: fix(update): a branch tip Git cannot read as a commit refuses the update

Sources: turnerbook:3e100c9cb92999a281e0f436563fe57411726ed7.

Reason: Drop this separate update-guard history step. Keep its final behavior and all safety assertions in the identical four-node update guard, then move that guard to the fleet update command. Sources: research/hermes-patches/MASTER-changes.tsv:200; work/hermes-union/C2/evidence/update-guard-blobs.json:1

## C2-195: fix(update): count carried commits without merge-base, against everything origin served

Sources: turnerbook:90a614ba5688c15e8056ef3cec8e0395f4026d4c.

Reason: Drop this separate update-guard history step. Keep its final behavior and all safety assertions in the identical four-node update guard, then move that guard to the fleet update command. Sources: research/hermes-patches/MASTER-changes.tsv:201; work/hermes-union/C2/evidence/update-guard-blobs.json:1

## C2-196: fix(update): decide carried commits before the update touches the checkout

Sources: turnerbook:2409aabf8bcd2b99aab75558eca068cc54f7187d.

Reason: Drop this separate update-guard history step. Keep its final behavior and all safety assertions in the identical four-node update guard, then move that guard to the fleet update command. Sources: research/hermes-patches/MASTER-changes.tsv:202; work/hermes-union/C2/evidence/update-guard-blobs.json:1

## C2-197: fix(update): only a readable config.yaml can authorize the carried-commits reset

Sources: turnerbook:30e696eb29d9785196ced07a48983cf3a7902ba9.

Reason: Drop this separate update-guard history step. Keep its final behavior and all safety assertions in the identical four-node update guard, then move that guard to the fleet update command. Sources: research/hermes-patches/MASTER-changes.tsv:203; work/hermes-union/C2/evidence/update-guard-blobs.json:1

## C2-198: fix(update): refuse to reset away commits carried on the update branch

Sources: turnerbook:a92fef37bb9b22efbf2203094831bfa3c8e3d80b.

Reason: Drop this separate update-guard history step. Keep its final behavior and all safety assertions in the identical four-node update guard, then move that guard to the fleet update command. Sources: research/hermes-patches/MASTER-changes.tsv:204; work/hermes-union/C2/evidence/update-guard-blobs.json:1

## C2-199: fix(update): refuse when Git cannot count the carried commits

Sources: turnerbook:d9056f6a694d6acfe41fdc76593832fff59aadc7.

Reason: Drop this separate update-guard history step. Keep its final behavior and all safety assertions in the identical four-node update guard, then move that guard to the fleet update command. Sources: research/hermes-patches/MASTER-changes.tsv:205; work/hermes-union/C2/evidence/update-guard-blobs.json:1

## C2-200: test(update): RED — a branch tip Git cannot read as a commit must refuse

Sources: turnerbook:d2df234670953bd32a8df296f7e4f628df3120e1.

Reason: Drop this separate update-guard history step. Keep its final behavior and all safety assertions in the identical four-node update guard, then move that guard to the fleet update command. Sources: research/hermes-patches/MASTER-changes.tsv:206; work/hermes-union/C2/evidence/update-guard-blobs.json:1

## C2-201: test(update): RED — a carried-commits count Git cannot make must refuse

Sources: turnerbook:57ed3087466f2d6dd4b74bf33b450f3625f82738.

Reason: Drop this separate update-guard history step. Keep its final behavior and all safety assertions in the identical four-node update guard, then move that guard to the fleet update command. Sources: research/hermes-patches/MASTER-changes.tsv:207; work/hermes-union/C2/evidence/update-guard-blobs.json:1

## C2-202: test(update): RED — an unreadable config.yaml must not authorize the carried-commits reset

Sources: turnerbook:dcce873d1022cfde4c3d17f441f0bbb63294c05c.

Reason: Drop this separate update-guard history step. Keep its final behavior and all safety assertions in the identical four-node update guard, then move that guard to the fleet update command. Sources: research/hermes-patches/MASTER-changes.tsv:208; work/hermes-union/C2/evidence/update-guard-blobs.json:1

## C2-203: test(update): RED — carried commits on the update branch must refuse the reset

Sources: turnerbook:c7efb10b0a4a3893d41fcf930371daf61c063df5.

Reason: Drop this separate update-guard history step. Keep its final behavior and all safety assertions in the identical four-node update guard, then move that guard to the fleet update command. Sources: research/hermes-patches/MASTER-changes.tsv:209; work/hermes-union/C2/evidence/update-guard-blobs.json:1

## C2-204: test(update): RED — nothing in the checkout may change before the carried-commits refusal

Sources: turnerbook:393f31c12fd4ad6949e16840f898d7b53b304f3b.

Reason: Drop this separate update-guard history step. Keep its final behavior and all safety assertions in the identical four-node update guard, then move that guard to the fleet update command. Sources: research/hermes-patches/MASTER-changes.tsv:210; work/hermes-union/C2/evidence/update-guard-blobs.json:1

## C2-205: test(update): RED — orphan history and a failing merge-base are not "nothing carried"

Sources: turnerbook:9fc685ba300ba46d6697a5b8cd140c78bbbb089a.

Reason: Drop this separate update-guard history step. Keep its final behavior and all safety assertions in the identical four-node update guard, then move that guard to the fleet update command. Sources: research/hermes-patches/MASTER-changes.tsv:211; work/hermes-union/C2/evidence/update-guard-blobs.json:1

## C2-207: Revert "carry: use node PyYAML in Codex pool regression"

Sources: max:47ef0232d2e06dd62d22ab7406a7da10127dccda.

Reason: Do not replay this superseded or reverted history step. Preserve the final implementation and its assertions. Sources: research/hermes-patches/MASTER-changes.tsv:216

## C2-208: Revert "test: accept model in auxiliary credential pool fakes"

Sources: max:8e769a11c688abf60422779ed6436c04d7df99cd.

Reason: Do not replay this superseded or reverted history step. Preserve the final implementation and its assertions. Sources: research/hermes-patches/MASTER-changes.tsv:217

## C2-210: carry: use node PyYAML in Codex pool regression

Sources: max:48d7364f144e7afd34a785c79f4ede7eafbf8024;max:bd04db36f6896ad6f8a885edb05ed63612577d05.

Reason: Do not replay this superseded or reverted history step. Preserve the final implementation and its assertions. Sources: research/hermes-patches/MASTER-changes.tsv:219
