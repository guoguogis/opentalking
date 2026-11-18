"""DUIX-backed offline video creation.

The streaming path renders frames locally and muxes them with the audio; DUIX
instead takes the audio *and* a driving video, renders on the vendor side and
returns a finished MP4. Everything here is therefore about staging inputs,
supervising the remote task and registering the result as a normal export.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import tempfile
import time
import uuid
import wave
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

import numpy as np

from opentalking.export_store import create_video_export_from_file
from opentalking.providers.offline.duix import DuixBusyError, DuixClient, DuixError
from opentalking.providers.offline.duix_assets import DuixAssetPublisher

log = logging.getLogger(__name__)

MODEL_ID = "duix"

ProgressSink = Callable[[float, str], Awaitable[None] | None]

_DRIVING_SUFFIXES = (".mp4", ".mov", ".webm", ".mkv")
_DRIVING_DIRS = ("source", "drive", "driving")
_DRIVING_NAMES = ("driving.mp4", "drive.mp4", "reference_video.mp4", "source.mp4")


def _settings_str(settings: object, name: str, default: str) -> str:
    raw = getattr(settings, name, None)
    text = str(raw).strip() if raw is not None else ""
    return text or default


def _settings_int(settings: object, name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default


def resolve_avatar_dir(settings: object, avatar_id: str) -> Path:
    root = Path(_settings_str(settings, "avatars_dir", "./data/avatars")).expanduser().resolve()
    candidate = (root / str(avatar_id or "").strip()).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"invalid avatar id: {avatar_id!r}") from exc
    if not candidate.is_dir():
        raise FileNotFoundError(f"avatar not found: {avatar_id}")
    return candidate


def resolve_driving_video(avatar_dir: Path) -> Path:
    """Find the driving video DUIX should animate.

    Avatars keep their original clip under ``source/``; fall back to a few
    conventional file names at the bundle root.
    """
    for folder in _DRIVING_DIRS:
        directory = avatar_dir / folder
        if not directory.is_dir():
            continue
        clips = sorted(
            (p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in _DRIVING_SUFFIXES),
            key=lambda p: p.stat().st_size,
            reverse=True,
        )
        if clips:
            return clips[0]
    for name in _DRIVING_NAMES:
        candidate = avatar_dir / name
        if candidate.is_file():
            return candidate
    clips = sorted(
        (p for p in avatar_dir.iterdir() if p.is_file() and p.suffix.lower() in _DRIVING_SUFFIXES),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    if clips:
        return clips[0]
    raise ValueError(
        "DUIX 需要形象自带驱动视频（avatars/<id>/source/*.mp4），"
        f"当前形象目录里没有可用的视频：{avatar_dir}"
    )


def _ffmpeg(settings: object) -> str:
    return _settings_str(settings, "ffmpeg_bin", "ffmpeg")


def audio_to_wav(settings: object, source: Path, destination: Path) -> Path:
    """Normalise any input audio to 16 kHz mono PCM WAV (DUIX-friendly)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            _ffmpeg(settings),
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0 or not destination.is_file():
        raise ValueError(f"无法解码音频：{result.stderr.strip()[:200] or 'ffmpeg failed'}")
    return destination


def pcm_to_wav(pcm: np.ndarray, destination: Path, sample_rate: int = 16000) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(destination), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(pcm.astype(np.int16, copy=False).tobytes())
    return destination


