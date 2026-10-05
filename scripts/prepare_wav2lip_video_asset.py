#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from opentalking.avatar.wav2lip_video_asset import build_wav2lip_video_asset


def prepare_asset(
    *,
    source_video: Path,
    out_dir: Path,
    avatar_id: str,
    name: str,
    target_width: int | None,
    target_height: int | None,
    fps: int | None,
    max_frames: int,
    wav2lip_model_root: Path | None = None,
    wav2lip_face_det_device: str | None = None,
    skip_model_crop: bool = False,
) -> None:
    out_dir = Path(out_dir)
    asset = build_wav2lip_video_asset(
        source_video=source_video,
        out_dir=out_dir,
        max_frames=max_frames,
        target_width=target_width,
        target_height=target_height,
        fps=fps,
        skip_model_crop=skip_model_crop,
        wav2lip_model_root=wav2lip_model_root,
        wav2lip_face_det_device=wav2lip_face_det_device,
    )
    metadata: dict[str, object] = {
        "description": "Preprocessed built-in Wav2Lip video avatar asset.",
        "reference_mode": "frames",
        "frame_dir": asset.frame_dir_rel,
        "frame_metadata": asset.frame_metadata_rel,
        "preprocessed": True,
        "preprocess_version": 1,
        "source_video": asset.source_video_rel,
        "source_fps": asset.source_fps,
        "source_frame_count": asset.source_frame_count,
        "extracted_frame_count": asset.extracted_frame_count,
        "source_image_path": asset.reference_image_rel,
        "source_image_hash": asset.source_image_hash,
    }
    for key in ("mouth_polygon_source", "face_box", "animation"):
        value = getattr(asset, key)
        if value is not None:
            metadata[key] = value
    manifest = {
        "id": avatar_id,
        "name": name,
        "model_type": "wav2lip",
        "fps": asset.fps,
        "sample_rate": 16000,
        "width": asset.width,
        "height": asset.height,
        "version": "1.0",
        "metadata": metadata,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare a built-in preprocessed Wav2Lip video avatar asset.")
    parser.add_argument("--source-video", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--avatar-id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--target-width", type=int)
    parser.add_argument("--target-height", type=int)
    parser.add_argument("--fps", type=int)
    parser.add_argument("--max-frames", type=int, default=125)
    parser.add_argument("--wav2lip-model-root", type=Path)
    parser.add_argument("--wav2lip-face-det-device")
    parser.add_argument("--skip-model-crop", action="store_true")
    args = parser.parse_args()
    prepare_asset(
        source_video=args.source_video,
        out_dir=args.out,
        avatar_id=args.avatar_id,
        name=args.name,
        target_width=args.target_width,
        target_height=args.target_height,
        fps=args.fps,
        max_frames=max(1, args.max_frames),
        wav2lip_model_root=args.wav2lip_model_root,
        wav2lip_face_det_device=args.wav2lip_face_det_device,
        skip_model_crop=args.skip_model_crop,
    )


if __name__ == "__main__":
    main()
