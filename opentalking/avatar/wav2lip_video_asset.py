"""Shared helpers to turn a source video into a Wav2Lip "dynamic" avatar asset.

Both the offline CLI (``scripts/prepare_wav2lip_video_asset.py``) and the WebUI
custom-avatar upload path use this module so a video avatar is always prepared
the same way:

- ``frames/frame_XXXXX.jpg``: downscaled frame sequence
- ``frames/mouth_metadata.json``: per-frame mouth landmarks (mediapipe)
- ``reference.png`` / ``preview.png``: first frame, used as the avatar still
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
from PIL import Image

from opentalking.avatar import mouth_metadata


@dataclass
class Wav2LipVideoAsset:
    """Result of building a Wav2Lip frame-sequence asset from a video."""

    width: int
    height: int
    fps: int
    source_fps: float
    source_frame_count: int
    extracted_frame_count: int
    source_video_rel: str
    frame_dir_rel: str = "frames"
    frame_metadata_rel: str = "frames/mouth_metadata.json"
    reference_image_rel: str = "reference.png"
    source_image_hash: str | None = None
    mouth_polygon_source: str | None = None
    face_box: list[float] | None = None
    animation: dict[str, Any] | None = None


def _write_frame_metadata(
    frame_path: Path,
    frame_bgr: Any,
    detector: Any | None = None,
) -> dict[str, Any] | None:
    landmarks = mouth_metadata.detect_mouth_landmarks(frame_bgr)
    if landmarks is None:
        return None
    height, width = frame_bgr.shape[:2]
    face_box = mouth_metadata._normalized_face_box(landmarks, width=width, height=height)
    metadata: dict[str, Any] = {
        "mouth_polygon_source": "mediapipe",
        "source_frame_hash": mouth_metadata.image_file_sha256(frame_path),
        "face_box": face_box,
        "animation": mouth_metadata._animation_from_landmarks(landmarks, width=width, height=height),
    }
    if detector is not None:
        from apps.cli.prepare_cache import _normalized_model_crop_from_coords

        coords = detector._detect_face_box(frame_bgr)
        metadata["model_crop"] = _normalized_model_crop_from_coords(coords, width=width, height=height)
        metadata["model_crop_source"] = "wav2lip_detector"
    return metadata


def _build_detector(
    *,
    wav2lip_model_root: Path | None,
    wav2lip_face_det_device: str | None,
) -> Any:
    import os

    from opentalking.models.wav2lip.runtime import Wav2LipRealtimeRuntime

    if wav2lip_face_det_device:
        os.environ["OPENTALKING_WAV2LIP_FACE_DET_DEVICE"] = wav2lip_face_det_device
    return Wav2LipRealtimeRuntime(
        models_dir=(Path(wav2lip_model_root).expanduser().resolve() if wav2lip_model_root else None),
        device="cpu",
    )


def build_wav2lip_video_asset(
    *,
    source_video: Path,
    out_dir: Path,
    max_frames: int = 125,
    target_width: int | None = None,
    target_height: int | None = None,
    fps: int | None = None,
    skip_model_crop: bool = True,
    wav2lip_model_root: Path | None = None,
    wav2lip_face_det_device: str | None = None,
) -> Wav2LipVideoAsset:
    """Extract a frame sequence + mouth metadata from ``source_video``.

    Writes ``frames/``, ``frames/mouth_metadata.json``, ``reference.png`` and
    ``preview.png`` under ``out_dir`` and returns the derived manifest values.
    """
    source_video = Path(source_video)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = out_dir / "frames"
    source_dir = out_dir / "source"
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True)
    source_dir.mkdir(parents=True, exist_ok=True)
    copied_source = source_dir / source_video.name
    if source_video.resolve() != copied_source.resolve():
        shutil.copy2(source_video, copied_source)

    cap = cv2.VideoCapture(str(source_video))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open source video: {source_video}")
    source_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    effective_fps = int(fps or round(source_fps) or 25)
    source_frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

    frames: dict[str, Any] = {}
    missing_frames: list[str] = []
    first_frame_path: Path | None = None
    detector = (
        None
        if skip_model_crop
        else _build_detector(
            wav2lip_model_root=wav2lip_model_root,
            wav2lip_face_det_device=wav2lip_face_det_device,
        )
    )
    index = 0
    while index < max(1, int(max_frames)):
        ok, frame = cap.read()
        if not ok:
            break
        if target_width and target_height:
            frame = cv2.resize(frame, (target_width, target_height), interpolation=cv2.INTER_AREA)
        frame_name = f"frame_{index:05d}.jpg"
        frame_path = frames_dir / frame_name
        # cv2.imwrite cannot handle non-ASCII paths on Windows, so encode in
        # memory and write the bytes ourselves (avatar ids may contain CJK).
        encoded_ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        if not encoded_ok:
            raise RuntimeError(f"Failed to encode frame {index} from {source_video}")
        frame_path.write_bytes(encoded.tobytes())
        if first_frame_path is None:
            first_frame_path = frame_path
        metadata = _write_frame_metadata(frame_path, frame, detector)
        if metadata is None:
            missing_frames.append(frame_name)
        else:
            frames[frame_name] = metadata
        index += 1
    cap.release()
    if first_frame_path is None:
        raise RuntimeError(f"No frames extracted from source video: {source_video}")

    reference = out_dir / "reference.png"
    preview = out_dir / "preview.png"
    first_img = Image.open(first_frame_path).convert("RGB")
    first_img.save(reference, format="PNG")
    first_img.save(preview, format="PNG")
    width, height = first_img.size

    (frames_dir / "mouth_metadata.json").write_text(
        json.dumps(
            {"version": 1, "frames": frames, "missing_frames": missing_frames},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    asset = Wav2LipVideoAsset(
        width=width,
        height=height,
        fps=effective_fps,
        source_fps=source_fps,
        source_frame_count=source_frame_count,
        extracted_frame_count=index,
        source_video_rel=copied_source.relative_to(out_dir).as_posix(),
        source_image_hash=mouth_metadata.image_file_sha256(reference),
    )
    if frames:
        first_meta = next(iter(frames.values()))
        asset.mouth_polygon_source = first_meta.get("mouth_polygon_source")
        asset.face_box = first_meta.get("face_box")
        asset.animation = first_meta.get("animation")
    return asset
