"""G13/G16: sender-facing queued-versus-answered and relay target-install diagnostics.

The relay reply op still carries ``reason`` (``gateway/session_bot.py:relay_operation``), so the
sender's reply waiter (``bot_mode_dm._wait_reply_main``) is the surviving consumer."""
import json

from hermes_constants import get_hermes_home
from tools import bot_failure_reasons as reasons
from tools import bot_mode_dm, bot_relay


def test_reply_not_relayed_is_reported_as_queued_never_as_the_answer(capsys):
    path = bot_relay.write_reply(get_hermes_home(), "a" * 32, reply="Queued for @scout, not yet answered",
                                 reason=reasons.REPLY_NOT_RELAYED)
    assert bot_mode_dm._wait_reply_main(str(path), "@scout on mini", "1") == 0
    out = capsys.readouterr().out
    assert out.startswith("Queued for @scout on mini, not yet answered") and "do NOT resend" in out
    assert "Reply from" not in out


def test_relay_target_scope_failure_names_the_target_install(capsys):
    root = get_hermes_home()
    base = bot_relay._ensure_dirs(root)
    envelope_id = "b" * 32
    (base / bot_relay.CLAIMED_DIR / f"{envelope_id}.json").write_text(json.dumps(
        {"target_connection": "mini", "target_profile": "scout", "target_handle": "scout"}), encoding="utf-8")
    error = "could not read this profile's secrets (an internal profile-scoping bug)"
    path = bot_relay.write_reply(root, envelope_id, error=error)
    assert bot_mode_dm._wait_reply_main(str(path), "@scout on mini", "1") == 1
    out = capsys.readouterr().out
    assert f"[reason: {reasons.TARGET_SCOPE_UNRESOLVED}]" in out
    assert "failed on THAT install" in out and "'mini'" in out and "Restarting this gateway cannot fix it" in out


def test_other_relay_failures_keep_the_target_text(capsys):
    path = bot_relay.write_reply(get_hermes_home(), "c" * 32, error="HTTP 529 overloaded")
    assert bot_mode_dm._wait_reply_main(str(path), "@scout", "1") == 1
    assert "HTTP 529 overloaded" in capsys.readouterr().out


def test_reply_not_relayed_is_typed_and_never_auto_retried():
    assert reasons.REPLY_NOT_RELAYED in reasons.ALL_REASONS
    assert not reasons.is_auto_retryable(reasons.REPLY_NOT_RELAYED)
    assert "turn_timeout" not in reasons.ALL_REASONS  # the retired CLI-child timeout is no DM transport event
