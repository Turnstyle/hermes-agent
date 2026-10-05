# Board Ops gap continuation

Base786a302d54527d7010f12a8aa783efbc58cac034, same maker thread. Source and tests only in this isolated worktree. Production databases, installed source, gateways, credentials and network are outside scope.

- [x] Read CoS review and verify clean named branch/base.
- [x] Inspect supported grant/request/gateway paths and record confirmed source gaps.
- [x] Demonstrate delayed eligible event refusal with a failing test before changing source.
- [x] Demonstrate absent bounded renewal/new-descendant admission with a failing test.
- [x] Demonstrate absent real-event gateway intake with a failing test.
- [x] Implement narrow bounded authority and delayed-event recovery; preserve security/hold checks.
- [x] Prove finite policies, revocation, dedup, scope, timer intake and zero idle inference in scratch fixtures.
- [x] Run the exact requested four-file proving regression; final results in .boardops-gap-evidence/final-proof.log.
- [x] Attempt one scoped staging operation: exit128, index.lock Operation not permitted. Commit remains unmade; no workaround.
- [x] Save local maker report/receipt/model identity. Requested outside-worktree report delivery also refused: Operation not permitted.

Confirmed before edits: policy.py94-136 enforces explicit task IDs and24h grants; board_ops.py44-48/89-107 reserves issuing immutable grants to owner; gateway/kanban_board_ops.py18-29 only processes manually submitted requests and claims notices; board_ops.py135-172 is request intake. board_ops.py229-230 turns event age>=300s into a refusal after scope/state checks but before the action. None of these paths currently renews or admits later descendants under durable standing authority.

Test-authoring check: these tests protect delayed eligible execution, bounded owner-authorized renewal/new-task admission and real timer intake. Credible bugs are age-only refusal, primary-chat dependency after expiry and missed blocked events. Existing tests assert the old deadline refusal and only manual request intake; no standing authority exists. Tests call supported API/gateway boundaries with the existing fixture board, not new test-only product APIs.

Non-gap clarified: supported owner-profile CLI issuance already needs no model/chat turn. The new authority closes delegated standing renewal/intake, not a fabricated owner-CLI chat requirement. The fresh owner CLI path is tested as existing behavior.

Additional demonstrated dedup correction: a valid manual request occupying a different intake event/operation key now produces a correlation-conflict audit instead of silently suppressing intake. Red in red-correlation.log, green in final proving run.

Remaining custody gaps: normal authorized git commit and copying the preserved local report to the requested lane path. Live adoption/busy-primary/timing/SAME Rescue proof remains outside this source-only continuation. The full original goal is not declared complete.
