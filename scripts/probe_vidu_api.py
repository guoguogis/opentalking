"""Probe the real Vidu S2-Avatar component API (creates one short live session).

Use this to verify what the production service actually returns — especially
whether ``managed_rtc`` hands back ``rtc`` join credentials — before trusting
the offline stand-in.

It creates a session, completes ``conn_init``, then immediately hangs up so the
billed duration stays in the seconds range. Nothing is written to disk except
stdout.

    set VIDU_API_KEY=vda_...
    python scripts/probe_vidu_api.py --image examples/avatars/anchor/reference.png

The API key is read from the environment and never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from websockets.asyncio.client import connect as ws_connect  # noqa: E402

from opentalking.core.config import load_process_env  # noqa: E402

# Make `.env` visible to os.environ so the key never has to appear on the CLI.
load_process_env()


def _mask(value: str, keep: int = 6) -> str:
    if not isinstance(value, str):
        return repr(value)
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}…({len(value)} chars)"


def _sanitize(payload: dict) -> dict:
    out: dict = {}
    for key, value in (payload or {}).items():
        if isinstance(value, dict):
            out[key] = _sanitize(value)
        elif isinstance(value, str) and ("token" in key.lower() or "secret" in key.lower()):
            out[key] = _mask(value)
        else:
            out[key] = value
    return out


def _image_uri(path: Path) -> str:
    raw = path.read_bytes()
    mime = "image/png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _ws_base(base: str) -> str:
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :]
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :]
    return base


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="examples/avatars/anchor/reference.png")
    parser.add_argument("--provider", default="artc")
    parser.add_argument("--keep-open", action="store_true", help="skip the automatic hangup")
    parser.add_argument("--ack-timeout", type=float, default=90.0)
    args = parser.parse_args()

    api_key = (os.environ.get("VIDU_API_KEY") or "").strip()
    if not api_key:
        print("VIDU_API_KEY is not set", file=sys.stderr)
        return 2
    base = (os.environ.get("VIDU_BASE_URL") or "https://api.vidu.cn").rstrip("/")

    image = Path(args.image)
    uri = _image_uri(image)
    print(f"[i] base_url={base} image={image} ({len(uri) // 1024} KiB as data URI)")

    body = {
        "model": "vidu-s2",
        "image_uri": uri,
        "managed_rtc": {"provider": args.provider},
        "idle_timeout_seconds": 60,
    }
    headers = {"Authorization": f"Token {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        started = time.monotonic()
        response = await client.post(f"{base}/live/s_avatar/component", json=body, headers=headers)
        elapsed = time.monotonic() - started
        print(f"[1] POST /live/s_avatar/component -> HTTP {response.status_code} in {elapsed:.1f}s")
        try:
            payload = response.json()
        except Exception:
            print(f"[!] non-JSON body: {response.text[:600]}")
            return 1
        print(json.dumps(_sanitize(payload), ensure_ascii=False, indent=2)[:2000])
        if response.status_code >= 400:
            return 1

    live = payload.get("live") or {}
    live_id = str(live.get("id") or "")
    secret = str(payload.get("client_secret") or "")
    rtc = payload.get("rtc")
    print(f"[2] live.id={live_id} status={live.get('status')} client_secret={_mask(secret)}")
    if isinstance(rtc, dict):
        print(f"[2b] rtc keys={sorted(rtc)} provider={rtc.get('provider')} channel={rtc.get('channel_id')}")
    else:
        print("[2b] !! response has NO rtc object — managed_rtc did not hand back join credentials")

    ws_url = f"{_ws_base(base)}/live/v1/external-lives/{live_id}/stream?conn_id=probe-{int(time.time())}"
    if secret:
        ws_url += f"&client_secret={secret}"
    ws = await ws_connect(ws_url, max_size=16 * 1024 * 1024, open_timeout=30, close_timeout=10)
    try:
        await ws.send(json.dumps({"type": 1, "live_id": live_id, "seq_id": 1,
                                  "payload": {"conn_init": {"version": 1}}}))
        deadline = time.monotonic() + args.ack_timeout
        ack = None
        while time.monotonic() < deadline:
            message = await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic())
            if isinstance(message, (bytes, bytearray)):
                continue
            data = json.loads(message)
            if data.get("type") == 2:
                ack = (data.get("payload") or {}).get("conn_init_ack") or {}
                print(f"[3] conn_init_ack success={ack.get('success')} error={ack.get('error_code')!r}")
                break
        if ack is None:
            print("[3] !! no conn_init_ack within timeout")
        if ack and ack.get("success") and not args.keep_open:
            await ws.send(json.dumps({"type": 5, "live_id": live_id, "seq_id": 2,
                                      "payload": {"hangup": {"hangup_reason": "client_hangup"}}}))
            print("[4] call_hangup sent")
    finally:
        await ws.close()

    async with httpx.AsyncClient(timeout=30.0) as client:
        query = await client.get(f"{base}/live/v1/lives/{live_id}", headers=headers)
        print(f"[5] GET /live/v1/lives/{live_id} -> HTTP {query.status_code}")
        if query.status_code < 400:
            print(json.dumps(_sanitize(query.json()), ensure_ascii=False, indent=2)[:1200])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
