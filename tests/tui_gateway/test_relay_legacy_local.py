"""G27: the legacy keyless relay bridge stays refused; the explicit delivery-id door admits once.

The fleet carry asserted that a legacy peer with no delivery id executed the turn locally. v0.22.5
refuses that unsupported bridge (``invalid_params``) and admits relayed DMs only through the
target authority's canonical ``bot_relay.deliver`` under the sender's delivery id.
"""
import tui_gateway.server as srv

DELIVERY_ID = "0123456789abcdef0123456789abcdef"


def _peer(tmp_path, monkeypatch):
    peer = tmp_path / "peer"
    (peer / "profiles" / "ops").mkdir(parents=True)
    (peer / "profiles" / "ops" / "config.yaml").write_text("{}\n")
    monkeypatch.setenv("HERMES_HOME", str(peer))
    calls = []

    def _authority_delivery(home, params):
        calls.append((home, dict(params)))
        return {"status": "settled", "delivery_id": params["id"], "reply": "answer"}

    monkeypatch.setattr("tools.bot_live_delivery.authority_delivery", _authority_delivery)
    return peer, calls


def test_legacy_peer_without_delivery_id_is_refused_and_runs_nothing(tmp_path, monkeypatch):
    peer, calls = _peer(tmp_path, monkeypatch)
    result = srv._methods["bot_relay.deliver"](1, {"profile": "ops", "message": "legacy body"}, _root=lambda: peer)
    assert result["error"]["data"]["reason"] == "invalid_params"
    assert calls == []


def test_explicit_delivery_id_reaches_the_canonical_door_once(tmp_path, monkeypatch):
    peer, calls = _peer(tmp_path, monkeypatch)
    params = {"id": DELIVERY_ID, "profile": "ops", "message": "hello", "from_profile": "sender",
              "from_handle": "sender", "from_connection": "node-a"}
    result = srv._methods["bot_relay.deliver"](1, params, _root=lambda: peer)
    assert result["result"] == {"status": "settled", "delivery_id": DELIVERY_ID, "reply": "answer"}
    [(home, forwarded)] = calls
    assert home == peer / "profiles" / "ops"
    assert forwarded["id"] == DELIVERY_ID and forwarded["profile"] == "ops"
    # Sender fields never pass through raw: the bridge derives the author the target records.
    assert not {"from_profile", "from_handle", "from_connection"} & set(forwarded)
    assert forwarded["author"]["is_bot"] is True
