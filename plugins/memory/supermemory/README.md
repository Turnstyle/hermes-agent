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
| `tunnel` | unset | `{"ssh_host", "forward"}` of the SSH local forward that `base_url` goes through; its listener is checked at start and while running (see [Availability Proof](#availability-proof)) |
| `availability_recheck_seconds` | `10` | How long a passed live tunnel check is trusted before the next client use re-runs it (1 to 300) |

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

The check is not only made at start. With `require_availability_proof` or `tunnel` set, every client use (tools, prefetch, capture, mirroring) re-checks the proof and the key in the current scope. With a `tunnel` block it also re-checks the tunnel's listener, at most every `availability_recheck_seconds`. When a re-check fails, the provider drops its client and its in-memory key, logs a warning, and tool calls return `Supermemory is disabled for this session: <reason>`. That provider instance stays off; a new session runs the start-up gate again.

`tunnel_key_helper.py` (stdlib only) is a helper for a proxy behind an SSH local forward. **The key never crosses the local forward port.** The helper reads the key and makes the authenticated `/health` request in one `ssh <host>` session, against the forward's remote end on that host. SSH authenticates the host, so a process that takes over the local port can't receive the key. Before that, it runs checks that carry no credential: the port is open; its only listener is this user's process whose exact argv is an ssh forward to the expected host (parsed the way ssh parses it; options that could redirect the connection or weaken host-key checks fail closed, and so does a remote command); and unauthenticated `/health` answers like the expected proxy. See its docstring for a `secrets.command` example; use `override_existing: true` there so the fresh key and proof beat inherited values. Pair it with a matching `tunnel` block:

```json
{
  "base_url": "http://127.0.0.1:16768",
  "require_availability_proof": true,
  "tunnel": {"ssh_host": "rosie", "forward": "127.0.0.1:16768:127.0.0.1:6768"}
}
```

`base_url` must be the local end of `tunnel.forward`, otherwise the provider stays unavailable. Between two live checks (up to `availability_recheck_seconds`), the provider's own requests still go to the local TCP port. A listener that replaces the tunnel inside that window could receive the key. Shorten the interval to narrow that window.

## Support

- [Supermemory Discord](https://supermemory.link/discord)
- [support@supermemory.com](mailto:support@supermemory.com)
- [supermemory.ai](https://supermemory.ai)
