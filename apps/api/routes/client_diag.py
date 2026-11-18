"""Browser-side diagnostics sink.

The Vidu overlay is rendered entirely in the browser (Aliyun RTC SDK), so a
failed channel join or a frozen picture leaves no trace on the server. The WebUI
posts a small JSON heartbeat here and it is written to ``service.log``, which is
the only place an operator can see what the user's machine actually received.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request, Response

log = logging.getLogger(__name__)

router = APIRouter(tags=["diagnostics"])

#: Keep one line readable in a log file without truncating a real payload.
_MAX_FIELD = 400


def _shrink(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_FIELD:
        return value[:_MAX_FIELD] + "…"
    if isinstance(value, dict):
        return {str(k)[:40]: _shrink(v) for k, v in list(value.items())[:24]}
    return value


@router.post("/client-diag")
async def client_diag(request: Request) -> Response:
    """Accept a diag heartbeat from the WebUI and log it (never fails)."""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - beacons may be plain text
        raw = await request.body()
        payload = {"raw": raw[:_MAX_FIELD].decode("utf-8", "replace")}
    ua = request.headers.get("user-agent", "")
    forwarded = request.headers.get("x-forwarded-for", request.client.host if request.client else "")
    log.info(
        "client diag: ip=%s ua=%r data=%s",
        forwarded,
        ua[:160],
        _shrink(payload),
    )
    return Response(status_code=204)
