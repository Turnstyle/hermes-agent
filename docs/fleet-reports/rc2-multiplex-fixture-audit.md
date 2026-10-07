# Missed spawn fixture

The full run reproduced a new failure in the served-profile worker test. It calls the normal spawn path with an unclaimed task. U2c2 now correctly refuses that shape before launch. The test must claim the task first. All environment and toolset assertions remain unchanged. The production admission check remains unchanged. Source: evidence/full/results.jsonl; jev-012.json.

Test-audit answers:
1. The existing test protects the served profile home, settings isolation and toolset pin.
2. A leaked dispatcher setting or wrong profile home must still fail its original assertions.
3. The existing coverage is sufficient. Only its invalid setup changes; no new test or hook is added.
4. No product code exists to serve this test. The setup uses the real claim function.

Red evidence is retained in the initial full run. Green proof is pending the quiet replay and final focused run. No pass is claimed yet.

The full run also found the review-skill test returning None from a fake successful spawn. U2c2 correctly treats that as uncertain. The fixture now returns inert PID 99999999. Every original skill, respawn-guard and status assertion remains. The independent uncertain-handoff tests still require the conservative production behavior. Its full-file green replay is pending. Sources: evidence/full/results.jsonl; jev-018.json.

The continuation found two more invalid setups. The model/provider spawn test now takes a real task claim and preserves all model/provider argv assertions. A wrong model or provider still fails them. Existing coverage owns the contract, so no new test or product hook is added. Source: candidate/tests/plugins/test_kanban_model_override.py:136; jev-021.json:1.

The Chronos rearm test previously replaced run_one_job with False. That return means processing did not complete, while a processed job failure returns True. It now exercises real claim, execution bookkeeping and rearm in a scratch store. Only detached handoff and job execution are stubbed. It retains the rearm assertion and adds stored job and execution failure checks. This catches lost rearm after a failed job and prevents a passing stub from hiding missing persistence. Existing coverage owns the behavior; there is no test-only product code. Source: candidate/tests/plugins/test_chronos_cron.py:205; candidate/cron/scheduler.py:2721; jev-021.json:1.

Both new fixture changes await the queued full-file replays and final focused run. The original red receipts remain in evidence/full/results.jsonl.

The macOS identity fixture now limits all three realpath substitutions to the named fake driver path and delegates every other call, including keyword arguments, to realpath. Retry01 proved the first two corrections with 12 passing cases before exposing the third broad stub. This preserves the driver path and signing assertions while preventing pytest cleanup and the write and home guards from crashing. A wrong app path or signer still fails. Existing coverage is retained, with no new product hook. Final focused must supply a complete candidate receipt. Sources: candidate/tests/tools/test_computer_use_cua_macos_identity.py; evidence/retry01/47a7c1e29ac2.log; jev-023.json; jev-026.json.

Retry01 also showed that the two reasoning-only spawn cases in the model override file had the same invalid unclaimed-task setup as the corrected model/provider case. Both now take real claims and retain every reasoning argv assertion. Product launch admission remains unchanged. Final focused must prove the complete file green. Sources: evidence/retry01/0692d7ef6848.log; candidate/tests/plugins/test_kanban_model_override.py; jev-026.json.
