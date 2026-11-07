"""Supported, shared Board Ops API for CLI and the existing gateway dispatcher.

Authorization, fresh state and receipts share an IMMEDIATE board transaction.
There is no inference client, second task store or model-driven polling here.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_board_ops_store as store
from hermes_cli.kanban_board_ops_actions import apply_action, assert_local_ownership, validate_payload
from hermes_cli.kanban_board_ops_policy import (
    Refused, assert_board_path, canonical, nonblank, recipient_subscription,
    runtime_home, startup_authority, validate_grant,
)
from hermes_cli.kanban_board_ops_transactions import compose_task_transition

ACTION_BOUND_SECONDS = 300
ESCALATION_BOUND_SECONDS = 600
BATCH_LIMIT = 32


def _now() -> int:
    return int(time.time())


class BoardOps:
    """The connection must point at the explicitly named canonical board.

    Public callers use open_board_ops. Gateway passes the same checked board
    connection it already owns; it never supplies a request-selected actor.
    """

    def __init__(self, conn: sqlite3.Connection, board: str):
        assert_board_path(conn, board)
        self.conn, self.board = conn, board

    def _control(self, *, owner=False, executor=False, enabled=True) -> dict:
        actor = runtime_home()
        control = store.control(self.conn, self.board, require_enabled=enabled)
        if owner and actor != control["owner_home"]:
            raise Refused("only the admitted node Conductor may manage control or grants")
        if executor and actor != control["executor_home"]:
            raise Refused("runtime profile is not the admitted deterministic executor")
        return control

    def control(self, *, enabled: bool, human_attribution: str, authority_provenance: str) -> dict:
        actor = runtime_home()
        human = nonblank(human_attribution, "human_attribution")
        provenance = nonblank(authority_provenance, "authority_provenance")
        with kbc.write_txn(self.conn):
            if store.installed(self.conn):
                try:
                    prior = self._control(owner=True, enabled=False)
                except Refused:
                    # An initialized table without an owner row can only occur
                    # before first admission; never reinterpret a disabled row.
                    if self.conn.execute("SELECT 1 FROM kanban_board_ops_control").fetchone():
                        raise
                    prior = None
            else:
                prior = None
            if enabled or prior is None:
                authority = startup_authority(self.board)
                if actor != authority["owner_home"]:
                    raise Refused("startup admission requires the configured node Conductor runtime")
                if prior and any(authority[k] != prior[k] for k in authority):
                    raise Refused("authority replacement requires a separate owner migration")
            else:
                authority = {k: v for k, v in prior.items() if k not in {"revision", "enabled", "cursor"}}
            authority.update(human_attribution=human, authority_provenance=provenance, admitted_at=_now())
            store.initialize(self.conn)
            self.conn.execute(
                "INSERT INTO kanban_board_ops_control(singleton,contract,enabled,revision) VALUES(1,?,?,1) "
                "ON CONFLICT(singleton) DO UPDATE SET contract=excluded.contract,enabled=excluded.enabled,revision=revision+1",
                (canonical(authority), int(enabled)),
            )
            # Revocation stays durable across disable/re-enable and rollback.
            if not enabled:
                self.conn.execute("UPDATE kanban_board_ops_grants SET revoked_at=COALESCE(revoked_at,?)", (_now(),))
            return store.control(self.conn, self.board, require_enabled=False)

    def grant(self, contract: dict) -> dict:
        with kbc.write_txn(self.conn):
            control = self._control(owner=True)
            data = validate_grant(contract, control, _now())
            for task_id in data["task_ids"]:
                snapshot = store.state_snapshot(self.conn, task_id)
                if snapshot["task"]["tenant"] != data["tenant"]:
                    raise Refused("grant tenant differs from a named task")
                assert_local_ownership(self.conn, snapshot["task"], control["node_id"], _now())
                recipient_subscription(self.conn, task_id, data["escalation_recipient"])
            if self.conn.execute("SELECT 1 FROM kanban_board_ops_grants WHERE id=?", (data["grant_id"],)).fetchone():
                raise Refused("grant id already issued; immutable grants cannot be extended or replaced")
            self.conn.execute(
                "INSERT INTO kanban_board_ops_grants(id,contract,digest) VALUES(?,?,?)",
                (data["grant_id"], canonical(data), store.digest(data)),
            )
            for task_id in data["task_ids"]:
                kb._append_event(self.conn, task_id, "board_ops_granted", {"grant_id": data["grant_id"], "digest": store.digest(data), "owner": control["owner_profile"], "human_attribution": data["human_attribution"], "authority_provenance": data["authority_provenance"]})
            return store.grant(self.conn, data["grant_id"])

    def revoke(self, grant_id: str) -> dict:
        with kbc.write_txn(self.conn):
            self._control(owner=True, enabled=False)
            data = store.grant(self.conn, grant_id)
            self.conn.execute("UPDATE kanban_board_ops_grants SET revoked_at=COALESCE(revoked_at,?) WHERE id=?", (_now(), grant_id))
            for task_id in data["task_ids"]:
                kb._append_event(self.conn, task_id, "board_ops_revoked", {"grant_id": grant_id, "revoked_by_home": runtime_home()})
            return store.grant(self.conn, grant_id)

    def _authorize_request(self, data: dict, control: dict, *, admission=False) -> dict:
        grant = store.grant(self.conn, data["grant_id"])
        now = _now()
        if grant["revoked_at"] is not None or not grant["issued_at"] <= now < grant["expires_at"]:
            raise Refused("grant revoked or expired")
        if data["node_id"] != control["node_id"] or data["board"] != self.board or data["node_id"] != grant["node_id"] or data["board"] != grant["board"]:
            raise Refused("request node/board outside grant")
        if data["task_id"] not in grant["task_ids"] or data["tenant"] != grant["tenant"]:
            raise Refused("request task/tenant outside grant")
        if data["operation"] not in grant["allowed_operations"]:
            raise Refused("operation outside grant")
        if data["actor_home"] != grant["operator_home"] or (admission and runtime_home() != grant["operator_home"]):
            raise Refused("runtime actor is not the named operator")
        if not admission and (data["grant_digest"] != grant["digest"] or data["control_revision"] != control["revision"]):
            raise Refused("grant content or control revision changed")
        return grant

    def request(self, data: dict) -> dict:
        required = {"correlation", "grant_id", "node_id", "board", "task_id", "tenant", "operation", "task_sha256", "event_id", "payload"}
        if not isinstance(data, dict) or set(data) != required:
            raise Refused("request fields must match documented contract; actor fields are forbidden")
        data = dict(data)
        for key in ("correlation", "grant_id", "node_id", "board", "task_id", "operation"):
            nonblank(data[key], key)
        if len(data["correlation"]) > 128 or type(data["event_id"]) is not int:
            raise Refused("invalid correlation or event id")
        if not isinstance(data["task_sha256"], str) or len(data["task_sha256"]) != 64 or any(c not in "0123456789abcdef" for c in data["task_sha256"]):
            raise Refused("task_sha256 must be the exact lowercase reviewed digest")
        validate_payload(data["operation"], data["payload"])
        data["actor_home"] = runtime_home()
        with kbc.write_txn(self.conn):
            control = self._control()
            grant = self._authorize_request(data, control, admission=True)
            duplicate = self.conn.execute("SELECT request FROM kanban_board_ops_requests WHERE correlation=?", (data["correlation"],)).fetchone()
            if duplicate:
                prior = json.loads(duplicate["request"])
                if any(prior[k] != v for k, v in data.items()):
                    raise Refused("correlation conflict: key already binds a different request")
                return store.receipt(self.conn, data["correlation"])
            snapshot = store.state_snapshot(self.conn, data["task_id"])
            task = snapshot["task"]
            assert_local_ownership(self.conn, task, control["node_id"], _now())
            if task["tenant"] != data["tenant"] or kb.compute_task_sha256(task["title"], task["body"]) != data["task_sha256"]:
                raise Refused("task tenant or exact text digest differs")
            event = self.conn.execute("SELECT created_at FROM task_events WHERE id=? AND task_id=? AND kind NOT LIKE 'board_ops_%'", (data["event_id"], task["id"])).fetchone()
            if event is None or type(event["created_at"]) is not int or not 0 <= event["created_at"] <= _now():
                raise Refused("original durable task event unavailable or timestamp invalid")
            recipient_subscription(self.conn, task["id"], grant["escalation_recipient"])
            data.update(grant_digest=grant["digest"], control_revision=control["revision"], admitted_at=_now())
            self.conn.execute(
                "INSERT INTO kanban_board_ops_requests(correlation,task_id,grant_id,request,snapshot,original_at) VALUES(?,?,?,?,?,?)",
                (data["correlation"], task["id"], data["grant_id"], canonical(data), canonical(snapshot), event["created_at"]),
            )
            kb._append_event(self.conn, task["id"], "board_ops_requested", {"correlation": data["correlation"], "operation": data["operation"], "actor_home": data["actor_home"], "event_id": data["event_id"]})
            return store.receipt(self.conn, data["correlation"])

    def list(self) -> dict:
        control = self._control(enabled=False)
        actor = runtime_home()
        grants = [store.grant(self.conn, r[0]) for r in self.conn.execute("SELECT id FROM kanban_board_ops_grants ORDER BY id")]
        if actor not in {control["owner_home"], control["executor_home"]}:
            grants = [g for g in grants if g["operator_home"] == actor]
        allowed_ids = {g["grant_id"] for g in grants}
        requests = [store.receipt(self.conn, r["correlation"]) for r in self.conn.execute("SELECT correlation,grant_id FROM kanban_board_ops_requests ORDER BY id") if r["grant_id"] in allowed_ids]
        return {"control": control, "grants": grants, "requests": requests}

    def receipt(self, correlation: str) -> dict:
        for row in self.list()["requests"]:
            if row["correlation"] == correlation:
                return row
        raise Refused("receipt not found in runtime actor scope")

    def _finish(self, row, state: str, facts: dict) -> dict:
        request = json.loads(row["request"])
        receipt = {"correlation": row["correlation"], "task_id": row["task_id"], "operation": request["operation"],
                   "actor_home": request["actor_home"], "grant_id": row["grant_id"], "grant_digest": request["grant_digest"],
                   "original_event_at": row["original_at"], "processed_at": _now(), "facts": facts,
                   "state": state, "execution_attempts": 1, "inference_calls": 0}
        due = _now() if state == "exception" else None
        self.conn.execute("UPDATE kanban_board_ops_requests SET state=?,receipt=?,due_at=? WHERE id=? AND state='pending'", (state, canonical(receipt), due, row["id"]))
        self.conn.execute("UPDATE kanban_board_ops_control SET cursor=MAX(cursor,?) WHERE singleton=1", (row["id"],))
        kb._append_event(self.conn, row["task_id"], "board_ops_receipt", receipt)
        return receipt

    def process_pending(self) -> list[dict]:
        # Healthy unchanged boards do not acquire a write lock or emit events.
        if not store.installed(self.conn):
            return []
        self._control(executor=True, enabled=False)
        ids = [r[0] for r in self.conn.execute("SELECT id FROM kanban_board_ops_requests WHERE state='pending' ORDER BY original_at,id LIMIT ?", (BATCH_LIMIT,))]
        receipts = []
        for request_id in ids:
            with kbc.write_txn(self.conn):
                row = self.conn.execute("SELECT * FROM kanban_board_ops_requests WHERE id=? AND state='pending'", (request_id,)).fetchone()
                if row is None:
                    continue
                try:
                    control = self._control(executor=True)
                    request = json.loads(row["request"])
                    grant = self._authorize_request(request, control)
                    snapshot = store.state_snapshot(self.conn, row["task_id"])
                    if canonical(snapshot) != row["snapshot"]:
                        raise Refused("task state/run/dependencies changed after admission")
                    task = snapshot["task"]
                    assert_local_ownership(self.conn, task, control["node_id"], _now())
                    if task["tenant"] != grant["tenant"] or kb.compute_task_sha256(task["title"], task["body"]) != request["task_sha256"]:
                        raise Refused("fresh task tenant or text digest differs")
                    recipient_subscription(self.conn, task["id"], grant["escalation_recipient"])
                    from agent.estop import is_engaged
                    if is_engaged():
                        raise Refused("ESTOP is engaged; no task action admitted")
                    if _now() - row["original_at"] >= ACTION_BOUND_SECONDS:
                        raise Refused("deterministic action deadline elapsed from original event")
                    with compose_task_transition(self.conn, row["task_id"]):
                        # Failed composed work rolls back before a refusal receipt.
                        with kbc.write_txn(self.conn, allow_nested=True):
                            state, facts = apply_action(self.conn, request, grant, task, _now())
                except (Refused, sqlite3.Error, RuntimeError, NameError, OSError) as exc:
                    state, facts = "exception", {"reason": f"{type(exc).__name__}: {exc}", "task_action_committed": False}
                receipts.append(self._finish(row, state, facts))
        return receipts

    def claim_escalations(self) -> list[dict]:
        if not store.installed(self.conn):
            return []
        self._control(executor=True, enabled=False)
        rows = self.conn.execute(
            "SELECT id FROM kanban_board_ops_requests WHERE (state='exception' AND due_at<=?) OR state='delivering' ORDER BY original_at,id LIMIT ?",
            (_now(), BATCH_LIMIT),
        ).fetchall()
        deliveries = []
        for item in rows:
            with kbc.write_txn(self.conn):
                self._control(executor=True, enabled=False)
                row = self.conn.execute("SELECT * FROM kanban_board_ops_requests WHERE id=?", (item["id"],)).fetchone()
                if row["state"] == "delivering":
                    # Admission is at-most-once. A crash between send and readback
                    # is not retried and is never reported as delivery success.
                    if row["delivery_claimed_at"] <= _now() - 30:
                        self._delivery_result(row, "delivery_unknown", {"reason": "executor interrupted during the single delivery attempt"})
                    continue
                if row["state"] != "exception":
                    continue
                try:
                    grant = store.grant(self.conn, row["grant_id"])
                    sub = recipient_subscription(self.conn, row["task_id"], grant["escalation_recipient"])
                except Refused as exc:
                    self._delivery_result(row, "recipient_unavailable", {"reason": str(exc)})
                    continue
                self.conn.execute("UPDATE kanban_board_ops_requests SET state='delivering',delivery_claimed_at=? WHERE id=? AND state='exception'", (_now(), row["id"]))
                deliveries.append({"correlation": row["correlation"], "sub": sub, "receipt": json.loads(row["receipt"]), "board": self.board})
        return deliveries

    def _delivery_result(self, row, state: str, facts: dict) -> None:
        facts.update(original_event_at=row["original_at"], recorded_at=_now(),
                     deadline_met=_now() <= row["original_at"] + ESCALATION_BOUND_SECONDS,
                     automatic_retry=False)
        self.conn.execute("UPDATE kanban_board_ops_requests SET state=?,delivery_result=? WHERE id=?", (state, canonical(facts), row["id"]))
        kb._append_event(self.conn, row["task_id"], "board_ops_escalation", {"correlation": row["correlation"], "state": state, **facts})

    def finish_escalation(self, correlation: str, *, delivered: bool, detail: str) -> dict:
        with kbc.write_txn(self.conn):
            self._control(executor=True, enabled=False)
            row = self.conn.execute("SELECT * FROM kanban_board_ops_requests WHERE correlation=?", (correlation,)).fetchone()
            if row is None:
                raise Refused("correlation not found")
            if row["state"] == "delivering":
                self._delivery_result(row, "notified" if delivered else "delivery_failed", {"detail": detail, "delivered": delivered})
            return store.receipt(self.conn, correlation)


@contextmanager
def open_board_ops(board: str):
    # Refuse workers before connect can initialize any schema. BoardOps does
    # not clear lineage markers or follow a worker's pinned database override.
    runtime_home()
    path = kb.kanban_home() / "kanban.db" if board == "default" else kb.board_dir(board) / "kanban.db"
    if not path.exists():
        raise Refused("owner must initialize the canonical board first")
    with kbc.connect_closing(db_path=path, board=board) as conn:
        conn.execute("PRAGMA busy_timeout=2000")
        yield BoardOps(conn, board)
