# H01: aggregate provider-age budget — candidate only

The controlling bound is end-to-end original-provider age below 1800 seconds on cache return, not a fresh 1800-second residence at every layer.

The shared helper transport enforces at most 1740 seconds, including rejection of older records declaring a longer TTL. The exact helper bypasses the intermediate ordinary 1Password memory/disk cache. Ordinary 1Password uses at most 1740 seconds across memory and disk together, with the same original timestamp on promotion. Its origin starts before the provider loop, not after publication. Future timestamps and excessively slow fetches fail closed. The age-budget-v2 cache-key namespace ignores legacy ordinary entries whose timestamps had the old publication-time semantics.

The loader adds at most 60 seconds across routed hydration, process-global applied-home guards, snapshots, and dotenv restoration together. These are one clock, not independent windows. Both budgets start before resolution; no cache hit, promotion, or publication resets the originating timestamp. Thus a nearly expired source result cannot receive more than 60 seconds of outer residence: source age <1740 plus outer residence <60 gives aggregate age <1800. At exactly either boundary the relevant cache expires.

Expiry clears snapshots, restore state, and applied-home flags. Only still-matching process writes are peeled to their pre-source baseline; external replacements remain. Failure cannot reuse our expired process write as a non-overriding source value. Per-home locking, reset, concurrency, default desktop/delegated guards, explicit literal helper opt-in and profile-local environment remain unchanged. Fresh outer hits do not read configuration or providers. Source cache hits do not read the actual provider.

Run from the worktree root:

    python3 -B scripts/fleet/context7_cache/loader/build_overlay.py
    python3 -B scripts/fleet/context7_cache/loader/build_env_overlay.py
    python3 -B scripts/fleet/context7_cache/loader/test_overlay.py
    python3 -B scripts/fleet/context7_cache/loader/test_env_overlay.py
    python3 -B scripts/fleet/context7_cache/loader/test_stacked_age.py

The stacked regression composes the production shared transport, exact-source 1Password overlay and exact-source env-loader AST harness. It fills the source at 10000, joins a cold loader at 11739, confirms another hit at 11798 without provider reads, then checks original-provider age exactly 1800 at 11800. It covers routed/global/dotenv paths, ordinary memory and disk promotion, shared-helper hits, and failed refresh. All clocks, provider outputs and environments are synthetic; no installed package startup or credentials are used.

Both narrow patches are generated against the hashed live source without installation. Rehash the live bases before any separately authorized deployment. The source/candidate/patch hashes are in source_manifest.json and env_loader_manifest.json. This correction changes both patches; older manifests and old-head verdicts do not authorize this head.

Limits: expiry is enforced on access, not background erasure. Already returned dicts, contextvars session copies, or child environments cannot be retroactively revoked by this loader. Third-party helpers with undisclosed independent caches are outside this two-path contract. Physical legacy-file cleanup remains deployment work. No live writes, merge, deployment, restart, communications or credential operations are part of these author checks. Independent deputy re-review is still required. Native Jev startup already failed in the parent; do not retry that credential path or invent advice.
