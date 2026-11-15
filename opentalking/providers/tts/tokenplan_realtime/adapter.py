"""TokenPlan realtime TTS adapter (OpenAI Realtime semantics over ``/api-ws/v1/realtime``).

The Bailian TokenPlan host exposes ``qwen-audio-3.0-realtime-plus`` on::

    wss://token-plan.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime?model=<model>

It speaks the OpenAI Realtime event protocol (NOT the DashScope ``run-task`` protocol used by
``providers/tts/dashscope_qwen``), so text-to-speech is driven by::

    session.update            -> session.updated
    conversation.item.create  -> conversation.item.created
    response.create           -> response.created
                              -> response.audio.delta (base64 PCM, repeated)
                              -> response.audio.done

First audio arrives in about 1.3 s (measured), versus 3-6 s for the non-streaming HTTP
``SpeechSynthesizer`` path, which is what makes the digital human start talking sooner.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import AsyncIterator

import numpy as np
from websockets.asyncio.client import connect

from opentalking.core.types.frames import AudioChunk

DEFAULT_BASE_URL = "wss://token-plan.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime"
DEFAULT_MODEL = "qwen-audio-3.0-realtime-plus"
DEFAULT_VOICE = "longanqian"
# qwen-audio-3.0-realtime-plus streams 24 kHz mono 16-bit little-endian PCM.
NATIVE_SAMPLE_RATE = 24000


class TokenPlanRealtimeTTSAdapter:
    """Streaming TTS backed by the TokenPlan realtime websocket."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "",
        model: str = "",
        voice: str = "",
        sample_rate: int = NATIVE_SAMPLE_RATE,
        chunk_ms: float = 300.0,
        timeout_s: float = 30.0,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).strip()
        self.model = (model or DEFAULT_MODEL).strip()
        self.voice = (voice or DEFAULT_VOICE).strip()
        self.sample_rate = int(sample_rate or NATIVE_SAMPLE_RATE)
        self.chunk_ms = float(chunk_ms or 300.0)
        self.timeout_s = float(timeout_s or 30.0)

    def _url(self) -> str:
        url = self.base_url
        if "model=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}model={self.model}"
        return url

    async def synthesize_stream(self, text: str, *, voice: str | None = None) -> AsyncIterator[AudioChunk]:
        if not self.api_key:
            raise RuntimeError(
                "TokenPlan realtime TTS selected but OPENTALKING_TTS_TOKENPLAN_API_KEY is empty."
            )
        payload_text = (text or "").strip()
        if not payload_text:
            return

        used_voice = (voice or self.voice).strip() or DEFAULT_VOICE
        chunk_samples = max(1, int(self.sample_rate * self.chunk_ms / 1000.0))
        buffer = bytearray()
        emitted = False

        async with connect(
            self._url(),
            additional_headers={"Authorization": f"Bearer {self.api_key}"},
            open_timeout=self.timeout_s,
            max_size=64 << 20,
        ) as ws:
            await asyncio.wait_for(ws.recv(), timeout=self.timeout_s)  # session.created
            await ws.send(
                json.dumps(
                    {
                        "type": "session.update",
                        "session": {
                            "modalities": ["text", "audio"],
                            "voice": used_voice,
                            "instructions": "只朗读用户提供的文本，不要增删内容。",
                            "output_audio_format": "pcm",
                        },
                    }
                )
            )
            await ws.send(
                json.dumps(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": payload_text}],
                        },
                    }
                )
            )
            await ws.send(
                json.dumps({"type": "response.create", "response": {"modalities": ["text", "audio"]}})
            )

            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=self.timeout_s)
                event = json.loads(raw)
                event_type = event.get("type", "")
                if event_type == "response.audio.delta":
                    buffer.extend(base64.b64decode(event.get("delta", "")))
                    # 首块用更小的阈值，让数字人尽快开始动嘴（后续恢复常规分块）。
                    threshold = (chunk_samples * 2) if emitted else max(2, int(self.sample_rate * 0.1) * 2)
                    while len(buffer) >= threshold:
                        piece = bytes(buffer[: chunk_samples * 2])
                        del buffer[: chunk_samples * 2]
                        pcm = np.frombuffer(piece, dtype="<i2").copy()
                        emitted = True
                        yield AudioChunk(
                            data=pcm,
                            sample_rate=self.sample_rate,
                            duration_ms=1000.0 * pcm.size / self.sample_rate,
                        )
                elif event_type in {"response.audio.done", "response.done"}:
                    break
                elif event_type == "error":
                    detail = event.get("error") or event
                    raise RuntimeError(f"TokenPlan realtime TTS error: {json.dumps(detail, ensure_ascii=False)[:300]}")

        if len(buffer) >= 2:
            if len(buffer) % 2:
                buffer = buffer[:-1]
            pcm = np.frombuffer(bytes(buffer), dtype="<i2").copy()
            if pcm.size:
                yield AudioChunk(
                    data=pcm,
                    sample_rate=self.sample_rate,
                    duration_ms=1000.0 * pcm.size / self.sample_rate,
                )


def build_tokenplan_realtime_tts() -> TokenPlanRealtimeTTSAdapter:
    """Build from ``OPENTALKING_TTS_TOKENPLAN_*`` environment variables."""
    return TokenPlanRealtimeTTSAdapter(
        api_key=os.environ.get("OPENTALKING_TTS_TOKENPLAN_API_KEY", ""),
        base_url=os.environ.get("OPENTALKING_TTS_TOKENPLAN_BASE_URL", ""),
        model=os.environ.get("OPENTALKING_TTS_TOKENPLAN_MODEL", ""),
        voice=os.environ.get("OPENTALKING_TTS_TOKENPLAN_VOICE", ""),
    )
