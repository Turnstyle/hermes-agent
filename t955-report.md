# t955 integration receipt

Implementation is committed in the requested worktree. No push was made.
The existing snapshot-only selection contract and all 21 existing tests remain intact.

## Delivered behavior

`registry: {"model_ids": ["openai_gpt-6-luna"]}` opts a snapshot policy into
Mission Control reads through the existing fleet_ops CLI. The adapter uses
`HERMES_FLEET_OPS` or the default HOME-anchored fleet-node-state/fleet_ops.py.
It makes exact gets only, with one attempt per ID and a shared two-second
selection budget. On POSIX, timeout cleanup terminates only the process group
created for that lookup, including a gcloud child with inherited output pipes.

Provider and model come from model_id. Capability names retain good_at and
best_fleet_uses strings without interpreting prose as tool support. Numeric
job_type_verdicts in [0,1] are task quality. Public evaluations contribute only
when they specify a task_class, an explicit [0,1] scale, and an in-range score.
The saved Luna record's untested verdicts and 34-point index yield no quality map.

Every converted record is uncertain. Route authentication and usability depend
on the owner-exported fleet_profile_config_v1 node/profile, credential provider
names, model_chain membership, and a fresh observed_at. Registry metadata never
supplies these proofs. The adapter does not refresh timestamps itself.

Fresh advice must explicitly name an uncertain route. For routine registry
selection, absent quality or lookup failure can use that advised profile chain.
Known insufficient quality or capabilities stay excluded. Exact locks cannot
use this relaxation or substitute another route. Failed quota/auth/etc. records
retain the existing newer-proof rule, and policy holds stay sticky.

Selection results include fixed registry_reason codes. Raw stderr, credentials,
and raw registry records are neither persisted nor printed in selection results.
The public select-model command, policy persistence, worker binding, and runtime
fallback all use the selector. No new registry, service, or core tool was added.

## Validation

All tests ran serially in this scratch worktree. The brief explicitly overrides
the root runner rule by requesting the shared venv's pytest command.

- `/Users/sheldon/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/test_kanban_model_registry.py -q --disable-warnings --maxfail=2`
  Before implementation: 2 failed, stopped after 2. Missing adapter.
- The same command with the base selector restored temporarily, adapter present:
  2 failed, stopped after 2. Base rejects registry policy. Changed bytes restored.
- `/Users/sheldon/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/test_kanban_model_registry.py tests/hermes_cli/test_kanban_model_selection.py -q --disable-warnings`
  First implementation run: 34 passed, 1 failed. Sandbox ancestry inspection
  prevented the harness from terminating the test's own timeout child.
- `/Users/sheldon/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/test_kanban_model_registry.py -q --disable-warnings`
  After POSIX child-group cleanup and the documented test-only guard marker:
  14 passed. After public-command/worker-scope/hold cases were added: 20 passed.
- Final combined command above: **41 passed, 0 failed in 20.08s**, comprising
  21 unchanged existing cases and 20 new cases.
- `git diff --check`: passed.

The timeout row explicitly uses live_system_guard_bypass for its disposable
reader/child process group because psutil ancestry inspection is sandboxed.
The fake reader logs attempts; outage cases assert exactly one attempt per
selection. Public-command and worker binding coverage uses isolated homes A→B→A.
The saved fixture is a metadata-only subset of REAL-LOOKUP-openai_gpt-6-luna.json.
Product additions: 161 lines, 7 removed. New test code: 169 lines. Fixture: 39 lines.

## Real reader evidence and limits

The real adapter was invoked for openai_gpt-6-luna with the default fleet_ops path,
using read_model_record and no supplied local route proof. Its printed result is
saved in t955-real-lookup.json:

```json
{"reason": "registry_lookup_failed", "converted_snapshot_record": null}
```

One bounded diagnostic replay also exited 1. Raw stderr was not printed or saved.
The exact underlying cause is unverified. No credentials, reader code, live profile
state, gateway, or plist were edited, and no further retries were made.

A successful live registry conversion remains **UNPROVEN**. Provider authentication,
model usability, actual Jev service advice, quota availability, and model output
were not exercised. Fixtures prove conversion and integration behavior, not live
service health. The profile route proof must still be supplied by its owner.
Windows descendant cleanup and the full repository suite were not verified.

