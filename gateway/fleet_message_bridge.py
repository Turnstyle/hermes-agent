"""Authority-owned scheduling of each served profile's fleet DM queue.

The fleet queue (``fleet_messages_v1`` plus the recipient's local spool) is drained only here, in
the gateway that owns the profile's Bot Chat authority, and only through that authority's own
delivery door (``tools/fleet_message_admission.py``). This replaces the retired TUI idle poller
and post-turn hook, whose UI-lease execution no longer exists.

Scheduling policy, per served profile, one bounded step every ``TICK_SECONDS``:

- Human priority: nothing is admitted while the Bot Chat FIFO holds a queued, started or unknown
  admission (a human turn, a goal continuation, a mailbox delivery) or a session writer holds
  its turn lease (``SessionDB.session_turn_lease_holder``, a read-only diagnostic: the authority
  still decides admission, this only declines to ask).
- Bounded bursts: at most one fleet message per step, and only once the previous one's native
  receipt settled and its outcome is recorded (``settle_inflight``).
- Fairness: after ``FLEET_RUN_BEFORE_YIELD`` consecutive fleet admissions an idle step yields, so
  a goal or another producer polling on its own schedule gets the boundary.
- Strict identity: each step runs under the owning profile's scope (``owner_scope``) and drains
  only docs addressed to that profile's own fleet identity, into that profile's own authority.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TICK_SECONDS = 15.0
FLEET_RUN_BEFORE_YIELD = 2
_PENDING_STATUSES = ("queued", "started", "unknown")


def bot_chat_busy(authority: Any) -> bool:
    """Whether the profile's Bot Chat has work ahead of a fleet message. Read-only."""
    db = authority.db
    row = db.get_session_by_title("Bot Chat")
    if row is None:
        return False  # the door creates the chat on the first delivery
    tip = db.get_compression_tip(row["id"]) or row["id"]
    with db._read_ctx() as conn:
        pending = conn.execute(
            "SELECT 1 FROM session_admissions WHERE target_session_id=? AND status IN (?,?,?) LIMIT 1",
            (tip, *_PENDING_STATUSES)).fetchone()
    if pending is not None:
        return True
    return db.session_turn_lease_holder(tip) is not None


class FleetMessageBridge:
    """One drain step per served profile per tick; never two concurrent steps for one profile."""

    def __init__(self, runner: Any):
        self.runner = runner
        self._streak: dict[str, int] = {}
        self._running: set[str] = set()

    async def tick(self) -> dict[str, str]:
        from gateway.session_authorities import serving_authorities

        results: dict[str, str] = {}
        for authority in serving_authorities(self.runner):
            # The profile's home comes from its own authority store, never from the launch env.
            home = Path(authority.db.db_path).parent.resolve()
            key = str(home)
            if key in self._running:
                results[key] = "running"
                continue
            self._running.add(key)
            try:
                results[key] = await asyncio.to_thread(self._drain_profile, authority, home)
            except Exception:  # health: allow BLE001 -- one profile's failure must not stop the rest
                logger.warning("fleet message drain step failed for %s", home, exc_info=True)
                results[key] = "error"
            finally:
                self._running.discard(key)
        return results

    def _drain_profile(self, authority: Any, home: Path) -> str:
        from gateway.session_authorities import owner_scope
        from tools.fleet_message_admission import drain_once
        from tools.fleet_message_drain import bot_identity, drain_config, inspect_recipient_queue

        # The opt-in is read from the profile's own config.yaml first, so a profile that never
        # enabled the queue costs one file read per tick and never enters its scope.
        if not inspect_recipient_queue(bot_identity(home), profile_home=home).enabled:
            return "disabled"
        # Config, the registry credentials and the spool all resolve under the OWNING profile.
        with owner_scope(authority, hydrate_secrets=True):
            config = drain_config()
            if config is None:
                return "disabled"
            key = str(home)
            busy = lambda: bot_chat_busy(authority)  # noqa: E731
            if self._streak.get(key, 0) >= FLEET_RUN_BEFORE_YIELD:
                self._streak[key] = 0
                drain_once(home, config=config, busy=lambda: True)  # settle + expire only
                return "yielded"
            result = drain_once(home, config=config, busy=busy)
        if result in ("admitted", "settled", "failed", "uncertain"):
            self._streak[key] = self._streak.get(key, 0) + 1
        elif result in ("busy", "idle"):
            self._streak[key] = 0
        return result


async def fleet_drain_watcher(runner: Any, *, interval: float = TICK_SECONDS) -> None:
    """Supervised gateway watcher (``_spawn_supervised``): a bounded drain step for every served
    profile each ``interval``. A profile without ``fleet_messages.drain_on_turn_end`` costs one
    config file read per tick. Cancellation (shutdown) ends it between steps; a step already in
    its worker thread finishes its one bounded door call and records that outcome."""
    bridge = FleetMessageBridge(runner)
    while True:
        await bridge.tick()
        await asyncio.sleep(interval)
