from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from opentalking.providers.offline.duix import DuixClient, resolve_duix_base_url
from opentalking.providers.offline.duix_assets import resolve_asset_dir

router = APIRouter(prefix="/duix", tags=["duix"])

_MEDIA_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}


def _asset_root(request: Request) -> Path:
    return resolve_asset_dir(request.app.state.settings).resolve()


@router.get("/assets/{token}/{name}", response_model=None)
async def get_job_asset(token: str, name: str, request: Request) -> FileResponse:
    """Serve an offline-renderer input.

    The renderer downloads these anonymously, so the unguessable token is the
    only capability. Path traversal is rejected before touching the filesystem.
    """
    if not token.isalnum() or len(token) < 16 or len(token) > 64:
        raise HTTPException(status_code=404, detail="not found")
    if "/" in name or "\\" in name or name in {"", ".", ".."}:
        raise HTTPException(status_code=404, detail="not found")
    root = _asset_root(request)
    path = (root / token / name).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(
        path,
        media_type=_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/status", response_model=None)
async def duix_status(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    client = DuixClient(settings)
    return {
        "id": "duix",
        "configured": client.configured,
        "base_url": resolve_duix_base_url(settings),
        "connected": await client.probe(),
        "asset_base_url": str(getattr(settings, "duix_asset_base_url", "") or ""),
        "asset_dir": str(_asset_root(request)),
    }
