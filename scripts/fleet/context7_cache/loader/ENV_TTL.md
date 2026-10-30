# Incremental env_loader TTL correction — candidate only

`env_loader.patch` is a narrow overlay against the same captured exact-live `hermes_cli/env_loader.py` as the original 1Password lane. It is **not applied** to the live tree or the older branch module. Apply it only as a separately authorized integration step after checking `env_loader_manifest.json`. The original `onepassword.patch` remains unchanged and is disjoint.

## Behavior

- Applied-home guards, per-home external-secret snapshots, and authoritative dotenv restore share a monotonic 1800-second maximum lifetime. Age begins before source resolution; cache hits and publication do not restart it. At exactly 1800, or a negative clock delta, state is invalid.
- Getters return an empty snapshot after expiry, without fetching or consulting ambient credentials. The next hydration/global apply resolves again. Snapshot access, hydration, global apply, and reset serialize on the existing reentrant lock.
- Expiry clears both snapshot maps and the applied-home guard. It peels only still-matching source writes back to their pre-source baseline (or absence), including source writes over dotenv placeholders. Otherwise the old process write could shadow a non-overriding provider indefinitely. Later external owners' replacement values stay untouched.
- A slow resolution consuming the entire lifetime cannot publish a usable snapshot. Global failure guards remain once-per-window; routed hydration failures remain immediately retryable, as before.
- Fresh guard/snapshot hits perform no config, bootstrap-file, or provider reads. Per-home reset and expiry leave siblings' snapshots and clocks alone.
- Desktop/delegated protection, literal `allow_unattended_helper is True`, profile-local token environment, dotenv baseline logic, and process/profile context resolution are unchanged. Tests compare ASTs of those functions in addition to behavioral guard checks. `HERMES_DELEGATED_CHILD_CONTEXT` is never unset; no board path is invoked or modified.

## Reproduce from worktree root

```sh
python3 -B scripts/fleet/context7_cache/loader/build_env_overlay.py
python3 -B scripts/fleet/context7_cache/loader/test_env_overlay.py
python3 -B scripts/fleet/context7_cache/loader/test_overlay.py
patch --dry-run -p1 -d /Users/sheldon/.hermes/releases/stable-v0.22.5-carry-d4c5aec662c8/candidate < scripts/fleet/context7_cache/loader/env_loader.patch
git diff --check -- scripts/fleet/context7_cache/loader
```

The env suite has 20 passing tests; the original 1Password suite has 9. All providers, environments, bootstrap parsers, and worker/session modules in the env suite are synthetic. The dotenv idempotence test uses a synthetic tokenizer with the unchanged loader baseline/publication implementation; no installed python-dotenv is available in the bare Python toolchain. Full application imports/integration are intentionally not attempted because they can bootstrap real secrets. Existing original-suite “residual” tests describe the unchanged live source, not this new candidate.

## Artifacts and limits

- `build_env_overlay.py`: source-hash-checked incremental generator.
- `env_loader.candidate.py`, `env_loader.patch`, `env_loader_manifest.json`: review source, narrow patch, hashes.
- `test_env_overlay.py`: fake-clock boundary, refresh, failure/revocation, concurrency/reset, isolation, startup guard, and dotenv regression tests.
- `env_verification.json`: actual execution receipts and read-only session identity evidence.

Identity was read from this profile's `state.db` for session `20261030_174117_95b924`: model `gpt-6.1-sol`, billing provider `openai-codex`, billing mode `subscription_included`. No new model or child was launched.

This bounds **loader residence age**, not an upstream provider result's undisclosed original fetch age. Provider/transport TTL enforcement remains their own layer's responsibility; no upstream freshness timestamp contract is introduced here. Expiration is checked on access, not by a background plaintext-memory sweeper; already-returned scope objects cannot be revoked by this module. Jev was not retried. No credentials, live writes, restarts, deployment, commits, pushes, messages, or board operations occurred.
