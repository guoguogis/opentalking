# AI 配置导出

导出时间：2026-11-15T23:56:04+08:00

> API Key 已脱敏。完整密钥见本地 `.env`（不入库）。


## LLM

| 项 | 值 |
|---|---|
| provider | openai_compatible |
| base_url | https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1 |
| model | qwen3.8-flash |
| api_key_set | True |

## STT

| 项 | 值 |
|---|---|
| provider | openai_compatible |
| enabled_providers | ["dashscope", "sensevoice", "openai_compatible", "xiaomi_mimo"] |
| base_url | https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1 |
| model | qwen-audio-3.0-asr-flash |
| api_key_set | True |
| service_url_set | True |

## TTS

| 项 | 值 |
|---|---|
| provider | openai_compatible |
| enabled_providers | ["edge", "dashscope", "openai_compatible", "xiaomi_mimo", "local_cosyvoice", "indextts"] |
| base_url | https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1 |
| model | qwen-audio-3.0-tts-plus |
| voice | longanhuan_v3.6 |
| api_key_set | True |
| service_url_set | True |

## Mem0

| 项 | 值 |
|---|---|
| llm | {"provider": "tokenplan", "base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1", "model": "qwen4-max-preview", "api_key_set": true} |
| embedder | {"provider": "openai", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "text-embedding-v4", "api_key_set": true} |

## 运行状态

| 项 | 值 |
|---|---|
| llm_provider | openai_compatible |
| llm_model | qwen3.8-flash |
| llm_key_set | True |
| stt_provider | openai_compatible |
| stt_model | qwen-audio-3.0-asr-flash |
| stt_key_set | True |
| tts_provider | openai_compatible |
| tts_model | qwen-audio-3.0-tts-plus |
| tts_voice | longanhuan_v3.6 |
| tts_key_set | True |
| default_model | musetalk |
