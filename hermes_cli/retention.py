"""Removal authority stays disabled until a separately reviewed enabling revision.

Preservation, age, interactive consent and runner-written permits are not authority.
There is deliberately no configuration or environment switch in this module.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetentionDecision:
    retained: bool = True
    removed: bool = False
    reason: str = "destructive_authority_off"

    @property
    def detail(self) -> str:
        return "Retained by default: destructive authority is disabled. Preservation does not authorize removal."


def removal_decision() -> RetentionDecision:
    """Return the non-enabling decision shared by cleanup callers."""
    return RetentionDecision()


class RetentionRefused(RuntimeError):
    """Existing bool/Attachment deletion APIs raise on retention, not false success.

    Missing objects still return False/None. No truthy replacement return type is
    introduced for callers that predate retention.
    """
    def __init__(self, reason: str = "destructive_authority_off"):
        self.reason = reason
        detail = (removal_decision().detail if reason == 'destructive_authority_off' else
                  f'Soft hide refused: {reason}. Existing rows and bytes are retained.')
        super().__init__(detail)
