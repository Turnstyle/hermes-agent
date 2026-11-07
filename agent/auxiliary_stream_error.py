"""Adapter-delivered ``error`` frames on an auxiliary chat stream.

Some adapters yield an SSE ``error`` frame as an ordinary chunk instead of raising the SDK's
status-less ``APIError``. The accumulator would read it as a choice-less chunk, and ``finish()``
would then synthesize an empty successful completion (or the stream waits out its progress
timeout). Raising with the structured body lets the recovery ladder's status-less
structured-provider-error rung route it like the SDK-raised shape.
"""

from __future__ import annotations

from typing import Any


class AuxiliaryStreamError(RuntimeError):
    """An adapter-delivered SSE error, retaining the structured provider body for recovery."""

    def __init__(self, body: Any):
        # Never echo the provider payload into the message (it reaches logs); recovery reads ``body``.
        super().__init__("Auxiliary stream returned a structured provider error")
        self.body = body


def raise_on_error_frame(chunk: Any) -> None:
    """Raise :class:`AuxiliaryStreamError` when *chunk* is a dict, attribute or ``model_extra``
    error frame; ordinary chunks (no or empty ``error``) pass through untouched."""
    error = chunk.get("error") if isinstance(chunk, dict) else getattr(chunk, "error", None)
    extras = getattr(chunk, "model_extra", None)
    if error is None and isinstance(extras, dict):
        error = extras.get("error")
    if error:
        raise AuxiliaryStreamError(error)
