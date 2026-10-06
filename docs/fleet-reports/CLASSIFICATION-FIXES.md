# U1 checker fixes applied by U2

Recorded 2026-10-06 16:08:22 CDT.

The Codex U1 checker gave PARTLY and named one finding. C2-073 contains local test assertions not proven by the official regression. U2 reclassified it from UPSTREAM-HAS to KEEP-FIX. Counts are now 122 KEEP-FIX, 60 MOVE-OUT, 41 DROP, zero UPSTREAM-HAS, 223 rows. The corrected table, README and lane deltas were written after U1-work/rc returned 0.

The named keeper is tests/hermes_cli/test_kanban_blocked_sticky.py::test_created_with_initial_status_blocked_is_not_promoted_by_recompute_ready. Its with_parent=False case checks blocked creation, three recompute passes, no promoted event, explicit unblock and ready status. It also retains the bookkeeping-event assertion. Both cases passed. Removing the sticky guard made both cases fail with ready instead of blocked; restoring the exact source bytes made both pass. Evidence: u2-evidence/attempt2-sticky-mutation/results.jsonl, attempt2-sticky-restored/results.jsonl and attempt2-sticky-preservation.json.

Sources: exec/work/hermes-goal/U1/chk-codex-gpt-6.1-sol/verdict.json, its carry-test.txt:442-469; u2-evidence/attempt2-effective-counts.json. Original inputs and hashes are preserved in u2-evidence/attempt2-inputs.

The Gemini checker returned PASS without named fixes. The Sheldon bot checker rejected the assignment as outside its repository authority; its FAIL explicitly does not judge U1. Copies of all three verdicts are under u2-evidence/attempt2-u1-chk-*-verdict.json.

Jev remains unavailable under this session's approval policy. This correction directly implements the named checker fix.
