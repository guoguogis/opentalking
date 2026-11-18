from __future__ import annotations

CORE_TTS_PROVIDERS = frozenset({"auto", "edge", "elevenlabs", "mock"})
OPENAI_COMPATIBLE_TTS_PROVIDERS = frozenset({"openai_compatible"})
TOKENPLAN_REALTIME_TTS_PROVIDERS = frozenset({"tokenplan_realtime"})
XIAOMI_MIMO_TTS_PROVIDERS = frozenset({"xiaomi_mimo", "xiaomi", "mimo"})
QWEN_TTS_PROVIDERS = frozenset({"dashscope", "bailian", "qwen", "qwen_tts"})
COSYVOICE_TTS_PROVIDERS = frozenset({"cosyvoice", "cosyvoice_http"})
SAMBERT_TTS_PROVIDERS = frozenset({"sambert", "dashscope_sambert"})
LOCAL_TTS_PROVIDERS = frozenset({"local_cosyvoice", "local_qwen3_tts", "local_indextts", "local_f5_tts"})
OMNIRT_TTS_PROVIDERS = frozenset({"omnirt_indextts"})
INDEXTTS_TTS_PROVIDERS = frozenset({"indextts"})
BAILIAN_TTS_PROVIDERS = (
    QWEN_TTS_PROVIDERS
    | COSYVOICE_TTS_PROVIDERS
    | SAMBERT_TTS_PROVIDERS
)
# NOTE: previously written as `(*TOKENPLAN_REALTIME_TTS_PROVIDERS, <union>)`,
# which produced a 2-element tuple ("tokenplan_realtime", <frozenset>) and made
# every other provider (edge/dashscope/mock/...) fail the membership check.
SUPPORTED_TTS_PROVIDERS = (
    TOKENPLAN_REALTIME_TTS_PROVIDERS
    | CORE_TTS_PROVIDERS
    | OPENAI_COMPATIBLE_TTS_PROVIDERS
    | XIAOMI_MIMO_TTS_PROVIDERS
    | BAILIAN_TTS_PROVIDERS
    | LOCAL_TTS_PROVIDERS
    | OMNIRT_TTS_PROVIDERS
    | INDEXTTS_TTS_PROVIDERS
)


def normalize_tts_provider(value: str | None, *, default: str | None = None) -> str | None:
    provider = (value or "").strip().lower()
    if not provider:
        return default
    if provider in XIAOMI_MIMO_TTS_PROVIDERS:
        return "xiaomi_mimo"
    if provider in {"index_tts", "index-tts"}:
        return "indextts"
    if provider not in SUPPORTED_TTS_PROVIDERS:
        raise ValueError(f"unsupported tts provider: {value}")
    return provider
