#!/usr/bin/env python3
"""runner_health_probe.py -- read-only card-runner health probe for ONE Hermes node.

Card t_0382ebfc (Turner order 2026-09-25). Stdlib only, so the same file runs on
TurnerBook (local) and on Sheldon / Max / Snowdrop through
`ssh <host> python3 - '<json args>' < runner_health_probe.py`.

Reads, never writes:
  - ~/.hermes/logs/gateway.log (tail): last spawn line, last "dispatcher stuck" warning
  - every Kanban board DB (read-only URI): running / ready counts, last local spawn event
  - ps: the gateway process (pid, age)
  - the install checkout (git): HEAD, branch, ahead/dirty counts, autostash + update-backup
    refs, reflog (which commit the gateway process started on), carried-fix markers

Prints one JSON object on stdout. Exit 0 even when a section fails (the error is recorded).
"""
import datetime as dt
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
HH = os.path.join(HOME, ".hermes")
REPO = os.path.join(HH, "hermes-agent")
LOG = os.path.join(HH, "logs", "gateway.log")
TAIL_BYTES = 6 * 1024 * 1024
DEFAULT_INTERVAL = 60.0
STUCK_WARN_EVERY_S = 300  # gateway re-warns every 5 min while still stuck (kanban_watchers.py)

ARGS = {}
if len(sys.argv) > 1 and sys.argv[1].strip().startswith("b64:"):
    import base64
    try:
        ARGS = json.loads(base64.b64decode(sys.argv[1].strip()[4:]).decode("utf-8"))
    except ValueError:
        ARGS = {}
elif len(sys.argv) > 1 and sys.argv[1].strip().startswith("{"):
    try:
        ARGS = json.loads(sys.argv[1])
    except ValueError:
        ARGS = {}
NOW = float(ARGS.get("now") or time.time())


def iso(ts):
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sh(args, cwd=None, timeout=20):
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip()
    except Exception as e:  # noqa: BLE001
        return 99, f"{type(e).__name__}: {e}"


def local_ts(s):
    """'2026-09-25 17:43:29' in the node's local time -> epoch seconds."""
    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))


# ---------------- config ----------------
def dispatch_interval():
    try:
        txt = open(os.path.join(HH, "config.yaml"), encoding="utf-8", errors="replace").read()
    except OSError:
        return DEFAULT_INTERVAL
    m = re.search(r"^kanban:\s*\n((?:[ \t]+.*\n|\s*\n)*)", txt, re.M)
    if m:
        m2 = re.search(r"^\s+dispatch_interval_seconds:\s*([0-9.]+)", m.group(1), re.M)
        if m2:
            try:
                return float(m2.group(1)) or DEFAULT_INTERVAL
            except ValueError:
                pass
    return DEFAULT_INTERVAL


# ---------------- gateway process ----------------
def _etime_s(s):
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = [int(p) for p in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts
    return days * 86400 + h * 3600 + m * 60 + sec


def gateway_proc():
    rc, out = sh(["ps", "-axo", "pid=,ppid=,etime=,command="])
    if rc != 0:
        return {"error": out[:200]}
    cands = []
    for line in out.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        pid, ppid, etime, cmd = parts
        if "gateway run" not in cmd:
            continue
        if any(x in cmd for x in ("stderr_timestamp", "osascript", "grep ", "ssh ", "runner_health")):
            continue
        if "python" not in cmd and "hermes" not in cmd:
            continue
        try:
            age = _etime_s(etime)
        except ValueError:
            continue
        cands.append({"pid": int(pid), "ppid": int(ppid), "age_s": age})
    if not cands:
        return {"running": False}
    pids = {c["pid"] for c in cands}
    # The real gateway is the leaf: not the parent of another candidate.
    leaves = [c for c in cands if c["pid"] not in {x["ppid"] for x in cands}] or cands
    g = min(leaves, key=lambda c: c["age_s"])
    return {"running": True, "pid": g["pid"], "started_at": iso(NOW - g["age_s"]),
            "started_ts": NOW - g["age_s"], "candidates": sorted(pids)}


# ---------------- gateway log ----------------
SPAWN_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ \w+ gateway\.run: kanban dispatcher \[([^\]]+)\]: spawned=(\d+)")
STUCK_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ WARNING gateway\.run: kanban dispatcher stuck: ready queue non-empty for (\d+) consecutive ticks")
EMBED_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ \w+ gateway\.run: kanban dispatcher: embedded in gateway")
TICKFAIL_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ ERROR gateway\.run: kanban dispatcher: tick failed on board (\S+)")
CLAIMREF_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ WARNING gateway\.run: kanban dispatcher \[([^\]]+)\]: (\d+) claim refusal")
RECLAIMREF_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ WARNING gateway\.run: kanban dispatcher \[([^\]]+)\]: (\d+) reclaim refusal")


