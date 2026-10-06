# Merge official 35ad70c035, attempt 2

The user approved a 20-file cap. Git reported 15 conflicts. Focused validation is complete. All 24 selected test files exit 0; environment skips are listed below. Source: u2-evidence/attempt2-merge.txt and attempt2-merge-conflicts.txt. Full side diffs are under u2-evidence/attempt2-conflict-inputs.

## agent/auxiliary_client.py

Both sides: Sheldon selects a credential pool entry for the requested model. Official binds a selected credential to its allowed host.

Resolution: Pass model through the official paired credential/base resolver.

Proof: tests/agent/test_auxiliary_client.py; tests/agent/test_actual_auxiliary_routing.py; tests/hermes_cli/test_codex_credential_host_binding.py Results are in u2-evidence/attempt2-merge/results.jsonl and any later merge-fix run.

## hermes_cli/doctor_state.py

Both sides: Sheldon checks Bot Chat turn locks. Official checks plugin update provenance.

Resolution: Keep both independent checks.

Proof: tests/hermes_cli/test_doctor_bot_turn_locks.py; tests/hermes_cli/test_doctor_update_provenance.py Results are in u2-evidence/attempt2-merge/results.jsonl and any later merge-fix run.

## hermes_cli/update_cmd.py

Both sides: Sheldon refuses an update that would lose carried commits. Official resolves channels and release checkouts before a new completion runner.

Resolution: Keep official target selection and completion. Run the carried-commit check before lockfile cleanup or autostash. Extend it to branch and detached release checkouts.

Proof: tests/hermes_cli/test_update_carried_commits_guard.py; tests/hermes_cli/test_update_autostash.py; tests/hermes_cli/test_update_diverged_rescue_ref.py; tests/hermes_cli/test_update_target_identity.py Results are in u2-evidence/attempt2-merge/results.jsonl and any later merge-fix run.

## plugins/memory/supermemory/__init__.py

Both sides: Sheldon carries tunnel transport, pinned sockets and memory guards. Official replaces lazy dependency loading with pm.ensure_import.

Resolution: Keep transport and guards; use the official package loader under contextlib.suppress. Adapt its socket-test loader fake.

Proof: tests/plugins/memory/test_supermemory_socket_transport.py; tests/plugins/memory/test_supermemory_guards.py; tests/plugins/memory/test_supermemory_provider.py Results are in u2-evidence/attempt2-merge/results.jsonl and any later merge-fix run.

## plugins/platforms/slack/adapter.py

Both sides: Sheldon carries slash aliases. Official scopes stream state with tuple keys and updates the adapter.

Resolution: Keep the alias fields with the official stream handling.

Proof: tests/gateway/test_slack_slash_aliases.py Results are in u2-evidence/attempt2-merge/results.jsonl and any later merge-fix run.

## pyproject.toml

Both sides: Sheldon registers real single-query and old post-swap test markers. Official replaces old OS markers with platforms and removes the post-swap API.

Resolution: Use official markers and retain the real_single_query_hard_exit marker. Drop the obsolete post-swap marker.

Proof: Focused merge test collection and subsequent union tests. Results are in u2-evidence/attempt2-merge/results.jsonl and any later merge-fix run.

## tests/agent/test_codex_cloudflare_headers.py

Both sides: Sheldon fakes model-aware pool selection. Official tests the new paired credential/base resolver.

Resolution: Keep official tests and let pool fakes accept model.

Proof: This test file. Results are in u2-evidence/attempt2-merge/results.jsonl and any later merge-fix run.

## tests/agent/test_codex_usage_attribution.py

Both sides: Sheldon fakes model-aware pool selection. Official uses the new credential resolver.

Resolution: Keep official resolver tests and model-aware fakes.

Proof: This test file. Results are in u2-evidence/attempt2-merge/results.jsonl and any later merge-fix run.

## tests/hermes_cli/test_codex_credential_host_binding.py

Both sides: Sheldon adds model-aware pool regression. Official adds host-binding regressions.

Resolution: Keep all official cases and append the carried model case. Its opaque fake key targets a custom test host, because official rejects opaque keys on ChatGPT.

