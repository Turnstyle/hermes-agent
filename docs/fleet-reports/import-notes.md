# U2 attempt 2 import notes

Inputs are frozen under u2-evidence/attempt2-inputs with SHA-256 values. Their final table governs changes where older Lane C cards differ. The branch started clean at 786a302d54527d7010f12a8aa783efbc58cac034. Evidence: u2-evidence/attempt2-preflight.txt and attempt2-inputs/hashes.json.

## C3.2, parent-gate chain

Reverted 7e5befd8f4, 07292b4ac8, a24d0fb8ef, a695cdba9a, f07d34aa12 and 93ae5fcd9f without conflict. These are C2-006, C2-088, C2-107 through C2-110. Completion now checks every parent's terminal status before and inside the transaction. Removed the unused rehome/PR gate and unchanged-block history rule. Restored initial blocked-event bookkeeping because the worker still consumes it. Retained protections are tested through complete_task for every open parent status, all-parent closure, blocked creation with and without a parent, and explicit unblock. Existing sticky-block tests retain explicit human-block and circuit-breaker coverage. Test status: all six C3 test files passed (169 tests). Evidence: u2-evidence/attempt2-c3/results.jsonl.

Test authoring: guards parent completion and initial-block event contracts; catches a release based on stale rehome/PR evidence or lost bookkeeping; replaces assertions coupled to a removed module and adds the missing no-parent/unblock cases; no test-only product code.

## C3.3, host-key fallback

Reverted fe4c39765b for C2-141 / g4#G28. Existing profile-isolation tests reproduce the bypass on Sheldon. No new test is needed. Notice: ../C3/G28-notice.md. Test status: 3 profile-isolation tests and 113 API tests passed. Evidence: u2-evidence/attempt2-c3/results.jsonl.

## Input exclusions

Unclassified untracked files and Sheldon's unreviewed dirty agent/credential_pool_admin.py are not imported blindly. Its committed version remains. The user's credential-file restriction prevents reviewing the dirty content in this run. Known classified dirty changes C2-222 and C2-223 remain required imports. Source: README.md:63-68 and nodes-20261006.md:216.

## Jev

The MCP call was rejected because approval policy is never. Requests and analyst decisions are recorded in u2-evidence/attempt2-decisions-001.json. No Jev judgment is claimed.

## C2-024

Source patch applied without conflict. Source: turnerbook 5dd331be1678fbecba0ff996a8aa060a77aa586b. Union commit: e819f6d0259c1c1381eba64b03abc6513b47b7c5. Apply receipt: u2-evidence/imports/C2-024-apply.log. Tests are recorded in the batch and union results.

## C2-096

Source patch applied without conflict. Source: turnerbook 80f08db3f165a2c6bd0ed4108caaadcc84d90d40. Union commit: 188b36a0b574fc28b5836b167e1eaa22244a1b1c. Apply receipt: u2-evidence/imports/C2-096-apply.log. Tests are recorded in the batch and union results.

## C2-129

Source patch applied without conflict. Source: turnerbook 306b5eb849d13f5545664521dae719e475547d62. Union commit: 2e18d5de94df51e037f27b3f63a68df837faf768. Apply receipt: u2-evidence/imports/C2-129-apply.log. Tests are recorded in the batch and union results.

## C2-180

Source patch applied without conflict. Source: turnerbook b997378d040f181979062e0f9424680552a76618. Union commit: 8922b5425badbb13e98b19b580aa8cbfd5a71ec0. Apply receipt: u2-evidence/imports/C2-180-apply.log. Tests are recorded in the batch and union results.

## C2-086

Source patch applied without conflict. Source: turnerbook 324ca2fdc53f8dd80fc8375a6d974e4a453035a2. Union commit: 776aa38fd7339bae8c174db77df33a22c798c5ea. Apply receipt: u2-evidence/imports/C2-086-apply.log. Tests are recorded in the batch and union results.

## C2-217

