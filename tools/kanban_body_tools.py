"""Native admitted parent body editing in the existing kanban toolset."""
from tools.kanban_tools_schemas import _schema, _prop
from tools.registry import no_cache_check_fn, registry

KANBAN_EDIT_BODY_SCHEMA = _schema(
    'kanban_edit_body',
    'Atomically replace a nonterminal task body on the live native parent assigned board and tenant. '
    'Requires frozen owner admission and selected kanban capability. Delegated children and terminal/execute_code '
    'descendants are denied. Exact old_body and new_body strings are required, each at most 262144 UTF-8 bytes. '
    'Whitespace is preserved. NULL does not equal empty string. Unchanged replacements are refused. '
    'A stale old_body returns body_conflict without mutation or audit. Done/archived targets are refused. '
    'Only body changes; no lifecycle transition occurs. Success returns the exact committed body.',
    {'task_id': _prop('string', 'Target task id on the assigned board and tenant.'),
     'old_body': _prop('string', 'Exact current body, without normalization.'),
     'new_body': _prop('string', 'Exact replacement body, without normalization.')},
    ['task_id', 'old_body', 'new_body'],
)


@no_cache_check_fn
def _check_native_parent() -> bool:
    from tools.kanban_tools import _native_parent_context
    return _native_parent_context() is not None


def _handle_edit_body(args: dict, **kw) -> str:
    from tools import kanban_tools as kt
    from hermes_cli.kanban_db_body import compare_and_set_body
    import json

    @kt._kanban_handler('kanban_edit_body')
    def edit(args: dict, **kw) -> str:
        kt._reject_delegated_child_mutation('kanban_edit_body')
        parent = kt._native_parent_context()
        kt._check(parent is not None, 'kanban_edit_body requires a live admitted native parent')
        kt._require_parent_board_arg(args)
        kt._check(isinstance(args.get('task_id'), str) and args['task_id'], 'task_id is required')
        kt._check('old_body' in args and 'new_body' in args, 'old_body and new_body are required')
        with kt._board(args.get('board')) as (_, conn):
            kt._require_assigned_board(conn)
            return json.dumps(compare_and_set_body(
                conn, args['task_id'], old_body=args['old_body'], new_body=args['new_body'],
                caller_assignment=parent), ensure_ascii=False)
    return edit(args, **kw)


registry.register(name='kanban_edit_body', toolset='kanban', schema=KANBAN_EDIT_BODY_SCHEMA,
                  handler=_handle_edit_body, check_fn=_check_native_parent, max_result_size_chars=2 * 1024 * 1024)
