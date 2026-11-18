"""DUIX offline renderer client.

DUIX is not a streaming model: it takes an audio URL plus a driving-video URL,
renders a lip-synced MP4 and hands it back through a task queue

    POST /duix/easy/submit      {"code", "audio_url", "video_url"}
    GET  /duix/easy/query?code= -> {status: 1 running, 2 done, 3 failed}
    GET  /duix-files/<code>-r.mp4

The upstream wraps every response in ``{"code": 10000|10001|..., "data": ...}``
where ``10001`` means "busy, retry later" - the renderer has a single task slot.
The finished task is dropped from memory as soon as it is queried once, so the
result must be downloaded immediately.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

# Envelope codes returned in the top-level "code" field.
CODE_OK = 10000
CODE_BUSY = 10001
CODE_NOT_FOUND = 10004

# Task states inside data.status.
STATUS_RUNNING = 1
STATUS_SUCCESS = 2
STATUS_FAILED = 3

ProgressCallback = Callable[[float, dict[str, Any]], Awaitable[None] | None]


class DuixError(RuntimeError):
    """Raised when DUIX rejects a task or reports a failure."""


class DuixBusyError(DuixError):
    """Raised when the single render slot is occupied (code 10001)."""


@dataclass(frozen=True)
class DuixTaskState:
    code: str
    status: int
    progress: float
    message: str
    result: str

    @property
    def done(self) -> bool:
        return self.status in (STATUS_SUCCESS, STATUS_FAILED)


def resolve_duix_base_url(settings: Any) -> str:
    """Return the DUIX API root, defaulting to "<omnirt_endpoint>/duix"."""
    explicit = str(getattr(settings, "duix_base_url", "") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    endpoint = str(getattr(settings, "omnirt_endpoint", "") or "").strip()
    if not endpoint:
        return ""
    parts = urlsplit(endpoint)
    scheme_map = {"ws": "http", "wss": "https"}
    scheme = scheme_map.get(parts.scheme.lower(), parts.scheme.lower())
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/") + "/duix", "", ""))


def duix_headers(settings: Any) -> dict[str, str]:
    key = str(getattr(settings, "duix_api_key", "") or "").strip() or str(
        getattr(settings, "omnirt_api_key", "") or ""
    ).strip()
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


class DuixClient:
    """Minimal async wrapper around the DUIX task queue."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.base_url = resolve_duix_base_url(settings)
        self.headers = duix_headers(settings)

    # -- configuration ------------------------------------------------
    def _float(self, name: str, default: float) -> float:
        raw = getattr(self.settings, name, default)
        try:
            return max(0.1, float(raw))
        except (TypeError, ValueError):
            return default

    @property
    def submit_timeout(self) -> float:
        return self._float("duix_submit_timeout_sec", 60.0)

    @property
    def task_timeout(self) -> float:
        return self._float("duix_task_timeout_sec", 1800.0)

    @property
    def poll_interval(self) -> float:
        return self._float("duix_poll_interval_sec", 2.0)

    @property
    def busy_retry(self) -> float:
        return self._float("duix_busy_retry_sec", 15.0)

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    # -- envelope helpers ---------------------------------------------
    @staticmethod
    def _unwrap(payload: dict[str, Any], *, action: str) -> dict[str, Any]:
        code = payload.get("code")
        if code == CODE_OK:
            data = payload.get("data")
            return data if isinstance(data, dict) else {}
        if code == CODE_BUSY:
            raise DuixBusyError("DUIX is busy (code 10001)")
        if code == CODE_NOT_FOUND:
            raise DuixError("DUIX task not found (code 10004)")
        raise DuixError(f"DUIX {action} failed: code={code} msg={payload.get('msg')!r}")

    # -- operations ---------------------------------------------------
    async def probe(self) -> bool:
        """True when the queue answers at all (unknown code -> 10004)."""
        if not self.configured:
            return False
        url = f"{self.base_url}/easy/query"
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(
                    url, params={"code": "__opentalking_probe__"}, headers=self.headers
                )
            if response.status_code != 200:
                return False
            payload = response.json()
        except Exception:
            return False
        return isinstance(payload, dict) and payload.get("code") in {
            CODE_OK,
            CODE_NOT_FOUND,
            CODE_BUSY,
        }

    async def submit(self, code: str, audio_url: str, video_url: str) -> None:
        if not self.configured:
            raise DuixError("DUIX base URL is not configured")
        url = f"{self.base_url}/easy/submit"
        body = {"code": code, "audio_url": audio_url, "video_url": video_url}
        async with httpx.AsyncClient(timeout=self.submit_timeout) as client:
            response = await client.post(url, json=body, headers=self.headers)
        if response.status_code >= 400:
            raise DuixError(f"DUIX submit HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise DuixError("DUIX submit returned non-JSON") from exc
        self._unwrap(payload, action="submit")
        logger.info("DUIX task submitted: %s", code)

    async def query(self, code: str) -> DuixTaskState:
        url = f"{self.base_url}/easy/query"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params={"code": code}, headers=self.headers)
        if response.status_code >= 400:
            raise DuixError(f"DUIX query HTTP {response.status_code}")
        data = self._unwrap(response.json(), action="query")
        return DuixTaskState(
            code=str(data.get("code") or code),
            status=int(data.get("status") or 0),
            progress=float(data.get("progress") or 0.0),
            message=str(data.get("msg") or ""),
            result=str(data.get("result") or ""),
        )

    async def wait(
        self,
        code: str,
        *,
        on_progress: ProgressCallback | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> DuixTaskState:
        """Poll until the task finishes; returns the terminal state."""
        deadline = asyncio.get_running_loop().time() + self.task_timeout
        last: DuixTaskState | None = None
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise DuixError("DUIX task cancelled")
            state = await self.query(code)
            last = state
            if on_progress is not None:
                result = on_progress(state.progress, state.__dict__)
                if asyncio.iscoroutine(result):
                    await result
            if state.status == STATUS_SUCCESS:
                return state
            if state.status == STATUS_FAILED:
                raise DuixError(f"DUIX task failed: {state.message or 'unknown error'}")
            if asyncio.get_running_loop().time() >= deadline:
                raise DuixError(
                    f"DUIX task timed out after {self.task_timeout:.0f}s "
                    f"(last progress {last.progress if last else 0:.0f}%)"
                )
            await asyncio.sleep(self.poll_interval)

    def result_url(self, code: str) -> str:
        """Absolute download URL for a finished task.

        Results live next to the API prefix (``/duix-files/<code>-r.mp4``), not
        under it, so the trailing ``/duix`` is stripped from the configured base.
        """
        parts = urlsplit(self.base_url)
        path = parts.path.rstrip("/")
        if path.endswith("/duix"):
            path = path[: -len("/duix")]
        return urlunsplit((parts.scheme, parts.netloc, f"{path}/duix-files/{code}-r.mp4", "", ""))

    async def download(self, code: str, destination: Path) -> Path:
        url = self.result_url(code)
        destination.parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=300.0) as client:
            async with client.stream("GET", url, headers=self.headers) as response:
                if response.status_code >= 400:
                    raise DuixError(f"DUIX download HTTP {response.status_code}")
                with destination.open("wb") as handle:
                    async for chunk in response.aiter_bytes(1 << 20):
                        handle.write(chunk)
        size = destination.stat().st_size
        if size == 0:
            raise DuixError("DUIX download produced an empty file")
        logger.info("DUIX result downloaded: %s (%d bytes)", destination.name, size)
        return destination
