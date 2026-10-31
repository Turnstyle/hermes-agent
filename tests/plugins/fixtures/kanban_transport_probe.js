// Behavioral component harness for KanbanPage & TaskDrawer transport, freshness,
// stale-read protection, and task/board identity.
// Executes the real shipped bundle IIFE (window.__HERMES_PLUGINS__.register("kanban", KanbanPage))
// inside a Node VM context with a deterministic React hooks runtime, controlled timers,
// controlled SDK.fetchJSON / SDK.buildWsUrl promises, and mock WebSockets.

"use strict";

const assert = require("assert");
const fs = require("fs");
const vm = require("vm");
const { URLSearchParams } = require("url");

function createHarness(bundlePath, initialBoardSlug) {
  const src = fs.readFileSync(bundlePath, "utf8");
  let nowMs = 1000;
  let nextTimerId = 1;
  const timers = new Map();
  const setTimeoutStub = (fn, delay) => {
    const id = nextTimerId++;
    timers.set(id, { id, fn, dueAt: nowMs + Math.max(0, Number(delay) || 0) });
    return id;
  };
  const clearTimeoutStub = (id) => timers.delete(id);

  const instances = [];
  let currentInst = null;

  function makeInstance(Component, initialProps) {
    return {
      Component,
      props: initialProps || {},
      hookSlotState: [],
      hookCursor: 0,
      needsRender: false,
      isUnmounted: false,
      lastTree: null,
      pendingEffects: [],
    };
  }

  function renderInstance(inst) {
    if (inst.isUnmounted) {
      inst.needsRender = false;
      return;
    }
    const prevInst = currentInst;
    currentInst = inst;
    try {
      let guard = 0;
      do {
        if (++guard > 25) throw new Error("Infinite re-render loop in component");
        inst.needsRender = false;
        inst.hookCursor = 0;
        inst.lastTree = inst.Component(inst.props);
      } while (inst.needsRender && !inst.isUnmounted);
    } finally {
      currentInst = prevInst;
    }
  }

  function flushInstanceEffects(inst) {
    if (inst.isUnmounted) {
      inst.pendingEffects.length = 0;
      return;
    }
    const prevInst = currentInst;
    currentInst = inst;
    try {
      while (inst.pendingEffects.length > 0 && !inst.isUnmounted) {
        for (const idx of inst.pendingEffects.splice(0, inst.pendingEffects.length)) {
          const slot = inst.hookSlotState[idx];
          if (!slot) continue;
          if (typeof slot.cleanup === "function") {
            const c = slot.cleanup;
            slot.cleanup = null;
            c();
          }
          const cleanup = slot.effect();
          slot.cleanup = typeof cleanup === "function" ? cleanup : null;
        }
        if (inst.needsRender && !inst.isUnmounted) renderInstance(inst);
      }
    } finally {
      currentInst = prevInst;
    }
  }

  function unmountInstance(inst) {
    inst.isUnmounted = true;
    inst.needsRender = false;
    inst.pendingEffects.length = 0;
    for (const slot of inst.hookSlotState) {
      if (slot && typeof slot.cleanup === "function") {
        const cleanup = slot.cleanup;
        slot.cleanup = null;
        cleanup();
      }
    }
  }

  async function flushMicrotasks(ticks = 8) {
    for (let i = 0; i < ticks; i++) {
      await Promise.resolve();
      for (const inst of instances) {
        if (inst.needsRender) renderInstance(inst);
      }
    }
    for (const inst of instances) {
      flushInstanceEffects(inst);
    }
    for (let i = 0; i < 8; i++) {
      await Promise.resolve();
      for (const inst of instances) {
        if (inst.needsRender) renderInstance(inst);
      }
    }
    for (const inst of instances) {
      flushInstanceEffects(inst);
    }
  }

  async function advanceTime(ms) {
    const target = nowMs + ms;
    while (true) {
      let earliest = null;
      for (const t of timers.values()) {
        if (
          t.dueAt <= target &&
          (!earliest || t.dueAt < earliest.dueAt || (t.dueAt === earliest.dueAt && t.id < earliest.id))
        ) {
          earliest = t;
        }
      }
      if (!earliest) break;
      timers.delete(earliest.id);
      nowMs = earliest.dueAt;
      earliest.fn();
      await flushMicrotasks();
    }
    nowMs = target;
    await flushMicrotasks();
  }

  const storage = new Map(initialBoardSlug ? [["hermes.kanban.selectedBoard", initialBoardSlug]] : []);
  const localStorageStub = {
    getItem: (k) => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
    removeItem: (k) => storage.delete(k),
  };

  const fetchCalls = [];
  const fetchHandlers = [];
  const onFetch = (matcher, handler) => fetchHandlers.unshift({ matcher, handler });

  function fetchJSON(url, opts) {
    fetchCalls.push({ url, opts: opts || null });
    for (const entry of fetchHandlers) {
      const pathname = url.split("?")[0];
      const matched =
        typeof entry.matcher === "function"
          ? entry.matcher(url, opts)
          : url === entry.matcher || pathname === entry.matcher;
      if (matched) return Promise.resolve().then(() => entry.handler(url, opts));
    }
    if (url.includes("/config")) return Promise.resolve({ render_markdown: true });
    if (url.includes("/boards")) {
      return Promise.resolve({
        current: initialBoardSlug || "default",
        boards: [
          { slug: "default", name: "Default" },
          { slug: "alpha", name: "Alpha" },
          { slug: "beta", name: "Beta" },
        ],
      });
    }
    if (url.includes("/home-channels")) return Promise.resolve({ home_channels: [] });
    throw new Error(`Unhandled fetchJSON: ${url}`);
  }

  const wsUrlCalls = [];
  let customBuildWsUrl = null;
  const setBuildWsUrl = (fn) => {
    customBuildWsUrl = fn;
  };
  function buildWsUrl(path, params) {
    const cloned = Object.assign({}, params || {});
    wsUrlCalls.push({ path, params: cloned });
    if (customBuildWsUrl) return customBuildWsUrl(path, cloned);
    const qs = new URLSearchParams(cloned);
    qs.set("token", "test-token");
    return Promise.resolve(`ws://127.0.0.1:8787${path}?${qs.toString()}`);
  }

  const sockets = [];
  let failNextSocketConstructor = false;
  class FakeWebSocket {
    constructor(url) {
      if (failNextSocketConstructor) {
        failNextSocketConstructor = false;
        throw new Error("injected WebSocket constructor failure");
      }
      this.url = url;
      this.readyState = 0;
      this.closed = false;
      this.onopen = null;
      this.onmessage = null;
      this.onclose = null;
      sockets.push(this);
    }
    open() {
      this.readyState = 1;
      if (typeof this.onopen === "function") this.onopen({ type: "open" });
    }
    sendFrame(payload) {
      if (typeof this.onmessage === "function") {
        this.onmessage({ data: typeof payload === "string" ? payload : JSON.stringify(payload) });
      }
    }
    serverClose(code = 1006, reason = "") {
      this.readyState = 3;
      this.closed = true;
      if (typeof this.onclose === "function") this.onclose({ code, reason });
    }
    close() {
      this.readyState = 3;
      this.closed = true;
    }
  }

  const depsEqual = (a, b) =>
    Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => Object.is(v, b[i]));

  const hooks = {
    useState(initial) {
      const inst = currentInst;
      const idx = inst.hookCursor++;
      if (!(idx in inst.hookSlotState)) {
        inst.hookSlotState[idx] = { value: typeof initial === "function" ? initial() : initial };
      }
      const slot = inst.hookSlotState[idx];
      return [
        slot.value,
        (next) => {
          const resolved = typeof next === "function" ? next(slot.value) : next;
          if (!Object.is(resolved, slot.value)) {
            slot.value = resolved;
            inst.needsRender = true;
          }
        },
      ];
    },
    useRef(initial) {
      const inst = currentInst;
      const idx = inst.hookCursor++;
      if (!(idx in inst.hookSlotState)) inst.hookSlotState[idx] = { current: initial };
      return inst.hookSlotState[idx];
    },
    useCallback(fn, deps) {
      const inst = currentInst;
      const idx = inst.hookCursor++;
      if (!inst.hookSlotState[idx] || !depsEqual(inst.hookSlotState[idx].deps, deps)) {
        inst.hookSlotState[idx] = { fn, deps };
      }
      return inst.hookSlotState[idx].fn;
    },
    useMemo(factory, deps) {
      const inst = currentInst;
      const idx = inst.hookCursor++;
      if (!inst.hookSlotState[idx] || !depsEqual(inst.hookSlotState[idx].deps, deps)) {
        inst.hookSlotState[idx] = { value: factory(), deps };
      }
      return inst.hookSlotState[idx].value;
    },
    useEffect(effect, deps) {
      const inst = currentInst;
      const idx = inst.hookCursor++;
      const prev = inst.hookSlotState[idx];
      if (!prev || !depsEqual(prev.deps, deps)) {
        inst.hookSlotState[idx] = { deps, cleanup: prev ? prev.cleanup : null, effect };
        inst.pendingEffects.push(idx);
      }
    },
  };

  let RootComponent = null;

  class ReactComponent {
    constructor(props) {
      this.props = props || {};
      this.state = {};
    }
    setState(partial) {
      this.state = Object.assign({}, this.state, typeof partial === "function" ? partial(this.state) : partial);
      for (const inst of instances) renderInstance(inst);
    }
  }

  function createElement(type, props, ...children) {
    const flat = [];
    const push = (c) => {
      if (Array.isArray(c)) c.forEach(push);
      else if (c !== null && c !== undefined && c !== false && c !== true) flat.push(c);
    };
    children.forEach(push);
    const fullProps = Object.assign({}, props || {});
    if (flat.length === 1) fullProps.children = flat[0];
    else if (flat.length > 1) fullProps.children = flat;
    return {
      type,
      typeName: typeof type === "function" ? type.name || "Anonymous" : String(type),
      props: fullProps,
      children: flat,
    };
  }

  const makePrimitive = (name) => {
    const fn = (props) => createElement(name, props, props && props.children);
    Object.defineProperty(fn, "name", { value: name });
    return fn;
  };

  const SDK = {
    React: Object.assign({ createElement, Component: ReactComponent }, hooks),
    components: {
      Card: makePrimitive("Card"),
      CardContent: makePrimitive("CardContent"),
      Badge: makePrimitive("Badge"),
      Button: makePrimitive("Button"),
      Input: makePrimitive("Input"),
      Label: makePrimitive("Label"),
      Select: makePrimitive("Select"),
      SelectOption: makePrimitive("SelectOption"),
      ConfirmDialog: makePrimitive("ConfirmDialog"),
    },
    hooks,
    utils: { cn: (...a) => a.filter(Boolean).join(" "), timeAgo: () => "1m ago" },
    fetchJSON,
    buildWsUrl,
  };

  const sandbox = {
    window: {
      __HERMES_PLUGIN_SDK__: SDK,
      __HERMES_PLUGINS__: {
        register: (id, comp) => {
          if (id === "kanban") RootComponent = comp;
        },
      },
      localStorage: localStorageStub,
      confirm: () => true,
      prompt: () => "summary",
      alert: () => {},
      addEventListener() {},
      removeEventListener() {},
    },
    document: {
      body: { appendChild() {}, removeChild() {} },
      addEventListener() {},
      removeEventListener() {},
      querySelector: () => null,
      elementFromPoint: () => null,
    },
    navigator: { clipboard: { writeText: () => Promise.resolve() } },
    localStorage: localStorageStub,
    WebSocket: FakeWebSocket,
    URLSearchParams,
    setTimeout: setTimeoutStub,
    clearTimeout: clearTimeoutStub,
    console,
    Object,
    Array,
    String,
    Number,
    Boolean,
    Math,
    Date,
    RegExp,
    Error,
    TypeError,
    Set,
    Map,
    Promise,
    JSON,
    encodeURIComponent,
    decodeURIComponent,
  };

  vm.runInNewContext(src, sandbox, { filename: bundlePath });
  assert.strictEqual(typeof RootComponent, "function", "KanbanPage was not registered");

  const rootInst = makeInstance(RootComponent, {});
  instances.push(rootInst);

  const findNodes = (node, pred, out = []) => {
    if (!node || typeof node !== "object") return out;
    if (pred(node)) out.push(node);
    if (Array.isArray(node.children)) node.children.forEach((ch) => findNodes(ch, pred, out));
    return out;
  };
  const getText = (n) =>
    n == null
      ? ""
      : typeof n === "string" || typeof n === "number"
        ? String(n)
        : Array.isArray(n)
          ? n.map(getText).join("")
          : Array.isArray(n.children)
            ? n.children.map(getText).join("")
            : "";

  function inspectView() {
    const lastTree = rootInst.lastTree;
    const columnsNode = findNodes(lastTree, (n) => n.typeName === "BoardColumns")[0] || null;
    const switcherNode = findNodes(lastTree, (n) => n.typeName === "BoardSwitcher")[0] || null;
    const toolbarNode = findNodes(lastTree, (n) => n.typeName === "BoardToolbar")[0] || null;
    const drawerNode = findNodes(lastTree, (n) => n.typeName === "TaskDrawer")[0] || null;
    const bulkBarNode = findNodes(lastTree, (n) => n.typeName === "BulkActionBar")[0] || null;
    const dialogsNode = findNodes(lastTree, (n) => n.typeName === "KanbanDialogs")[0] || null;
    const errorTexts = findNodes(
      lastTree,
      (n) => n.props && typeof n.props.className === "string" && n.props.className.includes("text-destructive")
    )
      .map(getText)
      .filter(Boolean);
    const cardsByColumn = {};
    const allCards = [];
    if (columnsNode && columnsNode.props && columnsNode.props.board && Array.isArray(columnsNode.props.board.columns)) {
      for (const col of columnsNode.props.board.columns) {
        cardsByColumn[col.name] = (col.tasks || []).map((t) => ({
          id: t.id,
          title: t.title,
          status: t.status,
          latest_summary: t.latest_summary || null,
        }));
        allCards.push(...(col.tasks || []));
      }
    }
    return {
      text: getText(lastTree),
      columnsNode,
      switcherNode,
      toolbarNode,
      drawerNode,
      bulkBarNode,
      dialogsNode,
      errorTexts,
      cardsByColumn,
      allCards,
      fetchCalls,
      boardFetchCalls: fetchCalls.filter(
        (c) => c.url.includes("/api/plugins/kanban/board") && !c.url.includes("/boards")
      ),
    };
  }

  async function mountComponent(Component, initialProps) {
    const inst = makeInstance(Component, initialProps);
    instances.push(inst);
    renderInstance(inst);
    await flushMicrotasks();
    return {
      async updateProps(nextProps) {
        inst.props = Object.assign({}, inst.props, nextProps);
        renderInstance(inst);
        await flushMicrotasks();
      },
      inspect() {
        const tree = inst.lastTree;
        const detailNode = findNodes(tree, (n) => n.typeName === "TaskDetail")[0] || null;
        const workerLogNode = findNodes(tree, (n) => n.typeName === "WorkerLogSection")[0] || null;
        const homeSubsNode = findNodes(tree, (n) => n.typeName === "HomeSubsSection")[0] || null;
        const errorTexts = findNodes(
          tree,
          (n) => n.props && typeof n.props.className === "string" && n.props.className.includes("text-destructive")
        )
          .map(getText)
          .filter(Boolean);
        return {
          tree,
          text: getText(tree),
          detailNode,
          workerLogNode,
          homeSubsNode,
          errorTexts,
        };
      },
      unmount() {
        unmountInstance(inst);
      },
      async replayEffects() {
        for (const slot of inst.hookSlotState) {
          if (slot && typeof slot.cleanup === "function") {
            const cleanup = slot.cleanup;
            slot.cleanup = null;
            cleanup();
          }
        }
        for (const slot of inst.hookSlotState) {
          if (slot && typeof slot.effect === "function") {
            const cleanup = slot.effect();
            slot.cleanup = typeof cleanup === "function" ? cleanup : null;
          }
        }
        if (inst.needsRender && !inst.isUnmounted) renderInstance(inst);
        await flushMicrotasks();
      },
    };
  }

  return {
    mount: async () => {
      renderInstance(rootInst);
      await flushMicrotasks();
    },
    unmount: () => {
      unmountInstance(rootInst);
    },
    failNextSocket: () => {
      failNextSocketConstructor = true;
    },
    mountComponent,
    flushMicrotasks,
    advanceTime,
    onFetch,
    setBuildWsUrl,
    wsUrlCalls,
    sockets,
    fetchCalls,
    inspectView,
  };
}

