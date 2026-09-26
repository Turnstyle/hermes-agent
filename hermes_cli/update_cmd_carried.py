"""``updates.carried_commits_policy``: refuse an update that would reset away commits carried on the
branch it updates.

Field incident (2026-09-25): the desktop hand-off updated an install whose ``main`` carried 13 local
commits; the same-branch reset parked them behind a rescue ref, the gateway restarted on code without
them, and the update exited 0. The guard fails closed: anything short of a readable ``reset`` refuses.
"""


def _carried_commits_policy() -> str:
    """``reset`` only from a config.yaml this run read successfully; ``unreadable`` when the read failed
    (the loader then serves defaults or the last good copy, and neither may authorize dropping
    commits); otherwise ``refuse``."""
    from hermes_cli.config import load_config
    from hermes_cli.config_read_errors import FailedConfigRead
    try:
        config = load_config()
    except Exception:
        return "unreadable"
    if isinstance(config, FailedConfigRead):
        return "unreadable"
    section = config.get("updates")
    policy = section.get("carried_commits_policy") if isinstance(section, dict) else None
    return "reset" if str(policy).strip().lower() == "reset" else "refuse"
