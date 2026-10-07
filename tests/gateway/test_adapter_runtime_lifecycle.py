"""Persist secondary adapter lifecycle changes, including adapters without runner fallbacks."""

import asyncio
import importlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway import status
from gateway.config import PlatformConfig


async def _idle():
    await asyncio.Event().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["signal", "email", "homeassistant", "sms"])
async def test_secondary_lifecycle_persists_state(platform, monkeypatch):
    """Exercise real lifecycle methods; replace only the external transport boundaries."""
    module = importlib.import_module(
        "gateway.platforms.signal" if platform == "signal" else f"plugins.platforms.{platform}.adapter")
    if platform == "signal":
        adapter = module.SignalAdapter(PlatformConfig(enabled=True, extra={"account": "+15550000000"}))
        client = AsyncMock()
        client.get.return_value = MagicMock(status_code=200)
        monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: client)
        monkeypatch.setattr(adapter, "_sse_listener", _idle)
        monkeypatch.setattr(adapter, "_health_monitor", _idle)
    elif platform == "email":
        adapter = module.EmailAdapter(PlatformConfig(enabled=True))
        adapter._address = "fixture@example.invalid"
        adapter._password = "fixture"
        adapter._imap_host = adapter._smtp_host = "example.invalid"
        imap = MagicMock()
        imap.uid.return_value = ("OK", [b"1 2"])
        monkeypatch.setattr(module.imaplib, "IMAP4_SSL", MagicMock(return_value=imap))
        monkeypatch.setattr(module.smtplib, "SMTP", MagicMock())
        monkeypatch.setattr(module.smtplib, "SMTP_SSL", MagicMock())
        monkeypatch.setattr(adapter, "_poll_loop", _idle)
    elif platform == "homeassistant":
        adapter = module.HomeAssistantAdapter(PlatformConfig(enabled=True, token="fixture"))
        session = MagicMock(closed=False, close=AsyncMock())
        ws = MagicMock(closed=False, close=AsyncMock(), send_json=AsyncMock(),
                       receive_json=AsyncMock(side_effect=[
                           {"type": "auth_required"}, {"type": "auth_ok"}, {"success": True}] * 2))
        session.ws_connect = AsyncMock(return_value=ws)
        monkeypatch.setattr(adapter, "_new_session", lambda: session)
        monkeypatch.setattr(adapter, "_listen_loop", _idle)
    else:
        adapter = module.SmsAdapter(PlatformConfig(enabled=True))
        adapter._from_number = "+15550000000"
        adapter._webhook_url = "https://example.invalid/webhooks/twilio"
        monkeypatch.setattr("gateway.platforms.shared_ingress.bind_listener", AsyncMock(return_value=AsyncMock()))
        monkeypatch.setattr(module, "_new_session", lambda **kwargs: AsyncMock())
    key = f"alpha:{platform}"
    adapter._runtime_status_platform_key = key
    try:
        for reconnect in (False, True):
            assert await adapter.connect(is_reconnect=reconnect)
            assert await status.flush_runtime_status_async()
            payload = status.read_runtime_status() or {}
            assert payload.get("platforms", {}).get(key, {}).get("state") == "connected"
            await adapter.disconnect()
            assert await status.flush_runtime_status_async()
            assert status.read_runtime_status()["platforms"][key]["state"] == "disconnected"
    finally:
        await adapter.disconnect()
        await status.flush_runtime_status_async()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform, class_name", [
    ("discord", "DiscordAdapter"), ("matrix", "MatrixAdapter"), ("mattermost", "MattermostAdapter")])
async def test_secondary_disconnect_persists_state(platform, class_name):
    module = importlib.import_module(f"plugins.platforms.{platform}.adapter")
    adapter = getattr(module, class_name)(PlatformConfig(enabled=True))
    key = f"alpha:{platform}"
    adapter._runtime_status_platform_key = key
    status.write_runtime_status(platform=key, platform_state="connected")
    await adapter.disconnect()
    assert await status.flush_runtime_status_async()
    assert status.read_runtime_status()["platforms"][key]["state"] == "disconnected"