Source patch applied without conflict. Source: turnerbook 4c37e829dfdda5eb01f4a54218f94f352b468f62. Union commit: f428c62692cdfb3d7c67435aba0eee8c500ecbbd. Apply receipt: u2-evidence/imports/C2-217-apply.log. Tests are recorded in the batch and union results.

## C2-163

Merged the checkpoint field list by retaining receipt_home plus output_log_path, exit_file_path and log_offset_bytes. Both receipt ownership and file-backed recovery remain. Source: turnerbook cf2433fa11b6425ff532c91e32ecf548b2444465. Union commit: ea1b3fe013a5445bdb7db05eaea1b53969289f64. Apply receipt: u2-evidence/imports/C2-163-apply.log. Tests are recorded in the batch and union results.

## C2-051

Source patch applied without conflict. Source: snowdrop ccb0808afada678413ed6ed974ba4481342dcbbb. Union commit: a3e996f62d4ca5dd4a8713290bf3d1dd12e9f742. Apply receipt: u2-evidence/imports/C2-051-apply.log. Tests are recorded in the batch and union results.

## C2-070

Kept the official stale-transcript refusal. Each locale conflict retains only queuedBehindBotMessage from the source; the DROP staleSessionMerged wording is excluded. Backend lease queuing and desktop queued-state handling applied cleanly. Source: turnerbook cf18938180632074ca40655788947eedebb07891. Union commit: 4e92a0d70b120a6acdbaa8dba1ade10168012b27. Apply receipt: u2-evidence/imports/C2-070-apply.log. Tests are recorded in the batch and union results.

## C2-038

Import only desktop relay attestation, queued-sender text and legacy-sender fallback from the mixed Max bundle. Other parts are mapped separately. Source: max c11de793a78eae8eb655893223eaa34f38e3601f. Union commit: 7876ca0c14c86734b2d35292f97e20f7607372a8. Apply receipt: u2-evidence/imports/C2-038-apply.log. Tests are recorded in the batch and union results.

## C2-018

Kept the existing spool method and its shared GATEWAY_DRAINING_REASON constant. Added the missing admission decorator and route wiring so draining session-chat requests reach the spool. Source: turnerbook a252888c4fbd202fc2c293b30663ef6a5505fda3. Union commit: 83ab392facd47e5b586e560d75bb0c14eeabaf22. Apply receipt: u2-evidence/imports/C2-018-apply.log. Tests are recorded in the batch and union results.

## C2-131

Source patch applied without conflict. Source: turnerbook 5127ddaf7259b9550299ba4262cf8994908ef837. Union commit: 88204b260c4311e81339e87ba49bb48da32c0965. Apply receipt: u2-evidence/imports/C2-131-apply.log. Tests are recorded in the batch and union results.

## C2-135

Source patch applied without conflict. Source: turnerbook af2ddd0629ee86fc2b89730af5d281268a5ad701. Union commit: 7ec0c6c94534dbe8e0e4c1a8035687bec68bbdd9. Apply receipt: u2-evidence/imports/C2-135-apply.log. Tests are recorded in the batch and union results.

## C2-137

Source patch applied without conflict. Source: turnerbook 961888544f02f29b695caa314c67db436c3a9b80. Union commit: 7d024d0bf3f28c722195387070a6374f264a59cd. Apply receipt: u2-evidence/imports/C2-137-apply.log. Tests are recorded in the batch and union results.

## C2-146

Port failed-replay spool retention and suppress turn-end drain after lease handoff. Busy-recipient admission is evaluated separately in C2.15. Source: max e078b958678f2e4097546469120ed9d0544acbe3. Union commit: 63929e20bd5f649db7877d11de4bca5c3975329c. Apply receipt: u2-evidence/imports/C2-146-apply.log. Tests are recorded in the batch and union results.

## C2-138,C2-136,C2-134,C2-148,C2-149,C2-152,C2-150

