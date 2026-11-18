"""Serve a tiny browser self-test page from the API app.

The page lives under the API prefix on purpose: the nginx rule for
``/opentalking/`` uses ``alias`` + SPA fallback, which 403s unknown static
files, while ``/opentalking/api/`` is proxied straight to this app.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["diagnostics"])

_PAGE = Path(__file__).with_name("selftest.html")


@router.get("/selftest.html", response_class=HTMLResponse, include_in_schema=False)
async def selftest_page() -> HTMLResponse:
    if not _PAGE.exists():
        return HTMLResponse("<h1>selftest.html missing</h1>", status_code=500)
    return HTMLResponse(_PAGE.read_text(encoding="utf-8"))
