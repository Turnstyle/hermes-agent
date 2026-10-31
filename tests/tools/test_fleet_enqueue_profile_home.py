"""The node queue helper must not resolve under a profile's sandbox HOME."""
import importlib.util
from pathlib import Path

import pytest

from tools import fleet_message_enqueue


@pytest.mark.parametrize("profile", ["sender", "recipient"])
def test_profile_sandbox_home_uses_node_writer(tmp_path, monkeypatch, profile):
    root = tmp_path / ".hermes"
    home = root / "profiles" / profile
    sandbox = home / "home"
    sandbox.mkdir(parents=True)
    helper = root / "fleet-node-state" / "fleet_ops.py"
    helper.parent.mkdir()
    helper.write_text(
        "def create_file_if_absent(path, collection, set_by):\n"
        "    assert collection == 'fleet_messages_v1'\n"
        "    return 0\n"
        "def get_doc(collection, message_id):\n"
        "    return {'message_id': message_id, 'status': 'delivered'}, 'version'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(sandbox))
    monkeypatch.setenv("HERMES_HOME", str(home))
    spec = importlib.util.spec_from_file_location("isolated_enqueue", fleet_message_enqueue.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.enqueue_busy_dm(sender="sender", recipient="recipient", body="probe")
    assert module._default_reader("existing")["status"] == "delivered"
    # Resolved per call under the canonical root, never under the sandbox HOME.
    assert module._fleet_ops_path() == helper
    assert not (sandbox / ".hermes").exists()


def test_missing_node_helper_is_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_message_enqueue, "_fleet_ops_path", lambda: tmp_path / "absent.py")
    with pytest.raises(fleet_message_enqueue.FleetEnqueueError):
        fleet_message_enqueue.enqueue_busy_dm(sender="sender", recipient="target", body="probe")