function makeBoardPayload(tasksByColumn, latestEventId) {
  const canonical = ["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done"];
  return {
    columns: canonical.map((name) => ({
      name,
      tasks: (tasksByColumn[name] || []).map((t) =>
        Object.assign(
          {
            id: t.id,
            title: t.title,
            status: name,
            priority: 2,
            assignee: t.assignee || "worker",
            tenant: t.tenant || null,
            latest_summary: t.latest_summary || null,
            link_counts: { parents: 0, children: 0 },
            comment_count: 0,
            progress: null,
            diagnostics: [],
            warnings: { count: 0, highest_severity: null, kinds: {}, latest_at: null },
          },
          t
        )
      ),
    })),
    tenants: [],
    assignees: ["worker"],
    latest_event_id: latestEventId,
    now: 1700000000,
  };
}

const createDeferred = () => {
  let resolve, reject;
  return {
    promise: new Promise((res, rej) => {
      resolve = res;
      reject = rej;
    }),
    resolve,
    reject,
  };
};

async function scenarioSingleEventAndDedup(bundlePath) {
  const h = createHarness(bundlePath, "default");
  let ver = 0;
  const pendingOutOfOrder = createDeferred();
  h.onFetch("/api/plugins/kanban/board", () => {
    ver++;
    if (ver === 1) return makeBoardPayload({ ready: [{ id: "t_card_1", title: "Card v1", latest_summary: "Initial" }] }, 10);
    if (ver === 2) return makeBoardPayload({ running: [{ id: "t_card_1", title: "Card v2 (running)", latest_summary: "Claimed" }] }, 11);
    if (ver === 3) return makeBoardPayload({ review: [{ id: "t_card_1", title: "Card v3 (review)", latest_summary: "Ready for review" }] }, 12);
    return pendingOutOfOrder.promise;
  });

  await h.mount();
  let view = h.inspectView();
  assert.strictEqual(view.boardFetchCalls.length, 1);
  assert.strictEqual(view.allCards[0].title, "Card v1");
  assert.strictEqual(h.wsUrlCalls[0].params.since, "10");

  const ws = h.sockets[0];
  ws.open();
  await h.flushMicrotasks();
  assert.strictEqual(h.inspectView().boardFetchCalls.length, 1, "Initial ws.onopen must not re-fetch /board");

  view.columnsNode.props.onOpen("t_card_1");
  await h.flushMicrotasks();

  const frame11 = {
    events: [{ id: 11, task_id: "t_card_1", kind: "status_changed", payload: { from: "ready", to: "running" }, created_at: 1700000010 }],
    cursor: 11,
  };
  ws.sendFrame(frame11);
  await h.advanceTime(260);

  view = h.inspectView();
  assert.strictEqual(view.boardFetchCalls.length, 2, "Supported task event should reload /board once");
  assert.strictEqual(view.cardsByColumn.running[0].title, "Card v2 (running)");
  assert.strictEqual(view.drawerNode.props.eventTick, 1);

  ws.sendFrame(frame11);
  await h.advanceTime(300);
  view = h.inspectView();
  assert.strictEqual(view.boardFetchCalls.length, 2, "Repeated frame for already-seen event id=11 must not trigger another /board reload");
  assert.strictEqual(view.drawerNode.props.eventTick, 1, "Repeated frame for event id=11 must not increment taskEventTick again");

  ws.sendFrame({ events: [{ id: 10, task_id: "t_card_1", kind: "created", payload: null, created_at: 1700000001 }], cursor: 10 });
  await h.advanceTime(300);
  view = h.inspectView();
  assert.strictEqual(view.boardFetchCalls.length, 2, "Out-of-order older frame (id=10 <= cursor=11) must not trigger a /board reload");
  assert.strictEqual(view.drawerNode.props.eventTick, 1);

  ws.sendFrame({
    events: [
      { id: 12, task_id: "t_card_1", kind: "comment_added", payload: null, created_at: 1700000020 },
      { id: 12, task_id: "t_card_1", kind: "comment_added", payload: null, created_at: 1700000020 },
    ],
    cursor: 12,
  });
  await h.advanceTime(260);
  view = h.inspectView();
  assert.strictEqual(view.boardFetchCalls.length, 3);
  assert.strictEqual(view.cardsByColumn.review[0].title, "Card v3 (review)");
  assert.strictEqual(view.drawerNode.props.eventTick, 2);

  // Unseen out-of-order events (14 then 13, then duplicate 14) before snapshot settles:
  ws.sendFrame({ events: [{ id: 14, task_id: "t_card_1", kind: "updated", payload: null, created_at: 1700000030 }], cursor: 14 });
  ws.sendFrame({ events: [{ id: 13, task_id: "t_card_1", kind: "updated", payload: null, created_at: 1700000029 }], cursor: 13 });
  ws.sendFrame({ events: [{ id: 14, task_id: "t_card_1", kind: "updated", payload: null, created_at: 1700000030 }], cursor: 14 });
  await h.flushMicrotasks();
  assert.strictEqual(h.inspectView().drawerNode.props.eventTick, 4, "Unseen events 14 and 13 should each increment eventTick once while duplicate 14 is ignored");
  await h.advanceTime(260);
  pendingOutOfOrder.resolve(makeBoardPayload({ done: [{ id: "t_card_1", title: "Card v4 (done)" }] }, 14));
  await h.flushMicrotasks();
  assert.strictEqual(h.inspectView().cardsByColumn.done[0].title, "Card v4 (done)");
}

