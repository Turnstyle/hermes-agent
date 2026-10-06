# Coverage scope

Candidate source: 9cfe45f5623b9f305a923ef4eecd5d195e07375c. 223 classified rows. 182 retained rows present; 41 DROP replay units absent. Source: coverage.tsv and u2-evidence/attempt2-coverage-source.json. This table distinguishes retained behavior from historical ancestry: DROP wording/duplicate steps may remain ancestors, while their explicitly named final keeper remains. See drop-kept.md.

Every non-Sheldon row has an import receipt or a semantic comparison. Inherited rows are tied to their Sheldon source and final blob identities; the merge conflict notes and final union test receipts establish the changed implementation. Test results are separate from source presence. The full official upstream suite and live fleet behavior are not claimed.

Absent historical test paths: cron token/job budgets belong to DROP C2-064/065. redtape_parent_and_promote was replaced by strict open-parent completion tests. kanban_fleet_mirror_protection_l1 belongs to the rejected emergency fence; canonical lifecycle, ownership and claimed-capacity assertions remain. kanban_decompose_block_loop_hold belongs to the rejected TurnerBook hold design; the Sheldon hold tests and imported dispatch guard remain. kanban_worker_hard_exit was replaced in kept C2-215 by test_single_query_hard_exit.py, which covers all one-shot runs. These absent paths are listed rather than counted as passes.

C2-143 is absent from the source but its live configuration prerequisite remains unproven. See C2-143-config-risk.md.
