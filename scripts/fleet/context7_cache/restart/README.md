# Exact-live coordinated restart overlay — review only

## Candidate outcome

Adds explicit `--no-notices` to **the same installed coordinated adapter**, alongside PR10's supported `gateway restart --yes` argv change. Default notice behavior remains opt-out, not silently disabled. All artifacts and edits stay in this directory; the installed adapter is never edited or imported.

The option is available on both `request` and `run`. It is saved in the request record, survives the request-to-coordinator handoff, and reaches ordinary and retained-pause coordination. Running an existing request with the option may only tighten its notice policy; omitting the option cannot re-enable a stored suppression policy. A join with a different notice policy is refused before changing the existing request, rather than falsely promising silence or changing another request's policy.

### Suppressed when explicitly selected

- Initial local notice publication, card comments and run steers.
- Deadline checkpoint **board comments**, including the independent direct subprocess path in `deadline_checkpoint` (not only the normal comment helper).
- Back-online notice publication and card comments.
- All coordinator Bot Chat wakes, including custom wake routes and fallback wake behavior.
- Optional requester `notify_cmd`, even when configured.
- Notice-only API key loading in the ordinary flow.

### Preserved

- Dispatch pause acquisition, barriers/checks, drain markers, cancellation, ownership and cleanup.
- Read-only work census and repeated fresh reads, including unreadable/unknown census refusal before the deadline.
- The installed checkpoint/deadline continuation policy, including checkpoint census, request bookkeeping and explicit empty posted-card evidence under suppression.
- Existing permission/ownership gates, retained manifests, auxiliary fencing, callback order, release behavior and locks.
- The same bounded default-profile restart with `--yes`, inherited child context and native guard. No `--force`, `--all`, bare restart retry or new fallback is added.
- Post-restart new/alive PID, `/health`, gateway state, platform/profile and heartbeat verification.
- Local logs, request/outcome records and Hermes' **native** interrupted-session recovery. This flag is not a promise to suppress Hermes-native recovery or arbitrary operator callbacks; it suppresses this coordinator's notice routes. It does not authorize any operation.

## Exact provenance and narrow scope

Installed source (read only):
`/Users/sheldon/.hermes/fleet-node-state/u3g/g3f/gateway_coordinated_restart.py`

Pinned SHA-256:
`df56ebff6a48c22b1a698d491da4195e2fe54d2d19f8df0133b31654eac3d3a8`

`build_overlay.py` requires that full hash and unique exact source anchors. It produces a narrow zero-context unified patch, **not a full older replacement file**. The full resulting source hash, changed-function allowlist, patch hash, fixture hash and evidence hash are in `manifest.json`. Bytes outside the listed functions are checked unchanged by the builder; the verifier independently checks the exact overlay/diff and outside-function AST. Zero context avoids representing blank context lines as trailing-whitespace additions in this patch artifact; deployment tooling would need zero-context support plus the mandatory exact full-source hash gate. No applier is included.

`source-evidence.json` contains source-only excerpts and full hashes for notice/checkpoint paths, request persistence, both coordination flows, installed CLI parser and lifecycle guard, and launcher provenance. The parser supports `-y`, `--yes`, `--interrupt` as `args.interrupt`: explicit consent to interrupt active work, not a guarantee of graceful completion. CLI `--force` has a different separate-gateway meaning and is not added. Selected `hermes_bin()` must still be revalidated by an authorized deployment owner.

**Existing policy/residuals, not changed or endorsed:** the installed adapter permits deadline continuation after a busy or unknown fresh census. Before the deadline it still refuses; suppression does not grant new continuation permission. The installed `run_restart` has a coordinator-force service-manager fallback for `Restart budget blocked`. This patch neither requests nor expands it; tests exercise it only through mocks. Arbitrary switch/auxiliary callbacks retain their existing semantics, so their independent outbound effects are not made safe by this flag.

## Offline verification

From the supplied worktree:

```sh
python3 -B scripts/fleet/context7_cache/restart/build_overlay.py
python3 -B scripts/fleet/context7_cache/restart/verify.py
```

The suite executes selected functions extracted through AST, never the adapter module or its imports. Every process, network, clock/sleep and operational file mutation reached by these tests is fake. Tests cover ordinary and retained coordination, explicit parser defaults, request persistence/join conflicts, direct deadline comments, helper suppression, preserved census/checkpoint/drain/lock/callback/restart/health paths, new-PID and health rejection, positive default notice controls, supported CLI consent, timeout/failure handling and preserved delegated-child environment. `test-result.json` records actual output; `artifact-hashes.json` inventories this package excluding itself. Source hash is checked again after tests.

No live request, status/release operation, restart, board mutation, message, deployment, commit, push or merge is performed. Tests do not prove live service health. Concurrent edits outside this directory belong to other workstreams and are not part of this candidate.

## Review and identity

Review readiness only; no self-approval or deployment authority. `jev-request.json`, `jev-result.json` and `prepare_jev_review.py` are historical artifacts for the earlier argv-only candidate, **not review evidence for this revised overlay**. No Jev retry, credential inspection or credential repair is performed; advice remains missing. Do not run the historical preparation script as a review of the revised candidate.

Runtime context identifies this writer as `gpt-6.1-sol`, provider `openai-codex`, platform `subagent`, inherited subscription only. That is inherited runtime identity evidence, not an independently queried billing/provider attestation. No additional model, provider or credential route was invoked.

An accountable owner must independently review the candidate, revalidate the exact live source and selected executable, and obtain separate operational authorization before any installation or restart.