async function scenarioDisconnectAndMissedEventRecovery(bundlePath) {
  // Part A: Initial /board read failure still opens WS and recovers on ws.onopen
  {
    const hInit = createHarness(bundlePath, "default");
    let initReads = 0;
    hInit.onFetch("/api/plugins/kanban/board", () =>
      ++initReads === 1 ? Promise.reject(new Error("503 initial")) : makeBoardPayload({ ready: [{ id: "t_init", title: "Recovered Initial" }] }, 10)
    );
    await hInit.mount();
    await hInit.flushMicrotasks();
    assert.strictEqual(hInit.inspectView().allCards.length, 0);
    assert.strictEqual(hInit.sockets.length, 1, "WebSocket must still open even when initial /board fetch fails");
    hInit.sockets[0].open();
    await hInit.flushMicrotasks();
    assert.strictEqual(hInit.inspectView().allCards[0].title, "Recovered Initial");
    assert.strictEqual(hInit.inspectView().errorTexts.length, 0);
  }

  // Part B: Rejected WS ticket and WebSocket constructor failure backoff + retry
  {
    const hRetry = createHarness(bundlePath, "default");
    let tickets = 0;
    hRetry.setBuildWsUrl((url, params) =>
      ++tickets === 1 ? Promise.reject(new Error("ticket failure")) : Promise.resolve("ws://127.0.0.1:8787/api/plugins/kanban/events")
    );
    hRetry.onFetch("/api/plugins/kanban/board", () => makeBoardPayload({ ready: [{ id: "t_retry", title: "Retry Card" }] }, 10));
    await hRetry.mount();
    assert.strictEqual(hRetry.sockets.length, 0);
    hRetry.failNextSocket();
    await hRetry.advanceTime(1000);
    assert.strictEqual(hRetry.sockets.length, 0, "Constructor throw must schedule backoff retry");
    await hRetry.advanceTime(2000);
    assert.strictEqual(hRetry.sockets.length, 1);
    hRetry.sockets[0].open();
    await hRetry.flushMicrotasks();
    assert.strictEqual(hRetry.inspectView().boardFetchCalls.length, 2);
  }

  // Part C: Disconnect visibly marks cached board stale/reconnecting, recovers missed events on reconnect,
  // suppresses replayed event covered by reconnect snapshot, and recovers on cursor gap
  const h = createHarness(bundlePath, "default");
  let phase = "initial";
  h.onFetch("/api/plugins/kanban/board", () => {
    if (phase === "initial") return makeBoardPayload({ ready: [{ id: "t_rec_1", title: "Before Disconnect" }] }, 20);
    if (phase === "missed_while_disconnected") return makeBoardPayload({ running: [{ id: "t_rec_1", title: "Recovered From Snapshot After Disconnect" }] }, 21);
    return makeBoardPayload({ done: [{ id: "t_rec_1", title: "Recovered After Cursor Gap" }] }, 25);
  });

  await h.mount();
  const ws1 = h.sockets[0];
  ws1.open();
  await h.flushMicrotasks();
  h.inspectView().columnsNode.props.onOpen("t_rec_1");
  await h.flushMicrotasks();
  assert.strictEqual(h.inspectView().drawerNode.props.eventTick, 0);

  phase = "missed_while_disconnected";
  ws1.serverClose(1006, "abnormal closure");
  await h.flushMicrotasks();
  let view = h.inspectView();
  assert.ok(
    view.errorTexts.length > 0 || /reconnecting|disconnected|stale/i.test(view.text),
    "Unexpected disconnect must visibly mark cached board as stale/reconnecting"
  );
  assert.strictEqual(view.cardsByColumn.ready[0].title, "Before Disconnect", "Cached cards must remain visible while disconnected");

  await h.advanceTime(1000);
  assert.strictEqual(h.sockets.length, 2);
  assert.strictEqual(h.wsUrlCalls[1].params.since, "20");

  const ws2 = h.sockets[1];
  ws2.open();
  await h.advanceTime(300);
  view = h.inspectView();
  assert.strictEqual(view.cardsByColumn.running.length, 1, "Disconnect/reconnect with missed event must recover via a real /board snapshot");
  assert.strictEqual(view.cardsByColumn.running[0].title, "Recovered From Snapshot After Disconnect");
  assert.strictEqual(
    view.drawerNode.props.eventTick,
    1,
    "Open TaskDrawer must increment eventTick when reconnect /board snapshot advances latest_event_id beyond pre-disconnect cursor"
  );
  assert.ok(!/reconnecting|disconnected/i.test(view.text), "Reconnected board must clear the stale/reconnecting banner");
  const fetchesAfterReconnect = view.boardFetchCalls.length;

  ws2.sendFrame({ events: [{ id: 21, task_id: "t_rec_1", kind: "status_changed", payload: null, created_at: 1700000030 }], cursor: 21 });
  await h.advanceTime(300);
  assert.strictEqual(h.inspectView().boardFetchCalls.length, fetchesAfterReconnect, "Replayed event covered by reconnect snapshot must not re-fetch /board");
  assert.strictEqual(h.inspectView().drawerNode.props.eventTick, 1, "Replayed event covered by reconnect snapshot must not double-increment TaskDrawer eventTick");

  phase = "cursor_gap";
  ws2.sendFrame({ events: [], cursor: 25 });
  await h.advanceTime(260);
  view = h.inspectView();
  assert.strictEqual(view.cardsByColumn.done.length, 1, "Cursor jump beyond snapshot must recover via /board");
  assert.strictEqual(view.cardsByColumn.done[0].title, "Recovered After Cursor Gap");
  assert.strictEqual(
    view.drawerNode.props.eventTick,
    2,
    "Cursor jump beyond snapshot must also increment open TaskDrawer eventTick"
  );
}

