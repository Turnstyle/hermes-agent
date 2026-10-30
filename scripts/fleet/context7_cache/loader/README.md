# Exact-live loader overlays — candidates only

See ENV_TTL.md for the current H01 correction and exact test commands. Earlier residence-only reports are historical, not acceptance evidence for this correction.

The source budget is 1740 seconds; the single outer loader budget is 60 seconds. Ordinary op memory/disk promotion retains provider-start time. Exact pinned helper selection bypasses the intermediate op cache and uses the shared transport's enforced 1740 ceiling. Old longer shared records are rejected. The ordinary age-budget-v2 cache key rejects entries with pre-fix publication-time semantics. No layer resets the source timestamp on a cache hit.

build_overlay.py and build_env_overlay.py read exact live source files without importing Hermes or bootstrapping secrets. They generate narrow onepassword.patch and env_loader.patch, candidate files and source/hash manifests. They never install anything. Use these overlays, not older whole-branch modules; rehash live source before deployment. The guards and metadata/config/coordinator behavior are otherwise unchanged.

Tests are AST isolated with synthetic clocks, providers, environments, and disk fixtures. test_stacked_age.py exercises the actual shared transport plus both exact-source overlays across late-joining routed/global/dotenv paths, ordinary memory and disk promotion, and failed refresh. A fresh snapshot/cache hit must not invoke the actual provider. Default desktop protection, delegated context, profile isolation, concurrency and literal helper opt-in remain tested.

No full application startup, live canary, cleanup, deployment or credentials are used. Expiry on access does not revoke copies already returned to session/contextvars/child environments. Unknown third-party helper caches are not assigned a fabricated original-age contract. Legacy physical-file deletion is separate deployment work. Native Jev advice remains unavailable from the already failed parent route; it is not retried here. Author tests are not independent review PASS.
