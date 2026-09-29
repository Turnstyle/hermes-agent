# MAKER report — card t_0a89b55d

Branch: `carry/fallback-service-tier-20260929`  
Base and current `git rev-parse HEAD`: `a5c914f7efa8196a809a868a8a1264213b8d0e6a`  
Maker: `tb-king maker <tb-king@bots.conductive.biz>`  
Status: implementation and focused tests complete; commit blocked by Git metadata permissions.
Orchestrator note (tb-king, 2026-09-29): the maker's sandbox could not write `.git`, so tb-king committed the maker's staged tree unchanged plus this report, and pruned only the stale `0a89b55d/base-wt` registration. No code was edited by tb-king.

## To-do list

- [x] Confirm clean starting worktree and venv import path.
- [x] Parse and validate each fallback entry's tier, failing closed on bad values.
- [x] Bind tier on every activated and credential-resolution fallback; restore primary state.
- [x] Gate fast fields by the actual provider/model/endpoint and record the last sent tier.
- [x] Document configuration and provider support.
- [x] Add the requested regression coverage and run the focused test set.
- [x] Compare unrelated test failures against the supplied base commit.
- [x] Prepare this report at the worktree root.
- [ ] Commit implementation and report. Git cannot create files under the live checkout's `.git/worktrees/fallback-service-tier-20260929` in this sandbox.
- [ ] Remove the stale detached base-worktree metadata. The temporary directory was removed, but `git worktree remove` could not delete its metadata under the same read-only `.git` directory.

## Files changed

- `agent/fast_mode.py` — shared fallback tier parser, active-tier selection, route warning, and sent-tier recorder.
- `agent/chat_completion_helpers.py` — bind tier on mid-turn activation and record iteration-summary requests.
- `agent/agent_init.py` — bind the selected entry on init-time credential fallback.
- `agent/agent_runtime_helpers.py` — clear fallback tier on primary/manual restore and preserve startup fallback until replacement.
- `agent/turn_api_error.py` — clear tier when transport recovery resets fallback state.
- `agent/turn_context.py` — reset last-sent tier at turn start.
- `agent/turn_api_call.py` — record the final per-attempt kwargs after Codex preflight.
- `agent/turn_finalizer.py` — report the tier from the last physical request.
- `hermes_cli/config.py` — validate `fallback_providers` and both fallback-key tiers.
- `hermes_cli/oneshot.py` — bind the pre-agent selected entry on one-shot runs.
- `gateway/run_turn_runner.py` — bind the gateway's pre-agent selected entry.
- `tui_gateway/server.py` — carry and bind the TUI/Desktop selected entry.
- `cron/scheduler.py` — carry and bind the cron selected entry.
- `website/docs/user-guide/features/fallback-providers.md` — config semantics and route support table.
- `tests/agent/test_fallback_service_tier.py` — vocabulary, activation, restore, xAI, validation, startup, and turn-result tests.
- `MAKER-REPORT.md` — this handoff and test receipt.

## Design decisions

| Decision | Implementation |
|---|---|
| D1 | `agent/fast_mode.py:30` parses the existing vocabulary and returns `(value, ok)`; the existing primary parsers remain unchanged. |
| D2 | `agent/fast_mode.py:94,127` selects the active entry and removes the primary's top-level `service_tier`/`speed` on fallback; an absent entry value parses as normal. |
| D3 | `hermes_cli/config.py:1144,1160,1237` reports severity `error` with the exact fallback path and allowed values; `agent/fast_mode.py:44` logs an error once per logical entry and uses normal. The bad raw value is not logged. |
| D4 | `agent/chat_completion_helpers.py:2105`, `agent/agent_init.py:883`, `agent/agent_runtime_helpers.py:1246,1328,2274`, and `agent/turn_api_error.py:369` bind/clear state; `agent/fast_mode.py:94,108` selects the effective tier at each turn. `agent.service_tier` is not rewritten. |
| D5 | `agent/fast_mode.py:127` copies overrides, removes only top-level tier/speed on fallback, then adds the route-approved fast field; `extra_body` is untouched. |
| D6 | `agent/fast_mode.py:68,127` calls `hermes_cli/models.py:1183` with the active route and warns once per provider/model when unsupported; `agent/transports/codex.py:763` retains the existing xAI strip. |
| D7 | `agent/agent_init.py:830,1060,1071`, `hermes_cli/fallback_config.py:77,103`, `gateway/run_turn_runner.py:1949`, `tui_gateway/server.py:2361,2488`, `cron/scheduler.py:1775,2448`, and `hermes_cli/oneshot.py:595` retain and apply the selected entry. Credential-resolution fallback has no usable primary client snapshot; `agent/agent_runtime_helpers.py:1249` keeps that entry active until a fresh agent is built. |
| D8 | `agent/fast_mode.py:100`, `agent/turn_api_call.py:95`, `agent/chat_completion_helpers.py:2227,2253`, `agent/turn_context.py:582`, and `agent/turn_finalizer.py:642` track top-level `service_tier`, else `extra_body.service_tier`, else `None` for the last request. |
| D9 | `website/docs/user-guide/features/fallback-providers.md:42-75` documents the shape, omission/invalid semantics, and route table derived from `hermes_cli/models.py:1164,1183` and `agent/transports/codex.py:763`. |

