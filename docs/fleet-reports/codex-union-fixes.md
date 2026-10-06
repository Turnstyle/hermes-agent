# C2.22 finding dispositions

No new HIGH finding remains unresolved in the bounded builder review. See codex-union.md:3 for scope and independence limits.

| Finding | Disposition |
|---|---|
| New fixture failures | Fixed in 6fb1582c54281c6473cf2b130ae07ac72dcd7792 and 9cfe45f5623b9f305a923ef4eecd5d195e07375c. C2.20-fixes.md records each unchanged behavior assertion and passing rerun. |
| Renamed baseline cases | Comparison-only mapping for two identical methods; raw IDs kept. u2-evidence/attempt2-test-case-rename.json:1 and scripts/u2_compare_tests.py:10. |
| Signal stop exits 1 | Kept. The scratch gateway completed cleanup and intentionally used its existing service-manager exit policy. u2-evidence/smoke/gateway.log, final 15 lines. |
| Whitespace and marker warning | Kept. Imported formatting and a deliberate test string have no demonstrated runtime defect. u2-evidence/attempt2-net-diff-check.txt:1 and repo/tests/test_audit_old_updater_imports.py:217. |
| C2-143 configuration | Open rollout prerequisite. No live config write is authorized in U2. C2-143-config-risk.md:3. |
| 42 baseline failures and platform limits | Open and listed, not counted as passed. The specified C2.20 zero-new-failure gate passes. u2-evidence/attempt2-test-totals.json:5. |
| Independent review and Jev | Not obtained. Work-alone instruction excludes a child checker. Jev's automatic approval policy refused all calls. JEV-ANSWERS.md. |
