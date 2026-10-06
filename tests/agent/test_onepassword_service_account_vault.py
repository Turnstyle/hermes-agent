"""Credential-free CLI regression for service-account 1Password item resolution."""
import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.vault_backends.onepassword import OnePasswordLoginBackend


_FAKE_OP = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root = Path(os.environ["HOME"])
args = sys.argv[1:]
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\n")
items = json.loads((root / "items.json").read_text())
if args[:2] == ["vault", "list"]:
    ids = sorted({(i.get("vault") or {}).get("id") for i in items if (i.get("vault") or {}).get("id")})
    print(json.dumps([{"id": v} for v in ids])); sys.exit(0)
if args[:2] == ["item", "list"]:
    if "--vault" in args:
        vault = args[args.index("--vault") + 1]
        items = [i for i in items if (i.get("vault") or {}).get("id") == vault]
    print(json.dumps(items)); sys.exit(0)
if args[:2] != ["item", "get"]:
    sys.exit(2)
if os.environ.get("OP_SERVICE_ACCOUNT_TOKEN") and "--vault" not in args:
    sys.stderr.write("a vault query must be provided when this command is called by a service account")
    sys.exit(1)
item_id = args[2]
vault = args[args.index("--vault") + 1] if "--vault" in args else None
matches = [i for i in items if i.get("id") == item_id and
           (vault is None or (i.get("vault") or {}).get("id") == vault)]
if len(matches) != 1:
    sys.stderr.write("fixture mismatch; fixture-only-password; 102938; fixture-only-token")
    sys.exit(1)
if matches[0].get("error"):
    sys.stderr.write("fixture-only-password; 102938; fixture-only-token")
    sys.exit(1)
print("102938" if "--otp" in args else "fixture-only-password")
'''


@pytest.fixture
def fake_op(tmp_path, monkeypatch):
    root = tmp_path / "isolated-home"
    root.mkdir()
    hermes_home = root / "hermes"
    hermes_home.mkdir()
    exe = root / "op"
    exe.write_text(_FAKE_OP)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("OP_CONNECT_HOST", raising=False)
    monkeypatch.delenv("OP_CONNECT_TOKEN", raising=False)
    monkeypatch.setattr(Path, "home", lambda: root)
    def setup(items, *, service=True):
        (root / "items.json").write_text(json.dumps(items))
        (root / "calls.jsonl").write_text("")
        if service:
            monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "fixture-only-token")
        else:
            monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
        backend = OnePasswordLoginBackend({"binary_path": str(exe)})
        if not service:
            from agent.vault_backends import unlock
            unlock.store_session_token("onepassword", "fixture-session", unlock.begin_unlock("onepassword"))
        return backend
    def calls():
        return [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
    yield setup, calls
    from agent.vault_backends import unlock
    unlock.lock("onepassword")


def _item(item_id, vault, site="https://site.test/login"):
    return {"id": item_id, "title": "fixture", "urls": [{"href": site}],
            **({"vault": {"id": vault, "name": "ignored"}} if vault is not None else {})}


@pytest.mark.platforms("posix")
def test_service_account_uses_unique_listed_vault_for_password_and_otp(fake_op):
    setup, calls = fake_op
    backend = setup([_item("first", "vault-A"), _item("second", "vault-B")])
    assert backend.get_meta("op:second").id == "op:second"
    assert backend.resolve_password("op:second") == "fixture-only-password"
    assert backend.resolve_otp("op:first") == "102938"
    gets = [c for c in calls() if c[:2] == ["item", "get"]]
    assert [c[2] for c in gets] == ["second", "first"]
    assert [c[c.index("--vault") + 1] for c in gets] == ["vault-B", "vault-A"]
    assert all("fixture-only-token" not in json.dumps(c) for c in calls())
    bad = _item("first", "vault-A")
    bad["error"] = True
    backend = setup([bad])
    with pytest.raises(RuntimeError) as exc:
        backend.resolve_password("op:first")
    assert not any(s in str(exc.value) for s in ("fixture-only-token", "fixture-only-password", "102938"))
    assert backend.resolve_otp("op:first") is None


@pytest.mark.platforms("posix")
def test_missing_ambiguous_and_changed_associations_fail_before_secret_read(fake_op):
    setup, calls = fake_op
    for records, handle in [
        ([_item("other", "vault-A")], "op:missing"),
        ([_item("first", None)], "op:first"),
        ([_item("first", "vault-A"), _item("first", "vault-B")], "op:first"),
        ([_item("first", "vault-A"), _item("second", "vault-B")], "op:second"),
    ]:
        backend = setup(records)
        if handle == "op:second":
            assert backend.get_meta(handle) is not None
            # Association changed between metadata admission and secret resolution.
            (Path(os.environ["HOME"]) / "items.json").write_text(json.dumps([_item("second", "vault-A")]))
        with pytest.raises(RuntimeError, match="vault association") as exc:
            backend.resolve_password(handle)
        assert not any(c[:2] == ["item", "get"] for c in calls())
        assert not any(s in str(exc.value) for s in ("fixture-only-token", "fixture-only-password", "102938"))
        assert backend.resolve_otp(handle) is None
        assert not any(c[:2] == ["item", "get"] for c in calls())
    # Interactive account sessions keep their original item-get contract (no --vault).
    backend = setup([_item("first", None)], service=False)
    assert backend.resolve_password("op:first") == "fixture-only-password"
    assert "--vault" not in [c for c in calls() if c[:2] == ["item", "get"]][0]
    # Both browser entry points refuse an unsaved sibling origin before CLI item-get.
    from tools import browser_vault_tool
    backend = setup([_item("first", "vault-A")])
    controls = [{"index": 0, "type": "text", "name": "otp", "autocomplete": "one-time-code"}]
    with patch("agent.vault_backends.backend_for_handle", return_value=backend), \
         patch.object(browser_vault_tool, "_focus_bound_origin", return_value=None), \
         patch.object(browser_vault_tool, "_current_page_origin", return_value="https://other.site.test"), \
         patch.object(browser_vault_tool, "_eval_js", return_value={"success": True, "result": json.dumps(controls)}):
        password = json.loads(browser_vault_tool.browser_vault_fill("op:first"))
        otp = json.loads(browser_vault_tool.browser_vault_enter_code("op:first"))
    assert password["error_type"] == otp["error_type"] == "origin_mismatch"
    assert not any(c[:2] == ["item", "get"] for c in calls())
