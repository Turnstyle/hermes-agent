"""Peer ping CLI refuses unsupported peers and never retries an uncertain POST."""

import argparse
import hashlib
import io
from types import SimpleNamespace
from urllib.error import HTTPError

from hermes_cli.subcommands import peer


def _args(action="ping"):
    return SimpleNamespace(peer_action=action, target="spark", idempotency_key="canary-1",
                           ping_key="canary-1", json=True)


def _setup(monkeypatch):
    monkeypatch.setattr(peer, "_load_peers", lambda: {"spark": {"url": "http://peer"}})
    monkeypatch.setattr(peer, "_peer_secret", lambda name: "test-peer-key-123456")
    monkeypatch.setattr(peer.socket, "gethostname", lambda: "turnerbook")


def test_parser_exposes_ping_and_status_commands():
    parser = argparse.ArgumentParser()
    peer.build_peer_parser(parser.add_subparsers())
    ping = parser.parse_args(["peer", "ping", "spark", "--idempotency-key", "canary-1"])
    status = parser.parse_args(["peer", "ping-status", "spark", "canary-1"])
    assert (ping.peer_action, ping.idempotency_key) == ("ping", "canary-1")
    assert (status.peer_action, status.ping_key) == ("ping-status", "canary-1")


def test_timeout_reads_back_without_second_post(monkeypatch, capsys):
    _setup(monkeypatch)
    calls = []
    sent = {}

    def request(url, key, *, method="GET", body=None, **kwargs):
        calls.append((method, url))
        if url.endswith("/v1/capabilities"):
            return {"features": {"peer_ping": {"supported": True, "durable": True}}}
        if method == "POST":
            sent.update(body)
            raise TimeoutError("post response lost")
        return {"object": "hermes.peer.ping", **sent, "received_at": "2026-09-29T01:30:01Z"}

    monkeypatch.setattr(peer, "_request", request)
    assert peer.cmd_peer(_args()) == 0
    assert [method for method, _ in calls] == ["GET", "POST", "GET"]
    assert sent["payload_sha256"] == hashlib.sha256(sent["nonce"].encode()).hexdigest()
    assert "received_at" in capsys.readouterr().out


def test_missing_feature_never_posts(monkeypatch, capsys):
    _setup(monkeypatch)
    calls = []

    def request(url, key, *, method="GET", **kwargs):
        calls.append((method, url))
        return {"features": {}}

    monkeypatch.setattr(peer, "_request", request)
    assert peer.cmd_peer(_args()) == 1
    assert len(calls) == 1 and calls[0][0] == "GET"
    assert "peer_ping" in capsys.readouterr().err


def test_ping_status_reads_only(monkeypatch):
    _setup(monkeypatch)
    calls = []

    def request(url, key, *, method="GET", **kwargs):
        calls.append((method, url))
        if url.endswith("/v1/capabilities"):
            return {"features": {"peer_ping": {"supported": True, "durable": True}}}
        return {"idempotency_key": "canary-1", "received_at": "now"}

    monkeypatch.setattr(peer, "_request", request)
    assert peer.cmd_peer(_args("ping-status")) == 0
    assert [method for method, _ in calls] == ["GET", "GET"]


def test_timeout_with_missing_readback_stays_uncertain(monkeypatch, capsys):
    _setup(monkeypatch)
    calls = []

    def request(url, key, *, method="GET", **kwargs):
        calls.append((method, url))
        if url.endswith("/v1/capabilities"):
            return {"features": {"peer_ping": {"supported": True, "durable": True}}}
        if method == "POST":
            raise TimeoutError("post response lost")
        raise HTTPError(url, 404, "missing", {}, io.BytesIO(b'{"error":{"message":"not found"}}'))

    monkeypatch.setattr(peer, "_request", request)
    assert peer.cmd_peer(_args()) == 1
    assert [method for method, _ in calls] == ["GET", "POST", "GET"]
    assert "outcome is uncertain" in capsys.readouterr().err
