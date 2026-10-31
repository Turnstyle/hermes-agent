"""Fleet default (G09): an unset busy_input_mode queues; interrupt only when chosen explicitly."""
import pytest

import gateway.run as gateway_run
from gateway.run import GatewayRunner


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("HERMES_GATEWAY_BUSY_INPUT_MODE", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_BUSY_TEXT_MODE", raising=False)
    return tmp_path


@pytest.mark.parametrize("configured, expected", [
    (None, "queue"), ("bogus", "queue"), ("interrupt", "interrupt"), ("steer", "steer"), ("queue", "queue"),
])
def test_busy_input_mode_defaults_to_queue_and_honors_explicit_modes(home, configured, expected):
    if configured is not None:
        (home / "config.yaml").write_text(f"display:\n  busy_input_mode: {configured}\n", encoding="utf-8")
    assert GatewayRunner._load_busy_input_mode() == expected


def test_routed_profile_scoping_still_layers_on_the_queue_default():
    """A profile that sets nothing inherits the runner's (queue) mode; an explicit profile choice
    stays scoped to that profile (per-profile snapshot contract)."""
    runner = object.__new__(GatewayRunner)
    runner._busy_input_mode, runner._busy_text_mode = "queue", "queue"
    runner._snapshot_profile_busy_modes("quiet", {})
    runner._snapshot_profile_busy_modes("strict", {"display": {"busy_input_mode": "interrupt"}})
    assert runner._busy_input_modes_by_profile == {"quiet": "queue", "strict": "interrupt"}
    assert runner._busy_text_modes_by_profile == {"quiet": "queue", "strict": "interrupt"}
