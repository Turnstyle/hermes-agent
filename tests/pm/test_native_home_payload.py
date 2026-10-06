"""A native home manifest is not a sealed package descriptor."""
import json
from pathlib import Path
import pytest
from pm import environments


@pytest.mark.parametrize("patched_home", [False, True])
def test_native_home_manifest_does_not_redirect_store(tmp_path, monkeypatch, patched_home):
    account = tmp_path / "account"
    home = account / ".hermes"
    repo = home / "hermes-agent"
    repo.mkdir(parents=True)
    (home / "manifest.json").write_text(json.dumps({"repo": "hermes-agent"}))
    monkeypatch.setenv("HOME", str(account))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("HERMES_DATA_DIR_SUFFIX", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "patched" if patched_home else account)
    monkeypatch.setattr(environments, "get_default_hermes_root", lambda: home)
    assert environments.store_root(repo) == home / "tools"


def test_sealed_payload_still_selects_its_store(tmp_path, monkeypatch):
    package = tmp_path / "package"
    repo = package / "repo"
    repo.mkdir(parents=True)
    (package / "manifest.json").write_text(json.dumps({"repo": "repo", "store": "tools"}))
    monkeypatch.setenv("HOME", str(tmp_path / "account"))
    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    assert environments.store_root(repo) == package / "tools"
