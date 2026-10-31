"""Auditing must classify the first durable user row without creating/migrating DBs."""
import hashlib
import json
import sqlite3

import pytest

from hermes_state import SessionDB
from scripts import audit_delegate_rule as audit

RULE = "Use 1 to 10 delegate_task sub-agents and don't do the work inside your main model, and pass this same instruction on to anyone you hand work to."


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("present", "api_server", model="claude-sonnet")
    db.append_message("present", "assistant", "preface")
    db.append_message("present", "user", "quoted " + RULE + " task")
    db.create_session("missing", "subagent", model="claude-opus")
    db.append_message("missing", "user", "original")
    db.append_message("missing", "user", RULE)
    db.create_session("negative", "cli", model="gpt-6.1-sol")
    db.append_message("negative", "user", "original")
    db.close()
    return path


def test_first_row_counts_and_readonly(database, monkeypatch):
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    real_connect = sqlite3.connect
    opened = []
    def checked_connect(database_uri, **kwargs):
        assert database_uri.endswith("?mode=ro") and kwargs["uri"] is True
        conn = real_connect(database_uri, **kwargs)
        opened.append(conn)
        return conn
    monkeypatch.setattr(audit.sqlite3, "connect", checked_connect)
    report = audit.audit_database(database)
    assert (report["present"], report["missing"], report["skipped_nonclaude"]) == (1, 1, 1)
    assert report["by_source"] == {"api_server": {"present": 1, "missing": 0}, "subagent": {"present": 0, "missing": 1}}
    assert report["errors"] == []
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("config", [
    "model: claude-sonnet", "model: {default: claude-sonnet}", "model: {model: claude-sonnet}",
    "model: {provider: anthropic, default: opaque}",
    "model: {default: {provider: anthropic, model: opaque}}",
])
def test_null_model_uses_profile_config(tmp_path, config):
    (tmp_path / "config.yaml").write_text(config)
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("unset", "api_server")
    db.append_message("unset", "user", "task")
    db.close()
    report = audit.audit_database(tmp_path / "state.db")
    assert (report["present"], report["missing"]) == (0, 1)
    assert report["errors"] == []


def test_provider_and_multimodal(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("parts", "desktop", model="opaque", model_config={"provider": "anthropic"})
    db.append_message("parts", "user", json.dumps([{"type": "input_text", "text": RULE}, {"type": "image_url", "image_url": {}}]))
    db.close()
    assert audit.audit_database(tmp_path / "state.db")["present"] == 1


def test_since_and_profile_discovery(database, tmp_path, capsys):
    root = tmp_path / "hermes"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    (profile / "state.db").write_bytes(database.read_bytes())
    assert audit.main(["--all-profiles", str(root), "--since", "99999999999"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["present"] == report["missing"] == 0
    assert len(report["by_profile"]) == 2
    assert report["errors"] and not (root / "state.db").exists()


def test_absent_and_bad_database_are_json_errors(tmp_path, capsys):
    absent, bad = tmp_path / "absent.db", tmp_path / "bad.db"
    bad.write_bytes(b"not sqlite")
    assert audit.main([str(absent), str(bad)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert len(report["errors"]) == 2
    assert not absent.exists()


def test_query_only_is_set(database, monkeypatch):
    real_connect = sqlite3.connect
    checks = []
    class Connection(sqlite3.Connection):
        def close(self):
            checks.append(self.execute("PRAGMA query_only").fetchone()[0])
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                self.execute("CREATE TABLE forbidden (id)")
            super().close()
    monkeypatch.setattr(audit.sqlite3, "connect", lambda *a, **kw: real_connect(*a, factory=Connection, **kw))
    audit.audit_database(database)
    assert checks == [1]


def test_malformed_profile_config_reports_error_without_guess(tmp_path):
    (tmp_path / "config.yaml").write_text("model: [broken")
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("unset", "api_server")
    db.close()
    report = audit.audit_database(tmp_path / "state.db")
    assert report["present"] == report["missing"] == 0
    assert report["errors"][0]["session_id"] == "unset"


@pytest.mark.parametrize("model,provider,config", [
    (None, "anthropic", "model: [broken"),
    ("   ", None, "model: {default: claude-sonnet}"),
])
def test_pin_is_only_consulted_when_route_cannot_decide(tmp_path, model, provider, config):
    (tmp_path / "config.yaml").write_text(config)
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("unset", "api_server", model=model, model_config={"provider": provider})
    db.append_message("unset", "user", "task")
    db.close()
    report = audit.audit_database(tmp_path / "state.db")
    assert (report["present"], report["missing"]) == (0, 1)
    assert report["errors"] == []
