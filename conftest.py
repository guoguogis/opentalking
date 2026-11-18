from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"

for path in (ROOT, SRC):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)


@pytest.fixture(autouse=True)
def _vidu_live_gate_is_instant(monkeypatch):
    """Keep the Vidu live gate out of the way of unrelated tests.

    Production releases the concurrency slot a couple of seconds after the
    hangup so Vidu can tear the live down; in tests that grace period would make
    the next test wait for it. ``close()`` also polls ``GET /live/v1/lives/{id}``
    until the live is terminal — pinned to zero here so unit tests stay offline
    and instant instead of hitting api.vidu.cn for a few seconds each.
    """
    monkeypatch.setenv("OPENTALKING_VIDU_LIVE_RELEASE_SEC", "0")
    monkeypatch.setenv("OPENTALKING_VIDU_END_WAIT_SEC", "0")
    from opentalking.providers.synthesis.vidu import live_gate

    live_gate.reset_for_tests()
    yield
    live_gate.reset_for_tests()
