"""Synthetic fixture tests for Kanban web board identity, resolution, and frontend selection.

All board stores and cards created in this module are synthetic isolated fixtures
under ``tmp_path``; no live ``~/.hermes`` stores are read or modified.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from plugins.kanban.dashboard import plugin_api

_WORKTREE = Path(__file__).resolve().parents[2]


@pytest.fixture
def synthetic_fleet_home(tmp_path, monkeypatch):
    """Isolated synthetic HERMES_HOME with a legacy 'default' board and an active 'fleet' board."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()

    # Seed synthetic legacy default board (<root>/kanban.db) with 1 synthetic card.
    kb.init_db()
    with kbc.connect_closing(board="default") as conn:
        kb.create_task(
            conn,
            title="[SYNTHETIC FIXTURE] legacy default card",
            tenant="fixture-legacy",
        )
    kb.write_board_metadata("default", name="Preamble")

    # Seed synthetic fleet board (<root>/kanban/boards/fleet/kanban.db) with 2 active + 1 archived cards.
    kb.create_board("fleet", name="Fleet")
    with kbc.connect_closing(board="fleet") as conn:
        kb.create_task(
            conn,
            title="[SYNTHETIC FIXTURE] fleet triage card",
            triage=True,
            tenant="turnerbook-fixture",
        )
        kb.create_task(
            conn,
            title="[SYNTHETIC FIXTURE] fleet running card",
            tenant="turnerbook-fixture",
            assignee="tb-cndr",
        )
        archived_id = kb.create_task(
            conn,
            title="[SYNTHETIC FIXTURE] fleet archived card",
            tenant="turnerbook-fixture",
        )
        conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (archived_id,))
        conn.commit()

    # Set active board pointer (<root>/kanban/current) to 'fleet'.
    kb.set_current_board("fleet")
    return home


def _find_node() -> str:
    node = shutil.which("node")
    if node:
        return node
    for candidate in ("/opt/homebrew/bin/node", "/usr/local/bin/node"):
        if os.access(candidate, os.X_OK):
            return candidate
    pytest.skip("node executable not available")


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/kanban")
    return TestClient(app)


def test_get_board_resolves_active_fleet_board_and_returns_slug(synthetic_fleet_home):
    """GET /board without ?board= resolves the active 'fleet' board from <root>/kanban/current
    and includes 'board': 'fleet' in the response payload."""
    client = _make_client()

    res = client.get("/api/plugins/kanban/board")
    assert res.status_code == 200
    body = res.json()
    assert body["board"] == "fleet"
    titles = [
        t["title"]
        for col in body["columns"]
        for t in col["tasks"]
    ]
    assert "[SYNTHETIC FIXTURE] fleet triage card" in titles
    assert "[SYNTHETIC FIXTURE] fleet running card" in titles
    assert "[SYNTHETIC FIXTURE] legacy default card" not in titles

    # Explicit ?board=default still returns the legacy default board.
    res_default = client.get("/api/plugins/kanban/board?board=default")
    assert res_default.status_code == 200
    body_default = res_default.json()
    assert body_default["board"] == "default"
    default_titles = [
        t["title"]
        for col in body_default["columns"]
        for t in col["tasks"]
    ]
    assert default_titles == ["[SYNTHETIC FIXTURE] legacy default card"]


