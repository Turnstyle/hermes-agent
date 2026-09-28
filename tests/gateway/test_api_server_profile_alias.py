"""Peers address multiplexed bots by the name the roster shows.

Live miss (2026-09-28): TurnerBook's ``hermes peer dm snowdrop/web-reviewer`` got a 404
because the Snowdrop profile id is ``webui-jtp``; "Web-Reviewer" is its Bot Chat title.
A UNIQUE display-name / title / previous-name slug match now resolves to the canonical id;
no match or an ambiguous match still fails closed (404).
"""

from __future__ import annotations

from types import SimpleNamespace

from gateway.platforms.api_server import _PROFILE_REJECTED, APIServerAdapter
from gateway.config import PlatformConfig


def _adapter():
    a = APIServerAdapter(PlatformConfig(enabled=True))
    a.gateway_runner = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=True))
    return a


def _request(profile):
    return SimpleNamespace(match_info={"profile": profile})


def _serve(monkeypatch, tmp_path, metas):
    pairs = []
    for name, meta in metas.items():
        home = tmp_path / name
        home.mkdir()
        pairs.append((name, home))
    monkeypatch.setattr("hermes_cli.profiles.profiles_to_serve", lambda multiplex=True, **k: pairs)
    by_home = {str(tmp_path / n): m for n, m in metas.items()}
    monkeypatch.setattr(
        "hermes_cli.profiles.read_profile_meta",
        lambda home: {"display_name": "", "bot_title": "", "previous_names": [], **by_home[str(home)]},
    )


def test_bot_title_alias_resolves_to_canonical_profile(monkeypatch, tmp_path):
    _serve(monkeypatch, tmp_path, {
        "default": {"display_name": "default-snowdrop"},
        "webui-jtp": {"bot_title": "Web-Reviewer"},
        "snow-king": {},
    })
    a = _adapter()
    assert a._resolve_request_profile(_request("web-reviewer")) == "webui-jtp"
    assert a._resolve_request_profile(_request("webui-jtp")) == "webui-jtp"
    assert a._resolve_request_profile(_request("default-snowdrop")) == "default"


def test_unknown_or_ambiguous_alias_still_fails_closed(monkeypatch, tmp_path):
    _serve(monkeypatch, tmp_path, {
        "a": {"bot_title": "Reviewer"},
        "b": {"display_name": "reviewer"},
    })
    a = _adapter()
    assert a._resolve_request_profile(_request("ghost")) is _PROFILE_REJECTED
    assert a._resolve_request_profile(_request("reviewer")) is _PROFILE_REJECTED
