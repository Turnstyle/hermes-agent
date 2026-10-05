"""Finite owner-issued renewal and durable lifecycle intake in the grant ledger.

No task registry or inference loop: descendants come from real creator-linked
creation events. Renewals issue new immutable grants and retain owner provenance.
"""

import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_board_ops_store as store
from hermes_cli.kanban_board_ops_actions import assert_local_ownership
from hermes_cli.kanban_board_ops_policy import (
    GRANT_FIELDS, Refused, canonical, recipient_subscription, runtime_home, validate_grant,
)
from hermes_cli.kanban_board_ops_transactions import compose_task_transition

# Real lifecycle events only. Comments and arbitrary event payloads never select
# an operation or supply executable text. Holds are inspected, never released.
INTAKE_KINDS = frozenset({"blocked", "block_loop_detected", "dependency_wait", "crashed", "timed_out"})


def now():
    from hermes_cli.kanban_board_ops import _now
    return _now()


class StandingOps:
    def _standing_root(self, root_id, control):
        root = store.grant(self.conn, root_id)
        policy = root.get("standing_policy")
        if not policy or root["revoked_at"] is not None or not root["issued_at"] <= now() < policy["renew_until"]:
            raise Refused("standing authority unavailable, revoked or expired")
        if any(root[k] != control[k] for k in ("node_id", "board", "owner_home")) or root["operator_home"] != control["executor_home"]:
            raise Refused("standing authority outside admitted node/board/executor")
        return root

    def _standing_task(self, root, task_id, control):
        task = store.state_snapshot(self.conn, task_id)["task"]
        policy = root["standing_policy"]
        if task["tenant"] != root["tenant"] or task["assignee"] not in policy["assignees"]:
            raise Refused("task tenant or assignee outside standing scope")
        if task_id not in root["task_ids"]:
            event = self.conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='created' ORDER BY id LIMIT 1", (task_id,)).fetchone()
            creator = json.loads(event["payload"] or "{}").get("creator_task_id") if event else None
            if not policy["allow_descendants"] or creator not in root["task_ids"] or event["id"] <= root["standing_after_event_id"]:
                raise Refused("task is not a direct creator-linked descendant of an admitted seed")
        assert_local_ownership(self.conn, task, control["node_id"], now())
        recipient_subscription(self.conn, task_id, root["escalation_recipient"])
        return task

    def renew(self, grant_id, task_ids):
        # This exception to routine grant management is limited to an immutable
        # owner's standing policy. Callers cannot choose rights or expiry.
        with kbc.write_txn(self.conn, allow_nested=True):
            control = self._control(executor=True)
            parent = store.grant(self.conn, grant_id)
            if parent["revoked_at"] is not None:
                raise Refused("renewal parent revoked")
            root = self._standing_root(parent.get("standing_root_id", grant_id), control)
            if runtime_home() != root["operator_home"]:
                raise Refused("renewal runtime is not the admitted operator")
            if not isinstance(task_ids, list) or not task_ids or len(task_ids) > 64 or any(not isinstance(t, str) for t in task_ids) or len(set(task_ids)) != len(task_ids):
                raise Refused("renewal requires explicit unique task IDs")
            for task_id in task_ids:
                self._standing_task(root, task_id, control)
            family = [store.grant(self.conn, r[0]) for r in self.conn.execute("SELECT id FROM kanban_board_ops_grants WHERE root_id=?", (root["grant_id"],))]
            admitted = {task for grant in family for task in grant["task_ids"]}
            if len(admitted | set(task_ids)) > root["standing_policy"]["max_tasks"]:
                raise Refused("standing cumulative task cap exhausted")
            for grant in family:
                if grant["revoked_at"] is None and grant["issued_at"] <= now() < grant["expires_at"] and set(grant["task_ids"]) == set(task_ids):
                    return grant
            data = {key: root[key] for key in GRANT_FIELDS}
            data.update(grant_id=f"{root['grant_id']}:renew:{len(family)}", task_ids=task_ids,
                        expires_at=min(now() + 86400, root["standing_policy"]["renew_until"]))
            data = validate_grant(data, control, now())
            data.update(standing_root_id=root["grant_id"], standing_root_digest=root["digest"])
            self.conn.execute("INSERT INTO kanban_board_ops_grants(id,contract,digest,root_id,intake_cursor) VALUES(?,?,?,?,?)",
                              (data["grant_id"], canonical(data), store.digest(data), root["grant_id"], 0))
            for task_id in task_ids:
                kb._append_event(self.conn, task_id, "board_ops_renewed", {"grant_id": data["grant_id"], "standing_root_id": root["grant_id"],
                    "standing_root_digest": root["digest"], "actor_home": runtime_home(), "human_attribution": root["human_attribution"],
                    "authority_provenance": root["authority_provenance"], "expires_at": data["expires_at"]})
            return store.grant(self.conn, data["grant_id"])

    def intake_events(self):
        if not store.installed(self.conn):
            return []
        control = self._control(executor=True, enabled=False)
        if not control["enabled"]:
            return []
        if "intake_cursor" not in {r[1] for r in self.conn.execute("PRAGMA table_info(kanban_board_ops_grants)")}:
            # Old owner-issued ledgers stay unchanged until supported admission
            # migrates them. No background schema mutation on an unadmitted board.
            return []
        roots = self.conn.execute("SELECT id,intake_cursor FROM kanban_board_ops_grants WHERE root_id IS NULL OR root_id=id ORDER BY intake_cursor,id").fetchall()
        admitted = []
        budget = 32
        for row in roots:
            if budget <= 0:
                break
            try:
                root = store.grant(self.conn, row["id"])
                if root["operator_home"] != runtime_home():
                    continue
                if root.get("standing_policy"):
                    self._standing_root(root["grant_id"], control)
                elif root["revoked_at"] is not None or not root["issued_at"] <= now() < root["expires_at"]:
                    continue
            except Refused:
                continue
            events = self.conn.execute("SELECT id,task_id,kind FROM task_events WHERE id>? ORDER BY id LIMIT ?", (row["intake_cursor"], budget)).fetchall()
            if not events:
                continue
            with kbc.write_txn(self.conn):
                control = self._control(executor=True)
                # Revalidate under the same lock as request/cursor writes.
                root = store.grant(self.conn, row["id"])
                if root.get("standing_policy"):
                    self._standing_root(root["grant_id"], control)
                elif root["revoked_at"] is not None or not root["issued_at"] <= now() < root["expires_at"]:
                    continue
                cursor = self.conn.execute("SELECT intake_cursor FROM kanban_board_ops_grants WHERE id=?", (row["id"],)).fetchone()[0]
                # Requests, new grants and cursor advance share the transaction;
                # after a crash an event is replayed or committed exactly once.
                for event in events:
                    if event["id"] <= cursor:
                        continue
                    if event["kind"] in INTAKE_KINDS and "inspect_wait" in root["allowed_operations"]:
                        try:
                            task = store.state_snapshot(self.conn, event["task_id"])["task"]
                            if task["status"] not in {"blocked", "todo", "triage"}:
                                continue
                            correlation = f"intake:{event['id']}:inspect_wait"
                            duplicate = self.conn.execute("SELECT request FROM kanban_board_ops_requests WHERE correlation=?", (correlation,)).fetchone()
                            if duplicate is not None:
                                prior = json.loads(duplicate[0])
                                if any(prior[k] != v for k, v in {
                                    "task_id": task["id"], "event_id": event["id"], "operation": "inspect_wait",
                                    "actor_home": runtime_home(), "node_id": control["node_id"], "board": self.board,
                                    "tenant": root["tenant"], "task_sha256": kb.compute_task_sha256(task["title"], task["body"]), "payload": {},
                                }.items()):
                                    raise Refused("correlation conflict: intake key binds a different event/operation")
                                continue
                            grant = root
                            with compose_task_transition(self.conn, task["id"]):
                                if task["id"] not in root["task_ids"] or now() >= root["expires_at"]:
                                    if not root.get("standing_policy"):
                                        continue
                                    grant = self.renew(root["grant_id"], [task["id"]])
                                admitted.append(self.request({"correlation": correlation,
                                    "grant_id": grant["grant_id"], "node_id": control["node_id"], "board": self.board,
                                    "task_id": task["id"], "tenant": root["tenant"], "operation": "inspect_wait",
                                    "task_sha256": kb.compute_task_sha256(task["title"], task["body"]), "event_id": event["id"], "payload": {}}))
                        except Refused as exc:
                            # A denied future task is outside actor scope. Audit
                            # the refusal on an admitted seed, never that card.
                            kb._append_event(self.conn, root["task_ids"][0], "board_ops_intake_refused", {"event_id": event["id"], "task_id": event["task_id"], "grant_id": root["grant_id"], "reason": str(exc)})
                self.conn.execute("UPDATE kanban_board_ops_grants SET intake_cursor=MAX(intake_cursor,?) WHERE id=?", (events[-1]["id"], root["grant_id"]))
            budget -= len(events)
        return admitted
