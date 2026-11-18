"""FlashHead realtime WebSocket client for OmniRT avatar sessions."""
from __future__ import annotations

import base64
import concurrent.futures
import json
import logging
import os
import struct
import time
from pathlib import Path
from typing import Any

import numpy as np

try:
    import websockets
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    websockets = None  # type: ignore[assignment]

from opentalking.core.types.frames import VideoFrameData
from opentalking.providers.synthesis.flashtalk.ws_client import MAGIC_AUDIO, MAGIC_VIDEO

log = logging.getLogger(__name__)

JPEG_DECODE_WORKERS = max(
    1,
    int(
        os.environ.get(
            "OPENTALKING_FLASHHEAD_JPEG_DECODE_WORKERS",
            os.environ.get("OPENTALKING_FLASHTALK_JPEG_DECODE_WORKERS", "1"),
        )
        or "1"
    ),
)

_JPEG_DECODE_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None


def _default_ws_url() -> str:
    return os.environ.get(
        "OPENTALKING_FLASHHEAD_WS_URL",
        "ws://localhost:8766/v1/avatar/realtime",
    )


def _auth_headers(url: str = "") -> dict[str, str]:
    """Bearer auth for FlashHead endpoints behind a gateway.

    A remote gateway (e.g. ``wss://host/ws/flashhead``) answers 401 without a key.
    Falls back to ``OPENTALKING_OMNIRT_API_KEY`` so a single gateway key covers
    both the audio2video and FlashHead channels.
    """
    key = (
        os.environ.get("OPENTALKING_FLASHHEAD_API_KEY", "").strip()
        or os.environ.get("OPENTALKING_OMNIRT_API_KEY", "").strip()
    )
    if not key:
        return {}
    if "api_key=" in str(url or ""):
        return {}  # 已经用查询参数传了，避免重复
    return {"Authorization": f"Bearer {key}"}


def _decode_jpeg(jpeg_bytes: bytes) -> np.ndarray:
    import cv2

    jpeg_buf = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    bgr = cv2.imdecode(jpeg_buf, cv2.IMREAD_COLOR)
    if bgr is None:
        raise RuntimeError("JPEG decoding failed")
    return bgr


# FlashHead's VAE (vae_stride=[8, 32, 32]) can render any width/height that is a
# multiple of 32 natively; everything else has to be generated at 512x512 and
# resized, which costs detail instead of gaining it.
VAE_STRIDE = 32
MIN_SIDE = 64
MAX_SIDE = 2048
DEFAULT_SIDE = 512
_VALID_FITS = frozenset({"native", "resize"})


def _clamp_side(value: Any, fallback: int) -> int:
    try:
        side = int(value)
    except (TypeError, ValueError):
        return fallback
    if side <= 0:
        return fallback
    return max(MIN_SIDE, min(MAX_SIDE, side))


