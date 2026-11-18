"""Fence worker actions and recovery on the concrete persistence store's sticky failure."""


def require_worker_persistence(agent):
    from agent.runtime_session_store import RuntimeSessionStore
    store = getattr(agent, '_session_db', None)
    if isinstance(store, RuntimeSessionStore):
        store.require_healthy()


def persistence_failure_result(agent, result):
    from agent.runtime_session_store import WorkerPersistenceError
    try:
        require_worker_persistence(agent)
    except WorkerPersistenceError:
        return {**(result if isinstance(result, dict) else {}), 'failed': True, 'completed': False,
                'failure_reason': 'persistence', 'failure_retryable': False,
                'error': 'Worker persistence failed; agent actions cannot continue.'}
    return result
