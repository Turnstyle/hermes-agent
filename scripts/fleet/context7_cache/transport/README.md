# Proposed private shared key-op cache (not deployed)

Only shld-deploy-deputy may deploy this proposal. No live source, launch agent,
profile configuration, ROLE, READINESS, credentials or board records were changed.

## Exact scope

The baseline is the actual read-only router tools/key_op.py, not a repository
facsimile. build_overlay.py reads only that Python source and keys_router_cli.py,
generates key_op.candidate.py, a unified patch, and source-overlay-manifest.json.
The manifest pins SHA256 of both live source files and every listed candidate.
Deployment must fail on any baseline hash mismatch and obtain a fresh review.

Proposed target is explicitly pinned, with no environment/profile override:
`/Users/sheldon/.hermes/profiles/shld-keys-router/cache/secret-values-v1`.
Deputy must first verify/create the parent `cache` securely; the helper creates
only its own leaf. Root-owned non-writable system ancestors are trusted; all
other ancestors must be owned by the current UID and not group/world writable.
Every component is opened relative to a directory FD with O_NOFOLLOW. The leaf
must be current-UID 0700; regular records and permanent locks must be 0600,
current-UID, single-link. Unsafe paths fail closed before any provider call.
Same-UID malicious processes remain inside the trust boundary: file permissions
cannot isolate secrets from another process running as Sheldon.

The exact current reference allowlist is duplicated and equality-tested against
the candidate. LEGACY_REFS canonicalization happens before hashing/locking.
Only the current keys-router branch is cached. The legacy jtp-bots-suite fallback
is unchanged and never cached. Custody authentication, desktop-disable guards,
base environment scrubbing, bounded subprocess reads, error handling and main
are preserved. No bootstrap/service-account/session tokens are written; the
cache only accepts printable non-whitespace API-key bytes and rejects ops_,
OP_SESSION_, op:// and Bearer forms. This intentionally tightens value validation
for the current API-key consumers, not the untouched legacy branch.

## Freshness and concurrent access

Production default TTL is 1740 seconds; the absolute validation cap remains
1800 seconds (finite positive numeric, not bool). Production resolve exposes
no TTL override. The 60-second margin allows the normal 30-second cleanup pass
to remove values before 1800 seconds, but does not guarantee an OS deadline.
The whole miss/provider/atomic publication is protected by a per-reference flock.
Eight spawned processes with different synthetic profile labels use one provider
read for the canonical legacy/current alias. Profiles do not contribute to keys.
Different non-LEGACY vault aliases are deliberately distinct identities.

Records require an exact schema, version2, canonical ref, canonical base64 value,
finite nonnegative timestamps, exact expiry and TTL <=1800. Both wall and
monotonic elapsed times must be nonnegative and below TTL, and agree within five
seconds. Wall rollback/reboot monotonic rollback/large jumps invalidate a record.
TTL starts before provider access. Expired/corrupt entries are removed before
provider access; failure never returns stale data or creates a negative cache.
Version2 uses explicit system-wide CLOCK_MONOTONIC rather than older Apple
Python's process-relative time.monotonic epoch. Version1 records are invalidated;
the independent-process test caught and now guards this cross-process defect.
Atomic private temp files are flushed/fsynced then replaced, and directory synced.
Persistent lock files must not be deleted: deleting them splits waiting lockers.
Crash-left temp files are never read as records; periodic cleanup removes
strictly named, private abandoned temp files under their matching per-ref lock. This is local plaintext secret-value storage
protected by permissions, not encryption or secure erasure from disk snapshots.

## Cleanup without future reads

`cleanup.launchd.plist` is an uninstalled, supported macOS LaunchAgent proposal.
Deputy installs the reviewed helper beside key_op.py and the plist into the
current user's LaunchAgents, checks its target/hash/ownership, then separately
loads/verifies it. It runs `secret_value_cache.py --cleanup` at load and every
30 seconds without any provider/auth import. It scans only known identities,
uses their same locks, and deletes expired/invalid records. Thus reads stop at
TTL exactly; physical deletion is normally within the next 30-second scheduled
pass plus scheduler/lock delay. launchd is not a real-time guarantee: sleep,
shutdown, scheduler failure or a blocked provider can delay physical deletion.
This limitation must not be described as an exact wall-clock disk erasure.
No daemon or launch agent was installed or run here.

### Explicit deployment, rotation and rollback purge

Deputy must quiesce cache consumers before deployment, credential rotation or
rollback; otherwise a concurrent consumer may refill immediately after purge.
Using the reviewed installed helper and its verified interpreter, explicitly run
`/opt/homebrew/bin/python3 /Users/sheldon/.hermes/profiles/shld-keys-router/tools/secret_value_cache.py --purge`.
This proposed command was NOT run against live state in this work. It removes
known value records even when unexpired, and abandoned private temporary files,
under the existing locks, without provider/auth access. Keep permanent lock
inodes and the bounded nonsecret metadata; never delete a live cache directory
or lock files. Verify no known value JSON or matching temporary files remain
while consumers are stopped, then install/check the exact reviewed overlay and
30-second cleanup agent before resuming consumers. On rotation purge before
resumption; on rollback purge with this helper before removing it. Unknown files
are not deleted automatically. Filesystem deletion is not secure erasure of
snapshots/backups, and sleep/scheduling/blocked locks can exceed the margin.

## Evidence and offline reproduction

The production overlay now always persists `<refhash>.metadata.json`, private
0600, atomic/fsynced under the same canonical-reference flock. Its exact schema
is `{v, ref_id, event, hit, miss, provider_read}`: version, canonical reference
SHA256, last fixed event and cumulative bounded counters (saturating at 2**63-1).
There are no secret values, value hashes/refvalues, raw references, token
contents, arguments, exceptions or environment. `provider_read` counts durable
attempt intent, not successful reads; a crash after recording intent may precede
the provider call. Corrupt/unsafe metadata fails closed. Metadata remains across
cleanup/purge and occupies one bounded record per allowed canonical reference.
The optional callback still receives only `{event, ref_id}` and is not required
for production evidence. Two independent fake-provider processes invoke the
actual candidate resolve path without a callback: first miss/read1, then hit
with zero additional provider reads. Their PIDs/counters are in the receipt.

From this folder, with existing Python and no installs:

```
PYTHONDONTWRITEBYTECODE=1 python3 build_overlay.py
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_transport
```

Tests use only fake providers, temporary private directories under the delegate's
scratch folder, and AST/static inspection/import of inert candidate definitions.
They never call live key_op main, custody_env, router, op, gcloud, or read .env/auth.
The simulated non-owner test patches stat because changing real file ownership
would need privileges. launchd validation is artifact parsing, not installation.
Inherited parent context says Jev already failed twice. This run did not retry
Jev, launch another model, repair credentials or invoke an auth launcher.
Runtime-provided identity is gpt-6.1-sol / openai-codex, inherited subscription;
this is supplied session evidence, not an independent billing/account check.
Missing advice does not authorize deployment or imply live cache proof.
`verify_offline.py` reruns the full synthetic suite plus50 eight-process dedup
runs and saves actual results and the manifest hash in offline-verification.json.
