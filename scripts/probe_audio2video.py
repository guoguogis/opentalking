"""Raw WS probe: does the private audio2video gateway return VIDX frames?"""

from __future__ import annotations

import asyncio
import os
import base64
import json
import struct
import time

import numpy as np
from websockets.asyncio.client import connect

MODEL = os.environ.get("PROBE_MODEL", "quicktalk")
URL = f"wss://218.28.9.108:50787/v1/audio2video/{MODEL}"
HEADERS = {"Authorization": "Bearer sk-litellm-master-key-123"}
IMAGE = "examples/avatars/anchor/reference.png"


async def main() -> None:
    ref = base64.b64encode(open(IMAGE, "rb").read()).decode()
    t0 = time.perf_counter()
    async with connect(URL, additional_headers=HEADERS, open_timeout=30,
                       max_size=64 << 20, ping_interval=20) as ws:
        await ws.send(json.dumps({"type": "init", "ref_image": ref,
                                  "prompt": "A person is talking.", "seed": 9999}))
        info = json.loads(await ws.recv())
        print(f"init: {info} ({(time.perf_counter()-t0)*1000:.0f}ms)")
        chunk = int(info.get("chunk_samples") or info["slice_len"] * 16000 // info["fps"])
        print("chunk_samples:", chunk)

        total_frames = 0
        for block in range(6):
            pcm = (np.sin(np.arange(chunk) / 20.0) * 8000).astype(np.int16)
            t_send = time.perf_counter()
            await ws.send(b"AUDI" + pcm.tobytes())
            deadline = time.perf_counter() + 25
            while time.perf_counter() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=25)
                except (asyncio.TimeoutError, TimeoutError):
                    print(f"block {block}: recv timeout")
                    break
                if isinstance(msg, str):
                    print(f"block {block}: text {msg[:120]}")
                    if '"error"' in msg:
                        return
                    continue
                if msg[:4] != b"VIDX":
                    print(f"block {block}: unexpected binary {msg[:8]!r}")
                    continue
                n = struct.unpack("<I", msg[4:8])[0]
                off = 8
                sizes = []
                for _ in range(n):
                    ln = struct.unpack("<I", msg[off:off + 4])[0]
                    off += 4
                    sizes.append(ln)
                    off += ln
                total_frames += n
                print(f"block {block}: VIDX frames={n} "
                      f"latency={(time.perf_counter()-t_send)*1000:.0f}ms "
                      f"bytes={msg[8:].__len__()} first_jpeg={sizes[:3]}")
                break
        print("total frames:", total_frames)
        await ws.send(json.dumps({"type": "close"}))


asyncio.run(main())
