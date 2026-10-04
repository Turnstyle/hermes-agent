# t955 integration plan

Implement in this worktree only. No push or changes to live Hermes, gateways,
credentials, plists, or other profiles.

- [x] Inspect brief, selector, tests, and existing fleet reader protocol.
- [x] Add fixture-based regression tests and prove they fail on the base.
- [x] Add exact-get subprocess adapter with one attempt and a short timeout.
- [x] Wire opt-in registry policy into selection and worker fallback.
- [x] Preserve exact locks, freshness, owner scope, and failed-route holds.
- [x] Run existing and new tests using the brief's explicit pytest command.
- [x] Perform one real read-only model lookup and report metadata only.
- [x] Review changes, record limitations, and commit without pushing.

Policy contract: `registry: {model_ids: [exact_document_id, ...]}` opts in.
A registry policy still carries the canonical snapshot for owner-exported local
route proof and outage fallback. The adapter reads model records by exact ID;
it never enumerates a registry or probes a provider. Registry records cannot
supply route authentication. Numeric task quality is retained only when its
scale is explicit and within [0, 1]. Missing quality requires fresh advisory
approval of the route, and is not converted into a numeric score. Known low
quality and known missing capabilities cannot be overridden by advice. Exact
locks retain the existing mandatory numeric-quality eligibility semantics.

Live lookup was attempted through the real adapter and returned
`registry_lookup_failed` with no converted record. Live conversion and provider
authentication remain unproven; no fixture is labeled as live evidence.