## Activation and restore paths checked

| Path | Handling |
|---|---|
| Mid-turn failover (`try_activate_fallback`) | Bind the chosen entry after its provider/model/base URL are selected (`agent/chat_completion_helpers.py:2105`). Each later entry rebinds. |
| Init-time no-credentials fallback | Bind before the agent's first request (`agent/agent_init.py:883,888`). `_fallback_entries` and `_init_fallback_chain` retain the dict's tier. |
| Gateway pre-agent credential fallback | `gateway/run.py:2305` carries `_fallback_entry`; `gateway/run_turn_runner.py:1949` binds it after the agent is resolved. |
| TUI/Desktop pre-agent credential fallback | `tui_gateway/server.py:2287,2361,2488` carries the actual chosen dict through construction. |
| One-shot pre-agent credential fallback | `hermes_cli/oneshot.py:529,595` binds the resolver's selected entry. |
| Cron pre-agent credential fallback | `cron/scheduler.py:1775,2389,2448` carries the chosen entry through setup. |
| New turn primary restore | `agent/agent_runtime_helpers.py:1246,1328` clears tier only after successful primary restoration; cooldown/entitlement gates keep the fallback and its tier active. |
| Explicit `/model` switch and `/model --once` | `agent/agent_runtime_helpers.py:2274` clears prior fallback state; temporary-model restoration shares `restore_primary_runtime`. Tier selection requires a bound fallback entry, so a temporary model does not masquerade as one. |
| Transport recovery to primary | `agent/turn_api_error.py:367-370` clears the fallback tier with `_fallback_activated`. |

## Provider support

| Route from `resolve_fast_mode_overrides` | Outgoing field |
|---|---|
| OpenAI `api.openai.com` or openai-codex `chatgpt.com`, eligible non-Codex `gpt-*`/`o*` | `service_tier: priority` |
| Anthropic `api.anthropic.com`, Opus 4.8/5/5.5 | `speed: fast` |
| xAI `api.x.ai`, Grok 4.6 family | `service_tier: priority` |
| Other routes, including xai-oauth, OpenRouter, Copilot, custom proxies | Neither field; once-per-route warning for requested fast tier |

## Tests and receipts

All pytest commands used the checkout venv, ran from their respective worktree roots, set `HOME` and `HERMES_HOME` to `/Users/turner/.hermes/profiles/tb-king/cache/scratch/0a89b55d/mk-home`, set `TMPDIR` to `/Users/turner/.hermes/profiles/tb-king/cache/scratch/0a89b55d/mk-tmp`, and redirected pytest stdin from `/dev/null`. They did not touch live profile config. The import proof printed this worktree's `agent/fast_mode.py` path.

Final focused command (1250 passed, 4 deselected):

