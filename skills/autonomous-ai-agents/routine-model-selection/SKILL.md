---
name: routine-model-selection
description: Select eligible routes while preserving model constraints.
version: 1.0.0
author: Sheldon King, Hermes Agent
license: MIT
metadata:
  hermes:
    category: autonomous-ai-agents
    tags: [models, kanban, routing]
---

# Routine model selection Skill

Select a routine route through the existing Kanban selection and fallback paths.
Routine model names are preferences. Task-essential evaluation identity, quality,
security, policy, authentication, and quota constraints remain binding.

## When to Use

Taskers, Coordinators, Conductors, Kings, and edge core/CoS seats use this procedure
when a preferred model is renamed, missing, exhausted, or unavailable. Use it for
metadata lookup before dispatch, not to reopen completed or held cards.

## Prerequisites

Use `read_file` for the original card/checkpoint/output contract and owner-exported
metadata. Use `terminal` for the existing authorized reader and Hermes CLI.
The source-verified reader is `/Users/sheldon/.hermes/fleet-node-state/fleet_ops.py`,
owned by `tb-mc-firestore`, SHA256
`60f25f2506d36d72bc39b97681cf8ac8376a29c0166b00ccd88bad16460a8434`.
It targets `mission-control-444444/fleet-operations`. This candidate does not
invoke that reader at runtime. Conductor owns current route resolution;
`shld-mc-firestore` has a standing prohibition on Firestore calls.
Do not guess an installed binding from a historical carry path.

The canonical collections are `model_capabilities` and
`fleet_profile_config_v1`, including `model_chain` and
`credential_provider_names`. The current reader source confirms
`mission-control-444444/fleet-operations`; live capability population is unproved.
No `model_routes` collection, ADC, credentials, collector, or registry writes.

## How to Run

1. Ask the owning authorized reader for `model-routes get --model MODEL_ID --json`
   and `profile-config get NODE PROFILE --json`. The former reads the exact
   `model_capabilities` document. The latter reads `fleet_profile_config_v1`
   at `NODE~PROFILE`. `model-routes get --node NODE --profile PROFILE --json`
   reads `fleet_node_state`, not profile-config. Only the owner runs these
   commands when local access is absent.
2. Export the two results to the metadata contract below. Confirm the mapping
   against the actual reader output before production use. Raw capability
   documents expose model-wide fields such as `fleet_access`, vendor and context;
   they do not establish this candidate's profile-specific authenticated/usable
   proof and task quality contract. The profile-config reader allows up to 20
   chain entries plus toolsets/platforms and other metadata. This candidate's
   reduced export accepts only selection fields and up to 32 entries. The
   adaptation is explicit and remains owner work, not a production mapper.
   This implementation has fixture coverage; file presence is not proof of a live reader or rollout.
3. Preview using `terminal`:

```text
hermes kanban select-model CARD_ID --policy-file owner-export.json
```

An authorized operator may add `--apply` to save policy for next dispatch.
This command does not release holds, requeue, claim, or accept output. Use the
existing owner-supported unblock/reclaim/resume path only after a separate
release and verified card/checkpoint identity. Do not reopen DONE cards.

## Quick Reference

- Ordinary names remain preferences, including names in loaded instructions.
- `hermes kanban create --model MODEL --provider PROVIDER --exact-model-reason REASON ...`
  creates a task-essential exact identity constraint. Provider and reason are required.
- `select-model` previews or applies a policy without changing original model
  preference, owner, body, checkpoint reference, output contract, or claim.
- `selected` means metadata selection. `model_output` and `accepted` stay
  `unobserved` until the existing completion/acceptance owners prove them.
- Invalid/unavailable metadata exits 2 without selecting a route or retrying it.

## Procedure

The policy JSON has `node`, `profile`, `task_class`, `required_capabilities`
array, `minimum_quality` number in [0,1], optional `checkpoint_ref`, optional
`exact_reason`, and `snapshot`. An exact-only policy may contain just
`exact_reason`; it preserves the explicitly chosen route and disables all
cross-model fallback. It does not prove authentication or route availability.

`snapshot` has only the two canonical collection keys. The export contract is:

```json
{
  "model_capabilities": [
    {"node":"NODE","profile":"PROFILE","provider":"PROVIDER","model":"MODEL",
     "capabilities":["tools"],"quality":{"coding":0.9},"uncertain":false,
     "proof":{"authenticated":true,"usable":true,"observed_at":"UTC_TIMESTAMP"}}
  ],
  "fleet_profile_config_v1": {
    "node":"NODE","profile":"PROFILE","model_chain":[{"provider":"PROVIDER","model":"MODEL"}],
    "credential_provider_names":["PROVIDER"],"observed_at":"UTC_TIMESTAMP"
  }
}
```

This is an owner-export boundary, not the raw Firestore document shape.
Source-backed automated reader invocation/adaptation is not implemented here. Never invent missing proof or copy credential values.
Provider names alone do not prove authentication. Proof must match the node and
profile, be successful, and be at most 900 seconds old with no future timestamp.
Capabilities and task-specific quality must independently meet the requirements.
Preference wins among eligible routes; otherwise the existing model chain order wins.

`failed_routes` optionally contains objects with `provider`, `model`,
`observed_at`, and `kind` from quota/auth/policy/timeout/outage/missing_model.
Failure excludes a route until strictly newer successful proof. Policy failures
remain excluded until an owner explicitly replaces that held policy.

Uncertain metadata requires a real owner-supplied Jev advisory in `advisory`
with `ok:true`, fresh `observed_at`, and `eligible_routes` naming the route.
Advice never grants authorization, authentication, or missing mandatory quality.
Preserve real `not_configured` or other refusal in the same advisory object.
No Jev setup, PRODUCT key, synthesized advice, or paid provider ping. Exported
advice is read once; no remote wait or retry occurs in selection. If advice is
unavailable, verified certain routes may proceed. Otherwise fail explicitly.

## Pitfalls

No current eligible route means an owner/capability gap. It is not permission to
swap accounts or lower quality. The existing provider resolver still verifies
local usability, and existing retry/cooldown/entitlement rules still apply.
The selected eligible chain is pinned against per-turn broadening. Each fallback
candidate is rechecked for fresh proof before the existing fallback resolver runs.
No new receipt router or observer is installed by this skill.

## Verification

Use `read_file` to verify actual selection events and accepted output separately.
Record fixture versus live evidence, original card/checkpoint, selected reason,
CLI/model/provider, and next owner. Owner-controlled distribution is proved only
by actual calls from each target seat. A source skill or committed CLI alone is
not an installed, live, or fleet-wide acceptance claim.
