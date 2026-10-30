# Exact-live 1Password loader overlay — candidate only

No live installation, config change, provider/op invocation, credential read, or commit performed. All artifacts in this directory. `onepassword.patch` is a narrow unified diff against the exact live source, not a branch replacement. `onepassword.candidate.py` is review material, not an installed module.

## Verified

- `python3 -B build_overlay.py`: source hashes captured in `source_manifest.json`; regenerations refuse any captured-source drift. env_loader additionally must match supplied SHA 755ed19cd94bafff49390e005030880628af66cc753899142d123dd8f8f82aaa.
- `python3 -B test_overlay.py`: 9 passing synthetic tests, saved in `test_results.txt`.
- `patch --dry-run -p1 -d <exact-live-root> < onepassword.patch`: exit 0, saved in `patch_check.txt`. Dry-run only.
- Central fetch clamps finite positive TTL to max 1800; zero/negative/invalid/nonfinite disables cache lookup and storage. Covers direct fetch, apply wrapper and registered source.
- Exact `/Users/sheldon/.hermes/profiles/shld-keys-router/tools/key_op.py` selection via binary_path OR supplied binary Path bypasses outer `_STORE` lookup/store entirely, including pre-existing op_cache/L1 entries. Both wrappers explicitly forward binary_path. No alternate spelling, symlink resolution, or similarly named helper exemption is introduced.
- Ordinary op L1 and disk cache expire at exactly 1800 even when caller requests 86400. Disk promotion retains fetched_at; it does not itself restart age.
- Offline tests execute parsed exact source with fake environments, providers and disk operations. No real package startup, real secret file, auth material, or network used. Synthetic values remain in memory only.
- env_loader remains byte-identical: default unattended desktop protection and explicit allow_unattended_helper opt-in are unchanged.

## Honest residuals / release limits

1. **Not an end-to-end all-cache freshness guarantee.** env_loader `_APPLIED_HOMES` has no expiry. `_SECRET_SOURCE_VALUES_BY_HOME` and `_SECRET_SOURCE_RESTORE_BY_HOME` have no timestamp; snapshot getter returns old values regardless of elapsed time. Hydration and global apply short-circuit for applied homes, and restore may reassert snapshots. Test demonstrates timestamp-free snapshot return; these need a separately reviewed refresh/revocation design. No env_loader overlay included.
2. Shared `_cache.py` accepts caller-provided TTL without a global hard cap. This patch bounds all 1Password calls at their single fetch boundary, but does not constrain other backends/direct users of SecretCache. It also does not repair future fetched_at / wall-clock rollback semantics. SecretCache.store unconditionally updates memory even for zero TTL; 1Password now avoids calling it when disabled.
3. Shared transport cache implementation belongs to another lane and was not invoked or independently validated here. Exact-helper bypass prevents stacking a newly timestamped outer cache on a nearly expired transport result; ultimate 1800 enforcement depends on that transport lane's checks. Third-party helpers/native-op internal caches are not validated.
4. Existing plaintext op_cache files are neither read nor removed by this work; helper mode ignores them. No claim of secure migration/deletion is made.
5. Jev advice is missing: parent established native CLI unavailable (exit 2) and startup unexpectedly bootstraps secrets. No safe permitted Jev route; did not rerun it. No synthetic advice substituted.
6. No full application integration suite (which could bootstrap secrets) or live canary. Candidate only, no deployment authorization inferred.

## Artifacts

- build_overlay.py — exact-source anchored generator, read-only inputs; no installer.
- onepassword.patch — review/apply candidate only.
- onepassword.candidate.py — generated exact source plus narrow overlay.
- source_manifest.json — source/candidate/patch SHA-256 values.
- test_overlay.py — offline tests, AST isolation to avoid Hermes startup.
- test_results.txt / patch_check.txt — execution receipts.

Do not replace a whole release/branch with the generated candidate. Any later application must verify manifest hashes first and separately authorize live changes.