```bash
HOME=/Users/turner/.hermes/profiles/tb-king/cache/scratch/0a89b55d/mk-home HERMES_HOME=/Users/turner/.hermes/profiles/tb-king/cache/scratch/0a89b55d/mk-home TMPDIR=/Users/turner/.hermes/profiles/tb-king/cache/scratch/0a89b55d/mk-tmp /Users/turner/.hermes/hermes-agent/venv/bin/python -m pytest -q --tb=line tests/agent/test_fallback_service_tier.py tests/agent/test_fast_mode_auto.py tests/agent/test_switch_model_request_overrides.py tests/hermes_cli/test_fast_command.py tests/agent/test_provider_parity.py tests/agent/transports/test_codex_transport.py tests/agent/test_auxiliary_client.py tests/gateway/test_custom_provider_request_overrides.py tests/tools/test_delegate_request_overrides.py tests/agent/test_primary_runtime_restore.py tests/agent/test_fallback_credential_isolation.py tests/agent/test_init_fallback_refusal_warning.py tests/hermes_cli/test_fallback_config.py tests/hermes_cli/test_config_validation.py tests/gateway/test_pre_agent_fallback_notice.py tests/tui_gateway/test_tui_gateway_server.py tests/cron/test_scheduler.py tests/hermes_cli/test_cli_provider_resolution.py tests/agent/test_turn_finalizer_final_response_persistence.py --deselect=tests/agent/test_auxiliary_client.py::TestAuxiliaryAuthRefreshRetry::test_refresh_provider_credentials_force_refreshes_anthropic_oauth_and_evicts_cache --deselect=tests/tui_gateway/test_tui_gateway_server.py::test_load_enabled_toolsets_rejects_disabled_mcp_env --deselect=tests/tui_gateway/test_tui_gateway_server.py::test_load_enabled_toolsets_falls_back_when_tui_env_invalid --deselect=tests/tui_gateway/test_tui_gateway_server.py::test_ensure_session_db_row_stamps_profile_name </dev/null
```

Earlier exact pytest argument lists, with the identical absolute venv/environment prefix and `</dev/null` shown above:

| Worktree | Arguments after `-m pytest` | Result |
|---|---|---|
| Branch | `-q tests/agent/test_fallback_service_tier.py tests/agent/test_fast_mode_auto.py tests/hermes_cli/test_config_validation.py` | 23 passed before the remaining new tests were added |
| Branch | `-q tests/agent/test_fallback_service_tier.py` | 7 passed, then 8 passed as coverage grew |
| Branch | `-q tests/agent/test_fallback_service_tier.py tests/agent/test_fast_mode_auto.py tests/agent/test_switch_model_request_overrides.py tests/hermes_cli/test_fast_command.py tests/agent/test_provider_parity.py tests/agent/transports/test_codex_transport.py tests/agent/test_auxiliary_client.py tests/gateway/test_custom_provider_request_overrides.py tests/tools/test_delegate_request_overrides.py tests/agent/test_primary_runtime_restore.py tests/agent/test_fallback_credential_isolation.py tests/agent/test_init_fallback_refusal_warning.py tests/hermes_cli/test_fallback_config.py tests/hermes_cli/test_config_validation.py tests/gateway/test_pre_agent_fallback_notice.py tests/tui_gateway/test_tui_gateway_server.py tests/cron/test_scheduler.py` | 1205 passed, 4 failed |
| Base | `-q tests/agent/test_auxiliary_client.py::TestAuxiliaryAuthRefreshRetry::test_refresh_provider_credentials_force_refreshes_anthropic_oauth_and_evicts_cache tests/tui_gateway/test_tui_gateway_server.py::test_load_enabled_toolsets_rejects_disabled_mcp_env tests/tui_gateway/test_tui_gateway_server.py::test_load_enabled_toolsets_falls_back_when_tui_env_invalid tests/tui_gateway/test_tui_gateway_server.py::test_ensure_session_db_row_stamps_profile_name` | 4 failed, same cases |
| Base | `-q --tb=line tests/gateway/test_api_server.py` | 54 passed, 59 failed at sandbox socket bind |
| Base | `-q --tb=line tests/hermes_cli/test_web_server.py tests/tools/test_delegate_capacity_interrupt.py tests/tools/test_vision_history_budget.py tests/tui_gateway/test_bot_relay_methods.py tests/tui_gateway/test_groups_methods.py tests/tui_gateway/test_session_id_injection.py` | 259 passed, 23 failed, same later broad-sweep cases |

The expanded branch run used the final focused argument list plus `tests/gateway/test_api_server.py` before the four `--deselect` flags: 1293 passed, 59 failed, 4 deselected. Its 59 failures matched the isolated base API-server module, all at `aiohttp/test_utils.py:85` (`PermissionError` on `127.0.0.1` bind).

