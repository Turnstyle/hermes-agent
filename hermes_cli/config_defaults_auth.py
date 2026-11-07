"""``auth:`` defaults for :data:`hermes_cli.config_defaults.DEFAULT_CONFIG`: login policy (the
credentials themselves live in auth.json / .env)."""

AUTH_DEFAULTS = {
    # true: this profile's bots never read an OS keychain, a borrowed Claude Code / gh login or a
    # git credential helper; Anthropic auth is access-only (explicit scoped injection or private
    # credentials/anthropic.json) and the Keys Router owns renewal. Owner CLI sign-ins are untouched.
    "no_keychain": False,
    # Borrow and refresh the Codex CLI (~/.codex/auth.json) and Claude Code (~/.claude/.credentials.json)
    # logins automatically when Hermes has no usable login of its own. Their refresh tokens are single-use
    # and rotate, so two programs on one login can log each other out; set false to make Hermes use only
    # its own logins (`hermes auth add <provider>`). `hermes auth add openai-codex` still offers the import
    # interactively.
    "adopt_external_logins": True,
    # How `hermes auth add openai-codex` / `hermes model` sign in to OpenAI Codex.
    # "device_code" (default): open a URL, enter a code. "browser": authorization-code + PKCE on
    # the loopback listener http://localhost:1455/auth/callback (the redirect OpenAI registered
    # for the Codex client) — for organizations that disable the device-code grant. Falls back
    # to device code when that port is busy. `hermes auth add openai-codex --browser` opts in
    # for one login without changing this key.
    "codex_login_flow": "device_code",
}
