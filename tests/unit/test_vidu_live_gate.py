"""Vidu live admission control: one live per session, no accidental doubles.

These tests never touch the network: ``_create_live`` is stubbed, so they run
offline and cannot bill the Vidu account.
"""
from __future__ import annotations

import asyncio

import pytest

from opentalking.providers.synthesis.vidu import live_gate
from opentalking.providers.synthesis.vidu.client import ViduAvatarClient


def _make_client(session_id: str, **overrides) -> ViduAvatarClient:
    kwargs = dict(
        api_key="test-key",
        base_url="http://127.0.0.1:1",
        image_uri="data:image/png;base64,AAAA",
        session_id=session_id,
        handshake_timeout=1.0,
        retry_delay=0.0,
    )
    kwargs.update(overrides)
    return ViduAvatarClient(**kwargs)


@pytest.fixture
def created(monkeypatch):
    """Record every live creation instead of calling Vidu."""
    calls: list[str] = []

    async def _stub_create_live(self, image_uri: str) -> dict:
        calls.append(self._session_id)
        return {
            "live": {"id": f"live-{len(calls)}", "status": "waiting"},
            "client_secret": "secret",
            "managed_rtc": {"channel_id": "chan", "token": "tok", "provider": "artc"},
        }

    async def _stub_open_stream(self) -> None:
        self._ws = object()

    async def _stub_publish(self) -> None:
        return None

    monkeypatch.setattr(ViduAvatarClient, "_create_live", _stub_create_live)
    monkeypatch.setattr(ViduAvatarClient, "_open_stream", _stub_open_stream)
    monkeypatch.setattr(ViduAvatarClient, "_publish_rtc_info", _stub_publish)
    calls.clear()
    return calls


async def test_gate_allows_only_one_live_at_a_time(monkeypatch):
    monkeypatch.setenv("OPENTALKING_VIDU_MAX_LIVES", "1")
    live_gate.reset_for_tests()

    await live_gate.acquire("sess_a")
    await live_gate.register_live("sess_a", "live-a")
    assert live_gate.active_count() == 1

    with pytest.raises(live_gate.ViduLiveBusy):
        await live_gate.acquire("sess_b", wait_sec=0.05)

    await live_gate.release("sess_a")
    await live_gate.acquire("sess_b", wait_sec=0.5)
    await live_gate.register_live("sess_b", "live-b")
    assert live_gate.active_lives() == {"sess_b": "live-b"}
    await live_gate.release("sess_b")


async def test_gate_is_reentrant_for_the_same_session(monkeypatch):
    monkeypatch.setenv("OPENTALKING_VIDU_MAX_LIVES", "1")
    live_gate.reset_for_tests()

    await live_gate.acquire("sess_a")
    await live_gate.acquire("sess_a")  # nested reservation, e.g. re-init
    await live_gate.release("sess_a")
    assert live_gate.active_count() == 1
    await live_gate.release("sess_a")
    assert live_gate.active_count() == 0


async def test_init_session_reuses_the_existing_live(created):
    """A second init for the same session must not create a second live."""
    client = _make_client("sess_same")
    await client.init_session()
    first_live = client.live_id
    await client.init_session()
    assert client.live_id == first_live
    assert created == ["sess_same"]


async def test_close_releases_the_slot(created):
    client = _make_client("sess_release")
    await client.init_session()
    assert "sess_release" in live_gate.active_lives()
    await client.close(send_close_msg=False)
    assert "sess_release" not in live_gate.active_lives()


async def test_second_session_waits_instead_of_double_creating(created, monkeypatch):
    """A busy account must not silently start a second live."""
    monkeypatch.setenv("OPENTALKING_VIDU_MAX_LIVES", "1")
    monkeypatch.setenv("OPENTALKING_VIDU_LIVE_WAIT_SEC", "0.05")
    live_gate.reset_for_tests()

    first = _make_client("sess_first")
    await first.init_session()

    second = _make_client("sess_second")
    with pytest.raises(live_gate.ViduLiveBusy):
        await second.init_session()
    assert created == ["sess_first"]
    assert live_gate.active_lives() == {"sess_first": "live-1"}


async def test_reattach_keeps_the_same_live(created):
    """reconnect_stream() must reuse the live instead of creating a new one."""
    client = _make_client("sess_reattach")
    await client.init_session()
    live_id = client.live_id
    client._ws = None  # control channel died
    await client.reconnect_stream()
    assert client.live_id == live_id
    assert created == ["sess_reattach"]


async def test_429_retries_within_budget_then_succeeds(created, monkeypatch):
    monkeypatch.setenv("OPENTALKING_VIDU_BUSY_RETRY_SEC", "5")
    live_gate.reset_for_tests()
    attempts = {"n": 0}

    async def _flaky_create(self, image_uri: str) -> dict:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError(
                'Vidu create session failed: HTTP 429 {"reason":"TooManyRequests",'
                ' "message":"live concurrency limit reached"}'
            )
        return {
            "live": {"id": "live-retry", "status": "waiting"},
            "client_secret": "secret",
            "managed_rtc": {"channel_id": "chan", "token": "tok"},
        }

    monkeypatch.setattr(ViduAvatarClient, "_create_live", _flaky_create)
    client = _make_client("sess_retry")
    await client.init_session()
    assert attempts["n"] == 2
    assert client.live_id == "live-retry"
    await client.close(send_close_msg=False)


async def test_429_gives_up_after_budget(created, monkeypatch):
    monkeypatch.setenv("OPENTALKING_VIDU_BUSY_RETRY_SEC", "0.2")
    live_gate.reset_for_tests()

    async def _always_busy(self, image_uri: str) -> dict:
        raise RuntimeError(
            'Vidu create session failed: HTTP 429 {"reason":"TooManyRequests",'
            ' "message":"live concurrency limit reached"}'
        )

    monkeypatch.setattr(ViduAvatarClient, "_create_live", _always_busy)
    client = _make_client("sess_busy")
    with pytest.raises(RuntimeError, match="429"):
        await client.init_session()
    # The slot must not leak when creation fails.
    assert "sess_busy" not in live_gate.active_lives()
