"""Raw WS probe for FlashHead (different message format from audio2video/*)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import time

import numpy as np
from websockets.asyncio.client import connect

URL = "wss://218.28.9.108:50787/ws/flashhead?api_key=sk-litellm-master-key-123"
IMAGE = os.environ.get("PROBE_IMAGE", "examples/avatars/anchor/reference.png")
BLOCKS = int(os.environ.get("PROBE_BLOCKS", "4"))


def parse_vidx(msg: bytes) -> list[bytes]:
    assert msg[:4] == b"VIDX", msg[:8]
    n = struct.unpack("<I", msg[4:8])[0]
    off, frames = 8, []
    for _ in range(n):
        ln = struct.unpack("<I", msg[off : off + 4])[0]
        off += 4
        frames.append(msg[off : off + ln])
        off += ln
    return frames


async def main() -> None:
    image_b64 = base64.b64encode(open(IMAGE, "rb").read()).decode()
    t0 = time.perf_counter()
    async with connect(URL, open_timeout=30, max_size=64 << 20, ping_interval=20) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "session.create",
                    "model": "flashhead",
                    "inputs": {"image_b64": image_b64, "prompt": "A person is talking."},
                    "config": {"seed": 9999},
                }
            )
        )
        info = json.loads(await ws.recv())
        print(f"create: {json.dumps(info)[:260]} ({(time.perf_counter()-t0)*1000:.0f}ms)")
        if info.get("type") != "session.created":
            return
        chunk = int((info.get("audio") or {}).get("chunk_samples") or 15360)
        print("chunk_samples:", chunk)

        for block in range(BLOCKS):
            pcm = (np.sin(np.arange(chunk) / 20.0) * 8000).astype(np.int16)
            t_send = time.perf_counter()
            await ws.send(b"AUDI" + pcm.tobytes())
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=40)
                except (asyncio.TimeoutError, TimeoutError):
                    print(f"block {block}: timeout")
                    break
                if isinstance(msg, str):
                    print(f"block {block}: text {msg[:120]}")
                    if '"error"' in msg:
                        return
                    continue
                frames = parse_vidx(msg)
                print(
                    f"block {block}: frames={len(frames)} "
                    f"latency={(time.perf_counter()-t_send)*1000:.0f}ms "
                    f"bytes={sum(len(f) for f in frames)}"
                )
                break
        await ws.send(json.dumps({"type": "session.close"}))


asyncio.run(main())
