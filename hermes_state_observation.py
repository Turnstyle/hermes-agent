"""Private observation fence for callbacks running while the owner writer is locked."""
from contextlib import contextmanager
from contextvars import ContextVar

_observed = ContextVar('observed_session_db', default=())


def deny_observation_write(db):
    if db in _observed.get():
        from hermes_state_runtime import RuntimeStoreError
        raise RuntimeStoreError('permission_denied')


@contextmanager
def observation_scope(db):
    deny_observation_write(db)
    token = _observed.set((*_observed.get(), db))
    try:
        yield
    finally:
        _observed.reset(token)


_boards = ContextVar('observed_board_paths', default=())


def deny_observation_board_write(path):
    if str(path) in _boards.get():
        from hermes_state_runtime import RuntimeStoreError
        raise RuntimeStoreError('permission_denied')


@contextmanager
def board_observation_scope(path):
    deny_observation_board_write(path)
    token = _boards.set((*_boards.get(), str(path)))
    try:
        yield
    finally:
        _boards.reset(token)
