"""Optional recover-ghost CLI. Recovery policy lives in this plugin, not core."""

from __future__ import annotations

import argparse


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("wait seconds must be >= 0")
    return parsed


def _setup_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("task_id", help="Task ID to recover")
    parser.add_argument("--run", type=int, required=True, dest="run_id", help="Expected running task_runs id")
    parser.add_argument("--board", default=None, help="Kanban board slug (default: active board)")
    parser.add_argument("--apply", action="store_true", help="Apply after rechecking under a write lock")
    parser.add_argument("--done", action="store_true", help="Record completion evidence (Fleet: handoff only)")
    parser.add_argument("--evidence", help="Non-empty evidence required with --done")
    parser.add_argument("--expect", help="Expected dry-run fingerprint")
    parser.add_argument("--wait", type=_nonnegative_int, help="Seconds to wait for Fleet sync (default 60)")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")


def _handle_command(args: argparse.Namespace) -> int:
    from .recover_ghost import recover_ghost
    return recover_ghost(args)


def register(ctx) -> None:
    ctx.register_cli_command(
        name="recover-ghost",
        help="Safely recover a claimless running task (dry-run by default)",
        setup_fn=_setup_parser,
        handler_fn=_handle_command,
        description="Governed claimless-task recovery with write-lock validation",
    )
