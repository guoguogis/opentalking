"""Vidu S2-Avatar synthesis provider (cloud-rendered avatar via RTC)."""

from opentalking.providers.synthesis.vidu.client import (
    VIDU_MAX_SESSION_SECONDS,
    VIDU_PCM_FRAME_MS,
    VIDU_PCM_SAMPLE_RATE,
    ViduAvatarClient,
    resample_pcm16,
)

__all__ = [
    "VIDU_MAX_SESSION_SECONDS",
    "VIDU_PCM_FRAME_MS",
    "VIDU_PCM_SAMPLE_RATE",
    "ViduAvatarClient",
    "resample_pcm16",
]