C2.10 final restart-budget batch. Net source range c9249ac397d7^..f3b5de513161 touches only this series. Sources c9249ac397d7,61c0fe5ce35b,d213de305992,56ce599c3059,a75de7cc0e32,b2c473ebd628,b4dcceb96f91,f3b5de513161. The custom PID checker from DROP C2-151 is absent in the final range. Removed the obsolete one-argument TypeError fallback from DROP C2-147. Retained only foreground claim settlement from folded DROP C2-111. Tests and source are imported as one final batch. Source: turnerbook c9249ac397d7. Union commit: e5a8152820422e997323e3ec9466955e5c1f3615. Apply receipt: u2-evidence/imports/C2-138,C2-136,C2-134,C2-148,C2-149,C2-152,C2-150-apply.log. Tests are recorded in the batch and union results.

## C2-118

Import the dispatch guard only. Keep the audited Sheldon hold design; do not import the superseded decomposer design. Source: turnerbook c7876a4ee2196dbbb20269642500355880801167. Union commit: a07c56544834de9ba8dc6c70a5568cef209ac8fd. Apply receipt: u2-evidence/imports/C2-118-apply.log. Tests are recorded in the batch and union results.

## C2-115

Kept the Sheldon explicit do-not-dispatch markers and added the independent active-PR-key helper. No hold or audited-unblock rule was removed. Source: turnerbook 17867615b5c4732390d156316f350812e64efb6f. Union commit: 649fb3f1e35f485eff1faf092fad97eccf7c74ab. Apply receipt: u2-evidence/imports/C2-115-apply.log. Tests are recorded in the batch and union results.

## C2-114

Source patch applied without conflict. Source: turnerbook 716b0dd272242fa6be30fba0d6fe9eb056bcedde. Union commit: af1ab293b1624e8dd8b0ef3b4f72eaa0773071be. Apply receipt: u2-evidence/imports/C2-114-apply.log. Tests are recorded in the batch and union results.

## C2-074

C2.11 part g of the mixed Kanban bundle: parse /proc stat after its last closing parenthesis. Dispatcher and block-comment parts follow as separate reviewed units. Source: turnerbook c9e7438ede5bbeb89d799243897f389c603847de. Union commit: 374e575aae249fd8d30bcd03f6b35e8245c15be8. Apply receipt: u2-evidence/imports/C2-074-apply.log. Tests are recorded in the batch and union results.

## C2-074

C2.11 part e: add BLOCKED comments only after block_task succeeds. Rebuild the source test with fixtures, avoiding import-time live-home paths and module-cache deletion. Source: turnerbook c9e7438ede5bbeb89d799243897f389c603847de. Union commit: 22e2dc02cfbd6070c25748cd0665d84dc5233f6d. Apply receipt: u2-evidence/imports/C2-074-apply.log. Tests are recorded in the batch and union results.

## C2-116

Imported the missing Snowdrop foreign-owner regression. Sheldon already resolves current_node before source_node with its canonical fence; the overlapping production rewrite is not applied. Source: snowdrop 90abe86a68829dd231db32a2ecfe2ec91d57b1d4. Union commit: b68d3736aab4c16bc5a964857afa6b3a74391699. Apply receipt: u2-evidence/imports/C2-116-apply.log. Tests are recorded in the batch and union results.

## C2-154

Merged stdout/stderr routing into the existing reported-linger and bounded-exit signature. Kept the stricter canonical Bot Chat drain gate rather than widening it to any message_agent surface. Retained both time and urllib imports in the test. Source: turnerbook a14ef5f970470d715fee87cb40ddae45e9ebc4b9. Union commit: 9868c9ec9881cab514517891cffd4c9e56224f67. Apply receipt: u2-evidence/imports/C2-154-apply.log. Tests are recorded in the batch and union results.

## C2-074

