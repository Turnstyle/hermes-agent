# Fleet Hermes union rc2

Source release candidate built from rc1 plus U2c, the U2c2 double-launch fix, U2d and two explicitly required carry repairs. No fleet service or installed checkout was switched by this build.

Branch: `fleet/0.21.5-union`. Tag: `fleet/0.21.5-union-rc2`. Fork: `https://github.com/Turnstyle/hermes-agent.git`.

The rc1 parent is `5cd04a99de76bf42dc0bcdb17e11274ca1e1e8fa`. The upstream base remains `35ad70c035dec839846056073ad81a9bdceffca4`, including v0.21.5 release `v2026.9.24`. Resolve the rc2 tag to obtain the final merge commit. This report was built at 2026-10-06 23:17:15 CDT, from source commit `c78997cdb3b793d278f952c81b7cc7156ef5c315`.

## Carried changes

[FINAL-CHANGES-rc2.tsv](docs/fleet-reports/FINAL-CHANGES-rc2.tsv) has 261 classification rows: 130 KEEP-FIX, 65 MOVE-OUT and 66 DROP. Rows can describe overlapping source behavior or host-local preservation; they are not counts of unique code patches. Same-id corrections replace earlier rows. The original rc1 table remains historical.

U2c adds shared launch admission and stall-aware delivery. U2c2 commits handoff intent before spawning, retains uncertain handoffs, and registers the default worker before PID persistence. U2d source-selection tests now run with the required credential modules present. Sheldon's initial profile credential claim is retained and tested.

C2-143 is KEEP-FIX in rc2. B9 proves explicit queue on 80 of 84 profiles and does not prove effective launch settings on any machine. The restored bot-message protection prevents an inbound bot from interrupting standing work. The human-interrupt control is retained. Source: [B9 proof](docs/fleet-reports/rc2-b9-c143-proof.json).

C2-223 claims preserved behavior through 28 passing ownership tests on U2c2 commit `01f37c457964af97c80d2724680fc2e7c95550bd`. Both product modules changed; no whole-file byte-identity claim is made. All four U2c rebased patches match their originals. Source: [rebase proof](docs/fleet-reports/rc2-rebase-preservation.txt).

## Tests

The canonical Python discovery scope is 5117 files. The same isolated runner executes every failing file against rc1 and compares exact failing test IDs. New failed cases: 0. Waived by the executor under the owner's speed rule (visible list, code-identity proof and Jev receipts in rc2-waivers.json): 1 failed cases and 2 files whose rc1 controls were cut short by host load. Missing files: 0. Pending controls: 0. Incomplete files: 0.

Files with no pytest cases collected on either ref: 2. Their source hashes match. Canonical runner semantics tolerate these files; they receive no passing-test credit. The exact files and both raw exit codes remain in the comparison receipt.

Final pytest totals are 52890 passed, 577 failed, 330 errors, 667 skipped, 1 expected failures and 0 unexpected passes. Separately, 39 subtests passed and 0 subtests failed. Event counts reconcile with pytest summaries; subtests and expected failures retain separate categories in the receipt. Totals include the two independent U2c2 handoff probes outside canonical discovery. These failures reproduce on rc1 under the same test constraints. They are not passes. ADC environment exports and real ADC discovery are refused by the test guard; affected behavior is not checked and its restriction receipts are listed separately. This larger scope discovers baseline failures outside rc1's earlier 332 selected files. See [comparison](docs/fleet-reports/rc2-test-comparison.json) for every exact failing test and the method.

The 124 canonical exclusions are integration, e2e and Docker jobs requiring their own environments. They are not checked. Tests use nice 10, separate scratch homes, an empty inherited credential environment, and bounded load. The wrapper is adapted because the repository shell runner discards TMPDIR and uses unbounded precompilation. One bounded precompile process warmed valid caches without changing tracked source.

Scratch smoke checks health, scratch Kanban create and dry-run dispatch, and an actual Bot Chat reply from a deterministic local provider. It is not proof of a real provider or live fleet behavior. Final commit smoke and publication receipts are in the U2e result.

## Rollout limits

This is a source release. Live update, rollback, macOS and aarch64 execution are not checked here. B9's other rollout conditions remain separate gates. Restoring C2-143 does not clear those gates. Preserve all dirty work and private runtime data before switching any machine.

The retained source and host-local preservation rows are in [CARRIED-STILL.md](CARRIED-STILL.md). MOVE-OUT does not authorize deleting a source or private artifact before its replacement or archive is verified. No SOUL file or session export is added by rc2.
