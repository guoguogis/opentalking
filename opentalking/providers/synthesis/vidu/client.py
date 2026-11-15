"""Vidu S2-Avatar (数字人组件版) client.

OpenTalking keeps ownership of ASR / LLM / TTS; Vidu only renders the avatar
and pushes the rendered video into an RTC channel. This client therefore
produces **no local frames**: it feeds 16 kHz PCM to Vidu over the control
WebSocket and the browser renders the RTC video track instead.

Protocol reference (China site):
- create: ``POST {base}/live/s_avatar/component``
- stream: ``wss://{host}/live/v1/external-lives/{live_id}/stream``
- first frame must be ``conn_init`` (type=1); ``conn_init_ack.success`` starts billing
- binary frames are raw PCM s16le mono **24 kHz**, 20 ms (960 bytes) recommended
- ``audio_interrupted`` (type=7) is the barge-in signal
- ``call_hangup`` (type=5) ends the session
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import numpy as np

try:  # pragma: no cover - import guard mirrors flashhead/ws_client.py
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

try:  # pragma: no cover
    import websockets  # noqa: F401
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # pragma: no cover
    websockets = None  # type: ignore[assignment]

from opentalking.core.redis_keys import vidu_rtc_key
from opentalking.core.types.frames import VideoFrameData

log = logging.getLogger(__name__)

#: Vidu expects 24 kHz mono s16le PCM on the binary channel.
VIDU_PCM_SAMPLE_RATE = 24000
#: Vidu recommends 20 ms (960 bytes) or 100 ms (4800 bytes) binary frames.
VIDU_PCM_FRAME_MS = 20
#: Vidu caps one live session at 7200 s.
VIDU_MAX_SESSION_SECONDS = 7200

_SIGNAL_CONN_INIT = 1
_SIGNAL_CONN_INIT_ACK = 2
_SIGNAL_CALL_HANGUP = 5
_SIGNAL_FORCE_HANGUP = 6
_SIGNAL_AUDIO_INTERRUPTED = 7
_SIGNAL_INPUT_TRANSCRIPTION = 9
_SIGNAL_OUTPUT_TRANSCRIPTION = 10

_NOT_READY = "NOT_READY"
_LIVE_ENDED = "LIVE_ENDED"


def resample_pcm16(pcm: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear resample of int16 mono PCM (adequate for TTS speech)."""
    samples = np.asarray(pcm, dtype=np.int16).reshape(-1)
    src = int(src_rate)
    dst = int(dst_rate)
    if samples.size == 0 or src <= 0 or dst <= 0 or src == dst:
        return samples
    out_len = int(round(samples.size * dst / src))
    if out_len <= 0:
        return np.zeros(0, dtype=np.int16)
    src_positions = np.arange(samples.size, dtype=np.float64)
    dst_positions = np.linspace(0.0, samples.size - 1, out_len, dtype=np.float64)
    resampled = np.interp(dst_positions, src_positions, samples.astype(np.float64))
    return np.clip(np.rint(resampled), -32768, 32767).astype(np.int16)


def _encode_image_data_uri(raw: bytes, suffix: str = "") -> str:
    suffix = (suffix or "").lower().lstrip(".")
    mime = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
    }.get(suffix)
    if mime is None:
        mime = "image/png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _ws_base(base_url: str) -> str:
    parts = urlsplit(base_url)
    scheme = "wss" if parts.scheme.lower() in {"https", "wss"} else "ws"
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _http_base(base_url: str) -> str:
    parts = urlsplit(base_url)
    scheme = "https" if parts.scheme.lower() in {"https", "wss"} else "http"
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


