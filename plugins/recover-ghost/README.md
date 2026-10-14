# recover-ghost

Optional governed recovery of a claimless running Kanban attempt. Extracted for
t_18523f0d / P8 / C2-085 from published source
691854aceaffc0127ac9b21a3aae8566eff2dde0.

Enable via the existing plugin setting (`plugins.enabled` includes
`recover-ghost`), or `hermes plugins enable recover-ghost`. This source change
alone does not enable or install the plugin on any fleet machine.

Both CLI spellings use the enabled plugin:

- `hermes recover-ghost TASK --run RUN` (new registered plugin command)
- `hermes kanban --board BOARD recover-ghost TASK --run RUN` (existing spelling)

Dry-run is the default. `--apply` revalidates under the SQLite writer lock.
`--expect` checks the dry-run fingerprint. `--done` requires `--evidence`.
Fleet-mapped attempts retain the existing sync handoff, owner and lease checks;
there is no new `--fleet` opt-in or C5 cutover here. The installed Fleet-owner
helper fallback and bounded read-only WAL open remain intact. Delegated children
are refused before any recovery probe on either command path. Disabled or failed
plugin discovery refuses the legacy spelling without initializing a board.

Compatibility boundary: Python callers must no longer import the removed
`hermes_cli.kanban_recover_ghost` module. Repository caller search found only the
migrated invariant test. Historical fleet ledgers retain their original paths.
The small nested CLI shim and parser remain in core to preserve command spelling;
all recovery policy and SQL now live in the plugin. No new upstream hook is
needed for this row.

Verification uses temporary local boards and a mocked process-marker probe.
It exercises dry-run/no writes, a successful recovery and idempotent repeat,
live claimant refusal, stale fingerprint refusal, Fleet-owner fallback/fail-closed,
real plugin manifest discovery and opt-in/disable gates, and delegated-child
refusal. It does not inspect host process environments or recover live cards.
Run the single focused file `tests/hermes_cli/test_kanban_recover_ghost.py`.
Adjacent WAL proof: `tests/hermes_cli/test_kanban_ro_open_wal.py`.

Reuse: historical Max C4.13 supplies the plugin registration approach. Its
implementation is not copied wholesale because it changes Fleet opt-in and
recovery semantics. The admitted implementation is moved with its safety policy
unchanged, plus the top-level child-context guard.
