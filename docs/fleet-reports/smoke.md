# Scratch-home smoke

PASS at source 9cfe45f5623b9f305a923ef4eecd5d195e07375c. Started 2026-10-06 18:28:39 CDT; finished 2026-10-06 18:29:03 CDT. Source: u2-evidence/smoke/receipt.json:2.

Every gateway and CLI command below binds HERMES_HOME on the same command line. The effective home was checked before start. HOME, cache, runtime, state, logs and temporary files were under WORK. Only the scratch gateway created by this script was signalled. Source: scripts/u2_smoke.py:24 and u2-evidence/smoke/receipt.json:10.

## Commands and results

- home-check: 2026-10-06 18:28:39 CDT.
effective_home: /home/max/Scratchpad/5-Oct-2026_Mass-Review/work/hermes-union/scratch-home

- gateway-start: 2026-10-06 18:28:39 CDT.

```sh
/usr/bin/env HERMES_HOME=/home/max/Scratchpad/5-Oct-2026_Mass-Review/work/hermes-union/scratch-home /home/max/Scratchpad/5-Oct-2026_Mass-Review/work/hermes-union/u2-test-env/bin/python -m hermes_cli.main gateway run --no-supervise -v
```

- health: 2026-10-06 18:28:46 CDT.
status: PASS

http_status: 200

response: {"status": "ok", "platform": "hermes-agent", "version": "0.21.5"}

- running: 2026-10-06 18:28:46 CDT.
status: PASS

gateway_state: running

code_sha: 9cfe45f5623b9f305a923ef4eecd5d195e07375c

- cli: 2026-10-06 18:28:52 CDT.

```sh
/usr/bin/env HERMES_HOME=/home/max/Scratchpad/5-Oct-2026_Mass-Review/work/hermes-union/scratch-home /home/max/Scratchpad/5-Oct-2026_Mass-Review/work/hermes-union/u2-test-env/bin/python -m hermes_cli.main kanban create U2 scratch smoke card --assignee default --workspace scratch
```

rc: 0

stdout: Created t_78208acc  (ready, assignee=default)


stderr:
⚠  Gateway is running but kanban.dispatch_in_gateway=false in config.yaml — the task will sit in 'ready' until you flip it back on and restart the gateway, OR run the legacy standalone daemon (`hermes kanban daemon --force`).


- cli: 2026-10-06 18:29:00 CDT.

```sh
/usr/bin/env HERMES_HOME=/home/max/Scratchpad/5-Oct-2026_Mass-Review/work/hermes-union/scratch-home /home/max/Scratchpad/5-Oct-2026_Mass-Review/work/hermes-union/u2-test-env/bin/python -m hermes_cli.main kanban dispatch --dry-run --max 1 --json
```

rc: 0

stdout: {
  "reclaimed": 0,
  "crashed": [],
  "timed_out": [],
  "stale": [],
  "auto_blocked": [],
  "promoted": 0,
  "reaped_terminal_workers": [],
  "reclaim_phase": "skipped (dry-run)",
  "spawned": [
    {
      "task_id": "t_78208acc",
      "assignee": "default",
      "workspace": ""
    }
  ],
  "skipped_unassigned": [],
  "skipped_placeholder": [],
  "skipped_nonspawnable": [],
  "owner_unavailable": [],
  "skipped_per_profile_capped": [],
  "auto_assigned_default": [],
  "respawn_guarded": [],
  "respawn_guard_lifted": [],
  "rate_limited": [],
  "profile_busy": [],
  "skipped_locked": false,
  "memory_pressure": null,
  "capacity_held": null,
  "reclaim_errors": [],
  "claim_errors": []
}


stderr:

- bot-chat-create: 2026-10-06 18:29:01 CDT.
http_status: 201

- bot-chat-send: 2026-10-06 18:29:02 CDT.
status: PASS

http_status: 200

response: {"object": "hermes.session.chat.completion", "session_id": "u2-scratch-bot-chat", "message": {"role": "assistant", "content": "Fleet union scratch Bot Chat answered."}, "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "runtime": {"provider": "custom", "model": "smoke-local", "route_source": "global", "requested": {"provider": "", "model": ""}}}, "runtime": {"provider": "custom", "model": "smoke-local", "route_source": "global", "requested": {"provider": "", "model": ""}}}

## Boundaries and cleanup

The model was a deterministic local HTTP fixture on 18899. The real gateway, session API and agent turn code ran. This proves the local integration, not a real provider or cross-machine DM. The provider request list and answer are in u2-evidence/smoke/receipt.json.

The gateway received SIGTERM after the checks and completed its drain, adapter disconnect and database close. It exited 1 by its stated signal-shutdown policy. No force kill was needed. The runner recorded scratch_gateway_stopped=true. Both 8799 and 18899 subsequently refused connections. Sources: u2-evidence/smoke/gateway.log (last 15 lines), u2-evidence/smoke/receipt.json (final fields), u2-evidence/attempt2-smoke-stopped.json:2.

Kanban gateway dispatch was disabled so the fixture card could not launch a worker. The create warning is preserved above. The explicit dispatch command used --dry-run and returned the selected card without a Python error. No live service or board was changed.
