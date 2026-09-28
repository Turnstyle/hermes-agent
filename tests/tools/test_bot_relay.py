"""Tests: cross-connection bot relay (tools/bot_relay.py + message_agent route).

Connections ARE the peer set: every Desktop-connected gateway must be
message_agent-reachable. These tests pin the gateway-side plumbing —
roster validation, target resolution (incl. ambiguity), outbox claim
atomicity, reply write validation — and the two behavior contracts the
relay adds to message_agent:

- a target resolving against the Desktop-synced relay roster is queued as
  an envelope and acknowledged like any DM (fire-and-forget, waiter spawned);
- the legacy-SOUL dedupe (empty protocol section) NO LONGER strips the tool:
  the injection/execution gates key on managed-install, not section text.
"""

import json
import re
from pathlib import Path

import pytest

from tools import bot_relay
from tools.bot_mode_dm import (
    MESSAGE_AGENT_TOOL_NAME,
    ensure_message_agent_tool,
    message_agent_tool,
)


@pytest.fixture()
def root(tmp_path):
    return tmp_path


def _write_claimed_envelope(root, env: dict, *, target_profile: str) -> None:
    base = bot_relay.relay_root(root)
    (base / bot_relay.CLAIMED_DIR).mkdir(parents=True, exist_ok=True)
    payload = {**env, "target_profile": target_profile}
    (base / bot_relay.CLAIMED_DIR / f"{env['id']}.json").write_text(
        json.dumps(payload), encoding="utf-8")


def _rows():
    return [
        {
            "profile": "default",
            "handle": "hermes",
            "connection_id": "cloud-1",
            "connection_label": "Hermes Cloud",
            "title": "Moxie",
            "description": "Main cloud agent",
        },
        {
            "profile": "researcher",
            "handle": "researcher",
            "connection_id": "ssh-vps",
            "connection_label": "VPS",
        },
    ]


# ── roster ───────────────────────────────────────────────────────────────────


def test_roster_roundtrip_and_validation(root):
    rows = _rows() + [
        {"profile": "", "handle": "x", "connection_id": "c"},  # no profile
        {"profile": "bad name!", "connection_id": "c"},  # bad charset
        "not-a-dict",
        {"profile": "default", "handle": "hermes", "connection_id": "cloud-1"},  # dupe
    ]
    count = bot_relay.write_remote_roster(root, rows)
    assert count == 2
    back = bot_relay.read_remote_roster(root)
    assert [r["profile"] for r in back] == ["default", "researcher"]
    assert back[0]["title"] == "Moxie"


def test_roster_read_missing_and_corrupt(root):
    assert bot_relay.read_remote_roster(root) == []
    base = bot_relay.relay_root(root)
    base.mkdir(parents=True)
    (base / bot_relay.ROSTER_FILE).write_text("{corrupt", encoding="utf-8")
    assert bot_relay.read_remote_roster(root) == []


def test_resolve_remote_target_forms(root):
    bot_relay.write_remote_roster(root, _rows())
    roster = bot_relay.read_remote_roster(root)
    assert bot_relay.resolve_remote_target("researcher", roster)["connection_id"] == "ssh-vps"
    assert bot_relay.resolve_remote_target("@hermes", roster)["profile"] == "default"
    # profile name resolves too
    assert bot_relay.resolve_remote_target("default", roster)["connection_id"] == "cloud-1"
    # exact connection-qualified form
    assert bot_relay.resolve_remote_target("hermes@cloud-1", roster)["profile"] == "default"
    # profile@connection — the form Desktop's mention middleware annotates
    # for remote bots (#97678); the UI alias form must not be required
    assert bot_relay.resolve_remote_target("default@cloud-1", roster)["profile"] == "default"
    assert bot_relay.resolve_remote_target("hermes@nope", roster) is None
    assert bot_relay.resolve_remote_target("ghost", roster) is None


def test_resolve_ambiguous_handle_across_connections(root):
    rows = _rows() + [
        {"profile": "researcher", "handle": "researcher", "connection_id": "cloud-1"}
    ]
    bot_relay.write_remote_roster(root, rows)
    roster = bot_relay.read_remote_roster(root)
    assert bot_relay.resolve_remote_target("researcher", roster) == "ambiguous"
    match = bot_relay.resolve_remote_target("researcher@ssh-vps", roster)
    assert match["connection_id"] == "ssh-vps"
    forms = bot_relay.remote_target_forms(roster)
    assert "researcher@ssh-vps" in forms and "researcher@cloud-1" in forms
    assert "hermes" in forms  # unique handle stays bare


# ── outbox / replies ─────────────────────────────────────────────────────────


def test_enqueue_claim_is_atomic_and_single_shot(root):
    bot_relay.write_remote_roster(root, _rows())
    roster = bot_relay.read_remote_roster(root)
    target = bot_relay.resolve_remote_target("researcher", roster)
    env = bot_relay.enqueue_envelope(
        root, target=target, message="hi", sender_profile="work", sender_handle="work"
    )
    assert re.match(r"^[0-9a-f]{32}$", env["id"])
    claimed = bot_relay.claim_pending_envelopes(root)
    assert [e["id"] for e in claimed] == [env["id"]]
    assert claimed[0]["target_connection"] == "ssh-vps"
    assert claimed[0]["message"] == "hi"
    # second drain: nothing (no double delivery)
    assert bot_relay.claim_pending_envelopes(root) == []


def test_claim_skips_non_dict_envelope(root):
    """A parseable-but-non-object outbox file must not reach the Desktop consumer or
    crash the claim sweep; the claim stays claimed so it is not re-queued."""
    bot_relay.write_remote_roster(root, _rows())
    roster = bot_relay.read_remote_roster(root)
    target = bot_relay.resolve_remote_target("researcher", roster)
    env = bot_relay.enqueue_envelope(
        root, target=target, message="hi", sender_profile="work", sender_handle="work"
    )
    base = bot_relay.relay_root(root)
    bad = base / bot_relay.OUTBOX_DIR / f"{'9' * 32}.json"
    bad.write_text('"not an envelope"', encoding="utf-8")
    claimed = bot_relay.claim_pending_envelopes(root)
    assert [e["id"] for e in claimed] == [env["id"]]
    assert not bad.exists() and (base / bot_relay.CLAIMED_DIR / bad.name).exists()