async function scenarioFailedReadRetainsPriorCard(bundlePath) {
  const h = createHarness(bundlePath, "default");
  let readStep = 0;
  const slowStaleRead = createDeferred();
  h.onFetch("/api/plugins/kanban/board", () => {
    readStep++;
    if (readStep === 1) return makeBoardPayload({ running: [{ id: "t_keep_1", title: "Prior Card Must Stay Visible" }] }, 30);
    if (readStep === 2) return Promise.reject(new Error("503: Database temporarily unavailable"));
    if (readStep === 3) return Promise.resolve({ detail: "Upstream gateway timeout" });
    if (readStep === 4) return slowStaleRead.promise;
    if (readStep === 5) return Promise.reject(new Error("502: Read failed after slow request started"));
    return makeBoardPayload({ done: [{ id: "t_keep_1", title: "Card Updated After Recovery" }] }, 34);
  });

  await h.mount();
  const ws = h.sockets[0];
  ws.open();
  await h.flushMicrotasks();

  ws.sendFrame({ events: [{ id: 31, task_id: "t_keep_1", kind: "updated", payload: null, created_at: 1700000040 }], cursor: 31 });
  await h.advanceTime(260);
  let view = h.inspectView();
  assert.strictEqual(view.cardsByColumn.running.length, 1, "Failed /board read must preserve prior cards (no false-zero view)");
  assert.strictEqual(view.cardsByColumn.running[0].title, "Prior Card Must Stay Visible");
  assert.ok(view.errorTexts.some((t) => t.includes("503: Database temporarily unavailable")), "Read error must stay visible on board");

  ws.sendFrame({ events: [{ id: 32, task_id: "t_keep_1", kind: "updated", payload: null, created_at: 1700000041 }], cursor: 32 });
  await h.advanceTime(260);
  view = h.inspectView();
  assert.strictEqual(view.cardsByColumn.running.length, 1, "Malformed /board response must not replace prior cards with false-zero or crash");
  assert.ok(view.errorTexts.some((t) => t.includes("Upstream gateway timeout")));

  view.toolbarNode.props.onRefresh();
  await h.flushMicrotasks();
  view.toolbarNode.props.onRefresh();
  await h.flushMicrotasks();
  slowStaleRead.resolve(makeBoardPayload({ ready: [{ id: "t_keep_1", title: "Stale Pre-Failure Card" }] }, 29));
  await h.flushMicrotasks();
  view = h.inspectView();
  assert.strictEqual(view.cardsByColumn.running[0].title, "Prior Card Must Stay Visible");
  assert.ok(view.errorTexts.some((t) => t.includes("502: Read failed after slow request started")));

  view.toolbarNode.props.onRefresh();
  await h.flushMicrotasks();
  view = h.inspectView();
  assert.strictEqual(view.errorTexts.length, 0);
  assert.strictEqual(view.cardsByColumn.done[0].title, "Card Updated After Recovery");
}

