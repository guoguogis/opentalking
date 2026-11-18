"""Admission control for Vidu component lives.

Vidu bills and throttles per *concurrent live*: starting a second live while the
first is still winding down is rejected with ``429 live concurrency limit
reached``, and an orphaned live keeps the slot busy until its
``idle_timeout_seconds`` runs out.

The gate makes the two invariants explicit:

* **one live per OpenTalking session** — a re-init/retry for the same session
  must reattach to the existing live instead of creating a second one. Slots are
  reference counted per session, so nested acquire/release pairs are safe.
* **at most ``OPENTALKING_VIDU_MAX_LIVES`` lives at once** (default ``1``) — a
  second session waits for a slot instead of racing Vidu into a 429.

Releasing is intentionally delayed by ``OPENTALKING_VIDU_LIVE_RELEASE_SEC``
(default 2s): Vidu needs a moment after ``call_hangup`` before the concurrency
slot is free again.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

DEFAULT_MAX_LIVES = 1
DEFAULT_WAIT_SEC = 45.0
DEFAULT_RELEASE_SEC = 2.0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


class ViduLiveBusy(RuntimeError):
    """No Vidu live slot became free in time.

    Raised before any HTTP call, so a busy account never creates (and bills) a
    live that Vidu would reject anyway.
    """


def max_lives() -> int:
    return max(1, _env_int("OPENTALKING_VIDU_MAX_LIVES", DEFAULT_MAX_LIVES))


def wait_seconds() -> float:
    return _env_float("OPENTALKING_VIDU_LIVE_WAIT_SEC", DEFAULT_WAIT_SEC)


def release_seconds() -> float:
    return _env_float("OPENTALKING_VIDU_LIVE_RELEASE_SEC", DEFAULT_RELEASE_SEC)


_lock: asyncio.Lock | None = None
_slots: asyncio.Semaphore | None = None
_slot_limit = 0
#: session_id -> (ref count, live_id or "")
_holders: dict[str, list[Any]] = {}


def _state() -> tuple[asyncio.Lock, asyncio.Semaphore]:
    """Lazily build the lock/semaphore so they bind to the running loop."""
    global _lock, _slots, _slot_limit
    limit = max_lives()
    if _lock is None or _slots is None or _slot_limit != limit:
        _lock = asyncio.Lock()
        _slots = asyncio.Semaphore(limit)
        _slot_limit = limit
    return _lock, _slots


def active_lives() -> dict[str, str]:
    """Snapshot of ``session_id -> live_id`` for diagnostics."""
    return {session: str(info[1] or "") for session, info in _holders.items()}


def active_count() -> int:
    return sum(int(info[0]) for info in _holders.values())


async def acquire(session_id: str, *, wait_sec: float | None = None) -> None:
    """Reserve the live slot for ``session_id``, waiting up to ``wait_sec``."""
    lock, slots = _state()
    async with lock:
        held = _holders.get(session_id)
        if held is not None:
            held[0] = int(held[0]) + 1
            return
        budget = wait_seconds() if wait_sec is None else max(0.0, wait_sec)
        try:
            await asyncio.wait_for(slots.acquire(), timeout=budget if budget > 0 else None)
        except asyncio.TimeoutError as exc:
            raise ViduLiveBusy(
                "Vidu 云端数字人并发已满（同一时刻最多 %d 路），请稍后重试 (ViduLiveBusy)"
                % max_lives()
            ) from exc
        _holders[session_id] = [1, ""]


async def register_live(session_id: str, live_id: str) -> None:
    """Record the live id owned by ``session_id``."""
    lock, _ = _state()
    async with lock:
        held = _holders.get(session_id)
        if held is None:
            # Registration without a reservation: adopt it so release() works.
            _holders[session_id] = [1, live_id]
            return
        held[1] = live_id


async def release(session_id: str) -> None:
    """Drop one reference; free the slot once the session holds none."""
    lock, slots = _state()
    release_after = release_seconds()
    pending: list[str] = []
    async with lock:
        held = _holders.get(session_id)
        if held is None:
            return
        held[0] = int(held[0]) - 1
        if int(held[0]) > 0:
            return
        _holders.pop(session_id, None)
        pending.append(session_id)
    if not pending:
        return
    if release_after <= 0:
        slots.release()
        return

    async def _release_later() -> None:
        try:
            await asyncio.sleep(release_after)
        finally:
            slots.release()

    task = asyncio.get_running_loop().create_task(_release_later())
    # Keep a reference so the task is not garbage collected mid-sleep.
    _pending_releases.add(task)
    task.add_done_callback(_pending_releases.discard)


_pending_releases: set[asyncio.Task[None]] = set()


def reset_for_tests() -> None:
    """Clear all bookkeeping and hand every permit back (tests only)."""
    global _lock, _slots, _slot_limit
    for task in list(_pending_releases):
        task.cancel()
    _pending_releases.clear()
    _holders.clear()
    _lock = None
    _slots = None
    _slot_limit = 0
