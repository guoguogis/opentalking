"""Vidu S2-Avatar 组件版 client protocol tests (no network, no API key)."""
from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from opentalking.providers.synthesis.vidu import client as vidu_client_module
from opentalking.providers.synthesis.vidu.client import (
    VIDU_PCM_FRAME_MS,
    VIDU_PCM_SAMPLE_RATE,
    ViduAvatarClient,
    resample_pcm16,
)

ws_serve = pytest.importorskip("websockets.asyncio.server").serve


class FakeViduServer:
    """Minimal stand-in for Vidu's external-live control WebSocket."""

    def __init__(self, *, not_ready_times: int = 0) -> None:
        self.not_ready_times = not_ready_times
        self.conn_inits: list[dict] = []
        self.signals: list[dict] = []
        self.pcm_frames: list[bytes] = []
        self.port = 0
        self._server = None

    async def _handler(self, ws) -> None:
        async for message in ws:
            if isinstance(message, (bytes, bytearray)):
                self.pcm_frames.append(bytes(message))
                continue
            data = json.loads(message)
            self.signals.append(data)
            signal = data.get("type")
            if signal != 1:
                continue
            self.conn_inits.append(data)
            if len(self.conn_inits) <= self.not_ready_times:
                ack = {"success": False, "error_code": "NOT_READY", "error_msg": ""}
            else:
                ack = {"success": True, "error_code": "", "error_msg": "", "server_timestamp": 1}
            await ws.send(
                json.dumps(
                    {
                        "type": 2,
                        "live_id": data.get("live_id"),
                        "conn_id": data.get("conn_id"),
                        "seq_id": data.get("seq_id", 0) + 1,
                        "payload": {"conn_init_ack": ack},
                    }
                )
            )

    async def __aenter__(self) -> "FakeViduServer":
        self._server = await ws_serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc_info) -> None:
        self._server.close()
        await self._server.wait_closed()


def _make_client(port: int, **overrides) -> ViduAvatarClient:
    kwargs = dict(
        api_key="test-key",
        base_url=f"http://127.0.0.1:{port}",
        image_uri="data:image/png;base64,AAAA",
        session_id="sess_test",
        handshake_timeout=5.0,
        retry_delay=0.01,
    )
    kwargs.update(overrides)
    return ViduAvatarClient(**kwargs)


async def _stub_create_live(self, image_uri: str) -> dict:
    _stub_create_live.last_image = image_uri  # type: ignore[attr-defined]
    return {
        "live": {"id": "live-42", "status": "waiting"},
        "client_secret": "secret-abc",
        "rtc": {"app_id": "app", "channel_id": "chan", "user_id": "uid", "token": "tok"},
    }


def test_resample_keeps_duration_ratio():
    pcm = np.arange(16000, dtype=np.int16)
    out = resample_pcm16(pcm, 16000, 24000)
    assert out.dtype == np.int16
    assert out.size == 24000
    assert resample_pcm16(pcm, 16000, 16000) is not None
    assert resample_pcm16(np.zeros(0, dtype=np.int16), 16000, 24000).size == 0


def test_extract_rtc_info_accepts_managed_rtc_response_shape():
    """Real API (2026-11) returns managed RTC credentials under ``managed_rtc``."""
    client = _make_client(1234)
    payload = {
        "live": {"id": "1019483896770732032", "status": "waiting"},
        "client_secret": "v1.abc",
        "managed_rtc": {
            "app_id": "4f495dcd-d225-49d5-8b54-31d7ed6c3321",
            "channel_id": "live-user-1019483896770732032",
            "user_id": "live-user-1003595675528605696-1019483896770732032",
            "token": "eyJhcHBpZCI6...",
            "token_expire_at": "1794822417",
            "provider": "artc",
        },
    }
    info = client._extract_rtc_info(payload)
    assert info["channel_id"] == "live-user-1019483896770732032"
    assert info["token"] == "eyJhcHBpZCI6..."
    assert info["app_id"] == "4f495dcd-d225-49d5-8b54-31d7ed6c3321"
    assert info["provider"] == "artc"
    assert info["managed"] is True


def test_extract_rtc_info_still_accepts_rtc_shape():
    client = _make_client(1234)
    info = client._extract_rtc_info({"rtc": {"channel_id": "c1", "token": "t1"}})
    assert info["channel_id"] == "c1"
    assert info["token"] == "t1"