def scan_log(path=LOG):
    out = {"log_path": path}
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
                f.readline()
            data = f.read().decode("utf-8", "replace")
    except OSError as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    last_spawn = last_tick = last_stuck = last_embed = last_tickfail = None
    stuck_n = None
    claim_ref = reclaim_ref = None
    refused_ids, refused_ts = set(), None
    for line in data.splitlines():
        if "kanban dispatcher" not in line:
            continue
        m = SPAWN_RE.match(line)
        if m:
            ts = local_ts(m.group(1))
            last_tick = ts
            if int(m.group(3)) > 0:
                last_spawn = (ts, int(m.group(3)), m.group(2))
            continue
        m = STUCK_RE.match(line)
        if m:
            last_stuck, stuck_n = local_ts(m.group(1)), int(m.group(2))
            continue
        m = EMBED_RE.match(line)
        if m:
            last_embed = local_ts(m.group(1))
            continue
        m = TICKFAIL_RE.match(line)
        if m:
            last_tickfail = local_ts(m.group(1))
            continue
        m = CLAIMREF_RE.match(line)
        if m:
            claim_ref = (local_ts(m.group(1)), int(m.group(3)))
            if refused_ts != claim_ref[0]:
                refused_ids, refused_ts = set(), claim_ref[0]
            refused_ids.update(re.findall(r"(t_[0-9A-Za-z_]+): claim:", line))
            continue
        m = RECLAIMREF_RE.match(line)
        if m:
            reclaim_ref = (local_ts(m.group(1)), int(m.group(3)))
    out.update({
        "last_spawn_at": iso(last_spawn[0]) if last_spawn else None,
        "last_spawn_ts": last_spawn[0] if last_spawn else None,
        "last_spawn_count": last_spawn[1] if last_spawn else None,
        "last_spawn_board": last_spawn[2] if last_spawn else None,
        "last_stuck_warning_at": iso(last_stuck),
        "last_stuck_warning_ts": last_stuck,
        "last_stuck_warning_ticks": stuck_n,
        "dispatcher_started_at": iso(last_embed),
        "dispatcher_started_ts": last_embed,
        "last_tick_failed_at": iso(last_tickfail),
        "last_claim_refusals": claim_ref[1] if claim_ref and NOW - claim_ref[0] < 600 else 0,
        "last_reclaim_refusals": reclaim_ref[1] if reclaim_ref and NOW - reclaim_ref[0] < 600 else 0,
        "tick_failed_recently": bool(last_tickfail and NOW - last_tickfail < 900),
    })
    out["_refused_ids"] = sorted(refused_ids) if claim_ref and NOW - claim_ref[0] < 600 else []
    return out


