"""Regression test probe for runner_health_probe.py.

Verifies that:
1. Placeholder profiles (default, alpha, beta, orch) without gateway workers
   are excluded from spawnable counting when allowlist is unset.
2. On boards without the Fleet adapter (no fleet_kanban_issue_map), placeholder
   cards are bucketed into ready_default_owned and never into ready_owned_here or
   ready_spawnable_here.
3. Explicit opt-in via kanban.dispatch_profiles enables dispatch for default.
"""
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

import pytest
import sys

_FNS_DIR = Path(__file__).resolve().parent.parent / "fleet-node-state"
if str(_FNS_DIR) not in sys.path:
    sys.path.insert(0, str(_FNS_DIR))

import runner_health_probe as rhp


def test_is_dispatch_enabled_profile_when_allowlist_unset(tmp_path, monkeypatch):
    """When dispatch_profiles is unset, placeholders are disabled, local dirs enabled."""
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "sage").mkdir()
    (profiles_dir / "sage" / "config.yaml").write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(rhp, "HH", str(tmp_path))
    monkeypatch.setattr(rhp, "dispatch_profiles_allowlist", lambda: None)

    for placeholder in ("default", "alpha", "beta", "orch", "DEFAULT", "Alpha", " Orch "):
        assert rhp.is_dispatch_enabled_profile(placeholder) is False, f"{placeholder} should not be enabled"

    assert rhp.is_dispatch_enabled_profile("sage") is True
    assert rhp.is_dispatch_enabled_profile("Sage") is True
    assert rhp.is_dispatch_enabled_profile("missing_profile") is False
    assert rhp.is_dispatch_enabled_profile(None) is False
    assert rhp.is_dispatch_enabled_profile("") is False


def test_is_dispatch_enabled_profile_with_explicit_opt_in(tmp_path, monkeypatch):
    """When dispatch_profiles explicitly includes default, default is enabled."""
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "sage").mkdir()
    (profiles_dir / "sage" / "config.yaml").write_text("{}\n", encoding="utf-8")
    (profiles_dir / "worker2").mkdir()
    (profiles_dir / "worker2" / "config.yaml").write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(rhp, "HH", str(tmp_path))
    monkeypatch.setattr(rhp, "dispatch_profiles_allowlist", lambda: frozenset({"default", "sage"}))

    # Opted-in default is enabled
    assert rhp.is_dispatch_enabled_profile("default") is True
    assert rhp.is_dispatch_enabled_profile("DEFAULT") is True
    # Opted-in sage is enabled
    assert rhp.is_dispatch_enabled_profile("sage") is True
    # Non-opted-in placeholder alpha is still disabled
    assert rhp.is_dispatch_enabled_profile("alpha") is False
    # Non-opted-in real profile worker2 is disabled
    assert rhp.is_dispatch_enabled_profile("worker2") is False


def test_no_adapter_board_placeholder_regression_probe(tmp_path, monkeypatch):
    """Regression probe: on boards without Fleet adapter, placeholder cards must NOT be spawnable."""
    db_path = tmp_path / "kanban.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            assignee TEXT,
            claim_lock TEXT,
            worker_pid INTEGER,
            created_at INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            kind TEXT,
            payload TEXT,
            created_at INTEGER
        )
    """)

    # Setup profiles dir: sage is real, default/alpha are placeholders with no dir
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "sage").mkdir()
    (profiles_dir / "sage" / "config.yaml").write_text("{}\n", encoding="utf-8")

    now = 1790600000
    # Insert test tasks:
    # t1: placeholder default (no adapter)
    conn.execute("INSERT INTO tasks VALUES ('t1', 'default card', 'ready', 'default', NULL, NULL, ?)", (now - 1000,))
    conn.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES ('t1', 'created', ?)", (now - 1000,))
    # t2: placeholder alpha (no adapter)
    conn.execute("INSERT INTO tasks VALUES ('t2', 'alpha card', 'ready', 'alpha', NULL, NULL, ?)", (now - 1000,))
    conn.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES ('t2', 'created', ?)", (now - 1000,))
    # t3: real profile sage (no adapter)
    conn.execute("INSERT INTO tasks VALUES ('t3', 'sage card', 'ready', 'sage', NULL, NULL, ?)", (now - 1000,))
    conn.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES ('t3', 'created', ?)", (now - 1000,))
    # t4: non-existent profile (no adapter)
    conn.execute("INSERT INTO tasks VALUES ('t4', 'ghost card', 'ready', 'ghost', NULL, NULL, ?)", (now - 1000,))
    conn.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES ('t4', 'created', ?)", (now - 1000,))
    conn.commit()
    conn.close()

    monkeypatch.setattr(rhp, "HH", str(tmp_path))
    monkeypatch.setattr(rhp, "NOW", now)
    monkeypatch.setattr(rhp, "board_dbs", lambda: [("default", str(db_path))])
    monkeypatch.setattr(rhp, "kanban_caps", lambda: (None, None))
    monkeypatch.setattr(rhp, "load_acks", lambda: {})
    monkeypatch.setattr(rhp, "dispatch_profiles_allowlist", lambda: None)

    res = rhp.board_counts()
    boards = res["boards"]
    b = boards["default"]

    # Invariants under unset allowlist:
    # 1. Placeholders (t1 default, t2 alpha) must land in ready_default_owned
    assert b["ready_default_owned"] == 2
    # 2. Ghost profile lands in ready_absent_profile
    assert b["ready_absent_profile"] == 1
    # 3. Only the real profile (t3 sage) is counted in ready_owned_here
    assert b["ready_owned_here"] == 1
    # 4. Only the real profile is counted in ready_spawnable_here
    assert b["ready_spawnable_here"] == 1
    assert res["ready_spawnable_here"] == 1

    # Invariants under explicit opt-in (dispatch_profiles: ["default", "sage"]):
    monkeypatch.setattr(rhp, "dispatch_profiles_allowlist", lambda: frozenset({"default", "sage"}))
    res2 = rhp.board_counts()
    b2 = res2["boards"]["default"]
    # t1 (default) is now counted as owned and spawnable
    assert b2["ready_default_owned"] == 1  # only t2 (alpha) remains in ready_default_owned
    assert b2["ready_owned_here"] == 2      # t1 (default) and t3 (sage)
    assert b2["ready_spawnable_here"] == 2  # t1 (default) and t3 (sage)