async function scenarioStaleAsyncWorkAfterBoardChange(bundlePath) {
  const h = createHarness(bundlePath, "alpha");
  const alphaSlowRefresh = createDeferred();
  const betaInitialLoad = createDeferred();
  const alphaSlowWsTicket = createDeferred();
  const alphaSlowPatch = createDeferred();
  let alphaLoads = 0;
  let betaLoads = 0;

  h.onFetch("/api/plugins/kanban/board", (url) => {
    if (url.includes("board=alpha")) {
      alphaLoads++;
      if (alphaLoads === 1) return makeBoardPayload({ ready: [{ id: "t_alpha", title: "Alpha Initial Card" }] }, 50);
      if (alphaLoads === 2) return alphaSlowRefresh.promise;
      return makeBoardPayload({ running: [{ id: "t_alpha", title: "Alpha Returned Card" }] }, 51);
    }
    if (url.includes("board=beta")) {
      betaLoads++;
      if (betaLoads === 1) return betaInitialLoad.promise;
      return makeBoardPayload({ ready: [{ id: "t_beta", title: "Beta Updated Card" }] }, 6);
    }
    throw new Error(`Unexpected board URL: ${url}`);
  });
  h.onFetch(
    (url, opts) => opts && opts.method === "PATCH" && url.includes("/api/plugins/kanban/tasks/t_alpha"),
    () => alphaSlowPatch.promise
  );

  await h.mount();
  let view = h.inspectView();
  const alphaWs1 = h.sockets[0];
  alphaWs1.open();
  await h.flushMicrotasks();

  view.columnsNode.props.onMove("t_alpha", "todo");
  await h.flushMicrotasks();
  view.toolbarNode.props.onRefresh();
  await h.flushMicrotasks();
  alphaWs1.sendFrame({ events: [{ id: 51, task_id: "t_alpha", kind: "updated", payload: null, created_at: 1700000050 }], cursor: 51 });
  alphaWs1.serverClose(1006, "alpha disconnect");
  await h.advanceTime(1000);

  const alphaWs2 = h.sockets[1];
  h.setBuildWsUrl((path, params) => {
    if (params.board === "alpha" && alphaLoads === 2) return alphaSlowWsTicket.promise;
    const qs = new URLSearchParams(params);
    qs.set("token", "test-token");
    return Promise.resolve(`ws://127.0.0.1:8787${path}?${qs.toString()}`);
  });
  alphaWs2.serverClose(1006, "alpha disconnect 2");
  await h.advanceTime(2000);
  alphaWs2.sendFrame({ events: [{ id: 52, task_id: "t_alpha", kind: "updated", payload: null, created_at: 1700000051 }], cursor: 52 });

  view = h.inspectView();
  view.switcherNode.props.onSwitch("beta");
  await h.flushMicrotasks();
  const socketsBeforeAlphaLateResolve = h.sockets.length;

  alphaSlowRefresh.resolve(makeBoardPayload({ running: [{ id: "t_alpha_stale", title: "STALE ALPHA CARD ON BETA" }] }, 99));
  await h.flushMicrotasks();
  alphaSlowWsTicket.resolve("ws://127.0.0.1:8787/api/plugins/kanban/events?since=51&board=alpha&token=late");
  await h.flushMicrotasks();
  await h.advanceTime(5000);

  view = h.inspectView();
  assert.strictEqual(
    view.allCards.length,
    0,
    `Old-board (alpha) in-flight loadBoard must not populate new board (beta); found cards=${JSON.stringify(view.allCards)}`
  );
  assert.ok(view.text.includes("Loading Kanban board"));
  assert.strictEqual(
    h.sockets.length,
    socketsBeforeAlphaLateResolve,
    "Old-board in-flight buildWsUrl or reconnect timers must not open a ghost WebSocket on beta"
  );

  betaInitialLoad.resolve(makeBoardPayload({ ready: [{ id: "t_beta", title: "Beta Fresh Card" }] }, 5));
  await h.flushMicrotasks();
  const betaFetchesBeforeLateAlphaPatch = h.inspectView().boardFetchCalls.length;
  alphaSlowPatch.reject(new Error("409: Alpha move conflict"));
  await h.flushMicrotasks();
  view = h.inspectView();
  assert.strictEqual(view.errorTexts.length, 0, "Late in-flight PATCH failure from alpha must not surface an error banner on beta");
  assert.strictEqual(view.boardFetchCalls.length, betaFetchesBeforeLateAlphaPatch, "Late in-flight PATCH failure from alpha must not reload beta");
  assert.strictEqual(view.allCards[0].title, "Beta Fresh Card");
  const betaWsCall = h.wsUrlCalls[h.wsUrlCalls.length - 1];
  assert.strictEqual(betaWsCall.params.board, "beta");
  assert.strictEqual(betaWsCall.params.since, "5");

  view.switcherNode.props.onSwitch("alpha");
  await h.flushMicrotasks();
  const alphaReturnWsCall = h.wsUrlCalls[h.wsUrlCalls.length - 1];
  assert.strictEqual(alphaReturnWsCall.params.board, "alpha");
  assert.strictEqual(
    alphaReturnWsCall.params.since,
    "51",
    `Alpha must preserve its board-local cursor (51) across board switches; got since=${alphaReturnWsCall.params.since}`
  );

  // Deleting a board and recreating it with the same slug must reset its board-local cursor
  h.onFetch(
    (url, opts) => opts && opts.method === "DELETE" && url.includes("/api/plugins/kanban/boards/alpha"),
    () => ({ archived: true })
  );
  h.onFetch("/api/plugins/kanban/board", (url) => {
    if (url.includes("board=alpha")) {
      return makeBoardPayload({ ready: [{ id: "t_alpha_recreated", title: "Recreated Alpha Card" }] }, 1);
    }
    return makeBoardPayload({ ready: [{ id: "t_default", title: "Default Card" }] }, 3);
  });
  await h.inspectView().switcherNode.props.onDeleteBoard("alpha");
  await h.flushMicrotasks();
  h.inspectView().switcherNode.props.onSwitch("alpha");
  await h.flushMicrotasks();
  const recreatedAlphaWsCall = h.wsUrlCalls[h.wsUrlCalls.length - 1];
  assert.strictEqual(recreatedAlphaWsCall.params.board, "alpha");
  assert.strictEqual(
    recreatedAlphaWsCall.params.since,
    "1",
    `Deleted and recreated board must start with fresh snapshot cursor (1), not stale cursor (51); got since=${recreatedAlphaWsCall.params.since}`
  );
  const recreatedAlphaWs = h.sockets[h.sockets.length - 1];
  recreatedAlphaWs.open();
  await h.flushMicrotasks();
  const fetchesBeforeRecreatedEvent = h.inspectView().boardFetchCalls.length;
  recreatedAlphaWs.sendFrame({
    events: [{ id: 2, task_id: "t_alpha_recreated", kind: "updated", payload: null, created_at: 1700000090 }],
    cursor: 2,
  });
  await h.advanceTime(260);
  assert.strictEqual(
    h.inspectView().boardFetchCalls.length,
    fetchesBeforeRecreatedEvent + 1,
    "Recreated board with low event IDs (id=2 < old cursor 51) must not ignore new WS events"
  );

  // Verify unmount cancels any late in-flight read follow-up reload
  const hUnmount = createHarness(bundlePath, "default");
  const pendingRead = createDeferred();
  let unmountReads = 0;
  hUnmount.onFetch("/api/plugins/kanban/board", () =>
    ++unmountReads === 1
      ? makeBoardPayload({ ready: [{ id: "t_u1", title: "Initial" }] }, 10)
      : unmountReads === 2
        ? pendingRead.promise
        : makeBoardPayload({ ready: [{ id: "t_u1", title: "Unexpected Post-Unmount Read" }] }, 11)
  );
  await hUnmount.mount();
  const wsU = hUnmount.sockets[0];
  wsU.open();
  await hUnmount.flushMicrotasks();
  hUnmount.inspectView().toolbarNode.props.onRefresh();
  await hUnmount.flushMicrotasks();
  wsU.sendFrame({ events: [{ id: 11, task_id: "t_u1", kind: "updated", payload: null, created_at: 1700000060 }], cursor: 11 });
  await hUnmount.flushMicrotasks();
  hUnmount.unmount();
  const fetchesBeforeLateResolve = hUnmount.inspectView().boardFetchCalls.length;
  pendingRead.resolve(makeBoardPayload({ ready: [{ id: "t_u1", title: "Late Snapshot" }] }, 10));
  await hUnmount.flushMicrotasks();
  await hUnmount.advanceTime(1000);
  assert.strictEqual(
    hUnmount.inspectView().boardFetchCalls.length,
    fetchesBeforeLateResolve,
    "Late read completion after unmount must not schedule another /board fetch"
  );
}