def test_write_reply_validates_envelope_id(root):
    with pytest.raises(ValueError):
        bot_relay.write_reply(root, "../../etc/passwd", reply="x")
    path = bot_relay.write_reply(root, "a" * 32, reply="pong")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["reply"] == "pong" and not data["error"]


def test_write_reply_keeps_the_first_settled_reply_for_an_envelope(root):
    """Idempotent by envelope id: a re-offered delivery's second outcome (or a late duplicate) must
    not displace the reply the waiter already read, so the answer never turns into an error."""
    env_id = "a" * 32
    first = bot_relay.write_reply(root, env_id, reply="the answer")
    second = bot_relay.write_reply(root, env_id, error="target busy", reason="target_busy")
    assert second == first
    record = json.loads(first.read_text(encoding="utf-8"))
    assert (record["reply"], record["error"], record["reason"]) == ("the answer", "", "")


def test_write_reply_reason_passthrough_and_classification(root):
    # explicit reason is persisted verbatim
    path = bot_relay.write_reply(root, "c" * 32, error="boom", reason="delivery_timeout")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["reason"] == "delivery_timeout" and data["error"] == "boom"
    # no reason given → classified from error text
    path = bot_relay.write_reply(root, "d" * 32, error="Error code: 429 - rate limit")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["reason"] == "provider_rate_limit"
    # success reply carries an empty reason
    path = bot_relay.write_reply(root, "e" * 32, reply="ok")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["reason"] == "" and data["reply"] == "ok"


def test_write_reply_persists_reply_relayed_when_given(root):
    """A relay hop's reply_relayed flag (#reply_not_relayed) must survive the write, not be
    silently dropped — the waiter/telemetry needs it to classify a REPLY NOT RELAYED ok reply the
    same way it classifies a typed error's reason."""
    path = bot_relay.write_reply(root, "f" * 32, reply="REPLY NOT RELAYED: ...",
                                 reason="reply_not_relayed", reply_relayed=False)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["reply_relayed"] is False
    assert data["reason"] == "reply_not_relayed"
    # Omitted entirely (not persisted as null/false) for an ordinary reply — additive, so an older
    # reader that never heard of the field sees the exact same record it always did.
    path = bot_relay.write_reply(root, "1" * 32, reply="ok")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert "reply_relayed" not in data


def test_target_scope_error_from_a_connection_target_is_rewritten_to_name_it(root):
    """A target-scope refusal that came back FROM a relay target must not keep the generic "run
    `hermes gateway restart`" advice — the delivery turn ran and failed on THAT install, so the
    sender's gateway is the wrong place to restart. Scoped to a cross-connection envelope."""
    scope = ("Hermes could not read this profile's API key (an internal profile-scoping bug on the "
             "multiplexed gateway, not your configuration). Run `hermes gateway restart`; if it keeps "
             "happening, report it with `hermes debug share`.")
    envelope = {"id": "a" * 32, "target_connection": "nebular-cooee", "target_profile": "default",
                "target_handle": "hermes", "message": "hi", "created_at": 1}

    out = bot_relay.relayed_failure_error(scope, envelope)
    assert "nebular-cooee" in out and "'default'" in out and "@hermes" in out
    assert "Restarting this gateway cannot fix it" in out
    assert "Run `hermes gateway restart`" not in out

    # Nothing to rewrite: a LOCAL delivery failure (no connection), another error class, no envelope.
    assert bot_relay.relayed_failure_error(scope, {"target_profile": "default"}) == scope
    assert bot_relay.relayed_failure_error("Error code: 429 - rate limit", envelope) == (
        "Error code: 429 - rate limit")
    assert bot_relay.relayed_failure_error(scope) == scope
    assert bot_relay.relayed_failure_error(scope, None) == scope


def test_target_scope_refusal_is_written_for_a_sender_on_another_machine():
    """The TARGET's own copy. It must name the profile and this install, and it must NOT send a
    remote sender to restart a gateway that has nothing to do with the refusal."""
    text = bot_relay.target_scope_refusal("default", "C:/Users/x/AppData/Local/hermes")
    assert "'default'" in text and "THIS install" in text
    assert "C:/Users/x/AppData/Local/hermes" in text
    assert "SENDER's gateway cannot fix it" in text
    # no home supplied, and an unnamed target: still honest, still names one
    assert "THIS install" in bot_relay.target_scope_refusal("ops")
    assert "'default'" in bot_relay.target_scope_refusal("")


def test_read_claimed_envelope_roundtrip_and_id_validation(root):
    env_id = "b" * 32
    base = bot_relay.relay_root(root)
    (base / bot_relay.CLAIMED_DIR).mkdir(parents=True, exist_ok=True)
    (base / bot_relay.CLAIMED_DIR / f"{env_id}.json").write_text(
        json.dumps({"id": env_id, "target_connection": "nebular-cooee"}), encoding="utf-8")
    assert bot_relay.read_claimed_envelope(root, env_id)["target_connection"] == "nebular-cooee"
    # absent, malformed id, and non-object payloads all answer {} — never raise at reply time
    assert bot_relay.read_claimed_envelope(root, "c" * 32) == {}
    assert bot_relay.read_claimed_envelope(root, "../evil") == {}
    (base / bot_relay.CLAIMED_DIR / f"{'d' * 32}.json").write_text("[1]", encoding="utf-8")
    assert bot_relay.read_claimed_envelope(root, "d" * 32) == {}


def test_waiter_is_a_runner_entrypoint_the_approval_gate_lets_through(root):
    """The waiter is spawned through terminal_tool from the SENDER's turn. When a bot replies to a
    teammate from its own one-shot delivery turn, that turn runs under ``approvals.single_query_mode``
    (default ``deny``), and inline interpreter code is a flagged pattern — so the reply waiter was
    refused exactly when bots talked to each other, and the reply never woke the sender."""
    import shlex

    from tools.approval import detect_dangerous_command

    env = {"id": "b" * 32, "target_handle": "researcher", "target_connection": "ssh-vps"}
    cmd = bot_relay.waiter_command(root, env)
    parts = shlex.split(cmd)

    assert detect_dangerous_command(cmd)[0] is False, cmd
    assert parts[1].endswith("bot_mode_dm.py") and parts[2] == "--wait-reply"
    assert parts[3] == str(bot_relay.relay_root(root) / bot_relay.REPLIES_DIR / f"{'b' * 32}.json")
    assert parts[4:] == ["@researcher on ssh-vps", str(bot_relay.REPLY_WAIT_SECONDS)]


