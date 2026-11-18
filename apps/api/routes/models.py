from __future__ import annotations

from fastapi import APIRouter, Request

from opentalking.providers.offline.duix import DuixClient
from opentalking.providers.synthesis import SYNTHESIS_PROVIDERS
from opentalking.providers.synthesis.availability import resolve_model_statuses

router = APIRouter(prefix="/models", tags=["models"])

# Offline task-queue renderers. They cannot stream audio, so they stay out of the
# realtime model list; the video-creation workspace is their only entry point.
OFFLINE_MODELS = ("duix",)


async def _offline_statuses(settings: object) -> list[dict[str, str | bool]]:
    client = DuixClient(settings)
    connected = await client.probe()
    if connected:
        reason = "configured"
    elif not client.configured:
        reason = "not_configured"
    else:
        reason = "unreachable"
    return [
        {
            "id": "duix",
            "backend": "duix",
            "connected": connected,
            "reason": reason,
        }
    ]


@router.get("")
async def list_registered_models(
    request: Request,
) -> dict[str, list[dict[str, str | bool]] | list[str] | str | None]:
    settings = request.app.state.settings
    statuses = await resolve_model_statuses(settings)
    offline = await _offline_statuses(settings)
    default_model = (getattr(settings, "default_model", "") or "").strip().lower()
    if default_model not in SYNTHESIS_PROVIDERS:
        default_model = None
    return {
        "models": [status.id for status in statuses],
        "statuses": [status.to_dict() for status in statuses],
        "offline_models": list(OFFLINE_MODELS),
        "offline_statuses": offline,
        "default_model": default_model,
    }