Proof: This test file. Results are in u2-evidence/attempt2-merge/results.jsonl and any later merge-fix run.

## tests/hermes_cli/test_update_autostash.py

Both sides: Sheldon opted old fixtures into intentional reset. Official replaces obsolete update paths with the completion runner and real-Git rescue-ref coverage.

Resolution: Use official autostash tests. Preserve carried refusal contracts in test_update_carried_commits_guard, adapted to run_completion, and add release checkout cases.

Proof: This file; test_update_carried_commits_guard.py; test_update_diverged_rescue_ref.py. Results are in u2-evidence/attempt2-merge/results.jsonl and any later merge-fix run.

## tests/tools/test_bot_live_owner_delivery.py

Both sides: Sheldon covers owner-sidecar write failure. Official changes shared delivery setup.

Resolution: Keep the carried failure case with the merged official setup.

Proof: This test file. Results are in u2-evidence/attempt2-merge/results.jsonl and any later merge-fix run.

## tests/tools/test_oneshot_completion_linger.py

Both sides: Sheldon checks flush, session end and lease release before linger. Official revises the linger test.

Resolution: Keep the carried ordering contract and the compatible official test body. C2-160 and C2-161 are KEEP under U1.

Proof: This test file; tests/hermes_cli/test_single_query_session_finalize.py. Results are in u2-evidence/attempt2-merge/results.jsonl and any later merge-fix run.

## tests/tui_gateway/test_bot_relay_methods.py

Both sides: Sheldon checks reply_relayed and target scope. Official expands relay behavior.

Resolution: Keep carried assertions and official added cases.

Proof: This test file. Results are in u2-evidence/attempt2-merge/results.jsonl and any later merge-fix run.

## tools/bot_mode_dm.py

Both sides: Sheldon has generation-aware delivery identity, bounded child wait, fast acknowledgement, recipient admission and release at report. Official has a simpler delivery block.

Resolution: Keep the carried delivery block on the surrounding official code.

Proof: tests/tools/test_bot_mode_dm.py; tests/tools/test_bot_mode_dm_busy_queue.py; tests/tools/test_bot_live_owner_delivery.py Results are in u2-evidence/attempt2-merge/results.jsonl and any later merge-fix run.

## tools/process_registry.py

Both sides: Sheldon retains file-backed reader ownership. Official introduces reader-owned final draining.

Resolution: Keep both exclusive completion paths: file-backed polling first, official reader finish second.

Proof: tests/tools/test_process_registry.py; tests/tools/test_process_registry_list_exit.py Results are in u2-evidence/attempt2-merge/results.jsonl and any later merge-fix run.

Jev request attempt2-jev-002 asked about release checkout guarding. Automatic approval review rejected the MCP call because approval policy is never. No Jev answer is claimed. The implemented choice preserves carried commits before any destructive update preparation.

## Focused validation adjustments

The official autostash and ahead-release cases intentionally discard local history. They now opt into carried_commits_policy=reset. Default refusal has 19 passing real-Git cases, including four new branch/detached release cases. Mailbox sidecar metadata is excluded from the message count in the relay receipt test (C2-014 intent).

The socket fixtures now honor TMPDIR. They skip real-socket checks when ancestor ownership or permissions cannot satisfy the product guard. This sandbox maps /home to uid 65534 and has group-writable workspace ancestors. The product checks are unchanged. The merge-fix run has 3 socket tests passed and 13 skipped; helper tests have 71 passed and 14 skipped. Real socket transport is not checked on this host. See attempt2-test-isolation.md.

Jev request 003 for this validation choice was rejected by automatic approval review (approval policy never). The analyst chose truthful environment skips over faking trusted ancestors or weakening the guard.

Final focused result at 2026-10-06 16:37:42 CDT: 24 files exit 0. Evidence: u2-evidence/attempt2-merge-final-tests.json. Removing the release guard made all four new release-checkout cases fail. Restoring it made the 19-case file pass. Evidence: attempt2-release-mutation/results.jsonl and attempt2-merge-fix3/results.jsonl. The relay budget test now uses a fixed one-second elapsed interval and checks a five-second remaining budget, independent of host load.