def stuck_ticks(log, interval, gw_started_ts):
    """Consecutive ticks with a non-empty ready queue and 0 spawns, as of NOW.

    The gateway only logs the count at >= 6 ticks and then every 5 min, so the
    live value is the last logged count plus the ticks elapsed since. A warning
    older than one re-warn period (+ slack) means the streak ended. A spawn or a
    dispatcher (re)start after the warning also ends it.
    """
    ts, n = log.get("last_stuck_warning_ts"), log.get("last_stuck_warning_ticks")
    if ts is None or n is None:
        return 0
    age = NOW - ts
    if age > STUCK_WARN_EVERY_S + 2 * interval:
        return 0
    for later in (log.get("last_spawn_ts"), log.get("dispatcher_started_ts"), gw_started_ts):
        if later and later > ts:
            return 0
    return n + int(age // interval)


# ---------------- boards ----------------
def board_dbs():
    dbs = []
    root = os.path.join(HH, "kanban.db")
    if os.path.exists(root):
        dbs.append(("default", root))
    for p in sorted(glob.glob(os.path.join(HH, "kanban", "boards", "*", "kanban.db"))):
        dbs.append((os.path.basename(os.path.dirname(p)), p))
    return dbs


def kanban_caps():
    try:
        with open(os.path.join(HH, "config.yaml"), encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError:
        return None, None
    m = re.search(r"^kanban:\s*\n((?:[ \t]+.*\n|\s*\n)*)", txt, re.M)
    blk = m.group(1) if m else ""
    def num(key):
        m2 = re.search(rf"^[ \t]+{key}:[ \t]*([0-9]+)", blk, re.M)
        return int(m2.group(1)) if m2 else None
    return num("max_in_progress"), num("max_in_progress_per_profile")


def dispatch_profiles_allowlist():
    try:
        with open(os.path.join(HH, "config.yaml"), encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError:
        return None
    m = re.search(r"^kanban:\s*\n((?:[ \t]+.*\n|\s*\n)*)", txt, re.M)
    blk = m.group(1) if m else ""
    m2 = re.search(r"^[ \t]+dispatch_profiles:[ \t]*(.*)", blk, re.M)
    if not m2:
        return None
    rest = m2.group(1).strip()
    if rest.startswith("[") and rest.endswith("]"):
        items = [s.strip().strip("'\"").casefold() for s in rest[1:-1].split(",") if s.strip()]
        return frozenset(items)
    items = []
    if rest.startswith("-"):
        val = rest[1:].split("#")[0].strip().strip("'\"").casefold()
        if val:
            items.append(val)
    pos = m2.end()
    for line in blk[pos:].splitlines():
        if re.match(r"^[ \t]+-[ \t]+", line):
            val = re.sub(r"^[ \t]+-[ \t]+", "", line).split("#")[0].strip().strip("'\"").casefold()
            if val:
                items.append(val)
        elif line.strip() and not line.startswith("#"):
            break
    return frozenset(items)


def local_profiles():
    names = set()
    try:
        names.update(p for p in os.listdir(os.path.join(HH, "profiles"))
                     if os.path.exists(os.path.join(HH, "profiles", p, "config.yaml")))
    except OSError:
        pass
    allowlist = dispatch_profiles_allowlist()
    if allowlist is not None:
        valid = set(p for p in names if p.casefold() in allowlist)
        if "default" in allowlist:
            valid.add("default")
        return valid
    return set(p for p in names if p.casefold() not in SYNC_EXCLUDED_ASSIGNEES)


# Mirror of hermes_cli.kanban_db_dispatch.is_placeholder_profile / is_dispatch_enabled_profile
SYNC_EXCLUDED_ASSIGNEES = frozenset({"default", "alpha", "beta", "orch"})


def is_dispatch_enabled_profile(assignee, profiles=None):
    """Test whether an assignee is a real profile with dispatch enabled on this node,
    matching hermes_cli.kanban_db_dispatch.is_dispatch_enabled_profile."""
    if not isinstance(assignee, str) or not assignee.strip():
        return False
    canon = assignee.strip().casefold()
    allowlist = dispatch_profiles_allowlist()
    if profiles is None:
        profiles = local_profiles()
    profiles_canon = {p.casefold() for p in profiles}
    if allowlist is not None:
        if canon not in allowlist:
            return False
        return canon == "default" or canon in profiles_canon
    if canon in SYNC_EXCLUDED_ASSIGNEES:
        return False
    return canon in profiles_canon


def sync_leasable(assignee):
    return (isinstance(assignee, str) and bool(assignee) and assignee == assignee.strip()
            and assignee.casefold() not in SYNC_EXCLUDED_ASSIGNEES
            and os.path.basename(assignee) == assignee and assignee not in {".", ".."}
            and os.path.isdir(os.path.join(HH, "profiles", assignee)))


FLEET_TRIGGER_NODE_RE = re.compile(
    r"INSERT\s+INTO\s+fleet_kanban_issue_map\s*\([^)]*\)\s*VALUES\s*\(\s*"
    r"NEW\s*\.\s*id\s*,\s*.+?\s*,\s*NEW\s*\.\s*title\s*,\s*NEW\s*\.\s*body\s*,\s*"
    r"'((?:[^']|'')+)'", re.I | re.S)


def board_node_id(c):
    """This board's installed Fleet node id (same source as kanban_db._fleet_adapter_installed_node_id).
    None = no Fleet adapter (every card is local); "" = trigger present but unparseable (fail closed)."""
    row = c.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='fleet_kanban_task_insert'").fetchone()
    if not row or not row[0]:
        return None
    m = FLEET_TRIGGER_NODE_RE.search(row[0])
    return m.group(1).replace("''", "'") if m else ""


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False


def _cols(c, table):
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}


# t_5373f9f5 (Rescue Artist Bug 4): why an owned ready card is not starting. A "policy" hold is the
# dispatcher doing its job (respawn guard, parent not done, cap, card only just became ready); it
# is never a broken runner, so it never pages. "actionable" = the card could start and did not.
READY_GRACE_S = 600       # a card ready for less than this cannot explain a 10-tick stuck streak
GUARD_EVENT_WINDOW_S = 600
PR_WINDOW_S = 86400       # = kanban_db_dispatch._RESPAWN_GUARD_PR_WINDOW
PR_URL_RE = re.compile(r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+", re.I)  # = _RESPAWN_GUARD_PR_URL_RE
READY_ENTRY_KINDS = ("created", "promoted", "unblocked", "reclaimed", "reclaimed_dead_worker", "crashed",
                     "timed_out", "status", "assigned", "gave_up")
ACTIONABLE = ("spawnable", "lease_refused")
HELD_LIST_MAX = 25

# t_kanban_stall_watch (2026-09-28, Sheldon 18h silent stall): the ownership/leasability filters
# above (sync_leasable, local-profile gate, _owner_is) previously just `continue`d past rows they
# rejected -- correct for "who counts as owned/actionable" but silent for "why is a ready card
# stuck forever." These four buckets classify (never discard) exactly the rows the filters above
# reject, mirroring TRACE-kanban-silent.md's four detection queries (paths 1/2/3/5) so a collector
# can alarm on them directly instead of it looking identical to "no ready work at all."
UNLEASABLE_LIST_MAX = 20


def classify_ready(c, tid, who, *, refused, guard_reason, per_profile, profile_cap, node_cap_full,
                   ev_cols, tasks_cols):
    """One owned ready card -> (bucket, detail dict)."""
    detail = {}
    open_parents = [r[0] for r in c.execute(
        "SELECT l.parent_id FROM task_links l JOIN tasks p ON p.id = l.parent_id "
        "WHERE l.child_id = ? AND p.status NOT IN ('done','archived') LIMIT 5", (tid,))] \
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_links'").fetchone() else []
    if open_parents:
        return "dependency_wait", {"parents": open_parents}
    if guard_reason:
        if guard_reason == "active_pr":
            for (body,) in c.execute("SELECT body FROM task_comments WHERE task_id = ? AND created_at >= ? "
                                     "ORDER BY created_at DESC", (tid, int(NOW) - PR_WINDOW_S)):
                urls = PR_URL_RE.findall(body or "")
                if urls:
                    detail["pr_urls"] = sorted(set(urls))[:3]
                    break
        return "guard:" + guard_reason, detail
    since = None
    if "created_at" in ev_cols:
        since = c.execute("SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND kind IN (%s)"
                          % ",".join("?" * len(READY_ENTRY_KINDS)), (tid, *READY_ENTRY_KINDS)).fetchone()[0]
    if since is None and "created_at" in tasks_cols:
        since = c.execute("SELECT created_at FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    if since is not None:
        detail["ready_for_s"] = int(NOW - since)
        if NOW - since < READY_GRACE_S:
            return "just_ready", detail
    if tid in refused:
        return "lease_refused", detail   # homed here, leasable assignee, still no lease: sync problem
    if profile_cap and per_profile.get(who, 0) >= profile_cap:
        return "profile_cap", detail
    if node_cap_full:
        return "node_cap_full", detail
    return "spawnable", detail


def _owner_is(node_id, mapped, cur_n, src_n):
    """Ownership rule = kanban_db._is_foreign_fleet_mirror: no Fleet adapter or no map row -> local;
    owner = source_node only when current_node IS NULL; blank/unknown owner -> foreign."""
    if node_id is None or not mapped:
        return True
    owner = src_n if cur_n is None else cur_n
    return isinstance(owner, str) and bool(owner.strip()) and owner == node_id


def _guard_reasons(c, ev_cols):
    """Latest respawn_guarded reason per card in the last GUARD_EVENT_WINDOW_S ({"reason": ...})."""
    out = {}
    pay = "payload" if "payload" in ev_cols else "NULL"
    for tid, p in c.execute(f"SELECT task_id, {pay} FROM task_events WHERE kind='respawn_guarded' "
                            "AND created_at > ? ORDER BY created_at", (int(NOW) - GUARD_EVENT_WINDOW_S,)):
        try:
            out[tid] = (json.loads(p) or {}).get("reason") or "unknown"
        except (TypeError, ValueError, AttributeError):
            out[tid] = "unknown"
    return out


def board_counts(refused_ids=()):
    boards, tot = {}, {"running_claimed": 0, "running_rows": 0, "running_live": 0, "running_ghost_here": 0,
                       "ready": 0, "ready_assigned": 0, "ready_owned_here": 0, "ready_spawnable_here": 0,
                       "ready_actionable_here": 0, "ready_default_owned": 0, "ready_absent_profile": 0,
                       "ready_cross_node_home": 0, "claimless_running": 0}
    last_spawn = None
    node_cap, profile_cap = kanban_caps()
    profiles = local_profiles()
    refused = set(refused_ids)
    held, ghosts, buckets = [], [], {}
    # Silent-path buckets (TRACE-kanban-silent.md paths 1/2/3/5): ids only, capped, never discarded.
    unleasable = {"ready_default_owned": [], "ready_absent_profile": [], "ready_cross_node_home": [],
                 "claimless_running": []}
    acks = load_acks()  # Verifier finding 2: read-only, human-owned ack/snooze file
    acked_count = 0
    opened = []
    # Pass 1: running rows on every board (the node cap is node-wide).
    for slug, path in board_dbs():
        b = {}
        boards[slug] = b
        try:
            c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        except Exception as e:  # noqa: BLE001
            b["error"] = f"{type(e).__name__}: {e}"[:200]
            continue
        try:
            q = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
            tcols, ecols = _cols(c, "tasks"), _cols(c, "task_events")
            node_id = board_node_id(c)
            b["fleet_node_id"] = node_id
            has_map = node_id is not None and bool(c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fleet_kanban_issue_map'").fetchone())
            mcols = "m.local_task_id IS NOT NULL, m.current_node, m.source_node" if has_map else "0, NULL, NULL"
            mjoin = " LEFT JOIN fleet_kanban_issue_map m ON m.local_task_id = t.id" if has_map else ""
            b["running_rows"] = q("SELECT COUNT(*) FROM tasks WHERE status='running'")
            b["running_claimed"] = q("SELECT COUNT(*) FROM tasks WHERE status='running' AND "
                                     "(claim_lock IS NOT NULL OR worker_pid IS NOT NULL)")
            b["ready"] = q("SELECT COUNT(*) FROM tasks WHERE status='ready'")
            b["ready_assigned"] = q("SELECT COUNT(*) FROM tasks WHERE status='ready' AND COALESCE(assignee,'') <> ''")
            # Live running = a worker pid alive on this host, or a hand claim (no pid) not yet expired.
            # Ghost = homed here, status running, and neither (Sheldon t_117ad91f: no claim, no pid).
            # Rows homed elsewhere are other nodes' workers: neither live here nor ghosts here.
            ce = "t.claim_expires" if "claim_expires" in tcols else "NULL"
            live, per_profile = 0, {}
            for tid, who, lock, exp, pid, mapped, cur_n, src_n in c.execute(
                    f"SELECT t.id, t.assignee, t.claim_lock, {ce}, t.worker_pid, {mcols} "
                    f"FROM tasks t{mjoin} WHERE t.status='running'").fetchall():
                if not _owner_is(node_id, mapped, cur_n, src_n):
                    continue
                # TRACE path 5: claimless orphan running rows (lock or expiry NULL) are stuck forever
                # behind the lifecycle fence, independent of whether the pid happens to still be
                # alive -- classified here from the raw columns, same predicate as the detection query.
                if lock is None or exp is None:
                    b["claimless_running"] = b.get("claimless_running", 0) + 1
                    if len(unleasable["claimless_running"]) < UNLEASABLE_LIST_MAX:
                        unleasable["claimless_running"].append({"id": tid, "board": slug, "assignee": who})
                if pid is not None and _pid_alive(pid):
                    alive = True
                else:
                    alive = pid is None and lock is not None and (exp is None or exp > NOW)
                if alive:
                    live += 1
                    per_profile[who] = per_profile.get(who, 0) + 1
                else:
                    why = ("worker pid %s gone" % pid if pid is not None else
                           "claim expired" if lock is not None else "no claim, no worker pid")
                    ghosts.append({"id": tid, "board": slug, "assignee": who, "why": why})
                    b["running_ghost_here"] = b.get("running_ghost_here", 0) + 1
            b["running_live"] = live
            owned = []
            for tid, who, mapped, cur_n, src_n in c.execute(
                    f"SELECT t.id, t.assignee, {mcols} FROM tasks t{mjoin} "
                    "WHERE t.status='ready' AND COALESCE(t.assignee,'') <> ''").fetchall():
                if not _owner_is(node_id, mapped, cur_n, src_n):
                    # TRACE path 3: foreign-homed, but the assignee has a local profile dir HERE ->
                    # this node is a candidate for "the assignee's real home", i.e. a cross-node home
                    # mismatch (the card can never lease on its stated home node either). A card
                    # homed elsewhere with an assignee unknown here is ordinary foreign work, not
                    # logged -- only the mismatch shape is worth a bucket.
                    if isinstance(who, str) and is_dispatch_enabled_profile(who, profiles) and who.casefold() not in SYNC_EXCLUDED_ASSIGNEES:
                        if tid in acks or who in acks:  # Verifier finding 2: human-owned ack/snooze
                            acked_count += 1
                        else:
                            b["ready_cross_node_home"] = b.get("ready_cross_node_home", 0) + 1
                            if len(unleasable["ready_cross_node_home"]) < UNLEASABLE_LIST_MAX:
                                unleasable["ready_cross_node_home"].append({"id": tid, "board": slug, "assignee": who})
                    continue
                if node_id is not None:
                    if not sync_leasable(who):  # Fleet board: only assignees the sync can lease
                        # TRACE paths 1 vs 2: split by *why* sync_leasable said no, so an operator
                        # doesn't have to re-derive it -- excluded pool assignee (default/alpha/beta/
                        # orch, t_a68b7e58) vs a real seat name with no profile dir on this node
                        # (t_bc1503d3). A malformed assignee string (path traversal, blank) is rare
                        # enough to fold into absent_profile rather than add a third label.
                        default_pool = isinstance(who, str) and who.casefold() in SYNC_EXCLUDED_ASSIGNEES
                        bucket = "ready_default_owned" if default_pool else "ready_absent_profile"
                        if tid in acks or who in acks:  # Verifier finding 2: human-owned ack/snooze
                            acked_count += 1
                        else:
                            b[bucket] = b.get(bucket, 0) + 1
                            if len(unleasable[bucket]) < UNLEASABLE_LIST_MAX:
                                unleasable[bucket].append({"id": tid, "board": slug, "assignee": who})
                        continue
                elif not is_dispatch_enabled_profile(who, profiles):
                    # No-adapter board: unconfigured placeholder profiles are non-actionable,
                    # bucketed into ready_default_owned rather than treated as local or absent.
                    default_pool = isinstance(who, str) and who.casefold() in SYNC_EXCLUDED_ASSIGNEES
                    bucket = "ready_default_owned" if default_pool else "ready_absent_profile"
                    if tid in acks or who in acks:  # Verifier finding 2: human-owned ack/snooze
                        acked_count += 1
                    else:
                        b[bucket] = b.get(bucket, 0) + 1
                        if len(unleasable[bucket]) < UNLEASABLE_LIST_MAX:
                            unleasable[bucket].append({"id": tid, "board": slug, "assignee": who})
                    continue
                owned.append((tid, who))
            b["ready_owned_here"] = len(owned)
            ls = q("SELECT MAX(created_at) FROM task_events WHERE kind='spawned'")
            b["last_spawn_event_at"] = iso(ls)
            if ls and (last_spawn is None or ls > last_spawn):
                last_spawn = ls
            opened.append((slug, c, b, owned, per_profile, tcols, ecols))
        except Exception as e:  # noqa: BLE001
            b["error"] = f"{type(e).__name__}: {e}"[:200]
            c.close()
    live_total = sum(int(b.get("running_live") or 0) for b in boards.values())
    node_cap_full = bool(node_cap and live_total >= node_cap)
    # Pass 2: why each owned ready card is (not) starting.
    for slug, c, b, owned, per_profile, tcols, ecols in opened:
        try:
            guards = _guard_reasons(c, ecols)
            bk = {}
            for tid, who in owned:
                bucket, detail = classify_ready(
                    c, tid, who, refused=refused, guard_reason=guards.get(tid), per_profile=per_profile,
                    profile_cap=profile_cap, node_cap_full=node_cap_full, ev_cols=ecols, tasks_cols=tcols)
                bk[bucket] = bk.get(bucket, 0) + 1
                buckets[bucket] = buckets.get(bucket, 0) + 1
                if bucket not in ACTIONABLE:
                    held.append({"id": tid, "board": slug, "assignee": who, "reason": bucket, **detail})
            b["ready_buckets"] = bk
            b["ready_actionable_here"] = sum(bk.get(k, 0) for k in ACTIONABLE)
            b["ready_spawnable_here"] = bk.get("spawnable", 0)
        except Exception as e:  # noqa: BLE001
            b["error"] = f"{type(e).__name__}: {e}"[:200]
        finally:
            c.close()
    for b in boards.values():
        for k in tot:
            tot[k] += int(b.get(k) or 0)
    minutes_since_last_spawn = ((NOW - last_spawn) / 60.0) if last_spawn else None
    return {"boards": boards, **tot, "node_cap": node_cap, "profile_cap": profile_cap,
            "fleet_boards": sorted(s for s, b in boards.items() if b.get("fleet_node_id") is not None),
            "node_cap_full": node_cap_full, "ready_buckets": buckets,
            "ready_held_here": held[:HELD_LIST_MAX], "running_ghosts_here": ghosts[:HELD_LIST_MAX],
            "board_errors": sorted(s for s, b in boards.items() if b.get("error")),
            "last_spawn_event_at": iso(last_spawn), "last_spawn_event_ts": last_spawn,
            # Reliable source for "minutes since last spawn": task_events.kind='spawned', already
            # aggregated across every board above (line ~445, MAX(created_at) per board, maxed here
            # across boards). Chosen over gateway.log's SPAWN_RE line because the DB timestamp is
            # UTC epoch straight from SQLite, while the log line is the node's local wall-clock time
            # reparsed by local_ts() -- fine for "how many ticks ago" on the same host, but a second
            # timezone-dependent failure mode a stall alarm should not depend on. dispatcher.
            # last_spawn_ts (scan_log) is kept as a fallback field for the collector only when a
            # board query fails entirely.
            "minutes_since_last_spawn": minutes_since_last_spawn,
            "ready_unleasable_here": unleasable,
            "acked_count": acked_count, "active_acks": sorted(acks.keys())}


# ---------------- install / carried fixes ----------------
def git(*a, timeout=20):
    return sh(["git", *a], cwd=REPO, timeout=timeout)


def install_state(markers, carry_commit, gw_started_ts):
    out = {"path": REPO}
    rc, head = git("rev-parse", "HEAD")
    if rc != 0:
        out["error"] = head[:200]
        return out
    out["commit"] = head
    out["branch"] = git("branch", "--show-current")[1] or "(detached)"
    rc, ahead = git("rev-list", "--count", "origin/main..HEAD")
    out["ahead_of_origin_main"] = int(ahead) if rc == 0 and ahead.isdigit() else None
    rc, cl = git("log", "--format=%h|%s", "-n", "40", "origin/main..HEAD")
    out["carried_commits"] = [dict(zip(("sha", "subject"), (l.split("|", 1) + [""])[:2]))
                              for l in cl.splitlines()] if rc == 0 and cl else []
    for c in out["carried_commits"]:
        c["subject"] = c["subject"][:100]
    rc, st = git("status", "--porcelain", "--untracked-files=no")
    out["dirty_files"] = len([l for l in st.splitlines() if l.strip()]) if rc == 0 else None
    rc, stash = git("stash", "list", "--format=%gd|%ct|%gs")
    out["update_autostashes"] = [
        {"ref": s.split("|", 2)[0], "at": iso(int(s.split("|", 2)[1])), "msg": s.split("|", 2)[2][:120]}
        for s in (stash.splitlines() if rc == 0 else []) if "hermes-update-autostash" in s][:10]
    rc, refs = git("for-each-ref", "refs/hermes-update-backups", "--format=%(refname)|%(objectname:short)")
    out["update_backup_refs"] = sorted(r.split("|")[0].split("/", 2)[-1] for r in refs.splitlines()) if rc == 0 and refs else []
    # Which commit did the running gateway start on? Last reflog entry at/before its start.
    rc, rl = git("reflog", "-n", "80", "--format=%H|%ct|%gs", "HEAD")
    moves = []
    for l in rl.splitlines() if rc == 0 else []:
        h, ct, msg = (l.split("|", 2) + ["", ""])[:3]
        if ct.isdigit():
            moves.append((int(ct), h, msg))
    out["head_moved_at"] = iso(moves[0][0]) if moves else None
    out["head_moved_by"] = moves[0][2][:120] if moves else None
    if gw_started_ts and moves:
        before = [m for m in moves if m[0] <= gw_started_ts]
        out["gateway_code_commit"] = before[0][1] if before else None
        out["gateway_runs_head"] = bool(before) and before[0][1] == head
    # Carried-fix checks.
    missing, present = [], []
    for mk in markers or []:
        f = os.path.join(REPO, mk["file"])
        try:
            ok = mk["pattern"] in open(f, encoding="utf-8", errors="replace").read()
        except OSError:
            ok = False
        (present if ok else missing).append(mk["name"])
    out["carried_markers_present"] = present
    out["carried_markers_missing"] = missing
    # Commits the collector saw carried on an earlier run: still in HEAD's history?
    # Matched by subject, so a rebase (new SHA) or an upstream merge of the same
    # change does not count as a loss; a reset that dropped it does.
    watch = [str(s)[:100] for s in (ARGS.get("watch_subjects") or [])][:40]
    if watch:
        norm = lambda s: re.sub(r"\s*\(#\d+\)\s*$", "", s.strip())[:90]  # noqa: E731  squash-merge suffix
        rc, subj = git("log", "-n", "4000", "--format=%s", "HEAD", timeout=40)
        have = {norm(s) for s in subj.splitlines()} if rc == 0 else None
        out["watched_subjects_missing"] = None if have is None else [s for s in watch if norm(s) not in have]
        # A missing subject that an operator saved under refs/preserve/* or refs/heads/backup/*
        # is most likely an intended rollback (e.g. a checker-FAIL undo), not a loss. Name the
        # ref so the alarm card says "preserved at <ref>" instead of "rescue refs: none".
        # Bounded: 30 newest refs, 300 subjects each, a 25 s total budget (the collector's probe
        # timeout is 150 s), and only when something is missing. Match on the EXACT subject (as
        # watched, first 100 chars), not norm(): norm() drops a "(#123)" suffix, so "fix: x (#123)"
        # and "fix: x (#124)" would collide, and a different commit could "preserve" a real loss.
        # Duplicate exact subjects are all kept. Anything unmatched in budget stays a loss.
        out["watched_subjects_preserved"] = {}
        if out["watched_subjects_missing"]:
            deadline = time.monotonic() + 25
            rc, prefs = git("for-each-ref", "--sort=-committerdate", "--count=30",
                            "--format=%(refname)", "refs/preserve", "refs/heads/backup", timeout=10)
            want = {}
            for s in out["watched_subjects_missing"]:
                want.setdefault(s[:100], []).append(s)
            for ref in (prefs.splitlines() if rc == 0 else []):
                left = deadline - time.monotonic()
                if not want or left < 1:
                    break
                rc2, rs = git("log", "-n", "300", "--format=%s", ref, timeout=min(10, left))
                if rc2 != 0:
                    continue
                for s in rs.splitlines():
                    for orig in want.pop(s.strip()[:100], None) or []:
                        out["watched_subjects_preserved"][orig] = ref.replace("refs/heads/", "", 1)
    else:
        out["watched_subjects_missing"] = []
    if carry_commit:
        rc, _ = git("merge-base", "--is-ancestor", carry_commit, "HEAD")
        out["carry_commit"] = carry_commit
        out["carry_commit_in_head"] = rc == 0
    return out


# ---------------- node-local dead-man sibling heartbeat (t_kanban_stall_watch part 2) ----------
# kanban_stall_watch.py (build/kanban/kanban_stall_watch.py) writes its heartbeat here on every
# --live run. Read-only stat, never opened/parsed as JSON (a stale or corrupt heartbeat file must
# still report an age, not raise) -- mirrors this module's own "read, never write" contract.
STALL_WATCH_DIR = os.path.join(HH, "fleet-node-state", "kanban-stall-watch")
STALL_WATCH_HEARTBEAT = os.path.join(STALL_WATCH_DIR, "heartbeat.json")

# Verdict finding 2 (2026-09-28, HIGH): STALL_WATCH_DIR above is also where the shared ack.json
# (load_acks() below) lives, and a human creates that dir just by following the snooze
# instructions -- with no watcher ever staged. "Installed" evidence must be independent of it: the
# scheduler unit the installers themselves write, which only kanban_stall_watch.py's own install
# script ever creates. Exactly one of the two exists on a given node's OS.
STALL_WATCH_UNIT_PATHS = (
    os.path.join(HOME, "Library", "LaunchAgents", "com.fleet.kanban-stall-watch.plist"),  # mac: install-turnerbook.sh / install-sheldon.sh
    os.path.join(HOME, ".config", "systemd", "user", "kanban-stall-watch.timer"),          # linux: install-max.sh / install-snowdrop.sh
)


def stall_watch_status():
    # "Ever installed" gate (verdict finding 2, 2026-09-28): a node that never had the watcher
    # staged must not alarm just because the heartbeat file is absent -- but the shared
    # kanban-stall-watch/ state dir is NOT sufficient evidence of that (see STALL_WATCH_UNIT_PATHS
    # comment above: it's also where a human-written ack.json lives). Only the scheduler unit the
    # installers write counts. install_evidence names whichever path matched, or None.
    install_evidence = next((p for p in STALL_WATCH_UNIT_PATHS if os.path.exists(p)), None)
    installed = install_evidence is not None
    heartbeat_exists = os.path.exists(STALL_WATCH_HEARTBEAT)
    age = None
    if heartbeat_exists:
        try:
            age = NOW - os.path.getmtime(STALL_WATCH_HEARTBEAT)
        except OSError:
            age = None
    return {
        "installed": installed,
        "install_evidence": install_evidence,
        "heartbeat_path": STALL_WATCH_HEARTBEAT,
        "heartbeat_exists": heartbeat_exists,
        # Exact field name requested by the lead: null when the file is absent (never installed,
        # or installed but not yet run once, or removed) -- collect.py's watch_dead alarm treats a
        # missing heartbeat on an INSTALLED node the same as an infinitely-old one.
        "stall_watch_heartbeat_age_s": age,
    }


# --- Acknowledgement / snooze (Verifier finding 2, 2026-09-28, MAJOR "nag-forever" risk) --------
# Same file, same schema, same read-only contract as kanban_stall_watch.py's own load_acks() (this
# module cannot import that stdlib-standalone script, or vice versa -- see that script's own
# comment on why it must stay a single deployable file -- so the small parsing function is
# intentionally mirrored here, not shared).
ACK_PATH = os.path.join(STALL_WATCH_DIR, "ack.json")


def load_acks():
    """Read-only; {} on a missing/malformed file or any entry with an unparsable/expired
    until_utc -- fails CLOSED (alarm still fires) never open (silent permanent suppression)."""
    try:
        raw = json.loads(open(ACK_PATH, encoding="utf-8").read())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        until_s = entry.get("until_utc")
        try:
            until_ts = dt.datetime.strptime(until_s, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=dt.timezone.utc).timestamp()
        except (TypeError, ValueError):
            continue
        if until_ts > NOW:
            out[key] = entry
    return out


FILES_TOP_PCT = 80.0  # list the top holders only when the table is this full (lsof is slow)
FILES_TOP_N = 5


def _sysctl_int(name):
    rc, out = sh(["/usr/sbin/sysctl", "-n", name], timeout=5)
    try:
        return int(out.split()[0]) if rc == 0 and out else None
    except ValueError:
        return None


def _top_open_file_pids():
    """Top FILES_TOP_N processes by open files. mac: one bounded `lsof -F pcf` pass; linux: /proc."""
    counts, names = {}, {}
    if sys.platform == "darwin":
        rc, out = sh(["lsof", "-n", "-P", "-w", "-F", "pcf"], timeout=45)
        if rc not in (0, 1) or not out:
            return {"error": f"lsof rc={rc}: {out[-120:]}"}
        pid = None
        for line in out.splitlines():
            tag, val = line[:1], line[1:]
            if tag == "p":
                pid = val
            elif tag == "c" and pid:
                names[pid] = val
            elif tag == "f" and pid:
                counts[pid] = counts.get(pid, 0) + 1
    else:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                counts[pid] = len(os.listdir(f"/proc/{pid}/fd"))
                with open(f"/proc/{pid}/comm") as f:
                    names[pid] = f.read().strip()
            except OSError:
                continue
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:FILES_TOP_N]
    return [{"pid": int(p), "files": n, "command": names.get(p, "?")[:60]} for p, n in top]


def file_table():
    """System-wide open-file table (t_1a920c0f, TurnerBook 2026-09-28 13:05-13:18 CDT: Claude.app's
    Virtualization VM held ~160k ~/.hermes handles and every bot hit ENFILE). On macOS the ceiling
    that ran out was kern.maxvnodes (263,168), not kern.maxfiles (491,520), so the page percentage
    is num_files over the SMALLER of the two; num_vnodes itself stays pinned at max by design."""
    if sys.platform == "darwin":
        num = _sysctl_int("kern.num_files")
        maxfiles, maxvnodes = _sysctl_int("kern.maxfiles"), _sysctl_int("kern.maxvnodes")
        lims = [(v, n) for v, n in ((maxvnodes, "kern.maxvnodes"), (maxfiles, "kern.maxfiles")) if v]
        limit, limit_name = min(lims) if lims else (None, None)
        free_vnodes = _sysctl_int("kern.free_vnodes")
    else:
        with open("/proc/sys/fs/file-nr") as f:
            parts = f.read().split()
        num, limit, limit_name, free_vnodes = int(parts[0]), int(parts[2]), "fs.file-max", None
    pct = round(100.0 * num / limit, 1) if num is not None and limit else None
    out = {"open": num, "limit": limit, "limit_name": limit_name, "pct": pct, "free_vnodes": free_vnodes}
    if pct is not None and pct >= FILES_TOP_PCT:
        out["top_pids"] = _top_open_file_pids()
    return out


def hermes_version(known):
    if known and known.get("commit") == ARGS.get("_head"):
        return known.get("version")
    for exe in (os.path.join(REPO, ".hermes", "bin", "hermes"), os.path.join(REPO, "venv", "bin", "hermes")):
        if os.path.exists(exe):
            rc, out = sh([exe, "--version"], timeout=40)
            if rc == 0 and out:
                return out.splitlines()[0][:80]
    return None


def main():
    interval = dispatch_interval()
    rec = {"probe_version": 1, "node_id": ARGS.get("node_id"), "probed_at": iso(NOW),
           "hostname": os.uname().nodename, "dispatch_interval_s": interval}
    try:
        rec["gateway"] = gateway_proc()
    except Exception as e:  # noqa: BLE001
        rec["gateway"] = {"error": f"{type(e).__name__}: {e}"[:200]}
    gw_ts = rec["gateway"].get("started_ts")
    try:
        log = scan_log()
    except Exception as e:  # noqa: BLE001
        log = {"error": f"{type(e).__name__}: {e}"[:200]}
    rec["dispatcher"] = log
    rec["dispatcher"]["stuck_ticks"] = stuck_ticks(log, interval, gw_ts) if "error" not in log else None
    try:
        rec["queue"] = board_counts(log.get("_refused_ids") or ())
    except Exception as e:  # noqa: BLE001
        rec["queue"] = {"error": f"{type(e).__name__}: {e}"[:200]}
    log.pop("_refused_ids", None)
    try:
        rec["install"] = install_state(ARGS.get("markers"), ARGS.get("carry_commit"), gw_ts)
    except Exception as e:  # noqa: BLE001
        rec["install"] = {"error": f"{type(e).__name__}: {e}"[:200]}
    ARGS["_head"] = rec["install"].get("commit")
    try:
        rec["install"]["hermes_version"] = hermes_version(ARGS.get("known_version"))
    except Exception:  # noqa: BLE001
        rec["install"]["hermes_version"] = None
    try:
        rec["stall_watch"] = stall_watch_status()
    except Exception as e:  # noqa: BLE001
        rec["stall_watch"] = {"error": f"{type(e).__name__}: {e}"[:200]}
    try:
        rec["files"] = file_table()
    except Exception as e:  # noqa: BLE001
        rec["files"] = {"error": f"{type(e).__name__}: {e}"[:200]}
    print(json.dumps(rec, sort_keys=True))


if __name__ == "__main__":
    main()
