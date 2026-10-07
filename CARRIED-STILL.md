# Still carried in rc2

This list includes retained source behavior and host-local preservation obligations. U2d-005 and U2d-033 are private or scratch artifacts to archive on their host; they are not shared Hermes source. Other MOVE-OUT rows remain pending extraction. Overlapping rows are preserved for provenance.

| Change | Classification | Subject |
|---|---|---|
| C2-001 | KEEP-FIX | carry: bot relay and Bot Chat DM delivery |
| C2-002 | KEEP-FIX | fix(bot-mode): the DM delivery runner selects the committed dependency environment |
| C2-004 | KEEP-FIX | test(bot-mode): zero the DM runner's live-wait budget, not the relay constant |
| C2-005 | MOVE-OUT | Fix busy-target enqueue under bare sender interpreter |
| C2-008 | MOVE-OUT | Route local blocked-config cron alerts to owning Bot Chat |
| C2-010 | KEEP-FIX | Warn and continue when a Bot Chat lock holds a send. |
| C2-011 | MOVE-OUT | carry: fast-ack busy remote Bot Chats (adapted from 3a47d45c1c) |
| C2-012 | KEEP-FIX | carry: legacy venv runtime adaptation for busy-DM delivery (from 0fa8ff6915) |
| C2-013 | KEEP-FIX | carry: port required relay helpers and resolve drain conflicts to TurnerBook main |
| C2-014 | KEEP-FIX | carry: stale relay sweep test counts payload sidecars |
| C2-015 | MOVE-OUT | carry: use checkout venv Python on v0.21.5 nodes |
| C2-016 | MOVE-OUT | feat(bot-chat): drain queued fleet_messages_v1 docs at Bot Chat turn end |
| C2-017 | MOVE-OUT | feat(bot-chat): drain stack - gcloud credential fallback, -Q Bot Chat turn-end drain, busy-target Firestore enqueue (maker Codex gpt-6-sol; tb-cndr ruling 2026-09-28) |
| C2-018 | KEEP-FIX | fix(api-server): accept a bot DM into the restart spool while draining |
| C2-019 | KEEP-FIX | fix(api-server): resolve peer DM profile by unique display/Bot Chat alias (snow-cndr, Turner 2026-09-28) |
| C2-020 | MOVE-OUT | fix(bot-chat): -Q drain reconciles an uncertain mark_read before record_error (checker HIGH; mirrors tui recover()) |
| C2-021 | MOVE-OUT | fix(bot-chat): busy enqueue runs under the source checkout launcher, not the install venv python (venv imports the stale editable workspace, which lacks tools.fleet_message_drain) |
| C2-022 | MOVE-OUT | fix(bot-chat): drain hook failure-safety (review t_fk_4a70a715 fixes 1-3) |
| C2-024 | KEEP-FIX | fix(bot-dm): nested DMs in api_server turns do not wait; reply lands in the transcript (t_51c3a4cb) |
| C2-025 | KEEP-FIX | fix(bot-mode): queue message_agent behind a held Bot Chat session until dm_queue_wait_seconds, then fail loudly |
| C2-026 | MOVE-OUT | fix(bot-relay): admit a busy DM only when the recipient can consume it |
| C2-027 | KEEP-FIX | fix(bot-relay): an unrelayed ok reply is not booked as the target's answer |
| C2-028 | KEEP-FIX | fix(bot-relay): bot_relay.deliver names delivered_profile and logs it, so a misroute can never be silent |
| C2-029 | MOVE-OUT | fix(bot-relay): busy-bot queue waits 30 min by default; Desktop deliver deadline grows to match (Turner 2026-09-28) |
| C2-030 | KEEP-FIX | fix(bot-relay): do not ack a lost one-shot completion, print an unattested reply, or outrun the Desktop deadline |
| C2-031 | KEEP-FIX | fix(bot-relay): flag REPLY NOT RELAYED for a relayed DM landed via prompt.submit into an open Bot Chat |
| C2-032 | KEEP-FIX | fix(bot-relay): forward REPLY NOT RELAYED's reason/reply_relayed through write_reply and the Desktop hop |
| C2-033 | KEEP-FIX | fix(bot-relay): retry relayed DMs behind a held Bot Chat session inside the Desktop deliver budget, then fail target_busy |
| C2-034 | KEEP-FIX | fix(bot-relay): stall-watch sidecar writes are best-effort, never strand a message |
| C2-035 | KEEP-FIX | fix(bot-relay): wait the full deliver budget for an open Bot Chat's reply, then flag REPLY NOT RELAYED instead of a fake answer |
| C2-036 | KEEP-FIX | fix(desktop): relay surfaces MISROUTED when delivered_profile differs from target_profile and prefers the target profile's route |
| C2-037 | MOVE-OUT | fix(fleet): create a queued DM only when the document is absent |
| C2-038 | KEEP-FIX | port(msgfix-0928): TurnerBook busy-bot messaging fix onto max, keeping every max-local fix |
| C2-039 | MOVE-OUT | test(bot-chat): busy-enqueue test accepts the checkout launcher when present |
| C2-040 | KEEP-FIX | test(bot-mode): RED — message_agent queues behind a held Bot Chat session instead of failing target_busy |
| C2-041 | KEEP-FIX | test(bot-relay): RED — REPLY NOT RELAYED's reason/reply_relayed survive the write and the Desktop hop instead of being dropped |
| C2-042 | KEEP-FIX | test(bot-relay): RED — a relayed DM landed via prompt.submit into an open Bot Chat flags REPLY NOT RELAYED |
| C2-043 | KEEP-FIX | test(bot-relay): RED — a relayed DM to an open Bot Chat waits for the real reply and flags when it cannot relay it |
| C2-044 | KEEP-FIX | test(bot-relay): RED — bot_relay.deliver names delivered_profile so a misroute can never be silent |
| C2-045 | KEEP-FIX | test(bot-relay): RED — relayed DMs queue behind a held Bot Chat session within turn_wait_seconds |
| C2-046 | KEEP-FIX | test(desktop): RED — relay refuses to report a misrouted delivery as success and dials the target profile's route |
| C2-047 | KEEP-FIX | Add durable no-agent peer ping receipts |
| C2-048 | MOVE-OUT | Drain queued fleet messages for idle and API Bot Chat turns |
| C2-049 | MOVE-OUT | Queue sender notifications when fleet messages expire |
| C2-051 | MOVE-OUT | fix: resolve snap gcloud for busy peer queue and classify rate limits |
| C2-052 | MOVE-OUT | carry: fleet message drain keys on strict Bot Chat, not message_agent surfaces (t_2e0ceb41) |
| C2-053 | KEEP-FIX | carry: message_agent on opted-in gateway surfaces + strict-Bot-Chat drain gate (t_2e0ceb41, TurnerBook-adapted) |
| C2-054 | KEEP-FIX | feat(slack): opt-in profile-scoped slash aliases (platforms.slack.extra.slash_aliases) (t_93f93c1f) |
| C2-055 | KEEP-FIX | fix(slack): alias targets are built-in commands only; aliases respect the 50-command manifest cap (t_93f93c1f r1) |
| C2-056 | KEEP-FIX | fix(slack): configured aliases outrank natives in the 50-slot manifest; trimmed natives stay reachable via /hermes (t_93f93c1f r2) |
| C2-057 | KEEP-FIX | feat(bot-chat): turn-lock holder sidecar, long-held lock watchdog, doctor warning |
| C2-058 | KEEP-FIX | fix(bot-chat): bound the fallback and peer DM child waits |
| C2-059 | KEEP-FIX | fix(bot-chat): hard deadline on DM child waits with detached reaper and turn_timeout failure |
| C2-060 | KEEP-FIX | fix(bot-chat): release parent turn lock after successful report |
| C2-062 | KEEP-FIX | fix(slack): !help reaction status uses own adapter config only |
| C2-063 | MOVE-OUT | fix(slack): concise !help for mobile with profile-true reactions |
| C2-066 | KEEP-FIX | Clear cron alert marker after all config checks pass |
| C2-068 | KEEP-FIX | fix(desktop): compare transcript tips, not counts, in the stale-send guard |
| C2-070 | KEEP-FIX | fix(desktop): queue a human send behind a bot CLI lease |
| C2-071 | KEEP-FIX | Spare durable jobs on shutdown and scope desktop stop |
| C2-072 | KEEP-FIX | fix(kanban): port lifecycle-fence isolation + recompute_ready foreign-owner guard to 0.21.5 (from e9ce8bc6+d2918c8e) |
| C2-073 | KEEP-FIX | test(kanban): created-blocked card stays blocked until explicit unblock (tb-cndr sticky-block decision) |
| C2-074 | KEEP-FIX | carry: kanban dispatch, crash-orphan group reap, owner_unavailable, claim_errors |
| C2-075 | KEEP-FIX | fix(kanban): bootstrap the worker argv on a bare store interpreter |
| C2-076 | MOVE-OUT | fix(kanban): stuck telemetry counts only cards this Fleet node owns |
| C2-077 | KEEP-FIX | fix(kanban): a worker refused its profile's session slot is profile busy, not a crash (t_76b0d62d) |
| C2-078 | KEEP-FIX | fix(kanban): busy backoff is exact and bounded under ties, overrides and long holds (checker round 1, t_76b0d62d) |
| C2-079 | KEEP-FIX | fix(kanban): busy evidence outranks expired claims and unsafe log tails (checker round 2, t_76b0d62d) |
| C2-080 | KEEP-FIX | fix(kanban): explicit busy evidence bypasses grace and crash accounting keeps the caller limit (checker round 3, t_76b0d62d) |
| C2-081 | KEEP-FIX | test(kanban): RED — a session-limit worker exit is profile busy, not a crash (t_76b0d62d) |
| C2-082 | KEEP-FIX | test(kanban): RED — busy backoff survives same-second ties, bounded overrides, long holds (checker round 1, t_76b0d62d) |
| C2-083 | KEEP-FIX | test(kanban): RED — busy survives expired claims, bounded overrides and tail cuts (checker round 2, t_76b0d62d) |
| C2-084 | KEEP-FIX | test(kanban): RED — launch grace and custom failure limits preserve reclaim semantics (checker round 3, t_76b0d62d) |
| C2-085 | MOVE-OUT | Add governed Kanban ghost recovery command |
| C2-086 | KEEP-FIX | Clarify kanban completion refusal by card status |
| C2-087 | KEEP-FIX | Fix active PR guard for input links and requeues |
| C2-089 | KEEP-FIX | Normalize nonpositive Kanban goal turn budgets on create |
| C2-090 | MOVE-OUT | Reclaim stale fleet message claims with guarded maintenance command |
| C2-091 | KEEP-FIX | Skip foreign Fleet mirrors during kanban decomposition |
| C2-092 | KEEP-FIX | Stop repeated kanban spawns without dropping fresh auth blocks. |
| C2-093 | KEEP-FIX | carry: kanban lifecycle-fence isolation, recompute_ready foreign-owner guard, host-cap mirror rows (Sheldon working tree preserved, Lane 1 2026-09-28) |
| C2-094 | MOVE-OUT | carry: keep Turner holds parked (adapted from b2ab2828defc387227609dccd4073650d85fba4e) |
| C2-095 | KEEP-FIX | carry: recover-ghost Fleet-owner helper fallback for Max |
| C2-096 | KEEP-FIX | fix(bot-relay): a lease miss releases the claim without spending a delivery attempt |
| C2-097 | KEEP-FIX | fix(kanban): capacity caps count only claimed running rows; zero-spawn tick names its cap (t_b19ce8b9) |
| C2-098 | KEEP-FIX | fix(kanban): chat -q worker hard-exits after cleanup so rc=75 workers stop hanging (t_fk_fb332447) |
| C2-099 | KEEP-FIX | fix(kanban): dispatch --dry-run makes no writes; skip or report the reclaim phase (t_5d6f61dd) |
| C2-100 | MOVE-OUT | fix(kanban): exclude placeholder profiles from spawnable health telemetry and report separately |
| C2-101 | MOVE-OUT | fix(kanban): exclude policy holds from paging, emit placeholder diagnostics, and add watcher tests |
| C2-103 | MOVE-OUT | fix(kanban): prevent successful spawns and capacity holds from masking failing cards |
| C2-104 | MOVE-OUT | fix(kanban): prevent unrelated holds from masking dispatch failures and track sibling probe |
| C2-105 | MOVE-OUT | fix(kanban): tie dispatch failures to eligible cards and track spawn exceptions |
| C2-106 | KEEP-FIX | fix: keep active PR guard through automatic Kanban events |
| C2-112 | KEEP-FIX | test(kanban): dry run leaves orphans alone; real ticks use max_spawn=0 (t_067955c7, tb-king r3b) |
| C2-114 | KEEP-FIX | Keep Kanban PR cache inside its board and isolate tests |
| C2-115 | KEEP-FIX | Lift Kanban PR respawn hold from fresh terminal cache |
| C2-116 | KEEP-FIX | carry(snowdrop): foreign Fleet mirror owner resolution matches TurnerBook |
| C2-117 | KEEP-FIX | fix(kanban): accept MERGED-at-bound-head when branch-rules API is plan-limited 403 and no classic protection (t_7e7fdbb8) |
| C2-118 | KEEP-FIX | fix(kanban): preserve block-loop hold across stale default assignment |
| C2-120 | KEEP-FIX | fix(hermes): default child delegation effort to high, guard workspace claims, and add grok-4.7 |
| C2-121 | KEEP-FIX | fix(kanban): keep WAL side files alive with a gateway keepalive + bounded retry for read-only board opens (t_2ed89c39) |
| C2-122 | KEEP-FIX | fix(vault): 1Password service-account mode passes --vault on every item call (card t_bae456c5; redo of t_ce7dcf79) |
| C2-123 | KEEP-FIX | carry(kanban): assemble accepted L10 target and unchanged reviewed Board Ops source |
| C2-124 | KEEP-FIX | carry-prereq: add _has_unreleased_block_loop for keep-spec (verbatim from 4430234eb7; helper only, no caller wired) |
| C2-125 | KEEP-FIX | carry: add hermes kanban specify --keep-spec flag with compact json sha256 guard |
| C2-126 | KEEP-FIX | carry: refuse empty --expect-sha256 without --keep-spec |
| C2-127 | KEEP-FIX | fix(kanban): clear audited triage holds without promoting cards |
| C2-128 | MOVE-OUT | test(fleet-messages): wait briefly for the turn lock release in the drain-timeout test (done.set fires before the worker leaves the with-block) |
| C2-129 | KEEP-FIX | carry: tui_gateway compute host queued admission and prompt receipts |
| C2-130 | MOVE-OUT | fix(supermemory): key helper keeps the credential inside its own ssh session |
| C2-131 | KEEP-FIX | Guard launchd plist writes against broken gateway launchers |
| C2-132 | KEEP-FIX | Keep fallback session model and select Codex pool by model |
| C2-133 | KEEP-FIX | Keep pre-agent fallback turns from pinning the session model |
| C2-134 | KEEP-FIX | Keep the restart-budget hour only when a restart actually starts. |
| C2-135 | KEEP-FIX | Keep the working launchd plist and still start/restart when a new launcher fails preflight |
| C2-136 | KEEP-FIX | Make the gateway restart budget atomic, and do not charge failed attempts. |
| C2-137 | KEEP-FIX | Prefer source launcher in bot and gateway PATH |
| C2-138 | KEEP-FIX | Refuse a second gateway restart inside 60 minutes unless crash recovery or force. |
| C2-140 | MOVE-OUT | carry: drop the second -Q exit drain (TurnerBook resolved this only in merge 0e8073226f) |
| C2-142 | MOVE-OUT | fix(fleet-messages): strip Hermes bootstrap PYTHONPATH/PYTHONHOME from the gcloud token subprocess (gcloud's own Python crashed on Hermes's pyOpenSSL) |
| C2-143 | KEEP-FIX | fix(gateway): an inbound bot message does not interrupt a running turn |
| C2-144 | MOVE-OUT | fix(gateway): busy_input_mode defaults to queue, so a mid-turn message never kills a running turn (Turner 2026-09-28) |
| C2-145 | MOVE-OUT | fix: sort fleet message drain page without Firestore orderBy |
| C2-146 | KEEP-FIX | port(msgfix-0928): review r1 - a busy bot's message waits instead of bouncing; no stale drain; failed replay keeps its spool |
| C2-148 | KEEP-FIX | restart budget: generic nonzero systemctl restart after wedged escalation does not keep the hour (r3 HIGH) |
| C2-149 | KEEP-FIX | restart budget: keep the hour only when a new live gateway PID is observed. |
| C2-150 | KEEP-FIX | restart budget: recovery exemptions use verified gateway identity, not a bare state PID (r8 HIGH) |
| C2-152 | KEEP-FIX | restart observer: use gateway.status.live_gateway_pid_for_home (start-time reuse guard + home ownership) instead of a custom check (r7 HIGH) |
| C2-153 | KEEP-FIX | Apply gateway stop policy per owning profile |
| C2-154 | MOVE-OUT | Book fleet message drains from quiet turn reports |
| C2-155 | MOVE-OUT | Bound and single-flight API fleet message drains |
| C2-156 | MOVE-OUT | Make fleet message drain idle-safe and nonblocking for API turns |
| C2-157 | MOVE-OUT | Map busy fleet drain turns to retryable API responses |
| C2-158 | KEEP-FIX | carry: message_agent on opted-in gateway surfaces (bot_mode.message_agent_platforms) (t_2e0ceb41) |
| C2-159 | KEEP-FIX | fix(hermes): return to primary after cooldown, live fallback refresh, and metered compression cap |
| C2-160 | KEEP-FIX | fix(cli): flush and end the one-shot session before releasing its lease |
| C2-161 | KEEP-FIX | fix(cli): release the one-shot session lease before the exit linger (port of 734ac474e5) |
| C2-162 | MOVE-OUT | fix(fleet-messages): push an expiry notice to the original sender |
| C2-163 | KEEP-FIX | carry: process registry receipts and terminal teardown ownership |
| C2-164 | MOVE-OUT | docs(supermemory): document container permissions and the availability proof |
| C2-165 | MOVE-OUT | docs(supermemory): document the Unix-socket tunnel and what it does not cover |
| C2-166 | MOVE-OUT | docs(supermemory): document the tunnel re-check and the helper's ssh-only key transport |
| C2-167 | MOVE-OUT | docs(supermemory): the transport pins the socket the start-up check verified |
| C2-168 | MOVE-OUT | feat(supermemory): tunnel key helper that authenticates the listener before fetching a key |
| C2-169 | MOVE-OUT | fix(supermemory): enforce capture approval, container permissions and an availability proof |
| C2-170 | MOVE-OUT | fix(supermemory): forward the tunnel to a Unix socket and give the SDK no other route |
| C2-171 | MOVE-OUT | fix(supermemory): gate capture retries with the same approval as capture |
| C2-172 | MOVE-OUT | fix(supermemory): keep reporting why the key was dropped |
| C2-173 | MOVE-OUT | fix(supermemory): one tunnel verification per start, and the same gate for the setup probe |
| C2-174 | MOVE-OUT | fix(supermemory): re-check availability before every client use and drop the key on down |
| C2-175 | MOVE-OUT | fix(memory): fail closed on chunk recall without parent provenance |
| C2-176 | MOVE-OUT | fix(memory): preserve nested document ids in recall |
| C2-177 | MOVE-OUT | fix(memory): quarantine exact Supermemory documents from recall |
| C2-178 | MOVE-OUT | fix(memory): quarantine parent document ids during recall |
| C2-179 | KEEP-FIX | Prefer source launcher in terminal child PATH |
| C2-180 | KEEP-FIX | carry: self-bounded tool deadline (port of bb339893d7 + af3e65741e) t_5ff1e374 |
| C2-181 | MOVE-OUT | fix(fleet-messages): keep fleet_ops progress output off the enqueue CLI's JSON stdout |
| C2-182 | MOVE-OUT | fix(tools): keep gRPC fork-child stderr out of local command output and file-tool reads (t_fk_93f4c45b) |
| C2-183 | KEEP-FIX | test: isolate local env PATH cases from checkout launcher |
| C2-184 | KEEP-FIX | Expect file-backed wrapper in systemd-run argv test |
| C2-185 | KEEP-FIX | Merge checkpoints across independent process owners |
| C2-186 | KEEP-FIX | Persist local background output and recover exit status |
| C2-187 | KEEP-FIX | Reconcile file-backed exit on list and poll refresh |
| C2-188 | KEEP-FIX | Refresh routing checkpoint only for file-backed jobs |
| C2-189 | KEEP-FIX | Wait for recovered completion to publish in test |
| C2-190 | KEEP-FIX | carry: h2025 A+B combined fallback/compression/delegate-effort fixes (from king/h2025-AB-combined 39e341ef7250, base 82f845753f98; patch sha256 9edfc3e5...) |
| C2-191 | KEEP-FIX | fix(vault): bind 1Password service-account reads to the listed vault ID (t_af975157, t_517fd36f) |
| C2-192 | MOVE-OUT | carry: 'hermes update' carried-commits guard, 0.21.5 backport (t_3485a287) |
| C2-206 | MOVE-OUT | fix(supermemory): keep the Sheldon lazy_deps SDK seam under the carried guard (t_fk_4fb28122) |
| C2-209 | KEEP-FIX | Soften goal-loop stops that are not real external holds. |
| C2-211 | KEEP-FIX | fix(fallback): same-account pool 429 falls back fast instead of 600s sleeps (t_fk_fb332447) |
| C2-212 | KEEP-FIX | test: accept model in auxiliary credential pool fakes |
| C2-213 | KEEP-FIX | carry: document why keep-spec must not rewrite reviewed text |
| C2-214 | KEEP-FIX | carry: keep-spec audit identity, full digest, test coverage |
| C2-215 | KEEP-FIX | fix(cli): every one-shot chat -q/-Q run hard-exits after cleanup |
| C2-216 | KEEP-FIX | fix(cli): one-shot exception paths also hard-exit after cleanup |
| C2-217 | KEEP-FIX | fix(usage): decide Anthropic percent vs fraction once per payload; add raw_used to hermes usage --json |
| C2-218 | KEEP-FIX | Decide busy relay status from the final provider attempt |
| C2-219 | KEEP-FIX | Integrate queue envelope identity and distinguish native Hermes home from package payloads |
| C2-220 | MOVE-OUT | fix(kanban): count lease fence on canonical-blocked sync-pending card as a hold, not a stall (t_0923de5e) |
| C2-221 | MOVE-OUT | test(kanban): cover real lease fence and spawn reset (t_0923de5e) |
| C2-222 | MOVE-OUT | fix(fleet-drain): sign in with named service-account key file, never ADC (A-L3) |
| C2-223 | KEEP-FIX | Skip foreign-home and unverified-owner recovery before probing PIDs or writing cards |
| G3d | KEEP-FIX | Fail-closed Hermes launch guard during restart pause |
| U2c-SHELDON | KEEP-FIX | Stall-aware Bot Chat delivery cap (t_d0edbb6e) |
| U2d-001 | MOVE-OUT | Named service-account source for fleet drain |
| U2d-003 | KEEP-FIX | Initial profile-owned single-use credential claim |
| U2d-004 | MOVE-OUT | Named service-account source for fleet drain |
| U2d-005 | MOVE-OUT | Private session export |
| U2d-008 | KEEP-FIX | Recover only verified local fleet cards |
| U2d-009 | KEEP-FIX | Recover only verified local fleet cards |
| U2d-032 | KEEP-FIX | Fleet recovery ownership regressions |
| U2d-033 | MOVE-OUT | Personal response style |
| U2d-034 | MOVE-OUT | Named service-account source for fleet drain |
| U2d-036 | KEEP-FIX | Stall-aware Bot Chat delivery cap |

The full reasons, source commits, proof tests and archive destinations are in docs/fleet-reports/FINAL-CHANGES-rc2.tsv. Runtime backup, archive and switch actions remain the rollout lane's work.
