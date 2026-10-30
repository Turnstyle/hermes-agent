# Context7 unattended helper overlay

This is an **overlay artifact**, not an env_loader replacement from this old fleet branch.

Live candidate: stable-v0.22.5-carry-d4c5aec662c8, original source commit
`d4c5aec662c801ffe102bba0ed14d87e85039389`, upstream `bf0203a794a07aac3ff764972ab9087baa4e4f4e`.
The local candidate is a tarball deployment without git metadata. The original
carry source is recorded on TurnerBook in CANDIDATE-SOURCE-E1.json; the commit is
not available from Turnstyle origin. Neither origin/main nor the closest available
fleet/0.21.5-union-rc3 contains this startup guard. Therefore this PR delivers the
exact reviewed five-line insertion as a patch for the live candidate only.
**Do not copy this branch's old env_loader.py into the live release.**

Expected live file before SHA256:
`0158b468b41de3fc2a819defae43de10940d1c93fe0a4a26309d50c10e1a89c2`
Expected after SHA256:
`755ed19cd94bafff49390e005030880628af66cc753899142d123dd8f8f82aaa`

The operator assertion must be literal YAML true and binary_path must be nonblank.
No-flag or no-helper unattended starts remain protected from desktop prompts.
No secret-reading command is part of these tests or config tools.

## Verification

Use Python >=3.10. The config tools need PyYAML from the existing Hermes runtime.

```
python scripts/fleet/test_context7_unattended_helper.py --source /isolated/patched/env_loader.py -v
python scripts/fleet/test_context7_helper_config_flags.py -v
python scripts/fleet/context7_helper_config_flags.py  # dry run only
```

The startup test AST-extracts the **actual** candidate function; it does not import
Hermes or contact 1Password. Before patching it must fail exactly the new-helper
case; after patching all six tests pass. The config test applies to synthetic
profiles under TMPDIR, verifies backup bytes/hash, excludes a different binary,
preserves comments/other fields and verifies second run idempotence.

Live discovery on Sheldon found **78** exact helper matches, not the estimated 35.
77 have cache_ttl_seconds 1800, shld-cndr has 86400. To meet the owner's daily-cache
requirement and avoid excess 1Password reads, the config script sets both
allow_unattended_helper true **and cache_ttl_seconds 86400** on only these exact
matches. It semantically verifies no other config change, preserves unrelated
bytes, backs up before each write, hashes before replacement, refuses concurrent
changes, and reads back after atomic os.replace. shld-keys-router itself currently
uses /opt/homebrew/bin/op, so is correctly excluded; its existing service token
path and 86400 TTL are unchanged. No credentials or model/fallback settings change.

Deployment requires merged exact-head review, cold backups, original live hash
check, same-folder temporary file/hash/os.replace, module-only stale pycache
cleanup if necessary, the config apply manifest, and a gateway reload **only** via
~/.hermes/fleet-node-state/u3g/g3f/gateway_coordinated_restart.py. Record status
success, release markers if required, and verify a new gateway PID. Do not claim
cached sessions adopted new configs. Artifact adoption here does not rebuild the
release or alter the staged source commit identity.

Keys Router was launched twice under the owner's requested sequence and declined
source authoring due to installed custody-only source/tool scope. The conductor
used the explicitly permitted two-failure author fallback. Writer served model:
GPT-6.1-Sol / openai-codex. Merge Marshal must use the strongest available reviewer,
at least as strong as this writer, under the current credentials/auth risk rule.
