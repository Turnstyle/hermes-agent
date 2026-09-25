# Supermemory Memory Provider

Semantic long-term memory with profile recall, semantic search, explicit memory tools, and per-turn conversation capture into one document per session per 4-hour window for richer profiles.

## Requirements

- The `supermemory` SDK, prepared through PM by `hermes memory setup` when you select Supermemory. Restart Hermes after preparation; do not install into its selected environment with pip.
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
| `tunnel` | unset | `{"ssh_host", "forward"}` of an SSH forward to a local Unix socket; every request then goes only to that socket, checked before each use and each request. `base_url` must be unset (see [SSH tunnel](#ssh-tunnel)) |

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
- If the proof fails in a single-profile process, `SUPERMEMORY_API_KEY` is removed from the process environment, so child processes that copy it do not inherit the key. Under a multiplexed gateway the shared environment is left alone and the provider simply refuses the key.
- The provider never edits a bound secret scope. TUI and Desktop bodies bind one even in a single-profile process, and multiplexed gateways bind one per profile. A key held there stays in that process until it exits, although the provider refuses it. Restart the process to remove it.

The check is not only made at start. With `require_availability_proof` or `tunnel` set, every client use (tools, prefetch, capture, mirroring) re-checks the proof and the key in the current scope, and the tunnel when there is one. Nothing is cached between uses. When a re-check fails, the provider drops its client and its in-memory key, logs a warning, and tool calls return `Supermemory is disabled for this session: <reason>`. That provider instance stays off; a new session runs the start-up gate again.

## SSH tunnel

For a proxy that is reachable only through SSH, forward a **Unix socket**, never a TCP port. Another local user
can bind a TCP port the moment ssh lets go of it; nobody else can create a socket inside a directory that only
you can enter. Create the directory once, then run the tunnel (launchd, systemd, or by hand):

```sh
install -d -m 0700 ~/.hermes/supermemory-tunnel      # yours, mode 0700; check `ls -lde` shows no ACL
ssh -N -o ExitOnForwardFailure=yes -o StreamLocalBindMask=0177 -o StreamLocalBindUnlink=yes \
    -L "$HOME/.hermes/supermemory-tunnel/rosie.sock:127.0.0.1:6768" rosie
```

Then point the provider at the socket. `forward` is exactly the `-L` value, with an absolute, symlink-free path;
`base_url` (and `SUPERMEMORY_BASE_URL`) must be unset:

```json
{
  "require_availability_proof": true,
  "tunnel": {"ssh_host": "rosie", "forward": "/Users/me/.hermes/supermemory-tunnel/rosie.sock:127.0.0.1:6768"}
}
```

With a `tunnel` block:

- **The socket is the only route.** The SDK's HTTP client has a single transport, `httpx.HTTPTransport(uds=<socket>)`.
  There is no TCP URL (the SDK sees the placeholder `http://supermemory-tunnel.invalid`), no environment proxy, no
  keep-alive connection and no fallback. A TCP `forward`, or any `base_url`, makes the provider unavailable.
- **Before every request** (inside the transport, so every caller is covered): every directory above the socket is a
  real directory owned by root or you and not writable by others (unless sticky, like `/tmp`); the socket's own
  directory is yours with mode 0700; the socket is a socket owned by you with no group/other permissions; and it is
  the same socket (inode) the session's start-up check verified. Otherwise the request is refused before a byte is
  sent.
- **Before every use:** the same checks, plus the listener: an empty connection's peer credentials must name a
  process of yours whose exact argv is an ssh forward of `forward` to `ssh_host` (parsed the way ssh parses it;
  options that could redirect the connection or weaken host-key checks fail closed, and so does a remote command).
  A refused connection (a stale socket file) counts as down. So does a different socket at the same path, even a
  working one after a tunnel restart: that session stays off and the next session verifies the new socket.

`tunnel_key_helper.py` (stdlib only) is the matching `secrets.command` helper. It runs the same socket and listener
checks, then an unauthenticated `/health` through the socket that must answer like the expected proxy. **The key never
crosses the local socket:** the helper reads the key and makes the authenticated `/health` request in one `ssh <host>`
session, against the forward's remote end on that host. See its docstring for a `secrets.command` example; use
`override_existing: true` there so the fresh key and proof beat inherited values.

What this does not protect against:

- **This same user, or root.** Either can replace the socket, read the helper's output, or read the key from the
  Hermes process. The checks catch accidents from them, not attacks.
- **A compromised ssh host, or other accounts on it.** The forward's far end is the proxy's loopback TCP port on that
  host. If the proxy is down, another account there could bind that port and receive forwarded requests with the key.
- **What the key can do on the server.** The `containers` permissions limit what this provider sends; they do not limit
  another holder of the key. Scope the key on the server side (read-only, per container, short-lived) if you need that.
- **ACLs.** The directory checks read POSIX mode bits only; an ACL that grants another user access is not seen.
- **Lifetime.** The checks run when the provider is used. An idle process keeps its key until it is used (and refuses)
  or exits, and a key in a bound secret scope stays until the process exits. To remove a key from memory, stop the
  process; to invalidate a key that may have been copied, rotate it on the server.

## Support

- [Supermemory Discord](https://supermemory.link/discord)
- [support@supermemory.com](mailto:support@supermemory.com)
- [supermemory.ai](https://supermemory.ai)