def _normalize_fit(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in _VALID_FITS else ""


def resolve_render_size(
    *,
    width: Any = None,
    height: Any = None,
    fit: Any = None,
    video_config: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[int, int, str, bool]:
    """Resolve the render target for one FlashHead session.

    Priority: explicit argument -> ``OPENTALKING_FLASHHEAD_*`` env -> per-avatar
    ``video_config`` -> client config -> 512x512. ``fit`` stays "auto" unless a
    caller asked for something specific; auto means native when both sides are a
    multiple of 32, otherwise resize.
    """
    video_config = video_config or {}
    config = config or {}

    sources = [
        (width, height),
        (
            os.environ.get("OPENTALKING_FLASHHEAD_WIDTH"),
            os.environ.get("OPENTALKING_FLASHHEAD_HEIGHT"),
        ),
        (video_config.get("width"), video_config.get("height")),
        (config.get("width"), config.get("height")),
    ]
    render_w = render_h = DEFAULT_SIDE
    for raw_w, raw_h in sources:
        render_w = _clamp_side(raw_w, DEFAULT_SIDE)
        render_h = _clamp_side(raw_h, DEFAULT_SIDE)
        if raw_w not in (None, "") or raw_h not in (None, ""):
            break

    requested_fit = _normalize_fit(fit) or _normalize_fit(
        os.environ.get("OPENTALKING_FLASHHEAD_FIT")
    ) or _normalize_fit(video_config.get("fit")) or _normalize_fit(config.get("fit"))
    fit_explicit = bool(requested_fit)

    native_ok = render_w % VAE_STRIDE == 0 and render_h % VAE_STRIDE == 0
    effective_fit = requested_fit or ("native" if native_ok else "resize")
    if effective_fit == "native" and not native_ok:
        log.warning(
            "FlashHead native fit needs both sides to be multiples of %d; %dx%d -> resize",
            VAE_STRIDE,
            render_w,
            render_h,
        )
        effective_fit = "resize"
    return render_w, render_h, effective_fit, fit_explicit


class FlashHeadWSClient:
    """Async client for OmniRT's native FlashHead realtime avatar protocol."""

    def __init__(
        self,
        ws_url: str | None = None,
        *,
        model: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        if websockets is None:
            raise RuntimeError("websockets package required: pip install websockets")
        self._ws_url = ws_url or _default_ws_url()
        self._model = model or os.environ.get("OPENTALKING_FLASHHEAD_MODEL", "soulx-flashhead-1.3b")
        self._config = dict(config or {})
        self._ws = None
        self._session_id = ""

        self.frame_num: int = 0
        self.motion_frames_num: int = 0
        self.slice_len: int = 0
        self.fps: int = 25
        self.height: int = 0
        self.width: int = 0
        # What the server says it actually generated, plus the resize target when
        # fit="resize" (render_* can be smaller than width/height).
        self.render_width: int = 0
        self.render_height: int = 0
        self.fit: str = ""
        self.sample_rate: int = 16000
        self.audio_chunk_samples: int = 0

    async def connect(self) -> None:
        self._ws = await ws_connect(
            self._ws_url,
            additional_headers=_auth_headers(self._ws_url),
            max_size=50 * 1024 * 1024,
            open_timeout=30,
            close_timeout=10,
        )
        log.info("Connected to FlashHead server at %s", self._ws_url)

    async def init_session(
        self,
        ref_image: bytes | str | Path,
        prompt: str = "A person is talking. Only the foreground characters are moving, the background remains static.",
        seed: int = 9999,
        *,
        width: int | None = None,
        height: int | None = None,
        fit: str | None = None,
        video_config: dict[str, Any] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Create the FlashHead session.

        FlashHead only consumes a reference image plus prompt/seed, but the shared
        synthesis runner calls every client with the wav2lip/quicktalk tuning
        keywords too. Accept and ignore them instead of raising TypeError, so
        switching the driving model to FlashHead does not fail during session
        setup.

        The render size comes from ``width``/``height``/``fit`` when given, else
        from ``video_config`` (the avatar's own resolution), else from settings.
        """
        if extra:
            log.debug(
                "FlashHead ignores irrelevant init options: %s",
                ", ".join(sorted(extra)),
            )
        if self._ws is None:
            await self.connect()

        if isinstance(ref_image, (str, Path)):
            ref_bytes = Path(ref_image).read_bytes()
        else:
            ref_bytes = ref_image

        render_w, render_h, effective_fit, fit_explicit = resolve_render_size(
            width=width,
            height=height,
            fit=fit,
            video_config=video_config,
            config=self._config,
        )
        config = {
            "seed": seed,
            **self._config,
            "width": render_w,
            "height": render_h,
        }
        # Older FlashHead builds never saw a "fit" key, so only send it when it
        # matters: an explicit request, or resize (which the server must be told).
        if fit_explicit or effective_fit == "resize":
            config["fit"] = effective_fit
        log.info(
            "FlashHead render target: %dx%d fit=%s (explicit=%s)",
            render_w,
            render_h,
            effective_fit,
            fit_explicit,
        )
        msg = {
            "type": "session.create",
            "model": self._model,
            "inputs": {
                "image_b64": base64.b64encode(ref_bytes).decode("ascii"),
                "prompt": prompt,
            },
            "config": config,
        }
        assert self._ws is not None
        await self._ws.send(json.dumps(msg, ensure_ascii=False))
        resp = json.loads(await self._ws.recv())
        if resp.get("type") == "error":
            raise RuntimeError(f"FlashHead init failed: {resp.get('message')}")
        if resp.get("type") != "session.created":
            raise RuntimeError(f"Unexpected FlashHead init response: {resp!r}")

        self._session_id = str(resp.get("session_id") or "")
        audio = dict(resp.get("audio") or {})
        video = dict(resp.get("video") or {})
        self.sample_rate = int(audio.get("sample_rate") or self.sample_rate)
        self.audio_chunk_samples = int(audio.get("chunk_samples") or self.audio_chunk_samples or 0)
        self.fps = int(video.get("fps") or self.fps)
        self.width = int(video.get("width") or render_w)
        self.height = int(video.get("height") or render_h)
        self.render_width = int(video.get("render_width") or self.width)
        self.render_height = int(video.get("render_height") or self.height)
        self.fit = str(video.get("fit") or effective_fit)
        self.slice_len = int(round(self.audio_chunk_samples * self.fps / max(1, self.sample_rate)))
        self.frame_num = int(video.get("frame_count") or self.slice_len)
        self.motion_frames_num = int(video.get("motion_frames_num") or max(0, self.frame_num - self.slice_len))
        log.info(
            "FlashHead session init OK: session=%s %dx%d (render %dx%d fit=%s) fps=%d chunk_samples=%d",
            self._session_id,
            self.width,
            self.height,
            self.render_width,
            self.render_height,
            self.fit,
            self.fps,
            self.audio_chunk_samples,
        )
        return resp

    async def generate(self, audio_pcm: np.ndarray) -> list[VideoFrameData]:
        if self._ws is None:
            raise RuntimeError("Not connected. Call connect() first.")
        pcm = np.asarray(audio_pcm, dtype=np.int16).reshape(-1)
        payload = MAGIC_AUDIO + pcm.tobytes()
        t0 = time.monotonic()
        await self._ws.send(payload)

        resp = await self._recv_video_payload()
        t_recv = time.monotonic()
        frame_count = struct.unpack("<I", resp[4:8])[0]

        offset = 8
        jpeg_parts: list[bytes] = []
        for _ in range(frame_count):
            jpeg_len = struct.unpack("<I", resp[offset:offset + 4])[0]
            offset += 4
            jpeg_parts.append(resp[offset:offset + jpeg_len])
            offset += jpeg_len
        t_parse = time.monotonic()

        if JPEG_DECODE_WORKERS <= 1 or len(jpeg_parts) <= 1:
            bgr_frames = [_decode_jpeg(jp) for jp in jpeg_parts]
        else:
            global _JPEG_DECODE_EXECUTOR
            if _JPEG_DECODE_EXECUTOR is None:
                _JPEG_DECODE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                    max_workers=JPEG_DECODE_WORKERS,
                    thread_name_prefix="flashhead-jpeg-decode",
                )
            bgr_frames = list(_JPEG_DECODE_EXECUTOR.map(_decode_jpeg, jpeg_parts))
        t_decode = time.monotonic()

        frames = [
            VideoFrameData(
                data=bgr,
                width=bgr.shape[1],
                height=bgr.shape[0],
                timestamp_ms=0.0,
            )
            for bgr in bgr_frames
        ]
        log.info(
            "FlashHead WS chunk: frames=%d payload=%dKB wait=%.2fs parse=%.3fs decode=%.3fs workers=%d",
            frame_count,
            len(resp) // 1024,
            t_recv - t0,
            t_parse - t_recv,
            t_decode - t_parse,
            JPEG_DECODE_WORKERS,
        )
        return frames

    async def _recv_video_payload(self) -> bytes:
        assert self._ws is not None
        while True:
            resp = await self._ws.recv()
            if isinstance(resp, str):
                msg = json.loads(resp)
                if msg.get("type") == "metrics":
                    continue
                if msg.get("type") == "error":
                    raise RuntimeError(f"FlashHead generate error: {msg.get('message')}")
                continue
            if len(resp) < 8 or resp[:4] != MAGIC_VIDEO:
                raise RuntimeError(f"Unexpected response: magic={resp[:4]!r}, len={len(resp)}")
            return resp

    async def close(self, send_close_msg: bool = True) -> None:
        if self._ws is None:
            return
        try:
            if send_close_msg:
                await self._ws.send(json.dumps({"type": "session.close"}))
                resp = json.loads(await self._ws.recv())
                log.info("FlashHead close: %s", resp.get("type"))
        except Exception:
            pass
        try:
            await self._ws.close()
        except Exception:
            pass
        self._ws = None
        self._session_id = ""