def test_waiter_outlives_the_desktop_deliver_deadline():
    """The Desktop posts its timeout reply when RELAY_DELIVER_TIMEOUT_MS passes. A waiter that gave
    up first left that reply, and any turn finishing after minute 15, in a file nobody read (#93911).
    relay-deliver-budget.test.ts pins the TS constants against these Python ones.

    The re-offer window (#111021) sits between the two: only past the Desktop's deadline — and the
    gateway's own worst-case deliver hold, which ends before it — is a claimed envelope's silence
    provably a dead Desktop rather than a slow turn, and the waiter must still be listening when the
    ONE re-offered delivery hits its own Desktop deadline."""
    live_hold_s = bot_relay.TURN_WAIT_SECONDS_FALLBACK + bot_relay.TURN_ATTEMPT_TIMEOUT_SECONDS * bot_relay.TURN_MAX_ATTEMPTS
    desktop_budget_s = live_hold_s + bot_relay.DESKTOP_DELIVER_SETTLEMENT_MARGIN_SECONDS
    assert bot_relay.DESKTOP_DELIVER_TIMEOUT_SECONDS == desktop_budget_s
    assert bot_relay.REOFFER_AFTER_SECONDS > desktop_budget_s > live_hold_s
    assert bot_relay.REPLY_WAIT_SECONDS > bot_relay.REOFFER_AFTER_SECONDS + desktop_budget_s


