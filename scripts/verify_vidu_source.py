"""Offline end-to-end check for the Vidu S2-Avatar source.

Starts a local stand-in for Vidu's HTTP + WebSocket endpoints (one aiohttp
server, so HTTP and WS share a port like the real service), points the real
`ViduAvatarClient` at it, and verifies:

1. ``POST /live/s_avatar/component`` body (managed_rtc + image data URI)
2. ``conn_init`` handshake and ``conn_init_ack`` handling
3. 16 kHz -> 24 kHz resampling and 20 ms (960 byte) binary framing
4. ``audio_interrupted`` (type=7) and ``call_hangup`` (type=5)

No Vidu credentials or network access are required. Run from the repo root:

    python scripts/verify_vidu_source.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import numpy as np
from aiohttp import WSMsgType, web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opentalking.providers.synthesis.vidu.client import (  # noqa: E402
    VIDU_PCM_FRAME_MS,
    VIDU_PCM_SAMPLE_RATE,
    ViduAvatarClient,
)

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

CREATE_RESPONSE = {
    "live": {"id": "live-e2e-1", "status": "waiting", "call_mode": "video"},
    "client_secret": "secret-e2e",
    "rtc": {
        "provider": "artc",
        "app_id": "demo-app",
        "channel_id": "demo-channel",
        "user_id": "demo-user",
        "token": "demo-token",
    },
}


class StandInVidu:
    def __init__(self) -> None:
        self.create_bodies: list[dict] = []
        self.conn_init: dict | None = None
        self.pcm_frames: list[bytes] = []
        self.signals: list[dict] = []

    async def create_live(self, request: web.Request) -> web.Response:
        self.create_bodies.append(await request.json())
        return web.json_response(CREATE_RESPONSE)

    async def stream(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            if message.type == WSMsgType.BINARY:
                self.pcm_frames.append(message.data)
                continue
            if message.type != WSMsgType.TEXT:
                continue
            data = json.loads(message.data)
            self.signals.append(data)
            if data.get("type") == 1:
                self.conn_init = data
                await ws.send_str(
                    json.dumps(
                        {
                            "type": 2,
                            "live_id": data.get("live_id"),
                            "conn_id": data.get("conn_id"),
                            "seq_id": 2,
                            "payload": {
                                "conn_init_ack": {
                                    "success": True,
                                    "error_code": "",
                                    "error_msg": "",
                                    "server_timestamp": 1,
                                }
                            },
                        }
                    )
                )
            elif data.get("type") == 5:
                break
        return ws


def build_app(server: StandInVidu) -> web.Application:
    # Real Vidu accepts up to 50 MB images and 32 MB request bodies, so allow
    # base64 avatar payloads here instead of aiohttp's 1 MB default.
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_post("/live/s_avatar/component", server.create_live)
    app.router.add_get("/live/v1/external-lives/{live_id}/stream", server.stream)
    return app


async def main_check() -> int:
    server = StandInVidu()
    runner = web.AppRunner(build_app(server))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    print(f"[1] stand-in Vidu (HTTP+WS) listening on 127.0.0.1:{port}")

    client = ViduAvatarClient(
        api_key="offline-test-key",
        base_url=f"http://127.0.0.1:{port}",
        image_uri="data:image/png;base64," + base64.b64encode(PNG_1PX).decode(),
        session_id="sess_e2e",
        retry_delay=0.01,
    )
    try:
        await client.connect()
        info = await client.init_session()
        print(
            f"[2] create session -> live_id={info['live_id']} "
            f"rtc.channel={info['rtc'].get('channel_id')}"
        )
        assert server.create_bodies, "no create request reached the stand-in"
        body = server.create_bodies[0]
        assert body["model"] == "vidu-s2", body
        assert "managed_rtc" in body and body["managed_rtc"]["provider"] == "artc", body
        assert body["image_uri"].startswith("data:image/png;base64,"), body["image_uri"][:32]
        print(f"[3] create body ok: model={body['model']} managed_rtc={body['managed_rtc']}")

        assert server.conn_init is not None, "conn_init never arrived"
        assert server.conn_init["payload"]["conn_init"]["version"] == 1
        print(
            f"[4] handshake ok: type=1 live_id={server.conn_init['live_id']} "
            f"conn_id={server.conn_init['conn_id']}"
        )

        frames = await client.generate(np.zeros(2400, dtype=np.int16))
        await asyncio.sleep(0.15)
        expected = VIDU_PCM_SAMPLE_RATE * VIDU_PCM_FRAME_MS // 1000 * 2
        assert frames == [], "Vidu source must not return local frames"
        assert server.pcm_frames, "no PCM reached the stand-in"
        assert all(len(f) == expected for f in server.pcm_frames), [
            len(f) for f in server.pcm_frames
        ]
        print(
            f"[5] pcm ok: 2400 samples @16k -> {len(server.pcm_frames)} frames x {expected}B "
            f"({VIDU_PCM_SAMPLE_RATE} Hz / {VIDU_PCM_FRAME_MS} ms)"
        )

        await client.interrupt()
        await client.close()
        await asyncio.sleep(0.15)
        types = [s.get("type") for s in server.signals]
        assert 7 in types, types
        assert 5 in types, types
        print(f"[6] signals ok: {types} (7=audio_interrupted, 5=call_hangup)")
    finally:
        await runner.cleanup()

    print("\nRESULT: Vidu source offline end-to-end check passed")
    return 0


async def serve_forever(port: int) -> None:
    """Run the stand-in Vidu server for wiring tests (OpenTalking backend on top)."""
    server = StandInVidu()
    runner = web.AppRunner(build_app(server))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    print(f"stand-in Vidu serving on http://127.0.0.1:{port}", flush=True)
    try:
        while True:
            await asyncio.sleep(5)
            print(
                f"create_bodies={len(server.create_bodies)} "
                f"conn_init={'yes' if server.conn_init else 'no'} "
                f"pcm_frames={len(server.pcm_frames)} "
                f"signals={[s.get('type') for s in server.signals]}",
                flush=True,
            )
    finally:
        await runner.cleanup()


def main() -> int:
    if "--serve" in sys.argv:
        port = 18099
        if "--port" in sys.argv:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        asyncio.run(serve_forever(port))
        return 0
    return asyncio.run(main_check())


if __name__ == "__main__":
    raise SystemExit(main())
