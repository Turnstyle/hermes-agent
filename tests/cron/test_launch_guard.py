"""G3d: manual/provider fires and queued cron work honor restart admission."""
from pathlib import Path

import pytest

from cron import scheduler as scheduler
from cron.scheduler_provider import InProcessCronScheduler
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    return root


@pytest.mark.parametrize("marker", ["ESTOP", ".drain_request.json", "lock"])
@pytest.mark.parametrize("entry", ["manual", "provider", "claimed", "run_job"])
def test_cron_refuses_before_execution(home, monkeypatch, marker, entry):
    from contextlib import nullcontext
    job = {"id": "guard-probe", "name": "probe"}
    calls = []
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", lambda job: calls.append("launch") or True)
    monkeypatch.setattr(scheduler, "_prepare_job_prompt", lambda *a: (calls.append("script") or (False, "", "", "test"), ""))
    provider = InProcessCronScheduler()
    monkeypatch.setattr(provider, "claim_fire", lambda *a, **k: dict(job))
    if marker != "lock":
        (home / marker).write_text("null")
    guard = kbc._dispatch_tick_lock(kb.kanban_db_path()) if marker == "lock" else nullcontext(True)
    with guard as held:
        assert held
        if entry == "manual":
            result = scheduler.run_one_job(dict(job))
        elif entry == "provider":
            result = provider.fire_due(job["id"])
        elif entry == "claimed":
            result = provider.fire_claimed(dict(job))
        else:
            result = scheduler.run_job(dict(job))[0]
        assert result is False
    assert calls == []


def test_provider_pause_does_not_claim_or_consume_an_occurrence(home, monkeypatch):
    from cron import jobs
    (home / ".drain_request.json").write_text("null")
    monkeypatch.setattr(jobs, "claim_job_for_fire", lambda *a, **k: pytest.fail("claimed while paused"))
    assert InProcessCronScheduler().claim_fire("job", force=True, manual=True) is None


def test_cron_handoff_is_tracked_then_releases_board_lock(home, monkeypatch):
    observed = []
    def handoff(job):
        tracked = job["id"] in scheduler.get_running_job_ids()
        # Card work in this already admitted job must be able to take the board lock.
        with kbc._dispatch_tick_lock(kb.kanban_db_path()) as held:
            observed.append((tracked, held))
        return True
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", handoff)
    assert scheduler.run_one_job({"id": "tracked"})
    assert observed == [(True, True)]
    assert "tracked" not in scheduler.get_running_job_ids()


def test_paused_queued_fire_keeps_occurrence_and_closes_attempt(home, monkeypatch):
    """A pause after queueing must not consume the due occurrence (G3d)."""
    from cron import jobs, executions
    job = jobs.create_job(prompt="local probe", schedule="every 5m")
    attempt = executions.create_execution(job["id"], source="builtin")
    job["execution_id"] = attempt["id"]
    before = jobs.get_job(job["id"])
    (home / ".drain_request.json").write_text("null")
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", lambda job: pytest.fail("launched while paused"))
    assert scheduler._process_due_job(job, None, None, False) is False
    assert jobs.get_job(job["id"]) == before
    assert executions.get_execution(attempt["id"])["status"] == "failed"
    assert job["id"] not in scheduler.get_running_job_ids()


@pytest.mark.parametrize("replace_owner", [False, True])
def test_refused_claimed_fire_closes_attempt_without_touching_new_owner(home, monkeypatch, replace_owner):
    """Refusal after provider claim must close its ledger and respect claim ownership."""
    from cron import jobs, executions
    job = jobs.create_job(prompt="local probe", schedule="every 5m")
    claimed = InProcessCronScheduler().claim_fire(job["id"], manual=True)
    assert claimed is not None
    if replace_owner:
        rows = jobs.load_jobs()
        next(row for row in rows if row["id"] == job["id"])["fire_claim"]["by"] = "new-owner"
        jobs.save_jobs(rows)
    before = jobs.get_job(job["id"])
    (home / "ESTOP").write_text("pause")
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", lambda job: pytest.fail("launched while paused"))
    assert InProcessCronScheduler().fire_claimed(claimed) is False
    after = jobs.get_job(job["id"])
    if replace_owner:
        assert after == before
    else:
        assert not after.get("fire_claim")
        assert after["last_status"] == "error"
    assert executions.get_execution(claimed["execution_id"])["status"] == "failed"


def test_queued_fire_registers_owner_for_scoped_shutdown(home, monkeypatch):
    """The acquired claim replaces the queue's unowned admission token."""
    from cron import jobs, executions
    job = jobs.create_job(prompt="local probe", schedule="every 5m")
    attempt = executions.create_execution(job["id"], source="builtin")
    job["execution_id"] = attempt["id"]
    monkeypatch.setattr(scheduler, "_interrupted_job_ids", set())

    def handoff(claimed):
        owner = claimed["fire_claim"]["by"]
        assert scheduler.mark_running_jobs_interrupted(
            "scratch shutdown probe", only_owners={(claimed["id"], owner)}) == [job["id"]]
        return True

    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", handoff)
    assert scheduler._process_due_job(job, None, None, False)
    current = jobs.get_job(job["id"])
    assert current["last_status"] == "error"
    assert current["last_error"] == "scratch shutdown probe"
    assert job["id"] not in scheduler.get_running_job_ids()