async function scenarioTaskAndBoardIdentity(bundlePath) {
  const h = createHarness(bundlePath, "alpha");
  let betaSnapStep = 0;
  const deletedTasks = [];
  const slowTask1Detail = createDeferred();
  const slowTask1Patch = createDeferred();
  const slowTask1Log = createDeferred();
  let task2LogReads = 0;

  h.onFetch("/api/plugins/kanban/board", (url) => {
    if (url.includes("board=alpha")) {
      return makeBoardPayload(
        {
          ready: [
            { id: "t_alpha_1", title: "Alpha Task 1" },
            { id: "t_alpha_2", title: "Alpha Task 2" },
          ],
        },
        10
      );
    }
    if (url.includes("board=beta")) {
      betaSnapStep++;
      if (betaSnapStep === 1) {
        return makeBoardPayload(
          {
            ready: [
              { id: "t_beta_1", title: "Beta Task 1" },
              { id: "t_beta_2", title: "Beta Task 2" },
            ],
          },
          20
        );
      }
      // Snapshot 2 on beta: t_beta_2 was removed externally
      return makeBoardPayload({ ready: [{ id: "t_beta_1", title: "Beta Task 1" }] }, 21);
    }
    throw new Error(`Unexpected board URL: ${url}`);
  });

  h.onFetch(
    (url, opts) => opts && opts.method === "DELETE" && url.includes("/api/plugins/kanban/tasks/"),
    (url) => {
      deletedTasks.push(url);
      return { deleted: true };
    }
  );
  h.onFetch(
    (url, opts) => opts && opts.method === "PATCH" && url.includes("/api/plugins/kanban/tasks/t_beta_1"),
    () => slowTask1Patch.promise
  );

  let task1Fetches = 0;
  let task2Fetches = 0;
  h.onFetch(
    (url, opts) => (!opts || !opts.method || opts.method === "GET") && url.includes("/api/plugins/kanban/tasks/"),
    (url) => {
      if (url.includes("/tasks/t_beta_1/log")) {
        return slowTask1Log.promise;
      }
      if (url.includes("/tasks/t_beta_2/log")) {
        task2LogReads++;
        if (task2LogReads === 1) {
          return { exists: true, content: "t_beta_2 worker log line 1", size_bytes: 26, truncated: false, path: "/tmp/t_beta_2.log" };
        }
        return Promise.reject(new Error("503: Transient log refresh error"));
      }
      if (url.includes("/tasks/t_beta_1")) {
        task1Fetches++;
        if (task1Fetches === 1) return slowTask1Detail.promise;
        return { task: { id: "t_beta_1", title: "Beta Task 1 Detail", status: "ready" }, comments: [], events: [], attachments: [], links: { parents: [], children: [] }, runs: [] };
      }
      if (url.includes("/tasks/t_beta_2")) {
        task2Fetches++;
        if (task2Fetches === 1) {
          return { task: { id: "t_beta_2", title: "Beta Task 2 Fresh Detail", status: "ready" }, comments: [], events: [], attachments: [], links: { parents: [], children: [] }, runs: [] };
        }
        return Promise.reject(new Error("503: Transient detail refresh failure"));
      }
      throw new Error(`Unexpected task detail URL: ${url}`);
    }
  );

  await h.mount();
  let view = h.inspectView();

  // 1. Open TaskDrawer for t_alpha_1 and drag/select on board alpha, then switch to board beta:
  view.columnsNode.props.onOpen("t_alpha_1");
  view.columnsNode.props.onDragStart("t_alpha_1");
  view.columnsNode.props.toggleSelected("t_alpha_1", true);
  await h.flushMicrotasks();
  view = h.inspectView();
  assert.ok(view.drawerNode, "TaskDrawer should open for t_alpha_1 on alpha");
  assert.strictEqual(view.drawerNode.props.taskId, "t_alpha_1");

  view.switcherNode.props.onSwitch("beta");
  await h.flushMicrotasks();
  view = h.inspectView();
  assert.strictEqual(
    view.drawerNode,
    null,
    "Switching boards (alpha -> beta) must clear selectedTaskId so alpha's TaskDrawer does not reopen against beta"
  );
  assert.strictEqual(view.columnsNode.props.draggingTaskId, null, "Switching boards must clear draggingTaskId");
  assert.strictEqual(view.columnsNode.props.selectedIds.size, 0, "Switching boards must clear selectedIds");

  // 2. Select both t_beta_1 and t_beta_2 on beta; when a fresh /board snapshot arrives without t_beta_2,
  // selectedIds must prune t_beta_2 so ghost task IDs cannot linger in bulk selection:
  view.columnsNode.props.toggleSelected("t_beta_1", true);
  view.columnsNode.props.toggleSelected("t_beta_2", true);
  await h.flushMicrotasks();
  assert.strictEqual(h.inspectView().columnsNode.props.selectedIds.size, 2);

  h.inspectView().toolbarNode.props.onRefresh();
  await h.flushMicrotasks();
  view = h.inspectView();
  assert.strictEqual(
    view.columnsNode.props.selectedIds.has("t_beta_2"),
    false,
    "Fresh /board snapshot must prune removed task IDs (t_beta_2) from selectedIds"
  );
  assert.strictEqual(view.columnsNode.props.selectedIds.has("t_beta_1"), true);

  // 3. Open t_beta_1 in TaskDrawer and delete t_beta_1 via deleteTask:
  // must pass ?board=beta and close TaskDrawer if deleted task was open.
  view.columnsNode.props.onOpen("t_beta_1");
  await h.flushMicrotasks();
  view = h.inspectView();
  assert.ok(view.drawerNode);
  const TaskDrawerComponent = view.drawerNode.type;

  const deletePromise = view.columnsNode.props.onDelete("t_beta_1");
  await h.flushMicrotasks();
  view = h.inspectView();
  if (view.dialogsNode && view.dialogsNode.props && view.dialogsNode.props.dialogProps && view.dialogsNode.props.dialogProps.onConfirm) {
    view.dialogsNode.props.dialogProps.onConfirm();
  }
  await deletePromise;
  await h.flushMicrotasks();
  assert.strictEqual(deletedTasks.length, 1);
  assert.ok(
    deletedTasks[0].includes("board=beta"),
    `deleteTask must include board query param via withBoard(); got URL=${deletedTasks[0]}`
  );
  assert.strictEqual(
    h.inspectView().drawerNode,
    null,
    "Deleting the currently-open task must close TaskDrawer (clear selectedTaskId)"
  );

  // 4. TaskDrawer stale-read and stale-mutation protection: switching taskId (t_beta_1 -> t_beta_2)
  // while t_beta_1 GET and PATCH are slow must ignore late t_beta_1 responses/errors, and a failed
  // background refresh on t_beta_2 must keep t_beta_2's detail visible.
  const drawer = await h.mountComponent(TaskDrawerComponent, {
    taskId: "t_beta_1",
    boardSlug: "beta",
    onClose: () => {},
    onOpenTask: () => {},
    onRefresh: () => {},
    renderMarkdown: true,
    allTasks: [],
    assignees: [],
    eventTick: 0,
  });
  await drawer.updateProps({ taskId: "t_beta_2", eventTick: 0 });
  assert.strictEqual(drawer.inspect().detailNode.props.data.task.id, "t_beta_2");

  // Resolve slow t_beta_1 response late:
  slowTask1Detail.resolve({
    task: { id: "t_beta_1", title: "STALE T_BETA_1 DETAIL", status: "ready" },
    comments: [],
    events: [],
    attachments: [],
    links: { parents: [], children: [] },
    runs: [],
  });
  await h.flushMicrotasks();
  assert.strictEqual(
    drawer.inspect().detailNode.props.data.task.id,
    "t_beta_2",
    "Late GET /tasks/t_beta_1 response must not overwrite TaskDrawer showing t_beta_2"
  );

  // Start a slow PATCH on t_beta_1 by temporarily switching to t_beta_1, firing onPatch, then switching back to t_beta_2:
  task2Fetches = 0;
  await drawer.updateProps({ taskId: "t_beta_1", eventTick: 0 });
  drawer.inspect().detailNode.props.setEditing(true);
  drawer.inspect().detailNode.props.onPatch({ title: "Edited t_beta_1" });
  await drawer.updateProps({ taskId: "t_beta_2", eventTick: 0 });
  assert.strictEqual(
    drawer.inspect().detailNode.props.editing,
    false,
    "Switching TaskDrawer taskId must reset editing state"
  );
  slowTask1Patch.reject(new Error("409: Stale t_beta_1 patch conflict"));
  await h.flushMicrotasks();
  assert.strictEqual(
    drawer.inspect().errorTexts.length,
    0,
    "Late PATCH error from t_beta_1 must not surface on TaskDrawer now showing t_beta_2"
  );

  // Trigger background eventTick refresh on t_beta_2 that fails with 503:
  await drawer.updateProps({ taskId: "t_beta_2", eventTick: 1 });
  const drawerViewAfterErr = drawer.inspect();
  assert.ok(
    drawerViewAfterErr.detailNode,
    "Transient background refresh error in TaskDrawer must preserve already-loaded TaskDetail"
  );
  assert.strictEqual(drawerViewAfterErr.detailNode.props.data.task.id, "t_beta_2");
  assert.ok(
    drawerViewAfterErr.errorTexts.some((t) => t.includes("503: Transient detail refresh failure")),
    "Transient background refresh error in TaskDrawer must surface error text"
  );

  // 5. WorkerLogSection stale-read and transient refresh error protection:
  const detailMount = await h.mountComponent(drawerViewAfterErr.detailNode.type, drawerViewAfterErr.detailNode.props);
  const WorkerLogSectionComponent = detailMount.inspect().workerLogNode.type;
  detailMount.unmount();

  task2LogReads = 0;
  const logMount = await h.mountComponent(WorkerLogSectionComponent, {
    taskId: "t_beta_1",
    boardSlug: "beta",
    eventTick: 0,
  });
  await logMount.updateProps({ taskId: "t_beta_2", boardSlug: "beta", eventTick: 0 });
  assert.ok(
    logMount.inspect().text.includes("t_beta_2 worker log line 1"),
    "WorkerLogSection must display t_beta_2 log content after switching to t_beta_2"
  );
  slowTask1Log.resolve({
    exists: true,
    content: "STALE T_BETA_1 WORKER LOG",
    size_bytes: 25,
    truncated: false,
    path: "/tmp/t_beta_1.log",
  });
  await h.flushMicrotasks();
  assert.ok(
    !logMount.inspect().text.includes("STALE T_BETA_1 WORKER LOG") &&
      logMount.inspect().text.includes("t_beta_2 worker log line 1"),
    "Late GET /tasks/t_beta_1/log response must not overwrite WorkerLogSection showing t_beta_2"
  );
  await logMount.updateProps({ taskId: "t_beta_2", boardSlug: "beta", eventTick: 1 });
  assert.ok(
    logMount.inspect().text.includes("t_beta_2 worker log line 1"),
    "Transient WorkerLogSection refresh failure must preserve already-loaded log content"
  );
  assert.ok(
    logMount.inspect().errorTexts.some((t) => t.includes("503: Transient log refresh error")),
    "Transient WorkerLogSection refresh failure must surface error message"
  );
  logMount.unmount();
  drawer.unmount();
}