def test_list_boards_counts_readonly_without_initializing_stub_boards(synthetic_fleet_home):
    """GET /boards reports current='fleet', non-archived totals, ignores board dirs
    without board.json (like 'sheldon'), and does not initialize an uninitialized board DB."""
    import sqlite3

    # A directory with kanban.db but no board.json (like live 'sheldon') is excluded.
    orphan_dir = kb.boards_root() / "sheldon"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(orphan_dir / "kanban.db") as sconn:
        sconn.execute("PRAGMA user_version = 0")

    # A board with board.json but an uninitialized 0-table kanban.db is listed with total=0
    # and its DB is NOT initialized with schema tables by GET /boards.
    stub_dir = kb.boards_root() / "stub-empty"
    stub_dir.mkdir(parents=True, exist_ok=True)
    (stub_dir / "board.json").write_text(
        json.dumps({"slug": "stub-empty", "name": "Stub Empty", "archived": False}),
        encoding="utf-8",
    )
    stub_db = stub_dir / "kanban.db"
    with sqlite3.connect(stub_db) as sconn:
        sconn.execute("PRAGMA user_version = 0")

    client = _make_client()
    res = client.get("/api/plugins/kanban/boards")
    assert res.status_code == 200
    payload = res.json()
    assert payload["current"] == "fleet"

    by_slug = {b["slug"]: b for b in payload["boards"]}
    assert "sheldon" not in by_slug
    assert by_slug["default"]["total"] == 1
    assert by_slug["default"]["is_current"] is False
    assert by_slug["fleet"]["total"] == 2
    assert by_slug["fleet"]["is_current"] is True
    assert by_slug["stub-empty"]["total"] == 0

    # Verify the stub DB was NOT initialized with schema tables by GET /boards.
    with sqlite3.connect(stub_db) as sconn:
        tables = [
            r[0]
            for r in sconn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
    assert tables == []


def _spy_write_conn(monkeypatch) -> list:
    calls = []
    orig_conn = plugin_api._conn

    def _spy(board=None):
        calls.append(board)
        return orig_conn(board=board)

    monkeypatch.setattr(plugin_api, "_conn", _spy)
    return calls


def test_read_endpoints_admit_existing_board_without_write_connection(synthetic_fleet_home, monkeypatch):
    """GET endpoints on an initialized board open it read-only: no init/migration
    connection, and a handler error (404) never falls back to initialization."""
    client = _make_client()
    calls = _spy_write_conn(monkeypatch)

    with kbc.connect_closing(board="fleet") as conn:
        task_id = kb.create_task(conn, title="[SYNTHETIC FIXTURE] read probe", tenant="fixture")
    for path in ("/board", "/stats", "/assignees", f"/tasks/{task_id}", "/diagnostics", "/workers/active"):
        res = client.get(f"/api/plugins/kanban{path}?board=fleet")
        assert res.status_code == 200, (path, res.text)
    assert client.get("/api/plugins/kanban/tasks/t_missing?board=fleet").status_code == 404
    assert calls == []


def test_read_endpoint_initializes_missing_board_once_then_reads_readonly(tmp_path, monkeypatch):
    """A fresh install still self-heals on the first GET: the missing DB is
    initialized once through the guarded write path, later reads stay read-only."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    assert not kb.kanban_db_path(board="default").exists()

    client = _make_client()
    calls = _spy_write_conn(monkeypatch)
    first = client.get("/api/plugins/kanban/board")
    assert first.status_code == 200, first.text
    assert first.json()["board"] == "default"
    assert kb.kanban_db_path(board="default").is_file()
    assert len(calls) == 1

    assert client.get("/api/plugins/kanban/board").status_code == 200
    assert client.get("/api/plugins/kanban/stats").status_code == 200
    assert len(calls) == 1


def test_plugin_api_conn_preserves_initialized_paths_cache(synthetic_fleet_home, monkeypatch):
    """_conn() must not call init_db() on every request and evict _INITIALIZED_PATHS."""
    client = _make_client()
    fleet_resolved = str(kb.kanban_db_path(board="fleet").resolve())
    assert fleet_resolved in kb._INITIALIZED_PATHS

    init_calls = []
    orig_init = kb.init_db

    def _spy_init_db(*args, **kwargs):
        init_calls.append((args, kwargs))
        return orig_init(*args, **kwargs)

    monkeypatch.setattr(kb, "init_db", _spy_init_db)

    res1 = client.get("/api/plugins/kanban/board")
    res2 = client.get("/api/plugins/kanban/assignees")
    assert res1.status_code == 200
    assert res2.status_code == 200
    assert init_calls == []
    assert fleet_resolved in kb._INITIALIZED_PATHS


def test_kanban_home_honors_contextvar_override_and_collapses_profile(tmp_path, monkeypatch):
    """kanban_home() respects set_hermes_home_override while collapsing <root>/profiles/<name> to <root>."""
    for var in ("HERMES_KANBAN_HOME", "HERMES_HOME"):
        monkeypatch.delenv(var, raising=False)
    custom_root = tmp_path / "custom_hermes_root"
    profile_dir = custom_root / "profiles" / "tb-cndr"
    profile_dir.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "unrelated_native_home")

    token = set_hermes_home_override(profile_dir)
    try:
        assert kb.kanban_home() == custom_root
    finally:
        reset_hermes_home_override(token)


def test_get_current_board_skips_archived_tombstone_in_current_file(synthetic_fleet_home):
    """If <root>/kanban/current points to a board whose board.json has archived=True,
    get_current_board() falls back to 'default' just like HERMES_KANBAN_BOARD does."""
    kb.create_board("temp-board", name="Temp")
    kb.remove_board("temp-board", archive=True)
    # Simulate a stale on-disk current file pointing at the archived board.
    kb.current_board_path().write_text("temp-board\n", encoding="utf-8")
    assert kb.get_current_board() == "default"


_NODE_HARNESS_PREAMBLE = r"""
const fs = require("fs");
const vm = require("vm");

const bundleCode = fs.readFileSync(process.argv[1], "utf8");

function runScenario(initialStorage, apiHandler) {
  const storage = new Map(Object.entries(initialStorage || {}));
  const localStorage = {
    getItem: (k) => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
    removeItem: (k) => storage.delete(k),
  };

  let stateSlots = [];
  let refSlots = [];
  let effectQueue = [];
  let stateIdx = 0;
  let refIdx = 0;
  let registeredComponent = null;
  const fetchedUrls = [];

  const React = {
    Component: class Component {
      constructor(props) { this.props = props; }
    },
    createElement: (type, props, ...children) => ({
      type,
      props: Object.assign({}, props, { children: children.flat() }),
    }),
    useState: (init) => {
      const idx = stateIdx++;
      if (!(idx in stateSlots)) {
        stateSlots[idx] = typeof init === "function" ? init() : init;
      }
      const setter = (val) => {
        stateSlots[idx] = typeof val === "function" ? val(stateSlots[idx]) : val;
      };
      return [stateSlots[idx], setter];
    },
    useRef: (init) => {
      const idx = refIdx++;
      if (!(idx in refSlots)) {
        refSlots[idx] = { current: init };
      }
      return refSlots[idx];
    },
    useCallback: (fn) => fn,
    useMemo: (fn) => fn(),
    useEffect: (fn) => { effectQueue.push(fn); },
  };

  const components = {
    Card: "Card",
    CardContent: "CardContent",
    Badge: "Badge",
    Button: "Button",
    Input: "Input",
    Label: "Label",
    Select: "Select",
    SelectOption: "SelectOption",
    Checkbox: "Checkbox",
    ConfirmDialog: "ConfirmDialog",
  };

  const SDK = {
    React,
    components,
    hooks: {
      useState: React.useState,
      useEffect: React.useEffect,
      useCallback: React.useCallback,
      useMemo: React.useMemo,
      useRef: React.useRef,
    },
    utils: {
      cn: (...args) => args.filter(Boolean).join(" "),
      timeAgo: () => "1m",
    },
    fetchJSON: (url, opts) => {
      fetchedUrls.push({ url, opts: opts || null });
      return Promise.resolve().then(() => apiHandler(url, opts));
    },
    buildWsUrl: () => Promise.resolve("ws://127.0.0.1:9/events"),
  };

  const sandbox = {
    window: {
      __HERMES_PLUGIN_SDK__: SDK,
      __HERMES_PLUGINS__: {
        register: (_id, comp) => { registeredComponent = comp; },
      },
      localStorage,
      addEventListener: () => {},
      removeEventListener: () => {},
      confirm: () => true,
    },
    document: {
      addEventListener: () => {},
      removeEventListener: () => {},
      querySelectorAll: () => [],
    },
    WebSocket: function () { this.close = () => {}; },
    URLSearchParams,
    encodeURIComponent,
    setTimeout: () => 1,
    clearTimeout: () => {},
    Promise,
    Object,
    Array,
    Set,
    Map,
    String,
    Number,
    Math,
    JSON,
    RegExp,
    Error,
    console,
  };

  vm.runInNewContext(bundleCode, sandbox);

  function renderPass() {
    stateIdx = 0;
    refIdx = 0;
    effectQueue = [];
    const tree = registeredComponent();
    const effects = effectQueue.slice();
    effectQueue = [];
    for (const eff of effects) eff();
    return tree;
  }

  function findByType(node, targetType) {
    if (!node || typeof node !== "object") return null;
    if (node.type === targetType) return node;
    const children = (node.props && node.props.children) || [];
    for (const c of children) {
      const found = findByType(c, targetType);
      if (found) return found;
    }
    return null;
  }

  function findByName(node, fnName) {
    if (!node || typeof node !== "object") return null;
    if (typeof node.type === "function" && node.type.name === fnName) return node;
    const children = (node.props && node.props.children) || [];
    for (const c of children) {
      const found = findByName(c, fnName);
      if (found) return found;
    }
    return null;
  }

  return { storage, fetchedUrls, renderPass, findByType, findByName };
}
"""


def test_dashboard_bundle_board_selection_and_fallback_in_node():
    """Execute the real plugins/kanban/dashboard/dist/index.js bundle in a minimal
    synthetic React/SDK harness under Node to verify:
    1. Unscoped legacy localStorage 'default' is cleared and 'fleet' is selected.
    2. Initial BoardSwitcher render with board=null uses the is_current board ('fleet'),
       not list[0] ('default').
    3. Explicit switch to 'default' while current='fleet' persists selectedBoard='default'
       with selectedBoardBase='fleet' and fetches ?board=default (#20879).
    4. Deleting a board falls back to res.current ('fleet'), not hardcoded 'default',
       and deleteTask appends ?board=<selectedBoard>.
    5. Stored explicit default pin (selectedBoard='default', selectedBoardBase='fleet')
       survives when API current changes to 'content' and keeps ?board=default.
    6. Stored valid named pin (selectedBoard='ops', selectedBoardBase='fleet')
       survives when API current changes to 'content' and keeps ?board=ops.
    """
    bundle_path = _WORKTREE / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    script = _NODE_HARNESS_PREAMBLE + r"""
(async () => {
  const defaultBoardsPayload = {
    current: "fleet",
    boards: [
      { slug: "default", name: "Preamble", total: 13, is_current: false },
      { slug: "fleet", name: "Fleet", total: 1743, is_current: true },
    ],
  };
  const fleetBoardPayload = {
    board: "fleet",
    columns: [{ name: "triage", tasks: [{ id: "t_1", title: "Synthetic Fleet Card", status: "triage" }] }],
    tenants: ["turnerbook-fixture"],
    assignees: ["tb-cndr"],
    latest_event_id: 42,
  };

  // Scenario 1: Legacy unscoped localStorage["hermes.kanban.selectedBoard"] = "default"
  // must be cleared and resolve to "fleet".
  const s1 = runScenario(
    { "hermes.kanban.selectedBoard": "default" },
    (url, opts) => {
      if (opts && opts.method === "DELETE" && url.includes("/boards/")) return { current: "fleet" };
      if (url.includes("/boards")) return defaultBoardsPayload;
      if (url.includes("/board")) return fleetBoardPayload;
      return {};
    }
  );
  s1.renderPass();
  await new Promise((r) => setImmediate(r));
  const tree1 = s1.renderPass();
  if (s1.storage.has("hermes.kanban.selectedBoard")) {
    throw new Error("Expected legacy unscoped default pin to be cleared from localStorage");
  }
  const switcherNode1 = s1.findByName(tree1, "BoardSwitcher");
  if (!switcherNode1 || switcherNode1.props.board !== "fleet") {
    throw new Error("Expected BoardSwitcher props.board to be 'fleet', got: " + (switcherNode1 && switcherNode1.props.board));
  }
  const renderedSwitcher1 = switcherNode1.type(switcherNode1.props);
  const select1 = s1.findByType(renderedSwitcher1, "Select");
  if (!select1 || select1.props.value !== "fleet") {
    throw new Error("Expected Select value to be 'fleet', got: " + (select1 && select1.props.value));
  }

  // Scenario 2: Even if BoardSwitcher renders while props.board is null, it must select
  // the is_current board ('fleet'), never list[0] ('default').
  const switcherNullRender = switcherNode1.type(
    Object.assign({}, switcherNode1.props, { board: null })
  );
  const selectNull = s1.findByType(switcherNullRender, "Select");
  if (!selectNull || selectNull.props.value !== "fleet") {
    throw new Error("Expected Select value when props.board=null to be 'fleet', got: " + (selectNull && selectNull.props.value));
  }

  // Scenario 3: Explicit user switch to 'default' while server current is 'fleet'
  // persists selectedBoard='default' with selectedBoardBase='fleet' and fetches ?board=default (#20879).
  switcherNode1.props.onSwitch("default");
  if (
    s1.storage.get("hermes.kanban.selectedBoard") !== "default" ||
    s1.storage.get("hermes.kanban.selectedBoardBase") !== "fleet"
  ) {
    throw new Error("Expected explicit switch to 'default' to record selectedBoard='default' and selectedBoardBase='fleet'");
  }
  s1.renderPass();
  await new Promise((r) => setImmediate(r));
  const tree2 = s1.renderPass();
  if (!s1.fetchedUrls.some((u) => u.url === "/api/plugins/kanban/board?board=default")) {
    throw new Error("Expected /api/plugins/kanban/board?board=default to be fetched");
  }

  // Scenario 4: Switching back to 'fleet' clears the localStorage override,
  // deleting a board falls back to res.current ('fleet'), and deleteTask includes ?board=fleet.
  const switcherNode2 = s1.findByName(tree2, "BoardSwitcher");
  switcherNode2.props.onSwitch("fleet");
  s1.renderPass();
  await new Promise((r) => setImmediate(r));
  const tree3 = s1.renderPass();
  if (s1.storage.has("hermes.kanban.selectedBoard")) {
    throw new Error("Expected switching back to active board 'fleet' to clear localStorage override");
  }
  const colsNode = s1.findByName(tree3, "BoardColumns");
  colsNode.props.onDelete("t_1");
  await new Promise((r) => setImmediate(r));
  const dlgNode = s1.findByName(s1.renderPass(), "KanbanDialogs");
  dlgNode.props.dialogProps.onConfirm();
  await new Promise((r) => setImmediate(r));
  if (!s1.fetchedUrls.some((u) => u.url === "/api/plugins/kanban/tasks/t_1?board=fleet" && u.opts && u.opts.method === "DELETE")) {
    throw new Error("Expected DELETE /api/plugins/kanban/tasks/t_1?board=fleet, got: " + JSON.stringify(s1.fetchedUrls));
  }

  // Scenario 5: Stored explicit default pin (selectedBoard="default", selectedBoardBase="fleet")
  // must survive when API current changes to "content" and all three boards are valid.
  const threeBoardsPayload = {
    current: "content",
    boards: [
      { slug: "default", name: "Preamble", total: 13, is_current: false },
      { slug: "fleet", name: "Fleet", total: 1742, is_current: false },
      { slug: "content", name: "Content", total: 5, is_current: true },
    ],
  };
  const defaultBoardPayload = {
    board: "default",
    columns: [{ name: "triage", tasks: [{ id: "t_def", title: "Synthetic Default Card", status: "triage" }] }],
    tenants: ["fixture-legacy"],
    assignees: [],
    latest_event_id: 7,
  };
  const s2 = runScenario(
    {
      "hermes.kanban.selectedBoard": "default",
      "hermes.kanban.selectedBoardBase": "fleet",
    },
    (url) => {
      if (url.includes("/boards")) return threeBoardsPayload;
      if (url.includes("/board")) return defaultBoardPayload;
      return {};
    }
  );
  s2.renderPass();
  await new Promise((r) => setImmediate(r));
  const treeS2 = s2.renderPass();
  if (s2.storage.get("hermes.kanban.selectedBoard") !== "default") {
    throw new Error("Expected explicit default pin to survive server-current change to 'content', got: " + s2.storage.get("hermes.kanban.selectedBoard"));
  }
  const switcherS2 = s2.findByName(treeS2, "BoardSwitcher");
  if (!switcherS2 || switcherS2.props.board !== "default") {
    throw new Error("Expected BoardSwitcher props.board to remain 'default', got: " + (switcherS2 && switcherS2.props.board));
  }
  const selectS2 = s2.findByType(switcherS2.type(switcherS2.props), "Select");
  if (!selectS2 || selectS2.props.value !== "default") {
    throw new Error("Expected Select value to remain 'default', got: " + (selectS2 && selectS2.props.value));
  }
  const s2BoardFetches = s2.fetchedUrls
    .map((u) => u.url)
    .filter((u) => u.startsWith("/api/plugins/kanban/board?") || u === "/api/plugins/kanban/board");
  if (
    s2BoardFetches.length === 0 ||
    !s2BoardFetches.every((u) => u.includes("board=default"))
  ) {
    throw new Error("Expected all /board requests to remain ?board=default, got: " + JSON.stringify(s2BoardFetches));
  }

  // Scenario 6: Stored valid named pin (selectedBoard="ops", selectedBoardBase="fleet")
  // must also survive when API current changes to "content".
  const fourBoardsPayload = {
    current: "content",
    boards: [
      { slug: "default", name: "Preamble", total: 13, is_current: false },
      { slug: "fleet", name: "Fleet", total: 1742, is_current: false },
      { slug: "content", name: "Content", total: 5, is_current: true },
      { slug: "ops", name: "Ops", total: 9, is_current: false },
    ],
  };
  const opsBoardPayload = {
    board: "ops",
    columns: [{ name: "triage", tasks: [{ id: "t_ops", title: "Synthetic Ops Card", status: "triage" }] }],
    tenants: ["ops-fixture"],
    assignees: [],
    latest_event_id: 9,
  };
  const s3 = runScenario(
    {
      "hermes.kanban.selectedBoard": "ops",
      "hermes.kanban.selectedBoardBase": "fleet",
    },
    (url) => {
      if (url.includes("/boards")) return fourBoardsPayload;
      if (url.includes("/board")) return opsBoardPayload;
      return {};
    }
  );
  s3.renderPass();
  await new Promise((r) => setImmediate(r));
  const treeS3 = s3.renderPass();
  if (s3.storage.get("hermes.kanban.selectedBoard") !== "ops") {
    throw new Error("Expected valid named pin 'ops' to survive server-current change to 'content', got: " + s3.storage.get("hermes.kanban.selectedBoard"));
  }
  const switcherS3 = s3.findByName(treeS3, "BoardSwitcher");
  if (!switcherS3 || switcherS3.props.board !== "ops") {
    throw new Error("Expected BoardSwitcher props.board to remain 'ops', got: " + (switcherS3 && switcherS3.props.board));
  }
  const s3BoardFetches = s3.fetchedUrls
    .map((u) => u.url)
    .filter((u) => u.startsWith("/api/plugins/kanban/board?") || u === "/api/plugins/kanban/board");
  if (
    s3BoardFetches.length === 0 ||
    !s3BoardFetches.every((u) => u.includes("board=ops"))
  ) {
    throw new Error("Expected all /board requests to remain ?board=ops, got: " + JSON.stringify(s3BoardFetches));
  }

  process.stdout.write("OK\n");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [_find_node(), "-e", script, str(bundle_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"node harness failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    assert proc.stdout.strip() == "OK"


def test_dashboard_bundle_deleted_board_sdk_404_status_fallback_in_node():
    """Verify that when a pinned board ('alpha') is deleted after the page is open
    and GET /board?board=alpha rejects with an SDK-shaped ApiError (numeric
    err.status == 404, message "Board 'alpha' not found" without a '404' prefix),
    loadBoard clears the obsolete pin and falls back to the server's current board ('beta').
    """
    bundle_path = _WORKTREE / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    script = _NODE_HARNESS_PREAMBLE + r"""
(async () => {
  let missing = false;
  const s = runScenario(
    {
      "hermes.kanban.selectedBoard": "alpha",
      "hermes.kanban.selectedBoardBase": "beta",
    },
    (url) => {
      if (url.includes("/boards")) {
        return {
          current: "beta",
          boards: [
            { slug: "default", name: "Preamble", total: 1, is_current: false },
            { slug: "alpha", name: "Alpha", total: 2, is_current: false },
            { slug: "beta", name: "Beta", total: 3, is_current: true },
          ],
        };
      }
      if (url.includes("/board")) {
        if (missing && url.includes("board=alpha")) {
          const err = new Error("Board 'alpha' not found");
          err.status = 404;
          throw err;
        }
        const slug = url.includes("board=alpha") ? "alpha" : "beta";
        return {
          board: slug,
          columns: [{ name: "triage", tasks: [{ id: "t_" + slug, title: "Card " + slug, status: "triage" }] }],
          tenants: [],
          assignees: [],
          latest_event_id: 1,
        };
      }
      return {};
    }
  );

  s.renderPass();
  await new Promise((r) => setImmediate(r));
  const initialTree = s.renderPass();
  const initialSwitcher = s.findByName(initialTree, "BoardSwitcher");
  if (!initialSwitcher || initialSwitcher.props.board !== "alpha") {
    throw new Error("Expected initial board to be 'alpha', got: " + (initialSwitcher && initialSwitcher.props.board));
  }

  // Board 'alpha' is now deleted on the server while the page is open; user triggers refresh.
  missing = true;
  const toolbar = s.findByName(initialTree, "BoardToolbar");
  toolbar.props.onRefresh();
  await new Promise((r) => setImmediate(r));

  if (s.storage.has("hermes.kanban.selectedBoard") || s.storage.has("hermes.kanban.selectedBoardBase")) {
    throw new Error("Expected obsolete 'alpha' pin to be cleared from localStorage after 404");
  }

  // Render pass after fallback to 'beta' triggers loadBoard() for 'beta'.
  s.renderPass();
  await new Promise((r) => setImmediate(r));
  const afterTree = s.renderPass();
  const afterSwitcher = s.findByName(afterTree, "BoardSwitcher");
  if (!afterSwitcher || afterSwitcher.props.board !== "beta") {
    throw new Error("Expected board to fall back to 'beta', got: " + (afterSwitcher && afterSwitcher.props.board));
  }
  const hasErrorBanner = (function checkErr(node) {
    if (!node || typeof node !== "object") return false;
    if (node.props && typeof node.props.className === "string" && node.props.className.includes("text-destructive")) {
      return true;
    }
    const children = (node.props && node.props.children) || [];
    return Array.isArray(children) && children.some(checkErr);
  })(afterTree);
  if (hasErrorBanner) {
    throw new Error("Expected no error banner after deleted-board 404 fallback");
  }
  const boardReads = s.fetchedUrls
    .map((u) => u.url)
    .filter((u) => u.startsWith("/api/plugins/kanban/board?") || u === "/api/plugins/kanban/board");
  if (!boardReads.includes("/api/plugins/kanban/board?board=beta")) {
    throw new Error("Expected fallback read to /api/plugins/kanban/board?board=beta, got: " + JSON.stringify(boardReads));
  }

  process.stdout.write("OK\n");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [_find_node(), "-e", script, str(bundle_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"node harness failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    assert proc.stdout.strip() == "OK"