Ported all four product assertions from the BLOCKED-comment source test. Replaced import-time environment changes and module purges with an isolated_board fixture. The two checks of the old harness itself are replaced by the shared write guard; no product assertion was removed. Source: turnerbook c9e7438ede5bbeb89d799243897f389c603847de. Union commit: 6c2668a89ee609c1c56f847f846e0160eb18cdef. Apply receipt: u2-evidence/imports/C2-074-apply.log. Tests are recorded in the batch and union results.

## C2-074

C2.12 part b: port verified worker-group termination and tracked crash-orphan reaping. Preserve Sheldon fence transactions, profile-busy classification and failure limits. 31 real-process tests passed. Disabling group termination made the grandchild-survival regression fail; restoration passed. Adapted decomposition fixtures to install a real canonical ownership trigger and mapping rather than infer foreign ownership from an id prefix. Source: turnerbook c9e7438ede5bbeb89d799243897f389c603847de. Union commit: d8ba8c2f92e842769c7035d0a72c27c702119f95. Apply receipt: u2-evidence/imports/C2-074-apply.log. Tests are recorded in the batch and union results.

## C2-074

C2.13 parts c and d: add one owner-unavailable event per outage, configurable quiet lanes, reassign reasons, and claim/reclaim refusal output in CLI and watcher. Existing fence and capacity code remains. 13 owner/output cases, 2 CLI passthrough cases and 9 claimed-capacity cases passed. Source: turnerbook c9e7438ede5bbeb89d799243897f389c603847de. Union commit: 9c236238a7d1ec558644e176a897b28414b6f4b3. Apply receipt: u2-evidence/imports/C2-074-apply.log. Tests are recorded in the batch and union results.

## C2-191

Keep Sheldon per-vault enumeration. Bind each service-account password or OTP read to the unique current listed vault and admitted origin; reject missing, duplicate or changed associations before item-get. Replace old cross-vault retry test with exact read-binding checks. Vault 62 tests passed; disabling vault selector causes fake CLI test failure. No real secrets used. Source: turnerbook f2e8604b1d5f1aea62932643b201807ea5d2d649. Union commit: ba9cc7ab2473a9e988974e7f6057677f4701d0c6. Apply receipt: u2-evidence/imports/C2-191-apply.log. Tests are recorded in the batch and union results.

## C2-002

Source patch applied without conflict. Source: turnerbook 5428b806d348e056b653a0e01e3db2bdb6ca1a9b. Union commit: 428df680e7a6851c7afb04f2b61d7528c5598fb1. Apply receipt: u2-evidence/imports/C2-002-apply.log. Tests are recorded in the batch and union results.

## C2-075

Source patch applied without conflict. Source: turnerbook 46019daa06e932f15f80a5328b3d18d09bd7dc67. Union commit: 6805299df2e0d363c64e04954937591fd37c2c2a. Apply receipt: u2-evidence/imports/C2-075-apply.log. Tests are recorded in the batch and union results.

## C2-068

Source patch applied without conflict. Source: turnerbook f8aafd95a748278a23e430c9e3f39d1137ce61b4. Union commit: a4c3e43fa400d569d4fa3c7c4d3f8aaa6d69a8ee. Apply receipt: u2-evidence/imports/C2-068-apply.log. Tests are recorded in the batch and union results.

## C2-095

Source patch applied without conflict. Source: max 7b3ddf2b25440313523e3e4c771b6fc586d6995a. Union commit: 02c04413b3ab42f2a71664892d68cce89456c057. Apply receipt: u2-evidence/imports/C2-095-apply.log. Tests are recorded in the batch and union results.

## C2-076

Source patch applied without conflict. Source: turnerbook 0bccd874d49d4c8039612cf64352c5f0f40395bd. Union commit: e7722f0e5d618b9fe1e6b3f81f70c36fc6d75f46. Apply receipt: u2-evidence/imports/C2-076-apply.log. Tests are recorded in the batch and union results.

## C2-100