async function scenarioBackendContractRoundtrip(bundlePath) {
  const fixture = JSON.parse(process.env.KANBAN_CONTRACT_FIXTURE || "{}");
  assert.ok(fixture.alphaBoard, "KANBAN_CONTRACT_FIXTURE env var is required");
  const h = createHarness(bundlePath, fixture.alphaBoard);
  let alphaStage = 0;

  h.onFetch("/api/plugins/kanban/board", (url) => {
    if (url.includes(`board=${fixture.alphaBoard}`)) {
      alphaStage++;
      if (alphaStage === 1) return fixture.alphaSnapshot1;
      if (alphaStage === 2) return fixture.alphaSnapshot2;
      if (alphaStage === 3) return Promise.reject(new Error("503: Simulated read failure on real snapshot"));
      return fixture.alphaSnapshot3;
    }
    if (url.includes(`board=${fixture.betaBoard}`)) return fixture.betaSnapshot1;
    throw new Error(`Unexpected URL: ${url}`);
  });

  await h.mount();
  let view = h.inspectView();
  assert.strictEqual(view.allCards[0].id, fixture.alphaTaskId);
  assert.strictEqual(view.allCards[0].title, fixture.alphaSnapshot1Title);

  const ws1 = h.sockets[0];
  ws1.open();
  await h.flushMicrotasks();
  ws1.sendFrame(fixture.alphaEventFrame1);
  ws1.sendFrame(fixture.alphaEventFrame1);
  await h.advanceTime(260);

  view = h.inspectView();
  assert.strictEqual(view.boardFetchCalls.length, 2, "Real WS event frame should trigger exactly one /board reload");
  assert.strictEqual(view.allCards[0].title, fixture.alphaSnapshot2Title);
  assert.strictEqual(view.allCards[0].status, fixture.alphaSnapshot2Status);

  view.toolbarNode.props.onRefresh();
  await h.flushMicrotasks();
  view = h.inspectView();
  assert.strictEqual(view.allCards[0].title, fixture.alphaSnapshot2Title);
  assert.ok(view.errorTexts.some((t) => t.includes("503: Simulated read failure on real snapshot")));

  ws1.serverClose(1006, "lost connection");
  await h.advanceTime(1000);
  const ws2 = h.sockets[1];
  ws2.open();
  await h.advanceTime(300);

  view = h.inspectView();
  assert.strictEqual(view.errorTexts.length, 0);
  assert.strictEqual(view.allCards[0].title, fixture.alphaSnapshot3Title);
  assert.strictEqual(view.allCards[0].status, fixture.alphaSnapshot3Status);

  view.switcherNode.props.onSwitch(fixture.betaBoard);
  await h.flushMicrotasks();
  view = h.inspectView();
  assert.strictEqual(view.allCards[0].id, fixture.betaTaskId);
  assert.strictEqual(view.allCards[0].title, fixture.betaSnapshot1Title);
}

async function scenarioHomeChannelsEventTickAndGuards(bundlePath) {
  const h = createHarness(bundlePath, "alpha");
  const pendingHomeEventTick = createDeferred();
  const pendingHomeStaleTask = createDeferred();
  const pendingHomeStaleBoard = createDeferred();
  const pendingHomeUnmounted = createDeferred();
  let homePhase = "eventTick";

  h.onFetch("/api/plugins/kanban/board", () =>
    makeBoardPayload(
      {
        ready: [
          { id: "t_alpha_1", title: "Alpha Task 1" },
          { id: "t_alpha_2", title: "Alpha Task 2" },
        ],
      },
      10
    )
  );
  h.onFetch("/api/plugins/kanban/tasks/t_alpha_1", () => ({
    task: { id: "t_alpha_1", title: "Alpha Task 1", status: "ready" },
    comments: [],
    events: [],
    attachments: [],
    links: { parents: [], children: [] },
    runs: [],
  }));
  h.onFetch("/api/plugins/kanban/tasks/t_alpha_2", () => ({
    task: { id: "t_alpha_2", title: "Alpha Task 2", status: "ready" },
    comments: [],
    events: [],
    attachments: [],
    links: { parents: [], children: [] },
    runs: [],
  }));
  h.onFetch(
    (url) => url.includes("/api/plugins/kanban/home-channels"),
    (url) => {
      if (homePhase === "eventTick" && url.includes("task_id=t_alpha_1") && url.includes("board=alpha")) {
        return pendingHomeEventTick.promise;
      }
      if (homePhase === "staleTask" && url.includes("task_id=t_alpha_1") && url.includes("board=alpha")) {
        return pendingHomeStaleTask.promise;
      }
      if (homePhase === "staleTask" && url.includes("task_id=t_alpha_2") && url.includes("board=alpha")) {
        return { home_channels: [{ platform: "discord", channel_id: "D2", name: "task2-chan", subscribed: true }] };
      }
      if (homePhase === "staleBoard" && url.includes("task_id=t_alpha_1") && url.includes("board=alpha")) {
        return pendingHomeStaleBoard.promise;
      }
      if (homePhase === "staleBoard" && url.includes("task_id=t_alpha_1") && url.includes("board=beta")) {
        return { home_channels: [] };
      }
      if (homePhase === "unmount" && url.includes("task_id=t_alpha_1") && url.includes("board=alpha")) {
        return pendingHomeUnmounted.promise;
      }
      return { home_channels: [] };
    }
  );

  await h.mount();
  let view = h.inspectView();
  view.columnsNode.props.onOpen("t_alpha_1");
  await h.flushMicrotasks();
  view = h.inspectView();
  const TaskDrawerComponent = view.drawerNode.type;
  view.drawerNode.props.onClose();
  await h.flushMicrotasks();

  // 1. Drawer mounted, home-channels GET pending, eventTick changes for same task/board,
  // response resolves with a subscribed Slack channel -> channel is shown.
  homePhase = "eventTick";
  const drawer1 = await h.mountComponent(TaskDrawerComponent, {
    taskId: "t_alpha_1",
    boardSlug: "alpha",
    onClose: () => {},
    onOpenTask: () => {},
    onRefresh: () => {},
    renderMarkdown: true,
    allTasks: [],
    assignees: [],
    eventTick: 0,
  });
  await drawer1.updateProps({ taskId: "t_alpha_1", boardSlug: "alpha", eventTick: 1 });
  pendingHomeEventTick.resolve({
    home_channels: [{ platform: "slack", channel_id: "C_OPS", name: "ops", subscribed: true }],
  });
  await h.flushMicrotasks();

  const d1Detail = drawer1.inspect().detailNode;
  assert.ok(d1Detail, "TaskDetail should be rendered in TaskDrawer");
  assert.strictEqual(
    d1Detail.props.homeChannels.length,
    1,
    "In-flight /home-channels response must not be discarded by an eventTick refresh for the same task/board"
  );
  assert.strictEqual(d1Detail.props.homeChannels[0].platform, "slack");
  assert.strictEqual(d1Detail.props.homeChannels[0].subscribed, true);

  const detailMount = await h.mountComponent(d1Detail.type, d1Detail.props);
  const homeSubsNode = detailMount.inspect().homeSubsNode;
  assert.ok(homeSubsNode, "HomeSubsSection should be rendered inside TaskDetail");
  const homeSubsMount = await h.mountComponent(homeSubsNode.type, homeSubsNode.props);
  assert.ok(
    homeSubsMount.inspect().text.includes("✓ slack"),
    "Subscribed Slack channel must be rendered inside TaskDetail HomeSubsSection"
  );
  homeSubsMount.unmount();
  detailMount.unmount();
  drawer1.unmount();

  // 2. Task change guard still holds: switching taskId discards pending home-channels read from prior task.
  homePhase = "staleTask";
  const drawer2 = await h.mountComponent(TaskDrawerComponent, {
    taskId: "t_alpha_1",
    boardSlug: "alpha",
    onClose: () => {},
    onOpenTask: () => {},
    onRefresh: () => {},
    renderMarkdown: true,
    allTasks: [],
    assignees: [],
    eventTick: 0,
  });
  await drawer2.updateProps({ taskId: "t_alpha_2", boardSlug: "alpha", eventTick: 0 });
  pendingHomeStaleTask.resolve({
    home_channels: [{ platform: "slack", channel_id: "C_STALE_TASK", name: "stale-task", subscribed: true }],
  });
  await h.flushMicrotasks();
  assert.deepStrictEqual(
    drawer2.inspect().detailNode.props.homeChannels.map((c) => c.name),
    ["task2-chan"],
    "Late /home-channels response for old taskId must be ignored after switching taskId"
  );
  drawer2.unmount();

  // 3. Board change guard still holds: switching boardSlug discards pending home-channels read from prior board.
  homePhase = "staleBoard";
  const drawer3 = await h.mountComponent(TaskDrawerComponent, {
    taskId: "t_alpha_1",
    boardSlug: "alpha",
    onClose: () => {},
    onOpenTask: () => {},
    onRefresh: () => {},
    renderMarkdown: true,
    allTasks: [],
    assignees: [],
    eventTick: 0,
  });
  await drawer3.updateProps({ taskId: "t_alpha_1", boardSlug: "beta", eventTick: 0 });
  pendingHomeStaleBoard.resolve({
    home_channels: [{ platform: "slack", channel_id: "C_STALE_BOARD", name: "stale-board", subscribed: true }],
  });
  await h.flushMicrotasks();
  assert.strictEqual(
    drawer3.inspect().detailNode.props.homeChannels.length,
    0,
    "Late /home-channels response for old boardSlug must be ignored after switching boardSlug"
  );
  drawer3.unmount();

  // 4. Unmount guard still holds: unmounting drawer discards pending home-channels read.
  homePhase = "unmount";
  const drawer4 = await h.mountComponent(TaskDrawerComponent, {
    taskId: "t_alpha_1",
    boardSlug: "alpha",
    onClose: () => {},
    onOpenTask: () => {},
    onRefresh: () => {},
    renderMarkdown: true,
    allTasks: [],
    assignees: [],
    eventTick: 0,
  });
  drawer4.unmount();
  pendingHomeUnmounted.resolve({
    home_channels: [{ platform: "slack", channel_id: "C_UNMOUNTED", name: "after-unmount", subscribed: true }],
  });
  await h.flushMicrotasks();
  assert.strictEqual(
    drawer4.inspect().detailNode.props.homeChannels.length,
    0,
    "Late /home-channels response after TaskDrawer unmount must be ignored"
  );
}