def test_extract_rtc_info_reports_missing_credentials():
    client = _make_client(1234)
    info = client._extract_rtc_info({"live": {"id": "1"}})
    assert "channel_id" not in info
    assert info["provider"] == "artc"


def test_stream_url_uses_client_secret_query():
    client = _make_client(1234)
    client._live_id = "live-1"
    client._conn_id = "conn-1"
    client._client_secret = "secret"
    url = client._stream_url()
    assert url.startswith("ws://127.0.0.1:1234/live/v1/external-lives/live-1/stream?")
    assert "conn_id=conn-1" in url
    assert "client_secret=secret" in url


def test_managed_rtc_is_default_and_encodes_image_from_bundle(tmp_path):
    avatar_dir = tmp_path / "avatar"
    avatar_dir.mkdir()
    (avatar_dir / "reference.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    client = _make_client(1234, image_uri="")
    uri = client._resolve_image_uri({"avatar_path": avatar_dir})
    assert uri.startswith("data:image/png;base64,")
    assert client._managed_rtc is True


async def test_init_session_handshake_and_rtc_payload(monkeypatch):
    monkeypatch.setattr(vidu_client_module.ViduAvatarClient, "_create_live", _stub_create_live)
    async with FakeViduServer() as server:
        client = _make_client(server.port)
        payload = await client.init_session(avatar_path=None)
        assert payload["live_id"] == "live-42"
        assert payload["rtc"]["channel_id"] == "chan"
        assert server.conn_inits and server.conn_inits[0]["payload"]["conn_init"]["version"] == 1
        assert server.conn_inits[0]["live_id"] == "live-42"
        await client.close()


async def test_generate_sends_24k_pcm_in_20ms_frames(monkeypatch):
    monkeypatch.setattr(vidu_client_module.ViduAvatarClient, "_create_live", _stub_create_live)
    async with FakeViduServer() as server:
        client = _make_client(server.port)
        await client.init_session()
        frames = await client.generate(np.zeros(1600, dtype=np.int16))
        await asyncio.sleep(0.05)
        assert frames == []
        expected_frame_bytes = VIDU_PCM_SAMPLE_RATE * VIDU_PCM_FRAME_MS // 1000 * 2
        assert server.pcm_frames, "no PCM reached the server"
        assert all(len(frame) == expected_frame_bytes for frame in server.pcm_frames)
        # 1600 samples @16k == 0.1 s -> 2400 samples @24k -> 5 frames of 480
        assert len(server.pcm_frames) == 5
        await client.close()


async def test_not_ready_retries_without_reconnect(monkeypatch):
    monkeypatch.setattr(vidu_client_module.ViduAvatarClient, "_create_live", _stub_create_live)
    async with FakeViduServer(not_ready_times=2) as server:
        client = _make_client(server.port)
        await client.init_session()
        assert len(server.conn_inits) == 3
        await client.close()


async def test_interrupt_sends_audio_interrupted(monkeypatch):
    monkeypatch.setattr(vidu_client_module.ViduAvatarClient, "_create_live", _stub_create_live)
    async with FakeViduServer() as server:
        client = _make_client(server.port)
        await client.init_session()
        await client.interrupt()
        await asyncio.sleep(0.05)
        assert any(signal.get("type") == 7 for signal in server.signals)
        await client.close()


async def test_close_sends_hangup(monkeypatch):
    monkeypatch.setattr(vidu_client_module.ViduAvatarClient, "_create_live", _stub_create_live)
    async with FakeViduServer() as server:
        client = _make_client(server.port)
        await client.init_session()
        await client.close()
        await asyncio.sleep(0.05)
        hangup = [s for s in server.signals if s.get("type") == 5]
        assert hangup, "call_hangup was not sent"
        assert hangup[0]["payload"]["hangup"]["hangup_reason"] == "client_hangup"


async def test_generate_without_session_raises():
    client = _make_client(1234)
    with pytest.raises(RuntimeError, match="Not connected"):
        await client.generate(np.zeros(10, dtype=np.int16))


async def test_connect_requires_api_key():
    client = _make_client(1234, api_key="")
    with pytest.raises(RuntimeError, match="VIDU_API_KEY"):
        await client.connect()