## Files and commits

Implementation commit: `781da981cc`.

- hermes_cli/kanban_model_registry.py
- hermes_cli/kanban_model_selection.py
- tests/hermes_cli/test_kanban_model_registry.py
- tests/hermes_cli/fixtures/model_registry_luna.json

Receipt files: t955-plan.md, t955-real-lookup.json, t955-report.md.

## Fix round 1

The round-1 fixes replace the proof and outage behavior described in the original
receipt above. Base: `21df3f1eb91fec00ac9aed104dc44e9541580aa7`.

- [x] Preserve owner proof and merge registry capabilities and quality.
- [x] Enforce capability and quality requirements during registry outages.
- [x] Resolve the registry once during worker binding.
- [x] Add regression coverage, run the focused suite, and review the diff.

`read_model_record` now returns provider/model identity, capabilities, and quality
only. Resolution deep-copies the owner policy and enriches existing records for
the exact node/profile/provider/model. It retains all owner proof fields and
timestamps, owner uncertainty, and existing capabilities and quality. Capability
names are combined; conflicting numeric quality uses the lower score. Registry
metadata cannot introduce an owner record or upgrade a known low quality score.
Missing, false, stale, foreign, or ambiguous owner proof remains ineligible.

The selector always checks required capabilities and the task-class quality
threshold. `validate_policy` has no default for `minimum_quality`; it requires an
explicit value in [0, 1]. A positive threshold requires numeric quality evidence.
A zero threshold permits absent quality, allowing owner-proven routine work with
no stated requirements to continue during registry failure. Advice still applies
to uncertain owner records and cannot waive proof, capabilities, quality, or holds.
Ineligible routine selection returns `no_verified_eligible_route`, alongside the
fixed registry failure code where applicable.

Worker binding resolves one registry snapshot and uses it for both the decision
and fallback chain, with one timestamp. Subsequent runtime fallback checks still
refresh eligibility through the existing public path.

Earlier fixture expectations were repaired where they encoded forbidden behavior:

- The shared registry policy now supplies a fresh owner model-capabilities proof
  record. Its no-requirement cases explicitly set `minimum_quality=0`.
- Adapter assertions now require absence of proof and uncertainty metadata.
- Numeric and low-quality cases retain a positive threshold. The exact case with
  absent quality also requires 0.8 and remains rejected.
- An exact route with fresh owner proof and no requirements can survive a registry
  outage while preserving its exact identity. Its former rejection depended on
  discarding owner evidence, so that expectation now asserts `exact_model_required`.
- The public command and worker A→B→A fixture updates the owner record's profile
  as well as the config profile and asserts one registry lookup per binding.

Regression evidence:

- New regression cases on the unchanged base selector and adapter: **9 failed,
  2 passed**, with 20 existing cases deselected. Failures showed proof overwrite,
  owner-record replacement, and capability/quality requirements bypassed on outage.
- First combined post-fix run: **52 passed in 19.63s**.
- Final run after adding registry-quality merge and conflicting owner-score cases:
  `/Users/sheldon/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/test_kanban_model_registry.py tests/hermes_cli/test_kanban_model_selection.py -q --disable-warnings`
  **54 passed in 20.81s**. Runs were serial in this scratch worktree, using the
  brief's explicitly requested command. The 21 snapshot-only tests are unchanged.
- `git diff --check`: passed. Changes are confined to the two allowed modules,
  their registry tests, and this report.

Changed test code: +90/-6 lines. Product code: +40/-26 lines. Report: +73 lines.
No new test-only product API or flag was introduced. The two new parametrized
regressions exercise successful fixture reads and missing-reader selection; the
existing worker test also checks the single-lookup contract. The previous tests
allowed fabricated proof and waived requirements, so they missed these bugs.

No live services, Firestore, credentials, or real provider routes were exercised
in this fix round. No push was made. Live route usability and authentication remain
unverified; the full repository suite and Windows cleanup remain unverified.

Commit: the maker sandbox could not write the linked-worktree index lock, so the
King committed the maker's exact four files from the normal shell (a9f3817fb9).