async function scenarioInitialBoardIdentityPromotion(bundlePath) {
  const h = createHarness(bundlePath, null);
  const slowInitialBoard = createDeferred();

  h.onFetch(
    (url) => url.includes("/api/plugins/kanban/boards"),
    () => ({
      current: "alpha",
      boards: [
        { slug: "default", name: "Default", is_current: false },
        { slug: "alpha", name: "Alpha", is_current: true },
        { slug: "beta", name: "Beta", is_current: false },
      ],
    })
  );
  h.onFetch(
    (url) => url.includes("/api/plugins/kanban/board") && !url.includes("/boards"),
    (url) => {
      if (!url.includes("board=")) return slowInitialBoard.promise;
      if (url.includes("board=alpha")) {
        return Object.assign(
          { board: "alpha" },
          makeBoardPayload({ todo: [{ id: "t_alpha_1", title: "Alpha Card", status: "todo" }] }, 10)
        );
      }
      throw new Error("Unexpected board read: " + url);
    }
  );

  await h.mount();
  slowInitialBoard.resolve(
    Object.assign(
      { board: "beta" },
      makeBoardPayload({ todo: [{ id: "t_beta_1", title: "Beta Card", status: "todo" }] }, 99)
    )
  );
  await h.flushMicrotasks();

  const view = h.inspectView();
  assert.strictEqual(
    view.switcherNode && view.switcherNode.props.board,
    "alpha",
    "Selected board should resolve to alpha from /boards"
  );
  assert.ok(
    view.boardFetchCalls.some((c) => c.url.includes("board=alpha")),
    "Promoting null -> alpha must trigger or ensure an explicit ?board=alpha read"
  );
  assert.deepStrictEqual(
    view.allCards.map((c) => c.id),
    ["t_alpha_1"],
    "Anonymous response for beta must not populate alpha"
  );
}

async function scenarioTaskDrawerEffectRemount(bundlePath) {
  const h = createHarness(bundlePath, "alpha");
  let detailReads = 0;
  let homeReads = 0;
  const detailDeferreds = [];
  const homeDeferreds = [];
  let interceptDrawerReads = false;

  h.onFetch(
    (url) => url.includes("/api/plugins/kanban/board") && !url.includes("/boards"),
    () =>
      Object.assign(
        { board: "alpha" },
        makeBoardPayload({ todo: [{ id: "t_alpha_1", title: "Alpha", status: "todo" }] }, 10)
      )
  );
  h.onFetch(
    (url) => url.includes("/api/plugins/kanban/tasks/t_alpha_1"),
    () => {
      if (!interceptDrawerReads) {
        return {
          task: { id: "t_alpha_1", title: "Alpha", status: "todo" },
          comments: [],
          events: [],
          attachments: [],
          links: { parents: [], children: [] },
          runs: [],
        };
      }
      detailReads += 1;
      const d = createDeferred();
      detailDeferreds.push(d);
      return d.promise;
    }
  );
  h.onFetch(
    (url) => url.includes("/api/plugins/kanban/home-channels"),
    () => {
      if (!interceptDrawerReads) {
        return { home_channels: [] };
      }
      homeReads += 1;
      const d = createDeferred();
      homeDeferreds.push(d);
      return d.promise;
    }
  );

  await h.mount();
  let view = h.inspectView();
  view.columnsNode.props.onOpen("t_alpha_1");
  await h.flushMicrotasks();
  view = h.inspectView();
  const TaskDrawerComponent = view.drawerNode.type;
  view.drawerNode.props.onClose();
  await h.flushMicrotasks();

  interceptDrawerReads = true;
  const drawer = await h.mountComponent(TaskDrawerComponent, {
    taskId: "t_alpha_1",
    boardSlug: "alpha",
    onClose: () => {},
    onOpenTask: () => {},
    onRefresh: () => {},
    renderMarkdown: true,
    allTasks: [],
    assignees: [],
    eventTick: 0,
  });

  assert.strictEqual(detailReads, 1, "Initial mount must start a task-detail read");
  assert.strictEqual(homeReads, 1, "Initial mount must start a home-channels read");

  await drawer.replayEffects();

  assert.strictEqual(detailReads, 2, "Effect remount must start a replacement task-detail read");
  assert.strictEqual(homeReads, 2, "Effect remount must start a replacement home-channels read");

  detailDeferreds[0].resolve({
    task: { id: "t_alpha_1", title: "Stale detail", status: "todo" },
    comments: [],
    runs: [],
    artifacts: [],
    attachments: [],
    links: { parents: [], children: [] },
  });
  homeDeferreds[0].resolve({ home_channels: [{ platform: "slack", subscribed: false }] });
  detailDeferreds[1].resolve({
    task: { id: "t_alpha_1", title: "Current detail", status: "todo" },
    comments: [],
    runs: [],
    artifacts: [],
    attachments: [],
    links: { parents: [], children: [] },
  });
  homeDeferreds[1].resolve({ home_channels: [{ platform: "slack", subscribed: true }] });
  await h.flushMicrotasks();

  const inspected = drawer.inspect();
  assert.strictEqual(Boolean(inspected.detailNode), true, "Drawer must not stay stuck on Loading after effect remount");
  assert.deepStrictEqual(
    inspected.detailNode.props.homeChannels,
    [{ platform: "slack", subscribed: true }],
    "Replacement home-channels read must populate drawer after effect remount"
  );
  drawer.unmount();
}

const SCENARIOS = {
  single_event_and_dedup: scenarioSingleEventAndDedup,
  disconnect_and_missed_event_recovery: scenarioDisconnectAndMissedEventRecovery,
  failed_read_retains_prior_card: scenarioFailedReadRetainsPriorCard,
  stale_async_work_after_board_change: scenarioStaleAsyncWorkAfterBoardChange,
  task_and_board_identity: scenarioTaskAndBoardIdentity,
  home_channels_event_tick_and_guards: scenarioHomeChannelsEventTickAndGuards,
  initial_board_identity_promotion: scenarioInitialBoardIdentityPromotion,
  task_drawer_effect_remount: scenarioTaskDrawerEffectRemount,
  backend_contract_roundtrip: scenarioBackendContractRoundtrip,
};

async function main() {
  const bundlePath = process.argv[2];
  const scenarioArg = process.argv[3] || "all";
  if (!bundlePath) process.exit(2);
  const names =
    scenarioArg === "all"
      ? [
          "single_event_and_dedup",
          "disconnect_and_missed_event_recovery",
          "failed_read_retains_prior_card",
          "stale_async_work_after_board_change",
          "task_and_board_identity",
          "home_channels_event_tick_and_guards",
          "initial_board_identity_promotion",
          "task_drawer_effect_remount",
        ]
      : [scenarioArg];
  let failed = false;
  for (const name of names) {
    try {
      await SCENARIOS[name](bundlePath);
      console.log(`PASS ${name}`);
    } catch (err) {
      console.error(`FAIL ${name}: ${err && err.stack ? err.message : err}`);
      failed = true;
    }
  }
  if (failed) process.exit(1);
  console.log("ALL_PASS");
}

main();

