"""``/p/<profile>/`` resolution on a multiplexed API listener, including unique aliases.

Peers address bots by the name the roster shows (display name, Bot Chat title) or a pre-rename
id. A prefix that is not a served profile id maps to the canonical served id only when exactly one
served profile answers to that slug; no match or an ambiguous match fails closed (404).
"""

import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def profile_alias_slug(text) -> str:
    """Lowercase, non-alphanumeric runs -> '-' ("Web-Reviewer" and "web reviewer" -> "web-reviewer")."""
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower()).strip("-")


def resolve_profile_alias(prefix: str, served_pairs) -> Optional[str]:
    """Canonical served profile whose display name, Bot Chat title, or previous name slugs to
    ``prefix``; None when nothing or more than one profile matches."""
    want = profile_alias_slug(prefix)
    if not want:
        return None
    from hermes_cli.profiles import read_profile_meta

    hits = set()
    for name, home in served_pairs:
        try:
            meta = read_profile_meta(Path(home))
        except (TypeError, ValueError, OSError):  # an unreadable home answers to no alias
            continue
        names = [meta.get("display_name"), meta.get("bot_title"), *(meta.get("previous_names") or [])]
        if any(profile_alias_slug(n) == want for n in names if n):
            hits.add(name)
    if len(hits) != 1:
        if hits:
            logger.warning("api_server: /p/%s/ alias is ambiguous (%d served profiles); refused",
                           prefix, len(hits))
        return None
    found = hits.pop()
    logger.info("api_server: /p/%s/ resolved by alias to profile %s", prefix, found)
    return found


def resolve_served_profile(profile: str, rejected):
    """The served profile *profile* names (exact id first, then a unique alias), else *rejected*."""
    try:
        from hermes_cli.profiles import profiles_to_serve
        served_pairs = list(profiles_to_serve(multiplex=True))
    except Exception:  # health: allow BLE001 -- an unreadable roster serves no prefix (fail closed)
        return rejected
    if profile in {name for name, _ in served_pairs}:
        return profile
    return resolve_profile_alias(profile, served_pairs) or rejected