Combine placeholder telemetry with owner_unavailable reporting. Keep one owner field and select placeholder versus missing-owner handling without removing canonical ownership checks. Source: turnerbook 8d32dac32d5e7c4aa217e8acd565d2f8bf6f8e90. Union commit: 45f6334535b679f3f5a3f454e5abf50d21a8f1eb. Apply receipt: u2-evidence/imports/C2-100-apply.log. Tests are recorded in the batch and union results.

## C2-101

Source patch applied without conflict. Source: turnerbook 2c02532c43eb318aac1dd0dca6e50428eeee16c1. Union commit: 2ef9c156e58b5c911b04aee800f927f22965bcee. Apply receipt: u2-evidence/imports/C2-101-apply.log. Tests are recorded in the batch and union results.

## C2-100,C2-101

Finish both telemetry conflicts: keep one owner_unavailable field and distinguish placeholder from missing owner. Earlier resolution script stopped at its assertion but the shell continued; the staged-marker check now prevents such a commit. This commit removes the two markers and AST parsing passes. No live code ran. Source: turnerbook 8d32dac32d5e7c4aa217e8acd565d2f8bf6f8e90. Union commit: 61b24462321e65b31a2e783de85159f8ebb99c7d. Apply receipt: u2-evidence/imports/C2-100,C2-101-apply.log. Tests are recorded in the batch and union results.

## C2-103

Keep canonical claim-error isolation, add eligible-work predicate and all protective telemetry tests. Update the watcher predicate as well as its warning text so unrelated holds or successful spawns cannot hide failures. Source: turnerbook 543029a788b10fe6f86c7bc9a0f53e7cfe654bf7. Union commit: f23c2c2cab80d55bb7711a08a566074f8f1383a4. Apply receipt: u2-evidence/imports/C2-103-apply.log. Tests are recorded in the batch and union results.

## C2-104

History proves 0a9d56 precedes 543029 (C2-103). C2-103 resolution already retained the full newer eligible-work predicate and protective tests, so keep those versions. Add the sibling runner-health probe and its tests because the final MOVE-OUT row explicitly retains them in v1. Source: turnerbook 0a9d56b02fffe0314acab52c051a24d3e6ecdd11. Union commit: 1fce7fc03913ac954890b206d1c148953321d5ae. Apply receipt: u2-evidence/imports/C2-104-apply.log. Tests are recorded in the batch and union results.

## C2-105

Source patch applied without conflict. Source: turnerbook 20991ca940d08c68525c05952d2958491d78befd. Union commit: 33bbcf0e0cd9ca16a6be50cbdf146e67bf4fd6d5. Apply receipt: u2-evidence/imports/C2-105-apply.log. Tests are recorded in the batch and union results.

## C2-220

Source patch applied without conflict. Source: turnerbook 2869834bb854d2d6597391f62fc130ae7bcf7c26. Union commit: 2a3cccc8a2fa44a35e8ba058f1925ec8f469a13f. Apply receipt: u2-evidence/imports/C2-220-apply.log. Tests are recorded in the batch and union results.

## C2-221

Source patch applied without conflict. Source: turnerbook 3aa092d8d8080fed3ec36b4781ca70f8013babc7. Union commit: aafca268745c8ce4471578eb117587bd3733e6f7. Apply receipt: u2-evidence/imports/C2-221-apply.log. Tests are recorded in the batch and union results.

## C2-179

Source patch applied without conflict. Source: turnerbook 13e592b2dfea2b546086f57061de1a8643acb6a2. Union commit: 77755283f8119b744f1ef2a8137b5d215b1c4ff2. Apply receipt: u2-evidence/imports/C2-179-apply.log. Tests are recorded in the batch and union results.

## C2-183

Source patch applied without conflict. Source: turnerbook f7317fff764a60b2ad669cf9cfe5d085455e5937. Union commit: 3b6b7978ff8337d56653152b00d46292f647816e. Apply receipt: u2-evidence/imports/C2-183-apply.log. Tests are recorded in the batch and union results.

## C2-222

