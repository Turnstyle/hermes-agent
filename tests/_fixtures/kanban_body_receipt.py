"""Optional observer for replayable native body-handler source receipts.

Load with ``-p tests._fixtures.kanban_body_receipt -o junit_logging=all``.
The observer records real dispatch results and independent DB readbacks; it
neither changes admission nor replaces the handler.
"""
import json
from dataclasses import asdict


import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    assignment = item.funcargs.get('parent_assignment')
    if not assignment:
        yield
        return

    from hermes_cli import kanban_db as kb
    from tools.registry import registry

    conn = assignment[0]
    dispatch = registry.dispatch

    def observed(name, args, **kwargs):
        if name != 'kanban_edit_body':
            return dispatch(name, args, **kwargs)
        target = args.get('task_id')
        before = kb.get_task(conn, target)
        events_before = kb.list_events(conn, target)
        result = dispatch(name, args, **kwargs)
        after = kb.get_task(conn, target)
        events_after = kb.list_events(conn, target)
        record = {
            'test': item.nodeid,
            'tool': name,
            'args': args,
            'result': json.loads(result) if isinstance(result, str) else result,
            'before': asdict(before) if before else None,
            'readback': asdict(after) if after else None,
            'events_before': len(events_before),
            'events_after': len(events_after),
        }
        print('NATIVE_BODY_RECEIPT ' + json.dumps(record, ensure_ascii=False))
        return result

    registry.dispatch = observed
    try:
        yield
    finally:
        registry.dispatch = dispatch