def test_turn_wait_seconds_clamps_above_the_desktop_deadline(monkeypatch):
    """A configured wait above the Desktop lock-wait mirror must not let the backend
    outlive RELAY_DELIVER_TIMEOUT_MS. The handler spends this wait, then two attempts."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"bot_mode": {"turn_wait_seconds": 2400}},
    )
    waited = bot_relay.turn_wait_seconds()
    assert waited == float(bot_relay.TURN_WAIT_SECONDS_FALLBACK)
    backend_total = waited + bot_relay.TURN_ATTEMPT_TIMEOUT_SECONDS * bot_relay.TURN_MAX_ATTEMPTS
    assert backend_total < bot_relay.DESKTOP_DELIVER_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    ("reply_file", "expected_code", "expected_tokens"),
    [
        ({"reply": "pong"}, 0, ["@researcher on ssh-vps", "pong"]),
        ({"reply": ""}, 0, ["@researcher on ssh-vps"]),
        ({"error": "turn failed", "reason": "provider_rate_limit"}, 1,
         ["@researcher on ssh-vps", "provider_rate_limit", "turn failed"]),
        ({"error": "turn failed"}, 1, ["@researcher on ssh-vps", "turn failed"]),
        (None, 1, ["@researcher on ssh-vps", "0.3s"]),
    ],
    ids=["reply", "empty-reply", "typed-error", "untyped-error", "gave-up"],
)
def test_waiter_prints_the_completion_notification_the_sender_wakes_on(root, capsys, reply_file, expected_code, expected_tokens):
    """The waiter's stdout IS the sender's completion notification: the reply, a typed failure the
    sender can branch on without parsing prose (#93091), or an honest give-up that names the budget."""
    from tools import bot_mode_dm

    env = {"id": "d" * 32, "target_handle": "researcher", "target_connection": "ssh-vps"}
    reply_path = bot_relay.relay_root(root) / bot_relay.REPLIES_DIR / f"{env['id']}.json"
    label = "@researcher on ssh-vps"
    if reply_file is not None:
        if reply_file.get("error"):
            bot_relay.write_reply(
                root, env["id"],
                error=str(reply_file["error"]),
                reason=str(reply_file.get("reason") or ""),
            )
        else:
            _write_claimed_envelope(root, env, target_profile="researcher")
            bot_relay.write_reply(
                root, env["id"],
                reply=str(reply_file.get("reply") or ""),
                delivered_profile="researcher",
            )

    code = bot_mode_dm._delivery_main(["--wait-reply", str(reply_path), label, "0.3"])

    out = capsys.readouterr().out
    assert code == expected_code
    assert all(token in out for token in expected_tokens), out
    assert bot_mode_dm._delivery_main(["--wait-reply", str(reply_path)]) == 2


def test_wait_reply_main_success_iff_attested_responder_matches_envelope_target(root, capsys):
    """The sender-side waiter must never treat another bot's ok reply as the envelope target's:
    success iff delivered_profile attests the same profile as the claimed envelope's target."""
    from tools import bot_mode_dm

    bot_relay.write_remote_roster(root, [
        {"profile": "tb-cndr", "handle": "tb-cndr", "connection_id": "relay-conn"},
    ])
    roster = bot_relay.read_remote_roster(root)
    target = bot_relay.resolve_remote_target("tb-cndr", roster)
    env = bot_relay.enqueue_envelope(
        root, target=target, message="ping", sender_profile="work", sender_handle="work",
    )
    claimed = bot_relay.claim_pending_envelopes(root)
    assert [e["id"] for e in claimed] == [env["id"]]

    label = "@tb-cndr on relay-conn"
    reply_path = bot_relay.relay_root(root) / bot_relay.REPLIES_DIR / f"{env['id']}.json"
    secret = "SECRET-DEFAULT-TEXT"

    misrouted = bot_relay.write_reply(root, env["id"], reply=secret)
    misrouted_data = json.loads(misrouted.read_text(encoding="utf-8"))
    misrouted_data["delivered_profile"] = "default"
    misrouted.write_text(json.dumps(misrouted_data), encoding="utf-8")
    code = bot_mode_dm._delivery_main(["--wait-reply", str(reply_path), label, "0.3"])
    out = capsys.readouterr().out
    assert code == 1
    assert "MISROUTED" in out and "target_scope_unresolved" in out
    assert secret not in out
    assert not out.lstrip().startswith("Reply from")

    reply_path.unlink()
    attested = bot_relay.write_reply(root, env["id"], reply=secret, delivered_profile="tb-cndr")
    assert attested == reply_path
    code = bot_mode_dm._delivery_main(["--wait-reply", str(reply_path), label, "0.3"])
    out = capsys.readouterr().out
    assert code == 0
    assert out.splitlines()[0] == f"Reply from {label}:"
    assert secret in out


def test_wait_reply_main_queued_admit_is_not_labeled_as_reply_from(root, capsys):
    """A queued fast-ack is admitted (exit 0) but must not print the trusted Reply from line."""
    from tools import bot_mode_dm
    from tools.bot_relay import relay_queued_sender_text
    from tui_gateway.contracts.groups_bot_relay import BotRelayDeliverResult

    parsed = BotRelayDeliverResult(
        reply=relay_queued_sender_text("ops"),
        status="queued",
        message_id="fm-queued",
        reply_relayed=False,
        delivered_profile="ops",
    )
    assert parsed.status == "queued" and parsed.message_id == "fm-queued"

    env_id = "a" * 32
    env = {
        "id": env_id,
        "target_handle": "ops-bot",
        "target_connection": "cloud-1",
        "target_profile": "ops",
    }
    _write_claimed_envelope(root, env, target_profile="ops")
    label = "@ops-bot on cloud-1"
    reply_path = bot_relay.relay_root(root) / bot_relay.REPLIES_DIR / f"{env_id}.json"
    bot_relay.write_reply(
        root,
        env_id,
        reply=relay_queued_sender_text("ops"),
        delivered_profile="ops",
        reply_relayed=False,
        status="queued",
        message_id="fm-queued",
    )
    code = bot_mode_dm._delivery_main(["--wait-reply", str(reply_path), label, "0.3"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Queued for" in out and "not yet answered" in out
    assert "Reply from" not in out


def test_waiter_unrelayed_ok_reply_is_not_booked_as_the_targets_answer(root, capsys):
    """An admitted ok reply whose answer stayed in the target chat (reply_not_relayed) must not
    print the trusted 'Reply from' line. The sender's completion carries a stable typed flag and
    keeps the do-not-resend prose; exit 0 because the message will run and a resend would duplicate it."""
    from tools import bot_mode_dm

    env_id = "f" * 32
    env = {
        "id": env_id,
        "target_handle": "tb-cndr",
        "target_connection": "turnerbook",
        "target_profile": "tb-cndr",
    }
    _write_claimed_envelope(root, env, target_profile="tb-cndr")
    label = "@tb-cndr on turnerbook"
    reply_path = bot_relay.relay_root(root) / bot_relay.REPLIES_DIR / f"{env_id}.json"
    prose = (
        "REPLY NOT RELAYED: @tb-cndr's Bot Chat is still open; your message will run as its next "
        "turn. Do not resend."
    )
    bot_relay.write_reply(
        root, env_id, reply=prose, delivered_profile="tb-cndr",
        reason="reply_not_relayed", reply_relayed=False,
    )
    code = bot_mode_dm._delivery_main(["--wait-reply", str(reply_path), label, "0.3"])
    out = capsys.readouterr().out
    assert code == 0
    assert "[reason: reply_not_relayed, reply_relayed: false]" in out
    assert prose in out
    assert not out.lstrip().startswith("Reply from")


def test_wait_reply_main_rejects_unattested_ok_reply(root, capsys):
    """An ok reply without delivered_profile must not print the trusted 'Reply from' success line."""
    from tools import bot_mode_dm

    env_id = "e" * 32
    env = {
        "id": env_id,
        "target_handle": "researcher",
        "target_connection": "ssh-vps",
        "target_profile": "researcher",
    }
    _write_claimed_envelope(root, env, target_profile="researcher")
    label = "@researcher on ssh-vps"
    reply_path = bot_relay.relay_root(root) / bot_relay.REPLIES_DIR / f"{env_id}.json"
    bot_relay.write_reply(root, env_id, reply="maybe-wrong-bot")
    code = bot_mode_dm._delivery_main(["--wait-reply", str(reply_path), label, "0.3"])
    out = capsys.readouterr().out
    assert code == 1
    assert "UNVERIFIED" in out and "target_scope_unresolved" in out
    assert not any(line.startswith("Reply from ") for line in out.splitlines())


def test_wait_reply_main_withholds_unattested_reply_body(root, capsys):
    """Unattested ok replies must not disclose the body; the on-disk copy stays for diagnosis."""
    import json

    from tools import bot_mode_dm

    env_id = "e" * 32
    env = {
        "id": env_id,
        "target_handle": "researcher",
        "target_connection": "ssh-vps",
        "target_profile": "researcher",
    }
    _write_claimed_envelope(root, env, target_profile="researcher")
    label = "@researcher on ssh-vps"
    reply_path = bot_relay.relay_root(root) / bot_relay.REPLIES_DIR / f"{env_id}.json"
    secret = "WRONG-PROFILE-PRIVATE-ANSWER"
    bot_relay.write_reply(root, env_id, reply=secret)
    code = bot_mode_dm._delivery_main(["--wait-reply", str(reply_path), label, "0.3"])
    out = capsys.readouterr().out
    assert code == 1
    assert secret not in out
    assert "UNVERIFIED" in out
    with open(reply_path, encoding="utf-8-sig") as fh:
        assert json.load(fh)["reply"] == secret


def test_roster_rejects_connection_id_outside_handle_charset(root):
    bad = [
        {"profile": "researcher", "handle": "researcher", "connection_id": "vps'); print(1)"},
        {"profile": "researcher", "handle": "researcher", "connection_id": "foo'bar"},
        {"profile": "researcher", "handle": "researcher", "connection_id": "ssh vps"},
        {"profile": "researcher", "handle": "researcher", "connection_id": "a" * 65},
    ]
    assert bot_relay.write_remote_roster(root, bad) == 0
    good = {
        "profile": "researcher",
        "handle": "researcher",
        "connection_id": "ssh-vps",
    }
    assert bot_relay.write_remote_roster(root, [good]) == 1


def test_hostile_roster_fields_ride_as_argv_data(root):
    """Envelope fields come from the Desktop-pushed roster — untrusted. They must never become
    source text: the waiter takes them as argv, so a payload shaped like Python is a label."""
    import shlex
    import subprocess

    inj = "x'); open(r'/tmp/pwned','w').write('pwned'); print('x"
    env = {"id": "c" * 32, "target_handle": "researcher", "target_connection": inj}
    cmd = bot_relay.waiter_command(root, env)
    parts = shlex.split(cmd)

    assert "-c" not in parts
    assert parts[4] == f"@researcher on {inj}"
    _write_claimed_envelope(root, env, target_profile="researcher")
    bot_relay.write_reply(root, env["id"], reply="pong", delivered_profile="researcher")
    proc = subprocess.run(parts, capture_output=True, text=True, timeout=30)

    assert proc.returncode == 0 and proc.stdout.splitlines() == [f"Reply from @researcher on {inj}:", "pong"]


# ── message_agent integration: relay route + legacy-SOUL gate fix ───────────

import textwrap


def _managed_home(tmp_path, *, legacy_soul=False):
    home = tmp_path / ".hermes"
    home.mkdir(exist_ok=True)
    d = home / "profiles" / "researcher"
    d.mkdir(parents=True, exist_ok=True)
    (d / "profile.yaml").write_text(
        textwrap.dedent(
            """\
            description: teammate for tests
            ui_meta:
              hermes-bots:
                shape: cloud
            """
        ),
        encoding="utf-8",
    )
    if legacy_soul:
        (home / "SOUL.md").write_text(
            "# Soul\n\n## Messaging other agents\nold shellout protocol\n",
            encoding="utf-8",
        )
    return home


class _FakeDB:
    def __init__(self, home, title):
        self.db_path = str(home / "state.db")
        self._title = title

    def get_session_title(self, _sid):
        return self._title


class _FakeAgent:
    def __init__(self, home, title="Bot Chat"):
        self._session_db = _FakeDB(home, title)
        self.session_id = "sess-1"
        self._session_title_hint = None
        self._bot_mode_protocol = True
        self.tools: list = []
        self.valid_tool_names: set = set()


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    from tools import bot_mode_probe

    bot_mode_probe._reset_cache_for_tests()
    yield
    bot_mode_probe._reset_cache_for_tests()


def test_tool_injects_despite_legacy_soul_protocol(tmp_path):
    """A SOUL.md still carrying the plugin-appended protocol must not cost the TOOL (nor,
    since load-time stripping, the live section)."""
    from tools import bot_mode_probe

    home = _managed_home(tmp_path, legacy_soul=True)
    assert bot_mode_probe.get_bot_mode_protocol_section(home) != ""
    agent = _FakeAgent(home)
    assert ensure_message_agent_tool(agent) is True
    assert [t["function"]["name"] for t in agent.tools] == [MESSAGE_AGENT_TOOL_NAME]


def test_relay_route_queues_envelope_and_spawns_waiter(tmp_path, monkeypatch):
    home = _managed_home(tmp_path)
    bot_relay.write_remote_roster(home, [
        {"profile": "default", "handle": "hermes", "connection_id": "cloud-1",
         "connection_label": "Hermes Cloud", "title": "Moxie"},
    ])

    spawned = {}

    def _fake_spawn(command, label, *, task_id, agent, **_):
        spawned["command"] = command
        spawned["label"] = label
        return json.dumps({"status": "queued", "to": label})

    monkeypatch.setattr("tools.bot_mode_dm._spawn_delivery", _fake_spawn)
    agent = _FakeAgent(home)
    out = json.loads(message_agent_tool(target="hermes", message="ping", agent=agent))
    assert out.get("status") == "queued"
    assert "Hermes Cloud" in spawned["label"]
    # envelope landed in the outbox with attribution prefixed
    pending = bot_relay.claim_pending_envelopes(home)
    assert len(pending) == 1
    assert pending[0]["target_connection"] == "cloud-1"
    assert pending[0]["target_profile"] == "default"
    assert pending[0]["message"].startswith("Message from 🤖 hermes (@hermes): ping")
    # waiter watches this envelope's reply file
    assert pending[0]["id"] in spawned["command"]


def test_relay_route_ambiguous_target_errors_with_forms(tmp_path, monkeypatch):
    home = _managed_home(tmp_path)
    bot_relay.write_remote_roster(home, [
        {"profile": "scout", "handle": "scout", "connection_id": "cloud-1"},
        {"profile": "scout", "handle": "scout", "connection_id": "ssh-vps"},
    ])
    monkeypatch.setattr(
        "tools.bot_mode_dm._spawn_delivery",
        lambda *a, **k: json.dumps({"status": "queued"}),
    )
    agent = _FakeAgent(home)
    out = json.loads(message_agent_tool(target="scout", message="hi", agent=agent))
    assert "scout@cloud-1" in out.get("error", "") and "scout@ssh-vps" in out["error"]
    # connection-qualified form goes through
    out2 = json.loads(message_agent_tool(target="scout@ssh-vps", message="hi", agent=agent))
    assert out2.get("status") == "queued"


def test_remote_default_is_addressable_by_its_title_slug(tmp_path, monkeypatch):
    """A remote ``default`` is ``@hermes`` like every gateway's own, so its Bot Mode title is the only
    bare form that singles it out: ``message_agent`` accepts the slug, the prompt roster offers it
    (never bare ``@hermes``, which is the LOCAL default), and the form does not depend on roster order."""
    from tools import bot_mode_probe

    home = _managed_home(tmp_path)
    rows = [
        {"profile": "default", "handle": "hermes", "connection_id": "vps-1", "title": "CoS Bot"},
        {"profile": "default", "handle": "hermes", "connection_id": "cloud-1", "title": "Ops Bot"},
        {"profile": "cos-bot", "handle": "cos-bot", "connection_id": "cloud-1", "title": "Other"},
    ]
    monkeypatch.setattr("tools.bot_mode_dm._spawn_delivery", lambda *a, **k: json.dumps({"status": "queued"}))
    agent = _FakeAgent(home)
    for order in (rows, rows[::-1]):
        bot_relay.write_remote_roster(home, order)
        roster = bot_relay.read_remote_roster(home)
        forms = dict(zip((r["connection_id"] + "/" + r["profile"] for r in roster),
                         bot_relay.remote_target_forms(roster, bot_mode_probe.local_taken_forms(home))))
        # an exact handle beats a colliding title slug; the collided title falls back to the qualified form
        assert forms == {"cloud-1/cos-bot": "cos-bot", "vps-1/default": "hermes@vps-1", "cloud-1/default": "ops-bot"}
        assert bot_relay.resolve_remote_target("cos-bot", roster)["profile"] == "cos-bot"
    out = json.loads(message_agent_tool(target="@ops-bot", message="ping", agent=agent))
    assert out.get("status") == "queued"
    [env] = bot_relay.claim_pending_envelopes(home)
    assert (env["target_connection"], env["target_profile"]) == ("cloud-1", "default")
    # a bare @hermes from the local default is the two remote defaults — offered under their reply-safe forms
    err = json.loads(message_agent_tool(target="hermes", message="ping", agent=agent))["error"]
    assert "hermes@vps-1" in err and "ops-bot" in err
    section = bot_mode_probe.get_bot_mode_protocol_section(home)
    assert "`@ops-bot`" in section and "`@hermes@vps-1`" in section and "- `@hermes` —" not in section




def test_protocol_section_lists_remote_teammates(tmp_path):
    from tools import bot_mode_probe

    home = _managed_home(tmp_path)
    bot_relay.write_remote_roster(home, [
        {"profile": "default", "handle": "hermes", "connection_id": "cloud-1",
         "connection_label": "Hermes Cloud", "title": "Moxie"},
    ])
    section = bot_mode_probe.get_bot_mode_protocol_section(home, force_refresh=True)
    # Offered under its title slug: bare `@hermes` is THIS gateway's own default (#103731).
    assert "`@moxie`" in section and "Hermes Cloud" in section


def test_capability_fingerprint_changes_with_relay_roster(tmp_path):
    from tools import bot_mode_probe

    home = _managed_home(tmp_path)
    before = bot_mode_probe.capability_fingerprint(home)
    bot_relay.write_remote_roster(home, [
        {"profile": "default", "handle": "hermes", "connection_id": "cloud-1"},
    ])
    after = bot_mode_probe.capability_fingerprint(home)
    assert before != after  # eternal Bot Chats refresh once on roster change


# ── stale artifact sweep (housekeeping contract) ─────────────────────────────


def test_cleanup_bot_relay_artifacts_sweeps_stale_plaintext(tmp_path, monkeypatch):
    import os as _os
    import time as _time

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    target = {"profile": "scout", "handle": "scout", "connection_id": "cloud-1",
              "connection_label": "", "title": "", "description": ""}
    stale_env = bot_relay.enqueue_envelope(
        tmp_path, target=target, message="old secret",
        sender_profile="default", sender_handle="hermes",
    )
    fresh_env = bot_relay.enqueue_envelope(
        tmp_path, target=target, message="new secret",
        sender_profile="default", sender_handle="hermes",
    )
    base = bot_relay.relay_root(tmp_path)
    stale_reply = bot_relay.write_reply(tmp_path, stale_env["id"], reply="done")
    stale_body = base / bot_relay.OUTBOX_DIR / f"{stale_env['id']}.json"
    stale_meta = base / bot_relay.OUTBOX_DIR / f"{stale_env['id']}.meta.json"
    stale_reply_meta = stale_reply.parent / f"{stale_env['id']}.reply.meta.json"
    fresh_body = base / bot_relay.OUTBOX_DIR / f"{fresh_env['id']}.json"
    fresh_meta = base / bot_relay.OUTBOX_DIR / f"{fresh_env['id']}.meta.json"
    old = _time.time() - bot_relay.STALE_AFTER_SECONDS - 1
    _os.utime(stale_body, (old, old))
    _os.utime(stale_reply, (old, old))

    removed = bot_relay.cleanup_bot_relay_artifacts()

    assert removed == 4  # Two stale payloads and their two metadata sidecars.
    assert not stale_body.exists()
    assert not stale_meta.exists()
    assert not stale_reply.exists()
    assert not stale_reply_meta.exists()
    assert fresh_body.exists()
    assert fresh_meta.exists()


def test_cleanup_bot_relay_artifacts_missing_dir_is_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nope"))
    assert bot_relay.cleanup_bot_relay_artifacts() == 0


# ── #93091 item 2: offline fail-fast + drain-time TTL ────────────────────────

import os as _os2
import time as _time2


def _target(conn="cloud-1", profile="scout", handle="scout"):
    return {"profile": profile, "handle": handle, "connection_id": conn,
            "connection_label": "", "title": "", "description": ""}


def test_enqueue_fails_fast_when_row_explicitly_offline(root):
    bot_relay.write_remote_roster(root, [
        {"profile": "scout", "handle": "scout", "connection_id": "cloud-1",
         "online": False},
    ])
    roster = bot_relay.read_remote_roster(root)
    assert roster[0]["online"] is False  # additive field survives normalize
    with pytest.raises(bot_relay.EnvelopeRefusedError) as ei:
        bot_relay.enqueue_envelope(
            root, target=roster[0], message="hi",
            sender_profile="default", sender_handle="hermes",
        )
    assert ei.value.reason == "runtime_offline"
    assert "offline" in str(ei.value)
    # nothing was written to the outbox
    outdir = bot_relay.relay_root(root) / bot_relay.OUTBOX_DIR
    assert not outdir.exists() or list(outdir.glob("*.json")) == []


def test_enqueue_fails_fast_when_target_absent_from_fresh_roster(root):
    bot_relay.write_remote_roster(root, _rows())  # fresh, no 'scout' row
    with pytest.raises(bot_relay.EnvelopeRefusedError) as ei:
        bot_relay.enqueue_envelope(
            root, target=_target(), message="hi",
            sender_profile="default", sender_handle="hermes",
        )
    assert ei.value.reason == "runtime_offline"


def test_enqueue_fails_open_when_liveness_unknown(root):
    # 1. no roster ever synced → unknown → enqueue
    env = bot_relay.enqueue_envelope(
        root, target=_target(), message="hi",
        sender_profile="default", sender_handle="hermes",
    )
    assert (bot_relay.relay_root(root) / bot_relay.OUTBOX_DIR / f"{env['id']}.json").exists()
    # 2. stale roster missing the target → unknown → enqueue
    bot_relay.write_remote_roster(root, _rows())
    roster_path = bot_relay.relay_root(root) / bot_relay.ROSTER_FILE
    old = _time2.time() - bot_relay.ROSTER_FRESH_SECONDS - 5
    _os2.utime(roster_path, (old, old))
    env2 = bot_relay.enqueue_envelope(
        root, target=_target(), message="hi again",
        sender_profile="default", sender_handle="hermes",
    )
    assert (bot_relay.relay_root(root) / bot_relay.OUTBOX_DIR / f"{env2['id']}.json").exists()
    # 3. fresh roster listing the target without an online flag → enqueue
    bot_relay.write_remote_roster(root, _rows())
    target = bot_relay.read_remote_roster(root)[1]  # researcher@ssh-vps
    env3 = bot_relay.enqueue_envelope(
        root, target=target, message="hello",
        sender_profile="default", sender_handle="hermes",
    )
    assert (bot_relay.relay_root(root) / bot_relay.OUTBOX_DIR / f"{env3['id']}.json").exists()


def test_drain_expires_old_envelope_with_queued_expired_reply(root):
    env = bot_relay.enqueue_envelope(
        root, target=_target(), message="too late",
        sender_profile="default", sender_handle="hermes",
    )
    base = bot_relay.relay_root(root)
    out_path = base / bot_relay.OUTBOX_DIR / f"{env['id']}.json"
    # backdate the envelope beyond the TTL
    env["created_at"] = int(_time2.time()) - bot_relay.DEFAULT_ENVELOPE_TTL_SECONDS - 10
    out_path.write_text(json.dumps(env), encoding="utf-8")

    claimed = bot_relay.claim_pending_envelopes(root)

    assert claimed == []  # not delivered
    assert not out_path.exists()  # expired outbox file removed
    reply = json.loads(
        (base / bot_relay.REPLIES_DIR / f"{env['id']}.json").read_text(encoding="utf-8")
    )
    assert reply["reason"] == "queued_expired"
    assert reply["error"]
    assert not reply["reply"]


def test_the_outbox_is_claimed_oldest_first(root):
    """Two DMs from one sender to one agent must arrive in the order they were sent. The Desktop
    delivers each target's claimed envelopes in the order this list gives them, one turn at a
    time, so the claim IS the delivery order — and sorting by filename ordered them by
    ``uuid4().hex``. The names here are forced into the reverse of the send order to pin that
    deterministically, which random ids reproduce half the time."""
    first = bot_relay.enqueue_envelope(
        root, target=_target(), message="do this first",
        sender_profile="default", sender_handle="hermes",
    )
    second = bot_relay.enqueue_envelope(
        root, target=_target(), message="then this",
        sender_profile="default", sender_handle="hermes",
    )
    outbox = bot_relay.relay_root(root) / bot_relay.OUTBOX_DIR
    now = _time2.time()
    for env, name, sent_at in ((first, "f" * 32, now - 2), (second, "0" * 32, now - 1)):
        path = outbox / f"{name}.json"
        (outbox / f"{env['id']}.json").rename(path)
        _os2.utime(path, (sent_at, sent_at))

    claimed = bot_relay.claim_pending_envelopes(root)

    assert [e["id"] for e in claimed] == [first["id"], second["id"]]
    assert [e["message"] for e in claimed] == ["do this first", "then this"]


def test_drain_delivers_fresh_envelope_under_ttl(root):
    env = bot_relay.enqueue_envelope(
        root, target=_target(), message="on time",
        sender_profile="default", sender_handle="hermes",
    )
    claimed = bot_relay.claim_pending_envelopes(root)
    assert [e["id"] for e in claimed] == [env["id"]]
    # no spurious expiry reply for a delivered envelope
    base = bot_relay.relay_root(root)
    assert not (base / bot_relay.REPLIES_DIR / f"{env['id']}.json").exists()


def test_drain_ttl_zero_disables_expiry(root, monkeypatch):
    monkeypatch.setattr(bot_relay, "_envelope_ttl_seconds", lambda: 0)
    env = bot_relay.enqueue_envelope(
        root, target=_target(), message="never expires",
        sender_profile="default", sender_handle="hermes",
    )
    base = bot_relay.relay_root(root)
    out_path = base / bot_relay.OUTBOX_DIR / f"{env['id']}.json"
    env["created_at"] = int(_time2.time()) - 10 * 3600
    out_path.write_text(json.dumps(env), encoding="utf-8")
    claimed = bot_relay.claim_pending_envelopes(root)
    assert [e["id"] for e in claimed] == [env["id"]]


def test_invalid_ttl_config_falls_back_instead_of_breaking_drain(monkeypatch):
    monkeypatch.setattr(bot_relay, "_bot_mode_cfg", lambda *args, **kwargs: "not-a-number")

    assert bot_relay._envelope_ttl_seconds() == bot_relay.DEFAULT_ENVELOPE_TTL_SECONDS




def test_message_agent_surfaces_runtime_offline_refusal(tmp_path, monkeypatch):
    home = _managed_home(tmp_path)
    bot_relay.write_remote_roster(home, [
        {"profile": "default", "handle": "hermes", "connection_id": "cloud-1",
         "connection_label": "Hermes Cloud", "online": False},
    ])
    monkeypatch.setattr(
        "tools.bot_mode_dm._spawn_delivery",
        lambda *a, **k: json.dumps({"status": "queued"}),
    )
    agent = _FakeAgent(home)
    out = json.loads(message_agent_tool(target="hermes", message="ping", agent=agent))
    assert out.get("reason") == "runtime_offline"
    assert "offline" in out.get("error", "")
    # fail-fast means no envelope was queued
    assert bot_relay.claim_pending_envelopes(home) == []


# ── delivery turn author (HERMES_TURN_AUTHOR on the recipient turn) ──────────


def test_delivery_turn_author_from_envelope_sender_fields():
    author = bot_relay.delivery_turn_author("ops", "ops-bot")
    assert author == {"id": "bot:ops", "name": "ops-bot", "is_bot": True}
    # The display name falls back to the profile; the id never comes from the handle.
    assert bot_relay.delivery_turn_author("ops", "") == {"id": "bot:ops", "name": "ops", "is_bot": True}
    assert bot_relay.delivery_turn_author("", "ops-bot") is None
    assert bot_relay.delivery_turn_author(None, None) is None


def test_delivery_turn_author_qualifies_a_remote_sender_by_its_connection():
    """A relayed DM always crosses gateways, so the sender's connection id is part of the author id, ``local``
    included; the recipient's own ``ops`` is the only bare ``bot:ops``."""
    remote = bot_relay.delivery_turn_author("ops", "ops-bot", "cloud-1")
    assert remote == {"id": "bot:cloud-1/ops", "name": "ops-bot", "is_bot": True}
    assert bot_relay.delivery_turn_author("ops", "ops-bot", "local") == {"id": "bot:local/ops", "name": "ops-bot", "is_bot": True}
    # An older Desktop that sends no connection id still yields an author.
    assert bot_relay.delivery_turn_author("ops", "ops-bot", "") == {"id": "bot:ops", "name": "ops-bot", "is_bot": True}


def test_delivery_env_carries_only_the_given_author(monkeypatch):
    """The dispatcher's own HERMES_TURN_AUTHOR never reaches the child: dropped without an author, replaced with one."""
    from agent.turn_author import TURN_AUTHOR_ENV

    monkeypatch.setenv("HERMES_RELAY_TEST_MARKER", "kept")
    monkeypatch.setenv(TURN_AUTHOR_ENV, json.dumps({"id": "bot:previous", "name": "previous", "is_bot": True}))
    monkeypatch.setenv("HERMES_SESSION_KEY", "session-A")
    monkeypatch.setenv("HERMES_UI_SESSION_ID", "ui-A")
    monkeypatch.setenv("HERMES_SESSION_ID", "session-A")
    monkeypatch.setenv("HERMES_SESSION_PROFILE", "profile-A")
    # A session-* knob, not identity: stripping it would break the child's watcher tuning.
    monkeypatch.setenv("HERMES_SESSION_STALL_TIMEOUT", "97")

    assert TURN_AUTHOR_ENV not in bot_relay.delivery_env(None)
    env = bot_relay.delivery_env(bot_relay.delivery_turn_author("ops", "ops"))
    assert json.loads(env[TURN_AUTHOR_ENV]) == {"id": "bot:ops", "name": "ops", "is_bot": True}
    assert env["HERMES_RELAY_TEST_MARKER"] == "kept"
    assert "HERMES_SESSION_KEY" not in env
    assert "HERMES_UI_SESSION_ID" not in env
    assert "HERMES_SESSION_ID" not in env
    assert "HERMES_SESSION_PROFILE" not in env
    assert env["HERMES_SESSION_STALL_TIMEOUT"] == "97"


def test_delivery_env_resolves_the_launch_home_instead_of_failing_closed():
    """A relayed DM into the LAUNCH profile's Bot Chat must spawn its turn.

    ``_profile_home`` answers None for the launch profile *by design* ("already the launch profile,
    no override needed"), and a relay RPC is sessionless so it binds no secret scope. That
    combination left ``served_profile_child_env`` failing closed under multiplex, so every relayed
    DM into a *default* profile died with "Hermes could not read this profile's API key" while
    deliveries to named secondary profiles — which do resolve a home — kept working.
    """
    from agent.secret_scope import (
        current_secret_scope, reset_secret_scope, set_multiplex_active, set_secret_scope)
    from hermes_constants import get_hermes_home_override, get_routing_process_hermes_home

    assert get_hermes_home_override() is None, "precondition: no host home override in this process"
    assert current_secret_scope() is None, "precondition: no scope bound on a sessionless relay RPC"

    set_multiplex_active(True)
    try:
        env = bot_relay.delivery_env(None, None)
        assert Path(env["HERMES_HOME"]).resolve() == Path(get_routing_process_hermes_home()).resolve()

        # A bound scope is still the truth when one exists: it must win over the launch-home fallback.
        token = set_secret_scope({"OPENROUTER_API_KEY": "sentinel-value"})
        try:
            assert bot_relay.delivery_env(None, None)["OPENROUTER_API_KEY"] == "sentinel-value"
        finally:
            reset_secret_scope(token)
    finally:
        set_multiplex_active(False)


def test_delivery_env_launch_fallback_never_carries_a_secondary_profiles_ambient_key(monkeypatch):
    """t_681656e1 checker r1 HIGH: the launch-home fallback must build from the launch env FROZEN
    at multiplex activation, not live ``os.environ``. The launch home is not a routed home, so
    ``served_profile_child_env`` skips its credential scrub; a key a secondary profile's code path
    put into ``os.environ`` after activation would otherwise ride into the launch child."""
    from agent.secret_scope import set_multiplex_active
    from tui_gateway import launch_profile_policy

    monkeypatch.delenv("T681656E1_SECONDARY_ONLY_KEY", raising=False)
    monkeypatch.setattr(launch_profile_policy, "_snapshot", None)
    set_multiplex_active(True)
    try:
        launch_profile_policy.capture_launch_env()  # activation-time freeze (clean)
        monkeypatch.setenv("T681656E1_SECONDARY_ONLY_KEY", "leaked-from-secondary")  # post-activation pollution
        env = bot_relay.delivery_env(None, None)
        assert "T681656E1_SECONDARY_ONLY_KEY" not in env
        assert env.get("HERMES_HOME")
    finally:
        set_multiplex_active(False)


def test_delivery_env_single_profile_process_is_unchanged(monkeypatch):
    """Not multiplexing: no fallback, the child still starts from ``os.environ`` exactly as before."""
    from agent.secret_scope import is_multiplex_active

    assert not is_multiplex_active()
    monkeypatch.setenv("T681656E1_AMBIENT", "kept")
    assert bot_relay.delivery_env(None, None).get("T681656E1_AMBIENT") == "kept"


def test_sidecar_write_failure_does_not_burn_the_claim_or_reoffer(root, monkeypatch):
    """Checker HIGH on e6bd66c00f: an OSError writing the advisory envelope sidecar must not drop the
    claimed envelope from the drain, nor consume its one re-offer without handing it out."""
    import time as _time

    bot_relay.write_remote_roster(root, _rows())
    target = bot_relay.resolve_remote_target("researcher", bot_relay.read_remote_roster(root))
    env = bot_relay.enqueue_envelope(root, target=target, message="hi", sender_profile="w", sender_handle="w")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(bot_relay, "_write_envelope_stall_meta_strict", boom)
    claimed = bot_relay.claim_pending_envelopes(root)
    assert [e["id"] for e in claimed] == [env["id"]]
    # Age the claim past the re-offer window: the one re-offer must still be handed out.
    now = _time.time()
    monkeypatch.setattr(bot_relay.time, "time", lambda: now + bot_relay.REOFFER_AFTER_SECONDS + 5)
    reoffered = bot_relay.claim_pending_envelopes(root)
    assert [e["id"] for e in reoffered] == [env["id"]]
    assert reoffered[0].get("reoffered_at")
    assert bot_relay.claim_pending_envelopes(root) == []
