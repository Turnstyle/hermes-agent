# Supermemory Memory Provider

Semantic long-term memory with profile recall, semantic search, explicit memory tools, and per-turn conversation capture into one document per session per 4-hour window for richer profiles.

## Requirements

- `pip install supermemory`
- Hosted: API key from [app.supermemory.ai/integrations?connect=hermes](http://app.supermemory.ai/integrations?connect=hermes)
- Self-hosted: a running [Supermemory local](https://supermemory.ai/docs/self-hosting/overview) server and the API key it prints on first boot

## Setup

```bash
hermes memory setup    # select "supermemory"
```

Or manually:

```bash
hermes config set memory.provider supermemory
echo 'SUPERMEMORY_API_KEY=***' >> ~/.hermes/.env
```

For a fully self-hosted setup, start Supermemory local and note the API key it
prints on first boot:

```bash
npx supermemory local
```

Before running `hermes memory setup`, add the local endpoint to
`$HERMES_HOME/supermemory.json`:

```json
{
  "base_url": "http://localhost:6767"
}
```

Then run `hermes memory setup` and enter the local server's API key. Configuring
the endpoint first ensures the setup connection probe also stays local.

## Config

Config file: `$HERMES_HOME/supermemory.json`

| Key | Default | Description |
|-----|---------|-------------|
| `base_url` | `https://api.supermemory.ai` | API endpoint for hosted or self-hosted Supermemory. Takes priority over `SUPERMEMORY_BASE_URL`. |
| `container_tag` | `hermes` | Container tag used for search and writes. Supports `{identity}` template for profile-scoped tags (e.g. `hermes-{identity}` → `hermes-coder`). |
| `auto_recall` | `true` | Inject relevant memory context before turns |
| `auto_capture` | `true` | Store cleaned user-assistant turns after each response |
| `max_recall_results` | `10` | Max recalled items to format into context |
| `profile_frequency` | `50` | Include profile facts on first turn and every N turns |
| `capture_mode` | `all` | Skip tiny or trivial turns by default |
| `search_mode` | `hybrid` | Search mode: `hybrid` (profile + memories), `memories` (memories only), `documents` (documents only) |
| `entity_context` | built-in default | Extraction guidance passed to Supermemory |
| `api_timeout` | `5.0` | Timeout for SDK requests |
| `containers` | unset | Per-container operation permissions (see [Container Permissions](#container-permissions)) |
| `require_availability_proof` | `false` | Require a per-process proof from a key helper, not just a key (see [Availability Proof](#availability-proof)) |

### Environment Variables

| Variable | Description |
|----------|-------------|
| `SUPERMEMORY_API_KEY` | API key (required) |
| `SUPERMEMORY_BASE_URL` | Compatibility fallback for the API endpoint when `base_url` is not configured |
| `SUPERMEMORY_CONTAINER_TAG` | Override container tag (takes priority over config file) |

Base URL precedence is `supermemory.json` → `SUPERMEMORY_BASE_URL` →
`https://api.supermemory.ai`. Hermes resolves it once and uses the same endpoint
for SDK operations and setup/status probes.

## Tools

Kebab-case names are registered for the agent; snake_case aliases remain supported.

| Tool | Alias | Description |
|------|-------|-------------|
| `supermemory-save` | `supermemory_store` | Store an explicit memory |
| `supermemory-search` | `supermemory_search` | Search memories by semantic similarity |
| `supermemory-forget` | `supermemory_forget` | Forget a memory by ID or best-match query |
| `supermemory-profile` | `supermemory_profile` | Retrieve persistent profile and recent context |

## Source attribution

All Supermemory API calls send `x-sm-source: hermes`, and document writes stamp
`metadata.sm_source: hermes`. This is a **functional routing key, not telemetry**:
it groups Hermes-written memories into a dedicated "Hermes" Space in the
Supermemory app, so you can filter, browse, and bulk-manage them per source agent
(alongside Codex, Claude Code, etc.) from the Supermemory UI.

## Behavior

When enabled, Hermes can:

- prefetch relevant memory context before each turn
- write each completed user/assistant turn to **one document per session per 4-hour window** (`customId` = `<session>_<date>_b<0-5>`, so the API appends deltas), matching the capture shape of the other Supermemory agent integrations
- retry failed turn writes on the next turn, session end, `/reset`, or shutdown (at-least-once: if the API accepted a write but the response was lost, the same turn is appended again)
- route every SDK and probe request through the configured hosted or self-hosted endpoint
- expose explicit tools for search, store, forget, and profile access


## Profile-Scoped Containers

Use `{identity}` in the `container_tag` to scope memories per Hermes profile:

```json
{
  "container_tag": "hermes-{identity}"
}
```

For a profile named `coder`, this resolves to `hermes-coder`. The default profile resolves to `hermes-default`. Without `{identity}`, all profiles share the same container.

## Multi-Container Mode

For advanced setups (e.g. OpenClaw-style multi-workspace), you can enable custom container tags so the agent can read/write across multiple named containers:

```json
{
  "container_tag": "hermes",
  "enable_custom_container_tags": true,
  "custom_containers": ["project-alpha", "project-beta", "shared-knowledge"],
  "custom_container_instructions": "Use project-alpha for coding tasks, project-beta for research, and shared-knowledge for team-wide facts."
}
```

When enabled:
- `supermemory-search`, `supermemory-save`, `supermemory-forget`, and `supermemory-profile` accept an optional `container_tag` parameter
- The tag must be in the whitelist: primary container + `custom_containers`
- Automatic operations (turn capture, prefetch, memory write mirroring) always use the **primary** container only
- Custom container instructions are injected into the system prompt

## Container Permissions

Instructions alone do not stop a write. To make a container read-only (for example a shared, curated corpus),
give each container explicit operations:

```json
{
  "container_tag": "hermes-{identity}",
  "enable_custom_container_tags": true,
  "custom_containers": ["shared-knowledge"],
  "containers": {
    "hermes-{identity}": {"read": true, "write": true},
    "shared-knowledge": {"read": true, "write": false}
  }
}
```

- Keys accept `{identity}`, resolved like `container_tag`.
- Once `containers` is present it is **default-deny**: an unlisted container, or a missing `read`/`write`, gets nothing.
- `supermemory-save` and `supermemory-forget` need `write`; `supermemory-search`, `supermemory-profile` and prefetch need `read`. A refused call returns a tool error and logs a warning.
- Automatic writes need both `auto_capture: true` and `write` on the primary container. This covers turn capture and mirroring of built-in memory-tool additions (`on_memory_write`). With `auto_capture: false`, nothing is written unless the agent calls `supermemory-save`.
- Without `containers`, every whitelisted container allows every operation (previous behaviour).

## Availability Proof

By default the provider is available whenever `SUPERMEMORY_API_KEY` is set. When the key comes from a
`secrets.command` helper that can only work while some path is up (for example an SSH tunnel), set
`"require_availability_proof": true`. The provider is then available only when
`SUPERMEMORY_AVAILABILITY_PROOF` is `v1:<pid of this Hermes process>:<first 16 hex of sha256(key)>`:

- A proof inherited from a parent process (different pid) or minted for another key is rejected.
- `down:<pid>:<reason>` means the helper found the path down. The reason shows in `hermes memory status`.
- If the proof fails in a single-profile process, `SUPERMEMORY_API_KEY` is removed from the process environment so child processes do not inherit it. Under a multiplexed gateway the shared environment is left alone and the provider simply refuses the key.

`tunnel_key_helper.py` (stdlib only) is a helper for a proxy behind an SSH local forward. Before it reads any key, it checks three things: the port is open, the listener is this user's `ssh -L <forward> <host>` process, and unauthenticated `/health` answers like the expected proxy. It then reads the key over SSH and confirms authenticated `/health`. See its docstring for a `secrets.command` example; use `override_existing: true` there so the fresh key and proof beat inherited values.

## Support

- [Supermemory Discord](https://supermemory.link/discord)
- [support@supermemory.com](mailto:support@supermemory.com)
- [supermemory.ai](https://supermemory.ai)
