"""Behavioral regression tests for Kanban web dashboard refresh, reconnect,
stale-read protection, and task/board identity.

Executes the shipped ``plugins/kanban/dashboard/dist/index.js`` ``KanbanPage``,
``TaskDrawer``, and ``WorkerLogSection`` components inside a deterministic Node
VM harness against:
1. Supported task events (exact-once card refresh, duplicate/out-of-order frame
   suppression, monotonic cursor).
2. WebSocket disconnect and missed-event recovery via a real ``/board`` snapshot,
   open ``TaskDrawer`` ``eventTick`` refresh on reconnect snapshot and cursor-gap
   recovery without duplicate ticks on replayed frames, initial read failure
   recovery on WS open, constructor/ticket retry backoff, and visible
   stale/reconnecting status while disconnected.
3. Failed, malformed, or out-of-order ``/board`` reads preserving prior cards
   and keeping the error banner visible without false-zero views.
4. Board-switch epoch/generation isolation and unmount cleanup preventing stale
   asynchronous work (in-flight ``loadBoard``, in-flight mutations, pending
   ``scheduleReload`` timer, in-flight ``SDK.buildWsUrl``, old WebSocket
   callbacks, post-unmount followups) from updating the new board, while keeping
   cursors board-local and resetting cursors when a board is deleted and
   recreated with the same slug.
5. Task and board identity invariants (clearing ``selectedTaskId`` and
   ``draggingTaskId`` on board switch, pruning removed task IDs from
   ``selectedIds``, passing ``?board=`` on ``deleteTask`` / ``deleteSelected``,
   and guarding ``TaskDrawer`` and ``WorkerLogSection`` against stale
   out-of-order reads/mutations and destructive background refresh errors).
6. Real ``plugin_api.py`` snapshots and ``/events`` WebSocket frames on isolated
   disposable boards in ``tmp_path``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb

REPO_ROOT = Path(__file__).resolve().parents[2]
DIST_BUNDLE = REPO_ROOT / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
# Upstream bundles that predate the transport fixes (v0.22.5 tag, then v0.22.0); the
# red-control tests prove each probe scenario fails on them.
_BASELINE_REVS = ("bf0203a794", "e06df9c2")
PROBE_SCRIPT = Path(__file__).resolve().parent / "fixtures" / "kanban_transport_probe.js"


def _load_plugin_router():
    plugin_file = REPO_ROOT / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_transport_test",
        plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


def _find_node() -> str:
    node = shutil.which("node")
    if node:
        return node
    for candidate in ("/opt/homebrew/bin/node", "/usr/local/bin/node"):
        if os.access(candidate, os.X_OK):
            return candidate
    pytest.skip("node executable not available")


def _run_probe(
    bundle: Path,
    scenario: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [_find_node(), str(PROBE_SCRIPT), str(bundle), scenario],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


@pytest.mark.parametrize(
    "scenario",
    [
        "single_event_and_dedup",
        "disconnect_and_missed_event_recovery",
        "failed_read_retains_prior_card",
        "stale_async_work_after_board_change",
        "task_and_board_identity",
        "home_channels_event_tick_and_guards",
        "initial_board_identity_promotion",
        "task_drawer_effect_remount",
    ],
)
def test_kanban_dashboard_transport_scenarios_pass_on_patched_bundle(scenario: str) -> None:
    res = _run_probe(DIST_BUNDLE, scenario)
    assert res.returncode == 0, f"Probe failed for {scenario}:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    assert f"PASS {scenario}" in res.stdout


def _resolve_baseline_bundle(tmp_path: Path) -> Path:
    for rev in _BASELINE_REVS:
        proc = subprocess.run(
            ["git", "show", f"{rev}:plugins/kanban/dashboard/dist/index.js"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            out = tmp_path / "transport-baseline.js"
            out.write_text(proc.stdout, encoding="utf-8")
            return out
    pytest.skip("baseline git revision not present (source export without history)")


@pytest.mark.parametrize(
    ("scenario", "expected_failure_substring"),
    [
        ("single_event_and_dedup", "Repeated frame for already-seen event id=11"),
        ("disconnect_and_missed_event_recovery", "WebSocket must still open even when initial /board fetch fails"),
        ("failed_read_retains_prior_card", "Cannot read properties of undefined"),
        ("stale_async_work_after_board_change", "Old-board (alpha) in-flight loadBoard must not populate new board (beta)"),
        ("task_and_board_identity", "Switching boards (alpha -> beta) must clear selectedTaskId"),
    ],
)
def test_kanban_dashboard_transport_red_control_fails_on_frozen_baseline(
    tmp_path: Path,
    scenario: str,
    expected_failure_substring: str,
) -> None:
    baseline = _resolve_baseline_bundle(tmp_path)
    res = _run_probe(baseline, scenario)
    assert res.returncode != 0, f"Expected RED failure on frozen baseline for {scenario}, but it passed"
    assert expected_failure_substring in res.stderr


def test_backend_event_and_snapshot_contract_roundtrip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import types
    import hermes_cli

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    kb.create_board("alpha")
    kb.create_board("beta")

    def _fake_ws_auth_ok(ws):
        return ws.query_params.get("token", "") == "secret-xyz"

    stub = types.SimpleNamespace(_SESSION_TOKEN="secret-xyz", _ws_auth_ok=_fake_ws_auth_ok)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_chat", stub)
    monkeypatch.setattr(hermes_cli, "web_server_chat", stub, raising=False)

    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    client = TestClient(app)

    created_alpha = client.post(
        "/api/plugins/kanban/tasks?board=alpha",
        json={"title": "Alpha Card v1", "priority": 1},
    )
    assert created_alpha.status_code == 200
    alpha_task_id = created_alpha.json()["task"]["id"]

    created_beta = client.post(
        "/api/plugins/kanban/tasks?board=beta",
        json={"title": "Beta Card v1", "priority": 2},
    )
    assert created_beta.status_code == 200
    beta_task_id = created_beta.json()["task"]["id"]

    alpha_snap_1 = client.get("/api/plugins/kanban/board?board=alpha").json()
    beta_snap_1 = client.get("/api/plugins/kanban/board?board=beta").json()
    since_cursor = alpha_snap_1["latest_event_id"]

    patched_alpha_1 = client.patch(
        f"/api/plugins/kanban/tasks/{alpha_task_id}?board=alpha",
        json={"title": "Alpha Card v2 (blocked)", "status": "blocked", "block_reason": "waiting"},
    )
    assert patched_alpha_1.status_code == 200
    alpha_snap_2 = client.get("/api/plugins/kanban/board?board=alpha").json()

    with client.websocket_connect(
        f"/api/plugins/kanban/events?token=secret-xyz&board=alpha&since={since_cursor}"
    ) as ws:
        alpha_event_frame_1 = ws.receive_json()

    assert alpha_event_frame_1["cursor"] >= alpha_snap_2["latest_event_id"]
    assert any(e["task_id"] == alpha_task_id for e in alpha_event_frame_1["events"])

    patched_alpha_2 = client.patch(
        f"/api/plugins/kanban/tasks/{alpha_task_id}?board=alpha",
        json={"title": "Alpha Card v3 (done)", "status": "done", "summary": "done"},
    )
    assert patched_alpha_2.status_code == 200
    alpha_snap_3 = client.get("/api/plugins/kanban/board?board=alpha").json()

    fixture = {
        "alphaBoard": "alpha",
        "betaBoard": "beta",
        "alphaTaskId": alpha_task_id,
        "betaTaskId": beta_task_id,
        "alphaSnapshot1": alpha_snap_1,
        "alphaSnapshot1Title": "Alpha Card v1",
        "alphaSnapshot2": alpha_snap_2,
        "alphaSnapshot2Title": "Alpha Card v2 (blocked)",
        "alphaSnapshot2Status": "blocked",
        "alphaSnapshot3": alpha_snap_3,
        "alphaSnapshot3Title": "Alpha Card v3 (done)",
        "alphaSnapshot3Status": "done",
        "betaSnapshot1": beta_snap_1,
        "betaSnapshot1Title": "Beta Card v1",
        "alphaEventFrame1": alpha_event_frame_1,
    }
    res = _run_probe(
        DIST_BUNDLE,
        "backend_contract_roundtrip",
        extra_env={"KANBAN_CONTRACT_FIXTURE": json.dumps(fixture)},
    )
    assert res.returncode == 0, f"Roundtrip probe failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    assert "PASS backend_contract_roundtrip" in res.stdout
