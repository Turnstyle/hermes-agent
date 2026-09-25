# Cursor ACP: text-only interface with native tool effects

`cursor-acp` is **not a read-only or no-tools execution environment**. Cursor's
native shell has executed in confirmed `ask` sessions without sending permission
requests. Rejecting ACP permissions or observing tool events cannot prevent that
first side effect. Disabling Hermes toolsets does not disable Cursor tools.

Completions fail before spawning Cursor unless the caller explicitly acknowledges
native tool effects on that request:

```python
client.chat.completions.create(
    model=exact_advertised_model_id,
    messages=[{"role": "user", "content": "Explain this text."}],
    extra_body={"cursor": {"allow_native_tools": True}},
)
```

This is an acknowledgment, not a sandbox switch. Only use it where the operator
accepts Cursor-native file reads, writes, commands, network access, and configured
MCP activity. Do not use this provider for requests requiring tool safety. The
adapter accepts text messages and returns text/reasoning; it rejects Hermes tool
schemas, tool histories, non-text content, structured formats, and explicit
`tool_choice` / `function_call` settings (including `"none"`). It does not route
Cursor tool events back through Hermes approvals. Ask mode and permission refusal
remain behavior requests, not isolation claims. Native tool metadata is available
on the client result; Hermes' streaming turn does not persist or display it.

The acknowledgment is read only from `extra_body.cursor.allow_native_tools` and
must be boolean `true`; strings, missing values and constructor kwargs do not
opt in. Ordinary Hermes calls remain blocked unless they supply this request
option and omit tool requirements. The safety rejection is non-retryable and does
not request fallback from the provider classifier. A user-configured Hermes
fallback chain retains its existing announced behavior.

## Controls investigated (2026-09-25)

Local Cursor `2026.09.23-86fc751` help exposes `--mode ask|plan`,
`--sandbox enabled|disabled`, and force/approval options; `acp --help` exposes no
no-tools switch. [ACP documentation](https://prod.cursor.com/docs/cli/acp)
describes modes and approval requests, not a negotiated no-tools guarantee.
[CLI permissions](https://prod.cursor.com/docs/cli/reference/permissions) provide
shell/read/write/web/MCP deny rules; [CLI configuration](https://prod.cursor.com/docs/cli/reference/configuration)
provides a custom config directory, approval mode, and sandbox settings. Those
controls have not been proven to enforce a complete no-tools boundary in this
ACP route. This adapter neither changes the owner's Cursor config nor claims
that enabling the sandbox establishes that boundary. Default refusal is the
conservative path until enforcement is independently demonstrated.

## Lifecycle

Each child owns a new POSIX session/process group. Close sends EOF, then signals
the owned group with TERM and, after a bounded grace, KILL, including descendants
left after the leader exits. The reader owns stdout closure. Cancellation fences
new RPC requests during setup and bounds an unacknowledged prompt cancellation.
Closed/cancelled clients cannot open another session; construct a new client.
Platforms without POSIX process-group signaling fail before spawn. A descendant
that deliberately detaches into a different session is outside this group cleanup
contract; this mechanism is process cleanup, not a security sandbox.
