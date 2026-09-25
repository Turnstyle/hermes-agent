"""``/proc/<pid>/stat`` parsing in gateway.status survives spaces and ``)`` in the process name.

Field 2 (comm) is free text: npm titles itself ``npm exec @playwright/mcp`` and Linux copies that
into comm, so a plain whitespace split shifted every later field (t_590dc20c review). Both readers
split after the LAST ``)`` instead.
"""

import pytest

import gateway.status as gs

_AFTER_COMM = ["S"] + [str(i) for i in range(4, 22)] + ["987654"] + ["0"] * 30


def _fake_stat(monkeypatch, line: str) -> None:
    class FakePath:
        def __init__(self, p):
            self.p = p

        def read_text(self, encoding=None):
            return line

    monkeypatch.setattr(gs, "Path", FakePath)


@pytest.mark.parametrize("comm", ["hermes", "npm exec @playw", "a) b", "(x y)"])
def test_start_time_is_field_22_whatever_the_process_name(monkeypatch, comm):
    _fake_stat(monkeypatch, f"4242 ({comm}) " + " ".join(_AFTER_COMM) + "\n")
    assert gs._get_process_start_time(4242) == 987654


@pytest.mark.parametrize("comm", ["node", "npm exec @playw", "a) Z b"])
def test_zombie_state_is_field_3_whatever_the_process_name(monkeypatch, comm):
    _fake_stat(monkeypatch, f"4242 ({comm}) Z " + " ".join(_AFTER_COMM[1:]) + "\n")
    assert gs._posix_is_zombie(4242) is True
    _fake_stat(monkeypatch, f"4242 ({comm}) S " + " ".join(_AFTER_COMM[1:]) + "\n")
    assert gs._posix_is_zombie(4242) is False