def wav_duration(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate() or 1
            return float(handle.getnframes()) / float(rate)
    except (wave.Error, OSError):
        return None


class DuixVideoCreationService:
    """Runs one DUIX task and stores the MP4 as a normal export."""

    def __init__(self, settings: object) -> None:
        self.settings = settings
        self.client = DuixClient(settings)
        self.publisher = DuixAssetPublisher(settings)

    async def create_from_audio_file(
        self,
        *,
        avatar_id: str,
        upload_path: Path,
        title: str = "",
        on_progress: ProgressSink | None = None,
    ) -> dict[str, Any]:
        avatar_dir = resolve_avatar_dir(self.settings, avatar_id)
        driving = resolve_driving_video(avatar_dir)
        with tempfile.TemporaryDirectory(prefix="opentalking_duix_") as tmp:
            wav = audio_to_wav(self.settings, upload_path, Path(tmp) / "speech.wav")
            return await self._render(
                avatar_id=avatar_id,
                avatar_dir=avatar_dir,
                driving=driving,
                audio_wav=wav,
                title=title,
                on_progress=on_progress,
            )

    async def create_from_pcm(
        self,
        *,
        avatar_id: str,
        pcm: np.ndarray,
        title: str = "",
        sample_rate: int = 16000,
        on_progress: ProgressSink | None = None,
    ) -> dict[str, Any]:
        avatar_dir = resolve_avatar_dir(self.settings, avatar_id)
        driving = resolve_driving_video(avatar_dir)
        with tempfile.TemporaryDirectory(prefix="opentalking_duix_") as tmp:
            wav = pcm_to_wav(pcm, Path(tmp) / "speech.wav", sample_rate=sample_rate)
            return await self._render(
                avatar_id=avatar_id,
                avatar_dir=avatar_dir,
                driving=driving,
                audio_wav=wav,
                title=title,
                on_progress=on_progress,
            )

    async def _notify(self, sink: ProgressSink | None, value: float, stage: str) -> None:
        if sink is None:
            return
        result = sink(max(0.0, min(1.0, value)), stage)
        if asyncio.iscoroutine(result):
            await result

    async def _render(
        self,
        *,
        avatar_id: str,
        avatar_dir: Path,
        driving: Path,
        audio_wav: Path,
        title: str,
        on_progress: ProgressSink | None,
    ) -> dict[str, Any]:
        job_id = f"duix_{uuid.uuid4().hex[:12]}"
        code = f"ot{uuid.uuid4().hex[:16]}"
        token = self.publisher.new_token()
        await self._notify(on_progress, 0.02, "publishing_inputs")
        audio_url = self.publisher.publish(token, audio_wav, "speech.wav")
        video_url = self.publisher.publish(token, driving, driving.name)
        log.info(
            "duix job %s staging done: audio=%s driving=%s (%s)",
            job_id,
            audio_url,
            video_url,
            driving.name,
        )

        try:
            await self._notify(on_progress, 0.05, "submitting")
            await self._submit_with_backoff(code, audio_url, video_url)

            async def report(progress: float, _state: Mapping[str, Any]) -> None:
                # Reserve the tail of the bar for the download + export steps.
                await self._notify(on_progress, 0.05 + 0.85 * (float(progress) / 100.0), "rendering")

            try:
                await self.client.wait(code, on_progress=report)
            except DuixError as exc:
                text = str(exc)
                if "download failed" in text.lower():
                    raise DuixError(
                        f"{text}（DUIX 拉不到 {audio_url} / {video_url}。"
                        "该渲染器没有公网出口，OPENTALKING_DUIX_ASSET_BASE_URL "
                        "必须指向它所在网络内可达的地址）"
                    ) from exc
                raise
            await self._notify(on_progress, 0.92, "downloading")

            work_dir = Path(tempfile.mkdtemp(prefix="opentalking_duix_out_"))
            try:
                downloaded = await self.client.download(code, work_dir / "result.mp4")
                await self._notify(on_progress, 0.96, "exporting")
                item = create_video_export_from_file(
                    Path(_settings_str(self.settings, "exports_dir", "./data/exports")),
                    source=downloaded,
                    mime_type="video/mp4",
                    kind="video_creation",
                    title=title.strip() or f"DUIX · {avatar_dir.name}",
                    duration_sec=wav_duration(audio_wav),
                    session_id=None,
                    avatar_id=avatar_id,
                    model=MODEL_ID,
                    max_bytes=_settings_int(self.settings, "export_max_bytes", 1024 * 1024 * 1024),
                    source_job_id=job_id,
                )
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)
            await self._notify(on_progress, 1.0, "done")
            log.info(
                "duix job %s finished: export=%s size=%sB",
                job_id,
                item.get("id"),
                item.get("size_bytes"),
            )
            return {
                "job_id": job_id,
                "status": "done",
                "source": MODEL_ID,
                "export_video": item,
                "duix": {
                    "code": code,
                    "model": MODEL_ID,
                    "driving_video": driving.name,
                    "resolution": f"{_settings_int(self.settings, 'duix_width', 0)}x"
                    f"{_settings_int(self.settings, 'duix_height', 0)}",
                },
            }
        finally:
            self.publisher.cleanup(token)

    async def _submit_with_backoff(self, code: str, audio_url: str, video_url: str) -> None:
        """Retry while the single render slot is busy (code 10001)."""
        deadline = time.monotonic() + max(60.0, self.client.task_timeout)
        delay = max(2.0, self.client.busy_retry)
        while True:
            try:
                await self.client.submit(code, audio_url, video_url)
                return
            except DuixBusyError:
                if time.monotonic() >= deadline:
                    raise DuixError("DUIX 单任务槽位长时间被占用，请稍后重试") from None
                log.info("duix slot busy, retrying in %.0fs", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, 60.0)
