"""Behavioral coverage for private, preservation-only preflight recovery."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.pending_turn_draft import (
    MAX_PENDING_TURN_DRAFT_BYTES,
    MAX_PENDING_TURN_DRAFT_MESSAGE_IDS,
    MAX_PENDING_TURN_DRAFT_RECORD_BYTES,
    PENDING_TURN_DRAFT_PREFIX,
    PENDING_TURN_DRAFT_TTL_SECONDS,
    PENDING_TURN_MANUAL_RESEND_NOTICE,
    PENDING_TURN_REDELIVERY_NOTICE,
    PendingTurnDraftError,
    PendingTurnDraftUnauthorized,
    acknowledge_pending_turn_drafts,
    commit_pending_turn_resend_acknowledgement,
    delete_pending_turn_draft,
    gc_pending_turn_drafts,
    has_pending_turn_draft_tombstone,
    has_unacknowledged_pending_turn_draft,
    link_pending_turn_draft,
    load_pending_turn_draft,
    pending_turn_principal_fingerprint,
    plan_pending_turn_resend_acknowledgement,
    stage_pending_turn_draft,
)
from gateway.session import SessionSource, SessionStore, build_session_key
from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin
from hermes_cli.prompt_stash import PromptStash
from hermes_state import SessionDB


def _source(user_id="U1", *, profile=None):
    return SessionSource(
        platform=Platform.SLACK,
        chat_id="C1",
        chat_type="channel",
        user_id=user_id,
        scope_id="W1",
        profile=profile,
    )


def _stage(
    db,
    *,
    text="finish the deployment",
    user_id="U1",
    session_key="agent:main:slack:channel:W1:C1",
    input_kind="plain_text",
    input_loss_reasons=(),
    platform_message_id="original-message-id",
    platform_message_ids=None,
    now=None,
):
    return stage_pending_turn_draft(
        db,
        text=text,
        predecessor_session_id="old-session",
        session_key=session_key,
        principal_fingerprint=pending_turn_principal_fingerprint(_source(user_id)),
        platform_message_id=platform_message_id,
        platform_message_ids=platform_message_ids,
        input_kind=input_kind,
        input_loss_reasons=input_loss_reasons,
        now=now,
    )


def _unprocessed(text="must survive", *, input_kind="plain_text", loss_reasons=()):
    reasons = list(loss_reasons)
    exact = input_kind == "plain_text" and not reasons
    value = {
        "version": 1,
        "kind": "accepted_user_input",
        "text": text,
        "input_kind": input_kind,
        "exact": exact,
        "lossy": not exact,
        "loss_reasons": reasons,
        "safe_to_replay": exact,
        "provider_call_sent": False,
        "executable": False,
    }
    if "surrogate_normalization" in reasons:
        value["normalization_changed"] = True
    return value


def _preflight_result(text="must survive", *, input_kind="plain_text", loss_reasons=()):
    return {
        "failed": True,
        "compression_exhausted": True,
        "turn_exit_reason": "context_compression_timeout",
        "api_calls": 0,
        "unprocessed_input": _unprocessed(
            text, input_kind=input_kind, loss_reasons=loss_reasons
        ),
    }


def _gateway_with_store(store, config):
    from gateway.run import GatewayRunner

    gateway = GatewayRunner.__new__(GatewayRunner)
    gateway.config = config
    gateway.session_store = store
    gateway._evict_cached_agent = MagicMock()
    gateway._clear_conversation_scope = MagicMock()
    gateway._sync_telegram_topic_binding = MagicMock()
    return gateway


def _only_pending_draft(db):
    rows = db.list_meta_prefix(PENDING_TURN_DRAFT_PREFIX)
    assert len(rows) == 1
    key, _raw = rows[0]
    return load_pending_turn_draft(db, key.removeprefix(PENDING_TURN_DRAFT_PREFIX))


def test_record_is_non_executable_and_survives_database_reopen(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("old-session", source="slack")
    draft = _stage(db)

    assert draft.executable is False
    assert draft.disposition == "manual_resend_only"
    assert draft.input_kind == "plain_text"
    assert draft.exact is True
    assert draft.lossy is False
    assert draft.loss_reasons == []
    assert db.get_messages("old-session", include_inactive=True) == []
    db.close()

    reopened = SessionDB(path)
    restored = load_pending_turn_draft(reopened, draft.draft_id)
    assert restored == draft
    assert reopened.get_messages("old-session", include_inactive=True) == []
    assert len(reopened.list_meta_prefix(PENDING_TURN_DRAFT_PREFIX)) == 1
    reopened.close()


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        (
            {"input_kind": "structured"},
            "structured_components_not_retained",
        ),
        (
            {"input_kind": "native_image"},
            "attachments_not_retained",
        ),
        (
            {"input_kind": "moa"},
            "per_turn_semantics_not_retained",
        ),
        (
            {"text": "continue \ud800"},
            "surrogate_normalization",
        ),
        (
            {"text": "use sk-abcdefghijklmnopqrstuvwxyz1234567890"},
            "redaction",
        ),
        (
            {"text": "x" * (MAX_PENDING_TURN_DRAFT_BYTES + 100)},
            "truncation",
        ),
    ],
)
def test_lossy_input_is_truthfully_classified_and_bounded(tmp_path, kwargs, reason):
    db = SessionDB(tmp_path / "state.db")
    draft = _stage(db, **kwargs)

    assert draft.executable is False
    assert draft.exact is False
    assert draft.lossy is True
    assert reason in draft.loss_reasons
    assert draft.stored_bytes <= MAX_PENDING_TURN_DRAFT_BYTES
    if reason == "redaction":
        assert "sk-abcdefghijklmnopqrstuvwxyz1234567890" not in draft.text
    db.close()


def test_empty_post_redaction_text_is_not_persisted(tmp_path, monkeypatch):
    import agent.redact as redact_module

    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(
        redact_module,
        "redact_sensitive_text",
        lambda *_args, **_kwargs: " \n\t ",
    )

    with pytest.raises(PendingTurnDraftError, match="empty after redaction"):
        _stage(db, text="accepted secret-bearing request")

    assert db.list_meta_prefix(PENDING_TURN_DRAFT_PREFIX) == []
    db.close()


def test_same_session_stages_are_distinct_live_generations(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    first = _stage(db, text="first private message")
    first = link_pending_turn_draft(
        db,
        first.draft_id,
        predecessor_session_id="old-session",
        successor_session_id="first-successor",
    )
    second = _stage(db, text="second private message")

    rows = db.list_meta_prefix(PENDING_TURN_DRAFT_PREFIX)
    assert len(rows) == 2
    assert {key for key, _raw in rows} == {
        f"{PENDING_TURN_DRAFT_PREFIX}{first.draft_id}",
        f"{PENDING_TURN_DRAFT_PREFIX}{second.draft_id}",
    }
    assert load_pending_turn_draft(db, first.draft_id) == first
    assert load_pending_turn_draft(db, second.draft_id).text == "second private message"
    db.close()


def test_any_generation_tombstones_every_constituent_id_after_pointer_rotation(
    tmp_path,
):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    session_key = "agent:main:slack:channel:W1:C1"
    principal = pending_turn_principal_fingerprint(_source("U1"))
    first = _stage(
        db,
        session_key=session_key,
        platform_message_id="batch-primary",
        platform_message_ids=("batch-part-1", "batch-part-2", "batch-primary"),
    )
    link_pending_turn_draft(
        db,
        first.draft_id,
        predecessor_session_id="old-session",
        successor_session_id="successor-before-later-reset",
    )
    second = _stage(
        db,
        text="newer generation",
        session_key=session_key,
        platform_message_id="newer-primary",
        platform_message_ids=("newer-part",),
    )
    link_pending_turn_draft(
        db,
        second.draft_id,
        predecessor_session_id="old-session",
        successor_session_id="successor-after-pointer-rotation",
    )

    assert first.platform_message_id == "batch-primary"
    assert first.platform_message_ids == [
        "batch-primary",
        "batch-part-1",
        "batch-part-2",
    ]
    for message_id in (*first.platform_message_ids, *second.platform_message_ids):
        assert has_pending_turn_draft_tombstone(
            db,
            session_key=session_key,
            principal_fingerprint=principal,
            platform_message_id=message_id,
        )
    assert not has_pending_turn_draft_tombstone(
        db,
        session_key=session_key,
        principal_fingerprint=principal,
        platform_message_id="distinct-manual-resend",
    )
    assert not has_pending_turn_draft_tombstone(
        db,
        session_key=session_key,
        principal_fingerprint=pending_turn_principal_fingerprint(_source("U2")),
        platform_message_id="batch-part-1",
    )
    assert not has_pending_turn_draft_tombstone(
        db,
        session_key="agent:main:slack:channel:W1:other",
        principal_fingerprint=principal,
        platform_message_id="batch-part-1",
    )
    db.close()

    reopened = SessionDB(path)
    assert has_pending_turn_draft_tombstone(
        reopened,
        session_key=session_key,
        principal_fingerprint=principal,
        platform_message_id="batch-part-2",
    )
    reopened.close()


def test_platform_message_id_sequence_is_bounded_and_strict(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    deduplicated = _stage(
        db,
        platform_message_id="primary",
        platform_message_ids=("primary", "secondary", "secondary"),
    )
    assert deduplicated.platform_message_ids == ["primary", "secondary"]

    with pytest.raises(PendingTurnDraftError, match="too many platform message ids"):
        _stage(
            db,
            platform_message_id=None,
            platform_message_ids=tuple(
                f"message-{index}"
                for index in range(MAX_PENDING_TURN_DRAFT_MESSAGE_IDS + 1)
            ),
        )
    with pytest.raises(PendingTurnDraftError, match="platform message id is invalid"):
        _stage(
            db,
            platform_message_id=None,
            platform_message_ids=("valid", " padded "),
        )
    db.close()


def test_tombstone_scan_skips_malformed_and_oversized_rows_without_selecting_text(
    tmp_path,
):
    db = SessionDB(tmp_path / "state.db")
    session_key = "agent:main:slack:channel:W1:C1"
    principal = pending_turn_principal_fingerprint(_source("U1"))
    valid = _stage(db, session_key=session_key, platform_message_id="valid-id")
    db.set_meta(f"{PENDING_TURN_DRAFT_PREFIX}malformed", "{")
    valid_key = f"{PENDING_TURN_DRAFT_PREFIX}{valid.draft_id}"
    raw = json.loads(db.list_meta_prefix(valid_key)[0][1])
    raw["untrusted_padding"] = "x" * MAX_PENDING_TURN_DRAFT_RECORD_BYTES
    db.set_meta(valid_key, json.dumps(raw))

    class ReadSpy:
        def _read_all(self, sql, params):
            assert "SELECT key, value" not in sql
            assert "json_extract(value, '$.text')," not in sql
            return db._read_all(sql, params)

    assert not has_pending_turn_draft_tombstone(
        ReadSpy(),
        session_key=session_key,
        principal_fingerprint=principal,
        platform_message_id="valid-id",
    )
    db.close()


def test_metadata_scan_deletes_overflow_poison_before_counting_live_rows(
    tmp_path, monkeypatch
):
    import gateway.pending_turn_draft as pending_module

    db = SessionDB(tmp_path / "state.db")
    started = datetime(2026, 9, 12, tzinfo=timezone.utc)
    session_key = "agent:main:slack:channel:W1:C1"
    principal = pending_turn_principal_fingerprint(_source("U1"))
    first = _stage(
        db,
        session_key=session_key,
        platform_message_id="first-id",
        now=started,
    )
    _stage(
        db,
        text="second generation",
        session_key=session_key,
        platform_message_id="second-id",
        now=started + timedelta(seconds=1),
    )
    malformed_keys = (
        f"{PENDING_TURN_DRAFT_PREFIX}!malformed-before",
        f"{PENDING_TURN_DRAFT_PREFIX}z-malformed-after",
    )
    for malformed_key in malformed_keys:
        db.set_meta(malformed_key, "{")
    monkeypatch.setattr(pending_module, "MAX_PENDING_TURN_DRAFT_RECORDS", 2)

    assert has_pending_turn_draft_tombstone(
        db,
        session_key=session_key,
        principal_fingerprint=principal,
        platform_message_id=first.platform_message_id,
        now=started + timedelta(seconds=2),
    )
    assert all(db.get_meta(key) is None for key in malformed_keys)
    assert len(db.list_meta_prefix(PENDING_TURN_DRAFT_PREFIX)) == 2
    assert has_unacknowledged_pending_turn_draft(
        db,
        session_key=session_key,
        principal_fingerprint=principal,
        now=started + timedelta(seconds=2),
    )
    db.close()


def test_tombstone_scan_is_bounded_and_db_errors_are_typed(tmp_path, monkeypatch):
    import gateway.pending_turn_draft as pending_module

    db = SessionDB(tmp_path / "state.db")
    principal = pending_turn_principal_fingerprint(_source("U1"))
    for index in range(3):
        _stage(
            db,
            session_key=f"agent:main:slack:channel:W1:C{index}",
            platform_message_id=f"message-{index}",
        )
    monkeypatch.setattr(pending_module, "MAX_PENDING_TURN_DRAFT_RECORDS", 2)
    with pytest.raises(PendingTurnDraftError, match="capacity"):
        has_pending_turn_draft_tombstone(
            db,
            session_key="agent:main:slack:channel:W1:C1",
            principal_fingerprint=principal,
            platform_message_id="message-id",
        )

    class Unreadable:
        def _read_all(self, _sql, _params):
            raise OSError("database offline")

    with pytest.raises(PendingTurnDraftError, match="could not be read"):
        has_pending_turn_draft_tombstone(
            Unreadable(),
            session_key="agent:main:slack:channel:W1:C1",
            principal_fingerprint=principal,
            platform_message_id="message-id",
        )
    db.close()


def test_acknowledgement_is_atomic_hashed_and_keeps_old_tombstones(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    started = datetime(2026, 9, 12, tzinfo=timezone.utc)
    first_acknowledged_at = started + timedelta(minutes=1)
    second_acknowledged_at = started + timedelta(minutes=2)
    session_key = "agent:main:slack:channel:W1:C1"
    principal = pending_turn_principal_fingerprint(_source("U1"))
    first = _stage(
        db,
        session_key=session_key,
        platform_message_id="old-primary",
        platform_message_ids=("old-constituent",),
        now=started,
    )
    second = _stage(
        db,
        text="second generation",
        session_key=session_key,
        platform_message_id="other-old-id",
        now=started,
    )
    assert has_unacknowledged_pending_turn_draft(
        db,
        session_key=session_key,
        principal_fingerprint=principal,
        now=started,
    )

    assert (
        acknowledge_pending_turn_drafts(
            db,
            session_key=session_key,
            principal_fingerprint=principal,
            manual_message_ids=("manual-primary", "manual-constituent"),
            now=first_acknowledged_at,
        )
        == 1
    )
    assert has_unacknowledged_pending_turn_draft(
        db,
        session_key=session_key,
        principal_fingerprint=principal,
        now=first_acknowledged_at,
    )
    oldest, remaining = sorted(
        (first, second), key=lambda draft: (draft.created_at, draft.draft_id)
    )
    restored_oldest = load_pending_turn_draft(
        db, oldest.draft_id, now=first_acknowledged_at
    )
    restored_remaining = load_pending_turn_draft(
        db, remaining.draft_id, now=first_acknowledged_at
    )
    assert (
        restored_oldest.manual_resend_acknowledged_at
        == first_acknowledged_at.isoformat()
    )
    assert len(restored_oldest.manual_resend_message_id_hash or "") == 64
    assert restored_remaining.manual_resend_acknowledged_at is None
    assert restored_remaining.manual_resend_message_id_hash is None
    assert (
        acknowledge_pending_turn_drafts(
            db,
            session_key=session_key,
            principal_fingerprint=principal,
            manual_message_ids=("manual-primary", "manual-constituent"),
            now=first_acknowledged_at,
        )
        == 0
    )
    assert (
        load_pending_turn_draft(
            db, remaining.draft_id, now=first_acknowledged_at
        ).manual_resend_acknowledged_at
        is None
    )
    raw = db.list_meta_prefix(f"{PENDING_TURN_DRAFT_PREFIX}{oldest.draft_id}")[0][1]
    assert "manual-primary" not in raw
    assert "manual-constituent" not in raw
    for original_id in ("old-primary", "old-constituent", "other-old-id"):
        assert has_pending_turn_draft_tombstone(
            db,
            session_key=session_key,
            principal_fingerprint=principal,
            platform_message_id=original_id,
            now=first_acknowledged_at,
        )
    assert (
        acknowledge_pending_turn_drafts(
            db,
            session_key=session_key,
            principal_fingerprint=principal,
            manual_message_ids=("another-manual-id",),
            now=second_acknowledged_at,
        )
        == 1
    )
    assert not has_unacknowledged_pending_turn_draft(
        db,
        session_key=session_key,
        principal_fingerprint=principal,
        now=second_acknowledged_at,
    )
    for draft in (first, second):
        restored = load_pending_turn_draft(
            db, draft.draft_id, now=second_acknowledged_at
        )
        assert restored.manual_resend_acknowledged_at is not None
        assert len(restored.manual_resend_message_id_hash or "") == 64
    db.close()

    reopened = SessionDB(path)
    assert not has_unacknowledged_pending_turn_draft(
        reopened,
        session_key=session_key,
        principal_fingerprint=principal,
        now=second_acknowledged_at,
    )
    assert has_pending_turn_draft_tombstone(
        reopened,
        session_key=session_key,
        principal_fingerprint=principal,
        platform_message_id="other-old-id",
        now=second_acknowledged_at,
    )
    reopened.close()


def test_one_manual_resend_preserves_the_next_lost_turn_obligation(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    started = datetime(2026, 9, 12, tzinfo=timezone.utc)
    session_key = "agent:main:slack:channel:W1:C1"
    principal = pending_turn_principal_fingerprint(_source("U1"))
    lost_a = _stage(
        db,
        text="lost turn A",
        session_key=session_key,
        platform_message_id="lost-a-id",
        now=started,
    )
    lost_b = _stage(
        db,
        text="lost turn B",
        session_key=session_key,
        platform_message_id="lost-b-id",
        now=started + timedelta(seconds=1),
    )
    resent_at = started + timedelta(seconds=2)

    assert (
        acknowledge_pending_turn_drafts(
            db,
            session_key=session_key,
            principal_fingerprint=principal,
            manual_message_ids=("manual-resend-a",),
            now=resent_at,
        )
        == 1
    )
    assert load_pending_turn_draft(
        db, lost_a.draft_id, now=resent_at
    ).manual_resend_acknowledged_at == resent_at.isoformat()
    assert (
        load_pending_turn_draft(
            db, lost_b.draft_id, now=resent_at
        ).manual_resend_acknowledged_at
        is None
    )
    assert has_unacknowledged_pending_turn_draft(
        db,
        session_key=session_key,
        principal_fingerprint=principal,
        now=resent_at,
    )
    db.close()


def test_resend_plan_rejects_changed_identity_and_commits_idempotently(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    session_key = "agent:main:slack:channel:W1:C1"
    principal = pending_turn_principal_fingerprint(_source("U1"))
    draft = _stage(
        db,
        session_key=session_key,
        platform_message_id="lost-delivery",
    )
    acknowledgement, replayed = plan_pending_turn_resend_acknowledgement(
        db,
        session_key=session_key,
        principal_fingerprint=principal,
        manual_message_ids=("manual-delivery",),
    )

    assert acknowledgement is not None
    assert replayed is False
    with pytest.raises(PendingTurnDraftUnauthorized, match="identity changed"):
        commit_pending_turn_resend_acknowledgement(
            db,
            acknowledgement,
            session_key=session_key,
            principal_fingerprint=principal,
            manual_message_ids=("different-delivery",),
        )
    assert load_pending_turn_draft(
        db, draft.draft_id
    ).manual_resend_acknowledged_at is None

    assert commit_pending_turn_resend_acknowledgement(
        db,
        acknowledgement,
        session_key=session_key,
        principal_fingerprint=principal,
        manual_message_ids=("manual-delivery",),
    )
    assert not commit_pending_turn_resend_acknowledgement(
        db,
        acknowledgement,
        session_key=session_key,
        principal_fingerprint=principal,
        manual_message_ids=("manual-delivery",),
    )
    db.close()


def test_old_delivery_id_never_acknowledges_pending_generations(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    started = datetime(2026, 9, 12, tzinfo=timezone.utc)
    session_key = "agent:main:slack:channel:W1:C1"
    principal = pending_turn_principal_fingerprint(_source("U1"))
    _stage(
        db,
        session_key=session_key,
        platform_message_id="old-primary",
        platform_message_ids=("old-constituent",),
        now=started,
    )

    assert (
        acknowledge_pending_turn_drafts(
            db,
            session_key=session_key,
            principal_fingerprint=principal,
            manual_message_ids=("old-constituent",),
            now=started + timedelta(seconds=1),
        )
        == 0
    )
    assert has_unacknowledged_pending_turn_draft(
        db,
        session_key=session_key,
        principal_fingerprint=principal,
        now=started + timedelta(seconds=1),
    )
    db.close()


def test_unacknowledged_scan_atomically_gcs_expired_and_malformed_rows(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    started = datetime(2020, 1, 1, tzinfo=timezone.utc)
    session_key = "agent:main:slack:channel:W1:C1"
    principal = pending_turn_principal_fingerprint(_source("U1"))
    _stage(db, session_key=session_key, now=started)
    db.set_meta(f"{PENDING_TURN_DRAFT_PREFIX}malformed", "{")

    assert not has_unacknowledged_pending_turn_draft(
        db,
        session_key=session_key,
        principal_fingerprint=principal,
        now=started + timedelta(seconds=PENDING_TURN_DRAFT_TTL_SECONDS + 1),
    )
    assert db.list_meta_prefix(PENDING_TURN_DRAFT_PREFIX) == []
    db.close()


def test_expired_record_is_lazily_deleted(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    started = datetime(2026, 9, 12, tzinfo=timezone.utc)
    draft = _stage(db, now=started)

    with pytest.raises(PendingTurnDraftError):
        load_pending_turn_draft(
            db,
            draft.draft_id,
            now=started + timedelta(seconds=PENDING_TURN_DRAFT_TTL_SECONDS + 1),
        )
    assert db.list_meta_prefix(PENDING_TURN_DRAFT_PREFIX) == []
    db.close()


def test_capacity_counts_each_generation_even_on_the_same_route(tmp_path, monkeypatch):
    import gateway.pending_turn_draft as pending_module

    monkeypatch.setattr(pending_module, "MAX_PENDING_TURN_DRAFT_RECORDS", 2)
    db = SessionDB(tmp_path / "state.db")
    one = _stage(db, session_key="agent:main:local:dm:one")
    _stage(db, session_key="agent:main:local:dm:two")

    with pytest.raises(PendingTurnDraftError, match="capacity"):
        _stage(db, session_key="agent:main:local:dm:three")
    with pytest.raises(PendingTurnDraftError, match="capacity"):
        _stage(db, text="replacement", session_key="agent:main:local:dm:one")

    assert len(db.list_meta_prefix(PENDING_TURN_DRAFT_PREFIX)) == 2
    assert load_pending_turn_draft(db, one.draft_id).text == "finish the deployment"
    db.close()


@pytest.mark.parametrize("message_id", [None, "", "   "])
def test_stage_refuses_missing_or_blank_platform_message_id(tmp_path, message_id):
    db = SessionDB(tmp_path / "state.db")

    with pytest.raises(PendingTurnDraftError, match="message id is required"):
        stage_pending_turn_draft(
            db,
            text="must not become replayable",
            predecessor_session_id="old-session",
            session_key="agent:main:signal:dm:recipient",
            principal_fingerprint=pending_turn_principal_fingerprint(_source()),
            platform_message_id=message_id,
        )

    assert db.list_meta_prefix(PENDING_TURN_DRAFT_PREFIX) == []
    db.close()


def test_whatsapp_fingerprint_matches_session_routing_across_jid_lid_aliases(
    tmp_path, monkeypatch
):
    phone = "15551234567"
    lid = "999999999999999"
    mapping_dir = tmp_path / "whatsapp" / "session"
    mapping_dir.mkdir(parents=True)
    (mapping_dir / f"lid-mapping-{lid}.json").write_text(
        json.dumps(f"{phone}@s.whatsapp.net"), encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    phone_source = SessionSource(
        platform=Platform.WHATSAPP,
        chat_id=f"{phone}@s.whatsapp.net",
        chat_type="dm",
        user_id=f"{phone}@s.whatsapp.net",
    )
    lid_source = SessionSource(
        platform=Platform.WHATSAPP,
        chat_id=f"{lid}@lid",
        chat_type="dm",
        user_id=f"{lid}@lid",
    )

    assert build_session_key(phone_source) == build_session_key(lid_source)
    assert pending_turn_principal_fingerprint(phone_source) == (
        pending_turn_principal_fingerprint(lid_source)
    )


def test_exact_generation_delete_and_malformed_poison_gc(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    first = _stage(db, text="first generation")
    second = _stage(db, text="second generation")

    assert delete_pending_turn_draft(db, first.draft_id) is True
    assert delete_pending_turn_draft(db, first.draft_id) is False
    assert load_pending_turn_draft(db, second.draft_id).text == "second generation"

    key = f"{PENDING_TURN_DRAFT_PREFIX}{second.draft_id}"
    raw = dict(json.loads(db.list_meta_prefix(key)[0][1]))
    raw["text"] = 42
    db.set_meta(key, json.dumps(raw))

    assert gc_pending_turn_drafts(db) == 1
    assert gc_pending_turn_drafts(db) == 0
    assert db.list_meta_prefix(PENDING_TURN_DRAFT_PREFIX) == []
    assert _stage(db, text="after cleanup").text == "after cleanup"
    db.close()


@pytest.mark.asyncio
async def test_preflight_input_is_staged_before_reset_as_route_independent_record(
    tmp_path, monkeypatch
):
    import gateway.pending_turn_draft as pending_module
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    config = GatewayConfig()
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
    source = _source()
    old = store.get_or_create_session(source)
    key = old.session_key
    order = []
    original_stage = pending_module.stage_pending_turn_draft
    original_reset = store.reset_session

    def stage_spy(*args, **kwargs):
        order.append("stage")
        return original_stage(*args, **kwargs)

    def reset_spy(*args, **kwargs):
        order.append("reset")
        return original_reset(*args, **kwargs)

    monkeypatch.setattr(pending_module, "stage_pending_turn_draft", stage_spy)
    store.reset_session = MagicMock(side_effect=reset_spy)
    gateway = _gateway_with_store(store, config)

    response, new = await gateway._hmwa_compression_exhaustion_reset(
        _preflight_result(),
        "compression timed out",
        old,
        key,
        source,
        event=MessageEvent(text="must survive", source=source, message_id="m1"),
    )

    assert order[:2] == ["stage", "reset"]
    assert new.session_id != old.session_id
    assert "pending_turn_draft_id" not in new.metadata
    draft = _only_pending_draft(store._db_for_key(key))
    assert draft.successor_session_id == new.session_id
    assert draft.platform_message_id == "m1"
    assert draft.executable is False
    assert "/retry" not in response
    assert "will not replay" in response
    assert (
        "Attachments and one-shot or per-turn semantics were not retained" in response
    )
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_preservation_failure_refuses_reset(tmp_path, monkeypatch):
    import gateway.pending_turn_draft as pending_module
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    config = GatewayConfig()
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
    source = _source()
    old = store.get_or_create_session(source)
    key = old.session_key
    reset_spy = MagicMock(wraps=store.reset_session)
    store.reset_session = reset_spy
    monkeypatch.setattr(
        pending_module,
        "stage_pending_turn_draft",
        MagicMock(side_effect=OSError("write refused")),
    )
    gateway = _gateway_with_store(store, config)

    response, returned = await gateway._hmwa_compression_exhaustion_reset(
        _preflight_result(),
        "compression timed out",
        old,
        key,
        source,
        event=MessageEvent(text="must survive", source=source, message_id="m1"),
    )

    reset_spy.assert_not_called()
    assert returned.session_id == old.session_id
    assert "reset was refused" in response
    assert store.peek_session_id(key) == old.session_id
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_reset_failure_rolls_back_staged_generation(tmp_path, monkeypatch):
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    config = GatewayConfig()
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
    source = _source()
    old = store.get_or_create_session(source)
    key = old.session_key
    store.reset_session = MagicMock(side_effect=OSError("reset refused"))
    gateway = _gateway_with_store(store, config)

    response, returned = await gateway._hmwa_compression_exhaustion_reset(
        _preflight_result("saved before reset failure"),
        "compression timed out",
        old,
        key,
        source,
        event=MessageEvent(text="saved", source=source, message_id="m1"),
    )

    assert returned.session_id == old.session_id
    assert "reset did not complete" in response
    rows = store._db_for_key(key).list_meta_prefix(PENDING_TURN_DRAFT_PREFIX)
    assert rows == []
    assert old.metadata.get("pending_turn_draft_id") is None
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_link_failure_keeps_route_independent_manual_only_record(
    tmp_path, monkeypatch
):
    import gateway.pending_turn_draft as pending_module
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    config = GatewayConfig()
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
    source = _source()
    old = store.get_or_create_session(source)
    key = old.session_key
    monkeypatch.setattr(
        pending_module,
        "link_pending_turn_draft",
        MagicMock(side_effect=OSError("backlink refused")),
    )
    gateway = _gateway_with_store(store, config)

    response, new = await gateway._hmwa_compression_exhaustion_reset(
        _preflight_result(),
        "compression timed out",
        old,
        key,
        source,
        event=MessageEvent(text="must survive", source=source, message_id="m1"),
    )

    assert "pending_turn_draft_id" not in new.metadata
    draft = _only_pending_draft(store._db_for_key(key))
    assert draft.successor_session_id is None
    assert draft.executable is False
    assert "will not replay" in response
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_retry_is_owner_generic_but_unrelated_principal_gets_normal_path(
    tmp_path, monkeypatch
):
    import gateway.pending_turn_draft as pending_module
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    config = GatewayConfig()
    config.group_sessions_per_user = False
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
    owner = _source("U1")
    intruder = _source("U2")
    old = store.get_or_create_session(owner)
    key = old.session_key
    draft = stage_pending_turn_draft(
        store._db_for_key(key),
        text="owner-only request text",
        predecessor_session_id=old.session_id,
        session_key=key,
        principal_fingerprint=pending_turn_principal_fingerprint(owner),
        platform_message_id="owner-original-id",
    )
    new = store.reset_session(key)
    link_pending_turn_draft(
        store._db_for_key(key),
        draft.draft_id,
        predecessor_session_id=old.session_id,
        successor_session_id=new.session_id,
    )
    before_raw = store._db_for_key(key).list_meta_prefix(PENDING_TURN_DRAFT_PREFIX)
    gateway = _gateway_with_store(store, config)
    gateway._handle_message = AsyncMock()
    content_loader = MagicMock(
        side_effect=AssertionError("/retry must not load recovery text")
    )
    monkeypatch.setattr(pending_module, "load_pending_turn_draft", content_loader)

    owner_result = await gateway._handle_retry_command(
        MessageEvent(
            text="/retry",
            message_type=MessageType.COMMAND,
            source=owner,
            message_id="retry-1",
        )
    )
    intruder_result = await gateway._handle_retry_command(
        MessageEvent(
            text="/retry",
            message_type=MessageType.COMMAND,
            source=intruder,
            message_id="retry-2",
        )
    )

    assert owner_result == PENDING_TURN_MANUAL_RESEND_NOTICE
    assert intruder_result == "No previous message to retry."
    assert intruder_result != owner_result
    assert "owner-only request text" not in owner_result
    gateway._handle_message.assert_not_awaited()
    content_loader.assert_not_called()
    assert (
        store._db_for_key(key).list_meta_prefix(PENDING_TURN_DRAFT_PREFIX) == before_raw
    )
    assert store.get_session_metadata(key, "pending_turn_draft_id") is None
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_same_id_redelivery_stops_before_prepare_but_new_id_passes(
    tmp_path, monkeypatch
):
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    config = GatewayConfig()
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
    source = _source()
    old = store.get_or_create_session(source)
    key = old.session_key
    draft = stage_pending_turn_draft(
        store._db_for_key(key),
        text="accepted but not run",
        predecessor_session_id=old.session_id,
        session_key=key,
        principal_fingerprint=pending_turn_principal_fingerprint(source),
        platform_message_id="original-id",
    )
    new = store.reset_session(key)
    link_pending_turn_draft(
        store._db_for_key(key),
        draft.draft_id,
        predecessor_session_id=old.session_id,
        successor_session_id=new.session_id,
    )
    gateway = _gateway_with_store(store, config)
    gateway._hmwa_resolve_session = AsyncMock(return_value=(source, new, key))
    gateway._hmwa_prepare_turn = AsyncMock(return_value=("normal pipeline", []))
    gateway._run_agent = AsyncMock()

    refused = await gateway._handle_message_with_agent(
        MessageEvent(
            text="accepted but not run", source=source, message_id="original-id"
        ),
        source,
        key,
        1,
    )
    assert refused == PENDING_TURN_REDELIVERY_NOTICE
    gateway._hmwa_prepare_turn.assert_not_awaited()
    gateway._run_agent.assert_not_awaited()

    passed = await gateway._handle_message_with_agent(
        MessageEvent(text="accepted but not run", source=source, message_id="new-id"),
        source,
        key,
        2,
    )
    assert passed == "normal pipeline"
    gateway._hmwa_prepare_turn.assert_awaited_once()
    gateway._run_agent.assert_not_awaited()
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_different_shared_room_principal_is_not_caught_by_tombstone(
    tmp_path, monkeypatch
):
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    config = GatewayConfig()
    config.group_sessions_per_user = False
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
    owner = _source("U1")
    other = _source("U2")
    old = store.get_or_create_session(owner)
    key = old.session_key
    draft = stage_pending_turn_draft(
        store._db_for_key(key),
        text="owner input",
        predecessor_session_id=old.session_id,
        session_key=key,
        principal_fingerprint=pending_turn_principal_fingerprint(owner),
        platform_message_id="shared-platform-id",
    )
    new = store.reset_session(key)
    link_pending_turn_draft(
        store._db_for_key(key),
        draft.draft_id,
        predecessor_session_id=old.session_id,
        successor_session_id=new.session_id,
    )
    gateway = _gateway_with_store(store, config)

    notice = await gateway._hmwa_pending_turn_redelivery_notice(
        MessageEvent(text="other input", source=other, message_id="shared-platform-id"),
        other,
        new,
        key,
    )

    assert notice is None
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_expired_recovery_record_no_longer_blocks_a_new_delivery(
    tmp_path, monkeypatch
):
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    config = GatewayConfig()
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
    source = _source()
    old = store.get_or_create_session(source)
    key = old.session_key
    staged_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    draft = stage_pending_turn_draft(
        store._db_for_key(key),
        text="old accepted input",
        predecessor_session_id=old.session_id,
        session_key=key,
        principal_fingerprint=pending_turn_principal_fingerprint(source),
        platform_message_id="old-id",
        now=staged_at,
    )
    new = store.reset_session(key)
    link_pending_turn_draft(
        store._db_for_key(key),
        draft.draft_id,
        predecessor_session_id=old.session_id,
        successor_session_id=new.session_id,
        now=staged_at,
    )
    gateway = _gateway_with_store(store, config)

    notice = await gateway._hmwa_pending_turn_redelivery_notice(
        MessageEvent(text="manual resend", source=source, message_id="new-id"),
        source,
        new,
        key,
    )

    assert notice is None
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_profile_recovery_uses_profile_db_and_never_root(tmp_path, monkeypatch):
    import hermes_cli.profiles as profiles
    import hermes_state

    root_db_path = tmp_path / "root" / "state.db"
    profile_home = tmp_path / "profiles" / "marshal"
    profile_home.mkdir(parents=True)
    root_db_path.parent.mkdir(parents=True)
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", root_db_path)
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "marshal")
    monkeypatch.setattr(profiles, "get_profile_dir", lambda _name: profile_home)
    config = GatewayConfig()
    config.multiplex_profiles = True
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
    source = _source(profile="marshal")
    old = store.get_or_create_session(source)
    key = old.session_key
    assert key.startswith("agent:marshal:")
    gateway = _gateway_with_store(store, config)

    _, new = await gateway._hmwa_compression_exhaustion_reset(
        _preflight_result(),
        "compression timed out",
        old,
        key,
        source,
        event=MessageEvent(text="must survive", source=source, message_id="m1"),
    )

    profile_db = store._db_for_key(key)
    assert "pending_turn_draft_id" not in new.metadata
    assert _only_pending_draft(profile_db).text == "must survive"
    assert store._db.list_meta_prefix(PENDING_TURN_DRAFT_PREFIX) == []
    assert profile_db.db_path == profile_home / "state.db"
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_generic_later_exhaustion_resets_without_recovery_record(
    tmp_path, monkeypatch
):
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    config = GatewayConfig()
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
    source = _source()
    old = store.get_or_create_session(source)
    key = old.session_key
    gateway = _gateway_with_store(store, config)

    _, new = await gateway._hmwa_compression_exhaustion_reset(
        {
            "failed": True,
            "compression_exhausted": True,
            "turn_exit_reason": "context_compression_exhausted",
            "api_calls": 1,
        },
        "cannot compress",
        old,
        key,
        source,
    )

    assert new.session_id != old.session_id
    assert new.metadata == {}
    assert store._db_for_key(key).list_meta_prefix(PENDING_TURN_DRAFT_PREFIX) == []
    store.close_all_db_handles()


def test_classic_cli_only_projects_exact_plain_text_to_editable_stash():
    cli = CLIChatTurnMixin()
    cli._prompt_stash = PromptStash()
    cli._pending_input = MagicMock()
    result = _preflight_result("edit me before resubmitting")
    result["recovery_persisted"] = True
    result["unprocessed_input"].update(
        record_version=1,
        admission_id="cli-integration-recovery",
        surface_key="cli:test-surface",
        status="pending",
        accepted_input_durably_committed=True,
    )
    turn = SimpleNamespace(result=result)

    assert cli._chat_stash_unprocessed_input(turn) is True
    assert cli._prompt_stash.peek().text == "edit me before resubmitting"
    cli._pending_input.put.assert_not_called()


def test_classic_cli_refuses_lossy_manual_recovery_projection():
    cli = CLIChatTurnMixin()
    cli._prompt_stash = PromptStash()
    result = _preflight_result(
        "[Native-image input marker]",
        input_kind="native_image",
        loss_reasons=("attachments_not_retained",),
    )
    result["recovery_persisted"] = True
    turn = SimpleNamespace(
        result=result
    )

    assert cli._chat_stash_unprocessed_input(turn) is False
    assert len(cli._prompt_stash) == 0
