"""Connection-local composition for Board Ops' database-only transitions.

Only the reviewed keep-spec and comment paths are composed. Transitions with
post-commit network, process or workspace effects must not use this context.
The ordinary write fence remains in write_txn, including for nested savepoints.
"""

from contextlib import contextmanager
from contextvars import ContextVar

_scope = ContextVar("kanban_board_ops_transaction_scope", default=None)


def composes_connection(conn) -> bool:
    scope = _scope.get()
    return scope is not None and scope[0] is conn


def promotion_in_scope(conn, task_id: str) -> bool:
    scope = _scope.get()
    return scope is None or scope[0] is not conn or scope[1] == task_id


@contextmanager
def compose_task_transition(conn, task_id: str):
    if not conn.in_transaction:
        raise RuntimeError("Board Ops composition requires an outer checked write transaction")
    token = _scope.set((conn, task_id))
    try:
        yield
    finally:
        _scope.reset(token)
