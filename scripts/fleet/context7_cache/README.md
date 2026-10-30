# Context7 quota correction candidate

Status: DRAFT — independent rule14 review required; NOT merge-ready or deploy-ready.

Current task-only authority: direct owner request names Turnstyle/hermes-agent and shld-mm-hermes-agent for this new candidate. Base fe928b3f25fc925e6e97042a88794f3e64c5a56d (fleet/0.21.5-union-rc3). This does not amend permanent ROLE/READINESS/Directory binding or extend PR9's historical ruling. No merge, live edit, restart, board write or deployment is authorized by this artifact. shld-deploy-deputy alone owns later deployment after independent review and normal exact-head merge.

Artifacts

- transport/: exact live key_op.py patch, candidate, secure shared exact-ref cross-process cache, synthetic tests, hash manifest and proposed periodic cleanup adapter.
- loader/: exact live onepassword.py patch caps TTL at1800 and bypasses outer profile cache for pinned helper to avoid layered TTL extension.
- config/: tested config transformer evidence; parent script now caps helper and ordinary TTLs and enables Context7 only in the two named delta targets.
- restart/: one-line exact-source coordinated restart patch adds supported --yes inside existing window, mocked tests and CLI semantics evidence.

Known incomplete acceptance criteria

- env_loader applied-home guards and secret snapshots currently have no TTL. Candidate does NOT yet prove ALL affected in-process snapshots expire within1800seconds. This must be resolved before approval. Do not infer end-to-end freshness from transport tests.
- Physical expired-record deletion is periodic (30seconds plus scheduler/lock delay); logical reads reject expiry exactly. A strict physical retention maximum1800 is not proved.
- Metadata event callback is tested, but production helper wiring for durable metadata-only hit/miss/provider-read measurement still requires review/completion. No real provider counts were measured here.
- Coordinator has no general notice suppression. Omitting notify-cmd and wake-cmd none do not suppress steering/board comments. Do not invoke coordinator under this task's no-message/no-board constraint without a separately authorized route.
- No independent rule14 review performed. Writers were served gpt-6.1-sol via included subscription. Parent must resolve strongest available named reviewer using existing local registry evidence; registry availability and reviewer served-model remain unproved here. Unknown model cannot PASS.
- Jev CLI unavailable. Two delegated attempts exited2 after native startup reported '1Password: applied 2 secrets'. No secret values printed. Fresh provider reads versus cached bootstrap were not measured. No repair or further CLI retry.

Observed offline author evidence (not independent review)

Transport19 tests plus50 eight-process dedup stress runs; config29 tests; unchanged desktop startup guard6 tests; restart8 tests; loader9 tests. Test outputs/manifests live in lane directories or marshal scratch. Do not sum these as a single integrated test run. Fresh config inventory scanned95, matched79, proposed3 changes: coordinator and host-diagnostics Context7 flags plus keys-router ordinary TTL86400 to1800. All78 helper TTLs already1800 before this authoring; do not attribute that preexisting correction to this PR.

Deputy deployment manifest/instructions (not executable authorization)

1. Require independent exact-head rule14 review with no HIGH blockers, known served model at least writer strength, current binding/custody and normal marshal merge. Resolve all incomplete criteria above first.
2. Rehash exact live sources against lane manifests; any drift requires fresh overlays/review. Never replace carry modules with older branch source.
3. Back up exact live files and hashes to approved cold storage. Deploy reviewed transport module beside key_op.py and apply exact patch; deploy loader overlay only against matching live source hash. No auth-token disk cache.
4. Precreate private0700 shared cache at transport manifest target,0600 records; validate non-symlink owned ancestors. Install reviewed cleanup adapter through supported deputy mechanism; verify expiry without future reads. Do not claim hard physical TTL from a scheduling interval.
5. Run config script DRYRUN first, compare exact inventory with current files, use fresh backup directory and exclusive writer for apply. Purge scoped stale op_cache.json older1800 under deputy's explicit directive without reading/printing secret contents.
6. Deploy restart one-line patch only after exact hash check. Coordinated restart is separately gated by no-notice constraint above; never --force, manual restart or kill.
7. Read back exact targets/hashes and selected literal config flags; exercise synthetic regressions and obtain metadata-only real provider-read counts from parent audit sibling, not a duplicate usage collector. No secret output. Only then report observed adoption.

Evidence scratch: /Users/sheldon/.hermes/profiles/shld-mm-hermes-agent/cache/scratch/context7-cache-correction-20261030
