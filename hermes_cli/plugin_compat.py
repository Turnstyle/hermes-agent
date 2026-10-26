"""Frozen exports for ``hermes update`` processes that predate the plugin compat layer's removal.

The Sep 2026 decomposition kept old import paths alive for external plugins until 2026-09-14; that
layer, and the scanner that reported plugins still using it, is gone. An updater already running from
an older checkout still lazy-imports these three names for its post-update notice after the swap
(``tests/compat/old_updater_surface.json``), so they stay as inert stubs with nothing left to report.

External plugins installed on fleet profiles still import ``HermesPluginCompatWarning``,
``warn_once`` and ``quiet_for_interactive`` from here, so those names stay importable and keep
their warning contract. Nothing in Hermes resolves a moved name through them any more: the removal
stays in effect.
"""
from __future__ import annotations

import datetime as _dt
import logging
import warnings
from typing import Any

COMPAT_REMOVAL_DATE = _dt.date(2026, 9, 14)
COMPAT_REMOVAL = COMPAT_REMOVAL_DATE.isoformat()

_log = logging.getLogger(__name__)
_seen: set[tuple[str, str]] = set()


class HermesPluginCompatWarning(FutureWarning):
    """A plugin imported a name from its pre-decomposition module path."""


def compat_report(manifests: Any = None, *, force: bool = False) -> dict[str, list[Any]]:
    return {}


def removal_in_effect(today: Any = None) -> bool:
    return True


def summary_lines(report: Any, *, today: Any = None) -> list[str]:
    return []


def warn_once(facade: str, name: str, target_module: str, target_name: str) -> None:
    """Per-name record that a moved name was resolved through its old path: a
    ``HermesPluginCompatWarning`` (so ``-W error`` catches it in plugin authors' CI) plus one
    WARNING log line."""
    key = (facade, name)
    if key in _seen:
        return
    _seen.add(key)
    msg = (f"hermes plugin compat: `{facade}.{name}` moved to `{target_module}.{target_name}`. The old "
           f"path was removed on {COMPAT_REMOVAL}; update your import.")
    _log.warning(msg)
    warnings.warn(msg, HermesPluginCompatWarning, stacklevel=3)


def quiet_for_interactive() -> None:
    """Hide the warning category from interactive stderr. Appends (does not override), so an
    explicit ``-W error::...HermesPluginCompatWarning`` still wins."""
    if not any(action == "error" and category is not None and issubclass(HermesPluginCompatWarning, category)
               for action, _msg, category, _mod, _line in warnings.filters):
        warnings.filterwarnings("ignore", category=HermesPluginCompatWarning, append=True)