class ViduAvatarClient:
    """Audio-in / RTC-video-out client for Vidu S2-Avatar 组件版."""

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.vidu.cn",
        image_uri: str = "",
        rtc_provider: str = "artc",
        managed_rtc: bool = True,
        customer_rtc: dict[str, Any] | None = None,
        fps: float = 25.0,
        sample_rate: int = 16000,
        audio_chunk_samples: int = 4800,
        width: int = 720,
        height: int = 1280,
        idle_timeout_seconds: int = VIDU_MAX_SESSION_SECONDS,
        enable_recording: bool = False,
        persona: str = "",
        voice: str = "",
        redis: Any = None,
        session_id: str = "",
        connect_timeout: float = 30.0,
        http_timeout: float = 120.0,  # image upload + cloud render startup can take a while
        handshake_timeout: float = 20.0,
        retry_delay: float = 1.0,
    ) -> None:
        if websockets is None:  # pragma: no cover
            raise RuntimeError("websockets package required: pip install websockets")
        if httpx is None:  # pragma: no cover
            raise RuntimeError("httpx package required: pip install httpx")

        self._api_key = (api_key or "").strip()
        self._base_url = _http_base(base_url or "https://api.vidu.cn")
        self._image_uri = (image_uri or "").strip()
        self._rtc_provider = (rtc_provider or "artc").strip().lower()
        self._managed_rtc = bool(managed_rtc)
        self._customer_rtc = dict(customer_rtc or {})
        self._idle_timeout_seconds = max(10, min(int(idle_timeout_seconds), VIDU_MAX_SESSION_SECONDS))
        self._enable_recording = bool(enable_recording)
        self._persona = persona or ""
        self._voice = voice or ""
        self._redis = redis
        self._session_id = session_id
        self._connect_timeout = float(connect_timeout)
        self._http_timeout = float(http_timeout)
        self._handshake_timeout = float(handshake_timeout)
        self._retry_delay = max(0.0, float(retry_delay))

        # Attributes consumed by FlashTalkRunner.
        self.fps = float(fps or 25.0)
        self.sample_rate = int(sample_rate or 16000)
        self.audio_chunk_samples = max(1, int(audio_chunk_samples or 4800))
        self.width = int(width or 720)
        self.height = int(height or 1280)
        self.slice_len = 0
        self.frame_num = 0
        self.motion_frames_num = 0

        self._ws: Any = None
        self._recv_task: asyncio.Task[None] | None = None
        self._live_id = ""
        self._client_secret = ""
        self._conn_id = ""
        self._seq = 0
        self._rtc_info: dict[str, Any] = {}
        self._ended = False
        self._last_ack: dict[str, Any] = {}

    # ------------------------------------------------------------------ lifecycle

    @property
    def live_id(self) -> str:
        return self._live_id

    @property
    def ended(self) -> bool:
        return self._ended

    async def connect(self) -> None:
        """Nothing to connect yet; the HTTP/WS session is created in init_session."""
        if not self._api_key:
            raise RuntimeError(
                "Vidu Avatar source is not configured: set VIDU_API_KEY "
                "(or OPENTALKING_VIDU_API_KEY)."
            )

    async def init_session(self, **kwargs: Any) -> dict[str, Any]:
        """Create the Vidu component live session and finish the WS handshake."""
        if self._ws is not None and self._live_id and not self._ended:
            return self._session_payload()

        await self.close(send_close_msg=self._live_id != "")
        self._ended = False
        self._seq = 0
        self._last_ack = {}

        image_uri = self._image_uri or self._resolve_image_uri(kwargs)
        if not image_uri:
            raise RuntimeError(
                "Vidu Avatar source needs an avatar image: configure "
                "OPENTALKING_VIDU_AVATAR_IMAGE_URI or use an avatar bundle with reference.png|.jpg"
            )

        payload = await self._create_live(image_uri)
        live = payload.get("live") or {}
        self._live_id = str(live.get("id") or payload.get("live_id") or "")
        self._client_secret = str(payload.get("client_secret") or "")
        if not self._live_id:
            raise RuntimeError(f"Vidu create session returned no live.id: {payload}")
        self._rtc_info = self._extract_rtc_info(payload)
        if not self._rtc_info.get("channel_id") or not self._rtc_info.get("token"):
            log.warning(
                "Vidu create session returned no RTC join credentials "
                "(keys=%s); the browser will not be able to join the channel",
                sorted(payload),
            )
        self._conn_id = f"opentalking-{self._session_id or 'session'}-{int(time.time())}"

        await self._open_stream()
        await self._publish_rtc_info()
        log.info(
            "Vidu avatar live ready: session=%s live_id=%s rtc_provider=%s managed=%s",
            self._session_id,
            self._live_id,
            self._rtc_info.get("provider") or self._rtc_provider,
            self._managed_rtc,
        )
        return self._session_payload()

    async def prewarm(self) -> dict[str, Any]:
        return self._session_payload()

    async def generate(self, audio_pcm: np.ndarray) -> list[VideoFrameData]:
        """Send PCM to Vidu. Returns no frames: video arrives through RTC."""
        if self._ws is None or self._ended:
            raise RuntimeError("Not connected. Call connect() first.")
        pcm = np.asarray(audio_pcm, dtype=np.int16).reshape(-1)
        if pcm.size == 0:
            return []
        wire = resample_pcm16(pcm, self.sample_rate, VIDU_PCM_SAMPLE_RATE)
        frame_samples = max(1, VIDU_PCM_SAMPLE_RATE * VIDU_PCM_FRAME_MS // 1000)
        started = time.monotonic()
        first_sent_at = 0.0
        for start in range(0, wire.size, frame_samples):
            chunk = wire[start : start + frame_samples]
            if chunk.size < frame_samples:
                chunk = np.pad(chunk, (0, frame_samples - chunk.size))
            await self._ws.send(chunk.tobytes())
            if not first_sent_at:
                first_sent_at = time.monotonic()
                log.info(
                    "Vidu first PCM chunk sent: session=%s at=%.6f",
                    self._session_id,
                    first_sent_at,
                )
        log.info(
            "Vidu PCM sent: session=%s in=%d out=%d first_chunk_ms=%.0f took=%.3fs",
            self._session_id,
            pcm.size,
            wire.size,
            (first_sent_at - started) * 1000.0 if first_sent_at else 0.0,
            time.monotonic() - started,
        )
        return []

    async def send_transcription(self, text: str, *, user: bool = False) -> None:
        """Send type=9 (user speech) or type=10 (avatar reply) for gesture matching."""
        if not text or self._ws is None or self._ended:
            return
        signal = _SIGNAL_INPUT_TRANSCRIPTION if user else _SIGNAL_OUTPUT_TRANSCRIPTION
        await self._send_signal(
            signal,
            {
                "text_msg": {
                    "msg_id": f"{self._conn_id}-{self._seq + 1}",
                    "content": text,
                    "timestamp": int(time.time() * 1000),
                }
            },
        )

    async def interrupt(self) -> None:
        """Barge-in: tell Vidu to drop the current reply (type=7)."""
        if self._ws is None or self._ended:
            return
        try:
            await self._send_signal(_SIGNAL_AUDIO_INTERRUPTED, {"audio_interrupted": {}})
        except Exception:
            log.warning("Vidu audio_interrupted send failed: session=%s", self._session_id, exc_info=True)

    async def close(self, send_close_msg: bool = True) -> None:
        ws = self._ws
        self._ws = None
        recv_task = self._recv_task
        self._recv_task = None
        if ws is not None and send_close_msg and not self._ended:
            try:
                self._seq += 1
                await ws.send(
                    json.dumps(
                        {
                            "type": _SIGNAL_CALL_HANGUP,
                            "live_id": self._live_id,
                            "conn_id": self._conn_id,
                            "seq_id": self._seq,
                            "payload": {"hangup": {"hangup_reason": "client_hangup"}},
                        }
                    )
                )
            except Exception:
                log.debug("Vidu hangup send failed", exc_info=True)
        if recv_task is not None:
            recv_task.cancel()
            try:
                await recv_task
            except (asyncio.CancelledError, Exception):
                pass
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                log.debug("Vidu ws close failed", exc_info=True)
        self._live_id = ""
        self._client_secret = ""
        self._rtc_info = {}
        self._ended = False

    # ------------------------------------------------------------------ internals

    def _session_payload(self) -> dict[str, Any]:
        return {
            "provider": "vidu",
            "live_id": self._live_id,
            "conn_id": self._conn_id,
            "ws_url": self._stream_url() if self._live_id else "",
            "rtc": dict(self._rtc_info),
            "client_secret": self._client_secret,
            "fps": self.fps,
            "sample_rate": self.sample_rate,
        }

    def _resolve_image_uri(self, kwargs: dict[str, Any]) -> str:
        ref_image = kwargs.get("ref_image")
        if isinstance(ref_image, (bytes, bytearray)):
            return _encode_image_data_uri(bytes(ref_image))
        if isinstance(ref_image, (str, Path)):
            path = Path(ref_image)
            if path.is_file():
                return _encode_image_data_uri(path.read_bytes(), path.suffix)
        avatar_path = kwargs.get("avatar_path")
        if avatar_path:
            base = Path(avatar_path)
            for name in ("reference.png", "reference.jpg", "reference.jpeg"):
                candidate = base / name
                if candidate.is_file():
                    return _encode_image_data_uri(candidate.read_bytes(), candidate.suffix)
        return ""

    async def _create_live(self, image_uri: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": "vidu-s2",
            "image_uri": image_uri,
            "idle_timeout_seconds": self._idle_timeout_seconds,
        }
        if self._managed_rtc:
            body["managed_rtc"] = {"provider": self._rtc_provider}
        else:
            body["customer_rtc"] = {"provider": self._rtc_provider, **self._customer_rtc}
        if self._enable_recording:
            body["enable_recording"] = True

        url = f"{self._base_url}/live/s_avatar/component"
        headers = {"Authorization": f"Token {self._api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=20.0,
                read=self._http_timeout,
                write=self._http_timeout,
                pool=20.0,
            )
        ) as client:
            response = await client.post(url, json=body, headers=headers)
        if response.status_code >= 400:
            raise RuntimeError(
                f"Vidu create session failed: HTTP {response.status_code} {response.text[:400]}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"Vidu create session returned unexpected payload: {payload!r}")
        return payload

    def _extract_rtc_info(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Pick the RTC join credentials out of a create-session response.

        Verified against the live API (2026-11): a managed-RTC create returns
        ``managed_rtc`` at the top level, not ``rtc``:

            {"live": {...}, "client_secret": "...", "managed_rtc": {
                "app_id", "channel_id", "user_id", "token",
                "token_expire_at", "provider"}}

        Older/other shapes (``rtc``) and customer-provided channels are accepted
        too so the client keeps working across the product variants.
        """
        info: dict[str, Any] = {}
        for key in ("rtc", "managed_rtc", "customer_rtc"):
            candidate = payload.get(key)
            if isinstance(candidate, dict) and candidate:
                info.update(candidate)
                break
        info.setdefault("provider", self._rtc_provider)
        info.setdefault("managed", self._managed_rtc)
        if not info.get("channel_id") and not self._managed_rtc:
            info.update({k: v for k, v in self._customer_rtc.items() if v})
        return info

    def _stream_url(self) -> str:
        base = _ws_base(self._base_url)
        query = f"conn_id={self._conn_id}"
        if self._client_secret:
            query = f"{query}&client_secret={self._client_secret}"
        return f"{base}/live/v1/external-lives/{self._live_id}/stream?{query}"

    async def _open_stream(self) -> None:
        url = self._stream_url()
        self._ws = await ws_connect(
            url,
            max_size=16 * 1024 * 1024,
            open_timeout=self._connect_timeout,
            close_timeout=10,
        )
        await self._handshake()
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def _handshake(self) -> None:
        delay = self._retry_delay
        last_error = ""
        for attempt in range(8):
            await self._send_signal(_SIGNAL_CONN_INIT, {"conn_init": {"version": 1}})
            ack = await self._await_ack()
            self._last_ack = ack
            if ack.get("success"):
                return
            last_error = str(ack.get("error_code") or "")
            if last_error == _NOT_READY:
                log.info(
                    "Vidu renderer not ready (attempt %d), retrying in %.1fs: session=%s",
                    attempt + 1,
                    delay,
                    self._session_id,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 1.6, 8.0)
                continue
            if last_error == _LIVE_ENDED:
                raise RuntimeError("Vidu live session already ended; create a new session")
            raise RuntimeError(f"Vidu conn_init failed: {ack}")
        raise RuntimeError(f"Vidu conn_init not ready after retries: {last_error or 'unknown'}")

    async def _await_ack(self) -> dict[str, Any]:
        assert self._ws is not None
        deadline = time.monotonic() + self._handshake_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Vidu conn_init_ack timeout")
            try:
                message = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("Vidu conn_init_ack timeout") from exc
            if isinstance(message, (bytes, bytearray)):
                continue
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("type") == _SIGNAL_CONN_INIT_ACK:
                payload = data.get("payload") or {}
                ack = payload.get("conn_init_ack") if isinstance(payload, dict) else {}
                return dict(ack) if isinstance(ack, dict) else {}

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        ws = self._ws
        try:
            while True:
                message = await ws.recv()
                if isinstance(message, (bytes, bytearray)):
                    continue
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                signal = data.get("type")
                if signal == _SIGNAL_FORCE_HANGUP:
                    reason = ((data.get("payload") or {}).get("hangup") or {}).get("hangup_reason")
                    log.warning("Vidu force_hangup: session=%s reason=%s", self._session_id, reason)
                    self._ended = True
                    break
                if signal in {12, 14, 98}:
                    log.info("Vidu signal type=%s payload=%s", signal, data.get("payload"))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.info("Vidu control channel closed: session=%s", self._session_id, exc_info=True)

    async def _send_signal(self, signal: int, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise RuntimeError("Not connected. Call connect() first.")
        self._seq += 1
        await self._ws.send(
            json.dumps(
                {
                    "type": signal,
                    "live_id": self._live_id,
                    "conn_id": self._conn_id,
                    "seq_id": self._seq,
                    "payload": payload,
                }
            )
        )

    async def _publish_rtc_info(self) -> None:
        if self._redis is None or not self._session_id:
            return
        try:
            await self._redis.set(
                vidu_rtc_key(self._session_id),
                json.dumps(
                    {
                        "live_id": self._live_id,
                        "rtc": self._rtc_info,
                        "fps": self.fps,
                        "created_at": time.time(),
                    }
                ),
                ex=VIDU_MAX_SESSION_SECONDS,
            )
        except Exception:
            log.warning("Failed to publish Vidu RTC info: session=%s", self._session_id, exc_info=True)


__all__ = [
    "VIDU_MAX_SESSION_SECONDS",
    "VIDU_PCM_FRAME_MS",
    "VIDU_PCM_SAMPLE_RATE",
    "ViduAvatarClient",
    "resample_pcm16",
]
