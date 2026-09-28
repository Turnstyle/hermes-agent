"""Turner 2026-09-28 invariant: with nothing configured, a mid-turn message queues; it never kills the turn."""
from gateway.run import GatewayRunner


def test_busy_input_mode_defaults_to_queue(monkeypatch):
    monkeypatch.delenv("HERMES_GATEWAY_BUSY_INPUT_MODE", raising=False)
    monkeypatch.setattr(GatewayRunner, "_env_or_cfg_str", classmethod(lambda cls, *a, **k: ""))
    assert GatewayRunner._load_busy_input_mode() == "queue"
    assert GatewayRunner._busy_input_mode == "queue"


def test_explicit_interrupt_still_honored(monkeypatch):
    monkeypatch.setattr(GatewayRunner, "_env_or_cfg_str", classmethod(lambda cls, *a, **k: "interrupt"))
    assert GatewayRunner._load_busy_input_mode() == "interrupt"
