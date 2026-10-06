# DROP history with retained final behavior

C2-003 and C2-023 are history-step drops, not removals of their later accepted contracts. The final U1 reasons expressly preserve accurate Supermemory guard documentation and recipient-admitted, create-only busy queuing. Reverting the old commits would restore false documentation and remove retained queue behavior. No reverse patch is applied. The original commits remain in Sheldon ancestry, but are not imported as independent union changes.

C2-003 keeper: plugins/memory/supermemory/__init__.py::_drop_inherited_key and test_recheck_reads_a_real_bound_secret_scope in tests/plugins/memory/test_supermemory_guards.py. Its 65-test file passed in attempt2-merge/results.jsonl. This is preserved guard documentation, as FINAL-CHANGES.tsv row C2-003 requires.

C2-023 keeper: tools/bot_mode_dm.py::_run_delivery_locked with recipient admission and delivery identity. tests/tools/test_bot_mode_dm.py (69 passed), test_bot_mode_dm_busy_queue.py (8 passed), and test_bot_live_owner_delivery.py (15 passed) passed in attempt2-merge/results.jsonl. The old unconditional enqueue promise is not the accepted behavior. C2.15 will check non-draining recipients after all variants are integrated.

C2-160 and C2-161 are KEEP-FIX under final U1. The older C2.7 instruction to revert them is superseded. Their finalization/linger tests passed in attempt2-merge and attempt2-merge-fix.

The other seven carried DROP rows were removed before the merge: six parent exception steps in 39398911ce33f2c7fa1ea5faad215c8b29de5bbf and the host-key bypass in 48ea15361f1cf11f81bb8e741ccab4ce51d96e51. The C3 regression and mutation receipts are under C2/u2-evidence.

Jev is unavailable (attempt2-jev-001-answer.json). This decision follows the final U1 reasons directly. Final coverage will distinguish inherited history from live behavior.