Import the final named service-account source from TurnerBook; explicit fallback account binding follows before validation. Source: max 4ebc79ea08a6d08219d01b6362a9c2a27e3f90e2. Union commit: 0cccb3b2d28f09370fccc2e2ca294d46f7cd0221. Apply receipt: u2-evidence/imports/C2-222-apply.log. Tests are recorded in the batch and union results.

## C2-222

Complete U1 named-account fix: gcloud fallback binds the fleet service account, strips ADC and Python environment selectors, and removes stale ADC messages. Fake tests reject ADC calls and verify exact account argv and child environment. No real gcloud or key source is used during this build. Source: max 4ebc79ea08a6d08219d01b6362a9c2a27e3f90e2. Union commit: c022dea619d361683ec2cae4c8b93fbe2fb0a00e. Apply receipt: u2-evidence/imports/C2-222-apply.log. Tests are recorded in the batch and union results.

## C2-223

Apply verified TurnerBook dirty patch sha256 8dc3fcb93f2fa3dc5a9f2d348475018bea7ebe3ff6109f898c68c99c56c537da. Canonical current/source owner and tenant are checked before orphan PID probes or writes and before nonlocal stale-claim recovery. All 25 recovery-ownership tests passed, including unchanged foreign/unknown rows, no foreign PID probe, local recovery, and enforced local lifecycle fence. Source: turnerbook 4ebc79ea08a6d08219d01b6362a9c2a27e3f90e2. Union commit: 53e174c5cfcc18b75c3159e592fec8b44de6e1fa. Apply receipt: u2-evidence/imports/C2-223-apply.log. Tests are recorded in the batch and union results.

## C2-103,C2-104,C2-105

Three focused tests exposed the CLI daemon still using the older any-hold/any-spawn suppression predicate. Port the final source predicate: count_spawnable_ready and eligible_work_stalled, matching gateway behavior. Warning throttle and test assertions are unchanged. Source: turnerbook 543029a788b10fe6f86c7bc9a0f53e7cfe654bf7. Union commit: 6bc102485cd3e501d90bc162a16cddba09314a34. Apply receipt: u2-evidence/imports/C2-103,C2-104,C2-105-apply.log. Tests are recorded in the batch and union results.

## C2-154

The short-lived drain test raced its 1.5-second child sleep against interpreter imports. Use a fixture release file: child cannot finish until the already-exited driver is observed. The same real child completion and busy-clear assertions remain. Child fixture timeout bounds cleanup; all temporary paths are under TMPDIR. Source: turnerbook a14ef5f970470d715fee87cb40ddae45e9ebc4b9. Union commit: 4850ed193aadeeba71448f765a8690a5d855970b. Apply receipt: u2-evidence/imports/C2-154-apply.log. Tests are recorded in the batch and union results.

## C2-219

Carry merge-only native-home discrimination in PM. Queue envelope identity, create-only enqueue, sender normalization, and launcher admission are already in Sheldon or earlier kept imports; their tests remain. Source: turnerbook 712bd5c4604cd284554c720c4bcc3cdfd6c55863. Union commit: a728a4c5ddbfb5ae50f1cb4c2fae971113296000. Apply receipt: u2-evidence/imports/C2-219-apply.log. Tests are recorded in the batch and union results.

## C2-049

Sheldon already queues one create-only expiry notice and suppresses notification loops. Preserve its system sender and deterministic ID. Add the remaining TurnerBook behavior: stale delivered/read expiry stores a sender-visible notice and accurately says a previous turn may have received the message, instead of claiming no delivery. Test checks both status lookup and queued notice. Source: turnerbook 78442e94faf3286b66e5e336bf8d075e4d2a80be. Union commit: 7678d46b047d1afc88fc3b5d2f86105d917f7f9a. Apply receipt: u2-evidence/imports/C2-049-apply.log. Tests are recorded in the batch and union results.

## C2-219

