"""JSON CLI for the shared Board Ops API; no surface-specific policy."""

from __future__ import annotations

import json
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_board_ops import open_board_ops
from hermes_cli.kanban_board_ops_policy import Refused


def _read(path: str) -> dict:
    file = Path(path)
    if file.stat().st_size > 65536:
        raise Refused("JSON input exceeds 64 KiB")
    data = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise Refused("JSON input must be an object")
    return data


def _control(api, args):
    return api.control(enabled=not args.disable, human_attribution=args.human, authority_provenance=args.provenance)


_HANDLERS = {
    "control": _control,
    "grant": lambda api, args: api.grant(_read(args.contract)),
    "revoke": lambda api, args: api.revoke(args.grant_id),
    "request": lambda api, args: api.request(_read(args.request_file)),
    "list": lambda api, args: api.list(),
    "receipt": lambda api, args: api.receipt(args.correlation),
    "tick": lambda api, args: {"receipts": api.process_pending(), "delivery": "gateway handles exception notifications"},
}


def command(args) -> int:
    try:
        action = getattr(args, "board_ops_action", None)
        if action not in _HANDLERS:
            raise Refused("choose control, grant, revoke, request, list, receipt or tick")
        with open_board_ops(kb.get_current_board()) as api:
            result = _HANDLERS[action](api, args)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, sort_keys=True))
        return 0
    except (Refused, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
