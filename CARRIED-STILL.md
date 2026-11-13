# Carried on top of v0.23.0 (Sheldon, serves shld-merge-marshal)

| Carry | Upstream | Why | Remove when |
|---|---|---|---|
| Unviewed parent resumes when its background work finishes | NousResearch/hermes-agent PR #153217, merge 8344f5a0b59bc7aa99482f759eabfda03c7aedbd (11 Nov 2026), with prerequisite PR #149568 (merged 10 Nov 2026) | Stock v0.23.0 writes a finished background helper's result into an unviewed session as a note and starts no turn, so the Merge Marshal left green queued PRs waiting until another bot messaged it (12-13 Nov 2026, Kanban t_d6cff895). | REMOVE WHEN RELEASED: drop this carry at the first Hermes release that contains 8344f5a0b59bc7aa99482f759eabfda03c7aedbd. Not in v0.23.1. Check on every update with `git merge-base --is-ancestor 8344f5a0b59b <release-tag>`. |

Owner order: Turner, 12 Nov 2026: carry a known upstream fix until it is released; keep it here and drop it when an update contains it.
Installed tree: Sheldon /Users/sheldon/.hermes/releases/official-hermes-v0230 (working-tree patch on top of local commit 6ff0933c, which is not on GitHub).
