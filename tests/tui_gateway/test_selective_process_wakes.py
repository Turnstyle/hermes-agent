"""Replay the TUI/Desktop idle raw completion consumer, not just the CLI formatter.

The idle poller (``_notif_handle_event``) applies the same surfacing policy as the post-turn drain:
a subagent-owned raw process event follows the owning profile's
``delegation.surface_child_process_notifications`` opt-in, while parent processes and delegation
results always arrive. Output text never silences a completion: the DM delivery runner's reply,
pending state or any unrecognised output fails open (stays visible).
"""
import json
import queue
import threading
from types import SimpleNamespace

import pytest

from tui_gateway import server
from tools import process_registry_notifications as policy


@pytest.fixture
def desktop(monkeypatch, tmp_path):
    calls = []
    # An accepted turn (the execution gate's True), so ``running`` stays claimed after dispatch.
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **k: (calls.append((a, k)), True)[1])
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_session_owns_notification_event", lambda *a: True)
    monkeypatch.setattr("tools.async_delegation.claim_event_delivery", lambda *a: "claimed")
    monkeypatch.setattr("tools.async_delegation.complete_event_delivery", lambda *a, **k: None)
    session = {"history_lock": threading.RLock(), "running": False, "history": [],
               "agent": None, "profile_home": str(tmp_path)}
    registry = SimpleNamespace(completion_queue=queue.Queue(), is_completion_consumed=lambda _: False)
    return session, registry, calls, tmp_path


@pytest.mark.parametrize("opt_in", [False, True])
@pytest.mark.parametrize("event_type", ["completion", "heartbeat", "watch_match"])
def test_child_raw_queue_obeys_profile_policy(desktop, opt_in, event_type):
    session, registry, calls, home = desktop
    (home / "config.yaml").write_text(
        f"delegation:\n  surface_child_process_notifications: {str(opt_in).lower()}\n")
    evt = {"type": event_type, "session_id": "proc_child", "owner_task_id": "sa-child",
           "task_id": "parent", "exit_code": 0, "command": "test"}
    assert server._notif_handle_event("sid", session, evt, set(), registry, lambda e: "notice", None)
    assert len(calls) == int(opt_in)
    assert session["running"] is opt_in


def test_child_policy_is_read_from_the_owning_profile(desktop, tmp_path):
    """The idle poller judges the opt-in under the session's profile, not the launch profile."""
    session, registry, calls, home = desktop
    (home / "config.yaml").write_text("delegation:\n  surface_child_process_notifications: true\n")
    other = tmp_path / "other-profile"
    other.mkdir()
    (other / "config.yaml").write_text("delegation:\n  surface_child_process_notifications: false\n")
    evt = {"type": "completion", "session_id": "proc_child", "owner_task_id": "sa-child", "exit_code": 0}
    assert server._notif_handle_event("sid", session, evt, set(), registry, lambda e: "notice", None)
    assert len(calls) == 1
    session.update(running=False, profile_home=str(other))
    assert server._notif_handle_event("sid", session, dict(evt, session_id="proc_child2"), set(), registry,
                                      lambda e: "notice", None)
    assert len(calls) == 1 and session["running"] is False


def test_parent_and_delegation_still_arrive(desktop):
    session, registry, calls, _ = desktop
    parent = {"type": "completion", "session_id": "proc_parent", "owner_task_id": "parent", "exit_code": 1}
    assert server._notif_handle_event("sid", session, parent, set(), registry, lambda e: "failed", None)
    assert len(calls) == 1
    session["running"] = False
    delegation = {"type": "async_delegation", "delegation_id": "d1", "session_key": "s", "results": []}
    assert server._notif_handle_event("sid", session, delegation, set(), registry, lambda e: "result", None)
    assert len(calls) == 2
    assert calls[-1][1]["display_kind"] == "async_delegation_complete"


# The surviving DM producer: ``tools/bot_mode_dm._spawn_delivery`` runs the delivery runner as a
# notify_on_complete background process; its stdout is the recipient's reply or the delivery state.
_RUNNER_OUTPUTS = [
    json.dumps({"reply": "Reply from @researcher: done", "status": "settled", "delivery_id": "dm_1"}),
    json.dumps({"status": "queued", "delivery_id": "dm_1",
                "detail": "Delivery remains pending or its outcome is unknown. Do not resend; receipt is retained."}),
    json.dumps({"status": "ambiguous", "delivery_id": "dm_1"}),
    json.dumps({"status": "queued", "message_id": "fm123", "reply_relayed": False, "reply": "queued (fm123)",
                "spool_status": "published", "queue_admission": "enabled"}),  # receipt-shaped: never inferred
    "",
    "Traceback (most recent call last): failed",
]


@pytest.mark.parametrize("output", _RUNNER_OUTPUTS)
def test_unknown_delivery_output_stays_visible(desktop, output):
    """No output text silences a parent-owned completion: the DM runner's reply, its pending/ambiguous
    state and anything unrecognised all reach the sender's completion batch (fail-open)."""
    session, registry, calls, _ = desktop
    evt = {"type": "completion", "session_id": "proc_dm", "owner_task_id": "parent", "task_id": "parent",
           "exit_code": 0, "command": "bot_mode_dm.py --run-delivery", "output": output}
    batch = []
    assert server._notif_handle_event("sid", session, evt, set(), registry, lambda e: "delivery", None, batch)
    assert batch == [(evt, "delivery")]
    assert policy.should_surface_notification(evt)


def test_child_owned_delivery_runner_still_follows_the_child_policy(desktop):
    """A subagent's own DM runner is child process noise like any other: hidden by default."""
    session, registry, calls, home = desktop
    evt = {"type": "completion", "session_id": "proc_child_dm", "owner_task_id": "sa-child", "exit_code": 0,
           "output": _RUNNER_OUTPUTS[0]}
    batch = []
    assert server._notif_handle_event("sid", session, evt, set(), registry, lambda e: "delivery", None, batch)
    assert batch == [] and calls == [] and not session["running"]
    assert registry.completion_queue.empty()  # dropped like the post-turn drain does, never requeued


def test_work_closeout_is_not_raw_child_noise():
    assert policy.should_surface_notification({"type": "async_delegation_work_closeout", "owner_task_id": "sa-child"})
