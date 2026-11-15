"""Vidu S2-Avatar backend wiring: model config, availability and RTC route."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from opentalking.core.config import Settings
from opentalking.core.model_config import clear_model_config_cache, get_model_backend
from opentalking.core.redis_keys import vidu_rtc_key
from opentalking.providers.synthesis.availability import resolve_model_statuses
from opentalking.providers.synthesis.backends import resolve_model_backend


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        omnirt_endpoint="",
        flashtalk_ws_url="",
        flashhead_ws_url="",
        vidu_api_key="",
        vidu_rtc_provider="artc",
        vidu_fps=25,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_vidu_backend_is_resolvable() -> None:
    clear_model_config_cache()
    assert get_model_backend("vidu") == "vidu"
    resolved = resolve_model_backend("vidu", _settings())
    assert resolved.backend == "vidu"
    assert resolved.uses_local_adapter is False


def test_vidu_backend_env_override(monkeypatch) -> None:
    clear_model_config_cache()
    monkeypatch.setenv("OPENTALKING_VIDU_BACKEND", "vidu")
    try:
        assert resolve_model_backend("vidu", _settings()).backend == "vidu"
    finally:
        monkeypatch.delenv("OPENTALKING_VIDU_BACKEND", raising=False)
        clear_model_config_cache()


async def test_vidu_availability_requires_api_key() -> None:
    statuses = {s.id: s for s in await resolve_model_statuses(_settings())}
    assert statuses["vidu"].backend == "vidu"
    assert statuses["vidu"].connected is False
    assert statuses["vidu"].reason == "not_configured"


async def test_vidu_availability_connected_with_api_key() -> None:
    statuses = {s.id: s for s in await resolve_model_statuses(_settings(vidu_api_key="k-123"))}
    assert statuses["vidu"].connected is True
    assert statuses["vidu"].reason == "vidu_configured"


def test_vidu_settings_defaults_and_env(monkeypatch) -> None:
    monkeypatch.setenv("VIDU_API_KEY", "legacy-key")
    monkeypatch.setenv("VIDU_RTC_PROVIDER", "trtc")
    settings = Settings()
    assert settings.vidu_api_key == "legacy-key"
    assert settings.vidu_rtc_provider == "trtc"
    assert settings.vidu_managed_rtc is True
    assert settings.vidu_fps == 25
    assert settings.vidu_sample_rate == 16000


def test_vidu_rtc_key_is_session_scoped() -> None:
    assert vidu_rtc_key("sess_1") == "opentalking:vidu:rtc:sess_1"
    assert vidu_rtc_key("sess_1") != vidu_rtc_key("sess_2")


class _FakeRedis:
    def __init__(self, value: str | None) -> None:
        self.value = value

    async def get(self, key: str) -> str | None:
        assert key == vidu_rtc_key("sess_1")
        return self.value


def _request(payload: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                redis=_FakeRedis(payload),
                settings=_settings(vidu_api_key="k-123"),
            )
        )
    )


async def _get_vidu(session_model: str, payload: str | None, monkeypatch):
    from apps.api.routes import sessions as sessions_routes

    async def _fake_get_session(_redis, session_id):
        assert session_id == "sess_1"
        return {"session_id": session_id, "model": session_model}

    monkeypatch.setattr(sessions_routes.session_service, "get_session", _fake_get_session)
    return await sessions_routes.get_vidu_rtc("sess_1", _request(payload))


async def test_vidu_route_returns_rtc_payload(monkeypatch) -> None:
    payload = (
        '{"live_id": "live-9", "fps": 25,'
        ' "rtc": {"app_id": "app", "channel_id": "chan", "user_id": "uid", "token": "tok"}}'
    )
    body = await _get_vidu("vidu", payload, monkeypatch)
    assert body["live_id"] == "live-9"
    assert body["rtc"]["channel_id"] == "chan"
    assert body["provider"] == "artc"


async def test_vidu_route_conflict_before_ready(monkeypatch) -> None:
    with pytest.raises(HTTPException) as exc:
        await _get_vidu("vidu", None, monkeypatch)
    assert exc.value.status_code == 409


async def test_vidu_route_rejects_other_models(monkeypatch) -> None:
    with pytest.raises(HTTPException) as exc:
        await _get_vidu("quicktalk", "{}", monkeypatch)
    assert exc.value.status_code == 400