Add runtime layout checks for the merge-only native-home fix: both ordinary and patched Path.home ignore a native home manifest; a sealed payload still selects its declared store. Uses only fixture manifests under WORK. Source: turnerbook 712bd5c4604cd284554c720c4bcc3cdfd6c55863. Union commit: a2af6167d6a8e7d6f883658e76b32d05582b4740. Apply receipt: u2-evidence/imports/C2-219-apply.log. Tests are recorded in the batch and union results.

## C2-053 — already covered

All changed production functions are AST-identical to the accepted source, including opted-in surfaces and the strict canonical Bot Chat drain gate. Evidence: u2-evidence/attempt2-semantic-comparison.json and the final union test receipts.

## C2-072 — already covered

Canonical Sheldon lifecycle fence and per-row refusal isolation remain. Its node parser matches the Snowdrop source. Later C2-223 adds recovery ownership before PID probing. Existing lifecycle/recompute tests cover the retained behavior. Evidence: u2-evidence/attempt2-semantic-comparison.json and the final union test receipts.

## C2-097 — already covered

Claimed-running counting and capacity-held reporting are already in Sheldon. count_running_tasks and _note_capacity_held match the accepted source. Cross-board count preserves dry-run read-only opens. Existing capacity and mirror-row tests remain. Evidence: u2-evidence/attempt2-semantic-comparison.json and the final union test receipts.

## C2-112 — already covered

The complete source patch reverse-applies to the union, proving its dry-run orphan assertions are already present. Evidence: u2-evidence/attempt2-semantic-comparison.json and the final union test receipts.

## C2-178 — already covered

The union has the stronger Sheldon document-provenance filter: include parent documents, inspect parent IDs and custom IDs, and reject missing provenance when a quarantine exists. This retains the parent-document quarantine intent. Evidence: u2-evidence/attempt2-semantic-comparison.json and the final union test receipts.

## C2-184 — already covered

The complete source patch reverse-applies to the union. The file-backed systemd wrapper assertions are already present. Evidence: u2-evidence/attempt2-semantic-comparison.json and the final union test receipts.

## C2-190 — already covered

The complete accepted Snowdrop source patch reverse-applies to the union. Its runtime functions and protection tests remain. No duplicate bulk patch is needed. Evidence: u2-evidence/attempt2-semantic-comparison.json and the final union test receipts.

## C2-218 — already covered

The relay computes SESSION_NOT_OWNED from the final process after any provider retry. Both final-proc and target-profile regression tests already exist and passed in attempt2-busy. The desktop relay portion was imported under C2-038. Evidence: u2-evidence/attempt2-semantic-comparison.json and the final union test receipts.

## C2-068,C2-070

Type checking found the carried systemNotice property does not exist on this official ChatMessage type. Official hydration marks backend notices with role system; use that representation and test it. Supply the required refreshSessions fixture callback in the busy-send test. Source: turnerbook f8aafd95a748278a23e430c9e3f39d1137ce61b4. Union commit: bf0bb184da125a6099a75c90418d4aeb4a72b1ec. Apply receipt: u2-evidence/imports/C2-068,C2-070-apply.log. Tests are recorded in the batch and union results.

### C2-137 merge variant and C2.20 test repairs

C2-137 source a463730a6dc8425e59861d628e37295f804ef067 merges the imported 961888544f02f29b695caa314c67db436c3a9b80 and has an empty combined diff. No second replay is needed. Source: u2-evidence/attempt2-c137-merge-variant.json.

Commit 6fb1582c54281c6473cf2b130ae07ac72dcd7792 fixes test isolation and setup only. The agent home is repinned per test, the short-deadline test loads the agent before measuring time, and the timezone fixture uses the supported TCP RPC path for its real child. Production code is unchanged. Focused reruns: 3 reasoning-streak, 12 live fallback, 3 primary cooldown, 20 tool-deadline, 1 cron execution, 15 cron watchdog, 11 timezone and 2 logging-isolation tests pass. The two remaining files have the same four failing cases and assertion text as baseline. Source: u2-evidence/attempt2-union-fix/results.jsonl and attempt2-test-comparison.json. The full run is continuing.
