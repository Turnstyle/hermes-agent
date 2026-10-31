#!/usr/bin/env python3
"""Read-only first-user-row delegate-rule audit. No provider or credential lookup.

Examples:
  python scripts/audit_delegate_rule.py --since 1793386800 /path/state.db
  python scripts/audit_delegate_rule.py --all-profiles /path/to/hermes

A NULL/empty session model uses that database's sibling config.yaml pin. This
is the current pin, not proof of the historical route. MoA slots and opaque
Claude aliases cannot be classified by this audit. Errors are JSON data; exit 0.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

# Permit direct execution from a checkout or extracted release.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hermes_yaml as yaml

from agent.owner_prompt_policy import has_rule, is_claude_route, route_requires_rule
from hermes_constants import get_default_hermes_root


def config_pin(path: Path) -> tuple[str, str]:
    if not path.exists():
        return "", ""
    cfg = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(cfg, dict):
        raise ValueError("config must be a mapping")
    model = cfg.get("model")
    if isinstance(model, str):
        return str(cfg.get("provider") or "").strip(), model.strip()
    if not isinstance(model, dict):
        return "", ""
    provider = str(model.get("provider") or cfg.get("provider") or "").strip()
    target = model.get("default") or model.get("model") or model.get("name") or ""
    if isinstance(target, dict):
        if provider.lower() in ("", "auto"):
            provider = str(target.get("provider") or "").strip()
        target = target.get("model") or target.get("default") or ""
    return provider, str(target).strip()


def _session_route(row: dict[str, Any]) -> tuple[Any, Any]:
    raw = row.get("model_config")
    cfg = json.loads(raw) if isinstance(raw, str) and raw else raw or {}
    if not isinstance(cfg, dict):
        raise ValueError("session model_config must be a mapping")
    runtime = cfg.get("gateway_runtime") or {}
    if not isinstance(runtime, dict):
        raise ValueError("gateway_runtime must be a mapping")
    return (runtime.get("provider") or cfg.get("provider") or row.get("billing_provider") or row.get("provider"),
            row.get("model") or runtime.get("model") or cfg.get("model"))


def _decoded_content(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("\x00json:"):
        try:
            return json.loads(value[len("\x00json:"):])
        except ValueError:
            return value
    if isinstance(value, str) and value.lstrip().startswith("["):
        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        if isinstance(decoded, list):
            return decoded
    return value


def audit_database(path: Path, since: float = 0) -> dict[str, Any]:
    path = path.expanduser().absolute()
    result: dict[str, Any] = {"path": str(path), "profile": path.parent.name if path.parent.parent.name == "profiles" else "default",
                              "present": 0, "missing": 0, "skipped_nonclaude": 0,
                              "by_source": {}, "errors": []}
    conn = None
    try:
        # mode=ro refuses missing files; query_only also prevents accidental writes
        # after opening. Never instantiate SessionDB: its constructor migrates.
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        pin = None
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        active_filter = " AND COALESCE(active, 1) = 1" if "active" in columns else ""
        for raw in conn.execute("SELECT * FROM sessions WHERE started_at >= ? ORDER BY started_at, id", (since,)):
            row = dict(raw)
            try:
                provider, model = _session_route(row)
                if (pin is None and not (isinstance(model, str) and model.strip())
                        and not is_claude_route(provider, model)):
                    pin = config_pin(path.parent / "config.yaml")
                if not route_requires_rule(provider, model, pin=pin or ("", "")):
                    result["skipped_nonclaude"] += 1
                    continue
                first = conn.execute(
                    "SELECT content FROM messages WHERE session_id = ? AND role = 'user'" + active_filter + " ORDER BY id LIMIT 1",
                    (row["id"],)).fetchone()
                status = "present" if first and has_rule(_decoded_content(first["content"])) else "missing"
                source = str(row.get("source") or "unknown")
                counts = result["by_source"].setdefault(source, {"present": 0, "missing": 0})
                counts[status] += 1
                result[status] += 1
            except (ValueError, TypeError, OSError, sqlite3.Error, yaml.YAMLError) as exc:
                result["errors"].append({"session_id": row["id"], "error": str(exc)})
    except (OSError, sqlite3.Error) as exc:
        result["errors"].append({"error": str(exc)})
    finally:
        if conn is not None:
            conn.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("databases", nargs="*", type=Path)
    parser.add_argument("--since", type=float, default=0, help="session started_at Unix epoch, inclusive")
    parser.add_argument("--all-profiles", nargs="?", type=Path,
                        const=get_default_hermes_root(), help="root home; defaults to the default Hermes home")
    args = parser.parse_args(argv)
    paths = list(args.databases)
    if args.all_profiles is not None:
        root = args.all_profiles.expanduser()
        paths += [root / "state.db", *sorted((root / "profiles").glob("*/state.db"))]
    reports = [audit_database(path, args.since) for path in dict.fromkeys(paths)]
    errors = [{"path": report["path"], **error} for report in reports for error in report["errors"]]
    if not paths:
        errors.append({"error": "provide state.db paths or --all-profiles"})
    print(json.dumps({"since": args.since, "present": sum(r["present"] for r in reports),
                      "missing": sum(r["missing"] for r in reports), "by_profile": reports,
                      "errors": errors}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
