# C2-143 configuration prerequisite

The read-only scan at 2026-10-06 17:12:08 CDT does not prove explicit queue mode on every managed profile. Source: u2-evidence/attempt2-busy-config-v3-{max,sheldon,turnerbook,snowdrop,rosie}.jsonl. Commands are in scripts/u2_busy_config_read_{linux,mac}.sh. The parser reads only literal display.busy_input_mode and the managed-bot marker; it does not establish running-process overrides or inherited effective settings.

Managed profiles without their own explicit queue scalar: Sheldon shld-keys-router and shld-task-breakdown; TurnerBook tb-core-advisor, tb-core-cos, tb-core-ea, tb-core-intern, tb-decider, tb-doctrine-detailer, tb-keys-router, tb-mc-firestore, tb-task-breakdown, tb-telephony-guru; Rosie rosie-jev. Rosie root explicitly says interrupt.

The DROP special case was not present in the starting Sheldon tree and was not imported. The kept default-queue behavior and explicit-interrupt option remain. This source build does not authorize changing live settings. The C2-143 deployment prerequisite remains unproven and must be resolved before fleet rollout.

Earlier v1 scan read the wrong section and v2 lacked a YAML parser on the Macs. They are superseded by v3 and are not used as evidence of configuration. No config or service was changed.