The four deselected tests failed in the earlier focused run (1205 passed, 4 failed) and identically on the untouched base (4 failed). The API-server module failed to bind loopback sockets in this sandbox: branch 59 failed/54 passed; untouched base 59 failed/54 passed. Exact test commands were the same pytest invocation with the named node IDs, or `tests/gateway/test_api_server.py`, from the respective worktree root.

Direct-import sweep: the 274 module paths were generated by `rg -l` for direct imports of changed modules, excluding `tests/hermes_cli/test_update_*` and the already-run API-server module. The selected paths are in `/Users/turner/.hermes/profiles/tb-king/cache/scratch/0a89b55d/mk-tmp/fallback-tier-direct-import-tests.txt`. The exact runner command was:

```bash
HOME=/Users/turner/.hermes/profiles/tb-king/cache/scratch/0a89b55d/mk-home HERMES_HOME=/Users/turner/.hermes/profiles/tb-king/cache/scratch/0a89b55d/mk-home TMPDIR=/Users/turner/.hermes/profiles/tb-king/cache/scratch/0a89b55d/mk-tmp bash -c 'test_paths=(); while IFS= read -r line; do test_paths+=("$line"); done < "$1"; /Users/turner/.hermes/hermes-agent/venv/bin/python -m pytest -q --tb=line --deselect=tests/agent/test_auxiliary_client.py::TestAuxiliaryAuthRefreshRetry::test_refresh_provider_credentials_force_refreshes_anthropic_oauth_and_evicts_cache --deselect=tests/tui_gateway/test_tui_gateway_server.py::test_load_enabled_toolsets_rejects_disabled_mcp_env --deselect=tests/tui_gateway/test_tui_gateway_server.py::test_load_enabled_toolsets_falls_back_when_tui_env_invalid --deselect=tests/tui_gateway/test_tui_gateway_server.py::test_ensure_session_db_row_stamps_profile_name "${test_paths[@]}" </dev/null' _ /Users/turner/.hermes/profiles/tb-king/cache/scratch/0a89b55d/mk-tmp/fallback-tier-direct-import-tests.txt
```

Branch sweep: 3012 passed, 45 failed, 11 setup errors, 13 skipped, 2 xfailed. Output: `/Users/turner/.hermes/profiles/tb-king/cache/scratch/0a89b55d/mk-tmp/fallback-tier-direct-import-tests.log`. Untouched-base sweep with the same command minus the new test file reproduced the first 22 failures and all 11 setup errors, then a KeyboardInterrupt stopped it at 65% (2356 passed). The six later failing modules were run separately on base with `python -m pytest -q --tb=line tests/hermes_cli/test_web_server.py tests/tools/test_delegate_capacity_interrupt.py tests/tools/test_vision_history_budget.py tests/tui_gateway/test_bot_relay_methods.py tests/tui_gateway/test_groups_methods.py tests/tui_gateway/test_session_id_injection.py </dev/null`: the remaining 23 failing cases reproduced exactly (259 passed). Base logs: `fallback-tier-base-direct-import-tests.log`, `fallback-tier-base-late-failures.log` in the same scratch directory. The broad bare-pytest sweep has cross-file state and sandbox socket/process restrictions; the focused fallback run above is green.

`git diff --check` passed. No push, merge, install, restart, live config change, or pattern-based process kill was run.

## Commit blocker and exact HEAD

`git add` staged the 15 implementation/test/doc files. `git commit` failed creating `/Users/turner/.hermes/hermes-agent/.git/worktrees/fallback-service-tier-20260929/index.lock` with `Operation not permitted`. A scratch `GIT_INDEX_FILE` still failed at `COMMIT_EDITMSG`; `git write-tree` failed creating a temporary object. A later `git add MAKER-REPORT.md` also failed at `index.lock`. The Git directory is outside the writable roots. The code is staged; this report remains untracked because no commit can be made from this sandbox. Current `git rev-parse HEAD` is `a5c914f7efa8196a809a868a8a1264213b8d0e6a`.

The temporary base directory was removed. `git worktree remove` could not remove `/Users/turner/.hermes/hermes-agent/.git/worktrees/base-wt1` (`Operation not permitted`), leaving a stale worktree registration. This also needs Git-directory write access. A report cannot contain its own eventual final commit hash because changing the report changes that hash; the final hash must be recorded in the handoff after committing.
