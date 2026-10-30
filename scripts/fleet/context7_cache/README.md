# Context7 quota correction candidate

Status: DRAFT — independent rule14 review required; NOT merge-ready or deploy-ready.

Current task-only authority: direct owner request names Turnstyle/hermes-agent and shld-mm-hermes-agent for this new candidate. Base fe928b3f25fc925e6e97042a88794f3e64c5a56d (fleet/0.21.5-union-rc3). This does not amend permanent ROLE/READINESS/Directory binding or extend PR9's historical ruling. No merge, live edit, restart, board write or deployment is authorized by this artifact. shld-deploy-deputy alone owns later deployment after independent review and normal exact-head merge.

Artifacts

- transport/: exact live key_op.py patch, candidate, secure shared exact-ref cross-process cache, synthetic tests, hash manifest and proposed periodic cleanup adapter.
- loader/: exact live onepassword.py patch caps source TTL at1740 and bypasses the intermediate profile cache for pinned helper. Env-loader adds only60 seconds.
- config/: tested config transformer evidence; parent script now caps helper and ordinary TTLs and enables Context7 only in the two named delta targets.
- restart/: one-line exact-source coordinated restart patch adds supported --yes inside existing window, mocked tests and CLI semantics evidence.

Final candidate changes and honest limits

- loader/env_loader.patch bounds applied-home guards, external-secret snapshots and dotenv restore to one shared 60-second window; ordinary source caches and shared transport enforce at most1740 seconds, giving original-provider end-to-end age below1800. See loader/ENV_TTL.md and test_stacked_age.py. Old-head deputy review is FAIL H01; this correction needs new exact-head re-review.
- Transport logical TTL defaults1740 seconds, hard cap1740. Physical cleanup is periodic30seconds plus OS scheduling/lock delay. Deputy must purge expired op_cache.json at deployment and prove a cleanup cycle; no hard host-suspend physical deadline is claimed. Independent reviewer must accept this limitation.
- Production helper automatically writes private0600 durable nonsecret hit/miss/provider-read counters. Separate-process tests use fake providers only; deputy owns real provider verification after deployment. No token cache added.
- Same coordinated adapter now offers explicit --no-notices, suppressing outbound steers, wakes, notices and board/checkpoint comments while retaining census/drain/checkpoint/permission/lock/health policies. All mutations are mocked in author tests; no restart occurred.
- .gitleaks.toml retains all default secret rules and excludes only exact known public SHA256 digests at exact two manifest paths, including prior PR10 digests. Hashes remain fully verifiable.
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
