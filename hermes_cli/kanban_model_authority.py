"""Owner-only model-policy mutation using existing node admission and fences.

Runtime home, not a request author or profile display alias, is the actor.
No protected route-authentication exporter exists here. Caller snapshots remain
preview metadata and cannot be installed as authenticated execution policies.
"""
from __future__ import annotations

from hermes_cli.kanban_board_ops_policy import Refused, assert_board_path, runtime_home, profile_home
from hermes_cli.kanban_board_ops_store import control


class SelectionPolicyRefused(ValueError):
    """Fixed refusal codes safe to render without reflecting request metadata."""


def selection_runtime_home():
    try:
        return runtime_home()
    except Refused as exc:
        raise SelectionPolicyRefused('worker_or_delegated_selection_mutation_forbidden') from exc


def authorize_policy_update(conn, task, policy):
    from hermes_cli import kanban_db as kb
    actor_home = selection_runtime_home()
    try:
        board = kb.get_current_board()
        assert_board_path(conn, board)
        admission = control(conn, board, require_enabled=False)
    except Refused as exc:
        raise SelectionPolicyRefused('owner_selection_authority_unavailable') from exc
    if actor_home != admission['owner_home']:
        raise SelectionPolicyRefused('selection_requires_admitted_owner_profile')
    profile_home(task.assignee)
    if policy.get('profile', task.assignee) != task.assignee or policy.get('node', admission['node_id']) != admission['node_id']:
        raise SelectionPolicyRefused('selection_owner_node_or_profile_mismatch')
    # An exact-only refresh must not strip an existing proof/quality boundary.
    if 'snapshot' in policy or 'snapshot' in (task.model_policy or {}):
        raise SelectionPolicyRefused('trusted_route_evidence_unavailable')
    return {
        'actor': admission['owner_profile'], 'actor_home': actor_home,
        'authority': {'source': 'kanban_board_ops_control', 'board': board,
                      'node': admission['node_id'], 'revision': admission['revision']},
        'route_evidence': {'source': 'task_exact_route', 'task_id': task.id,
                           'provider': task.provider_override, 'model': task.model_override,
                           'authenticated': 'unobserved', 'usable': 'unobserved'},
    }
