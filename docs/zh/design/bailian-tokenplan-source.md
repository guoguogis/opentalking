# 百炼 TokenPlan 来源接入（2026-11-15 实测，端点已打通）

Base host：`https://token-plan.cn-beijing.maas.aliyuncs.com`
凭据：`sk-sp-…`（与 `.env` 里 `OPENTALKING_STT_OPENAI_API_KEY` 同一把）

## 1. 五种能力的真实接口（全部实测）

| 能力 | 接口 | 协议 | 实测 |
| --- | --- | --- | --- |
| LLM | `POST /compatible-mode/v1/chat/completions`，`qwen4-max-preview` | OpenAI 兼容 | ✅ 200 |
| TTS | `POST /api/v1/services/audio/tts/SpeechSynthesizer`，`qwen-audio-3.0-tts-plus` | **DashScope** | ✅ 200，返回 `output.audio.url` |
| ASR | `POST /api/v1/services/aigc/multimodal-generation/generation`，`qwen-audio-3.0-asr-flash` | **DashScope** | ✅ 200，返回 `output.text` |
| 实时语音对话 | `WS wss://…/api-ws/v1/realtime?model=qwen-audio-3.0-realtime-plus` | OpenAI Realtime 事件 | ✅ 握手成功（`session.created`） |
| Embedding | —— | —— | ❌ 端点内无任何 embedding 模型 |

### TTS 请求/响应

```json
POST /api/v1/services/audio/tts/SpeechSynthesizer
{"model":"qwen-audio-3.0-tts-plus",
 "input":{"text":"你好","voice":"longanhuan_v3.6","format":"wav","sample_rate":16000}}
→ 200 {"output":{"audio":{"url":"http://dashscope-result-bj.oss…wav","expires_at":…}},
        "usage":{"characters":33,"input_tokens":18,"output_tokens":43,"total_tokens":61}}
```

要点：`voice` 必须与模型版本匹配（如 `longanhuan_v3.6`、`longanqian`）；返回的是**有时效的 URL**，需要立刻下载；`format` 支持 `mp3/pcm/wav/opus`。

### ASR 请求/响应

```json
POST /api/v1/services/aigc/multimodal-generation/generation
{"model":"qwen-audio-3.0-asr-flash",
 "input":{"messages":[{"role":"user","content":[
   {"type":"input_audio","input_audio":{"data":"data:audio/wav;base64,… 或 https URL"}}]}]},
 "parameters":{"format":"wav","sample_rate":16000,"language_hints":["zh"]}}
→ 200 {"output":{"text":"你好，我是数字人主播，很高兴认识你。"},"usage":{"duration":3}}
```

要点：`parameters.format` **必填**；音频支持公网 URL 或 `data:audio/{format};base64,`；单次 ≤ 5 分钟；**非流式**（一次请求返回整段文本）。

### 实时语音对话

```python
wss://token-plan.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime?model=qwen-audio-3.0-realtime-plus
Authorization: Bearer sk-sp-…
session.update: {modalities:["audio","text"], voice:"longanqian",
                 input_audio_format:"pcm", output_audio_format:"pcm",
                 instructions:"…", turn_detection:{type:"server_vad", threshold:0.5, silence_duration_ms:600}}
→ input_audio_buffer.append {audio: base64(pcm16)}
← conversation.item.input_audio_transcription.completed {transcript}
← response.audio.delta {delta: base64(pcm)} / response.audio_transcript.delta
```

注意：走的是 `/api-ws`，事件名与 OpenAI Realtime 一致；`/realtime`（compatible-mode 下）虽然也能握手，但只有 `qwen-audio-3.0-realtime-plus` 可用，`asr-flash`/`tts-plus` 不行。

### 往返验证

TTS 生成 wav（110 KB）→ 直接喂 ASR → 文本与原文一致（`ROUNDTRIP_OK: True`）。说明 TTS/ASR 可独立用于「非实时」链路。

## 2. 与现有代码的对接方式

| 目标 | 复用 | 改动量 |
| --- | --- | --- |
| LLM | `providers/llm/openai_compatible` | 纯配置（base_url + key + model） |
| TTS | `providers/tts/dashscope_qwen` 的 **HTTP** 路径 | 中：现有实现走 `api-ws/v1/realtime`，需加 `SpeechSynthesizer` 分支（返回 URL → 下载 → 解码 PCM） |
| ASR | `providers/stt/dashscope` | 中：现有是 paraformer **流式 WS**；TokenPlan 是**非流式 HTTP**，需加 `multimodal-generation` 分支（前端需按「整段上传」而不是「流式追加」） |
| 实时对话 | 新增 `providers/duplex/openai_realtime` | 大：新协议族（session.update / append / server_vad / response.audio.delta） |
| Mem0 LLM | 同 LLM | 纯配置 |
| Mem0 Embedding | 仍用现有来源（如百炼主站 `text-embedding-v*`） | 无（TokenPlan 无 embedding 模型） |

## 3. 建议落地顺序

1. **LLM + Mem0 LLM**：配置层直接切，10 分钟可验证（`qwen4-max-preview` 是推理模型，需压 `max_tokens`/关闭 thinking 以免拖慢开口）。
2. **TTS**：加 `dashscope_http_tts` 适配器（URL 下载 + ffmpeg 解码），保留现有流式 TTS 作为低延迟首选。
3. **ASR**：加 `dashscope_http_asr`（非流式），适合「录音后整段识别」；实时对话仍建议走第 4 步。
4. **实时对话**：`openai_realtime` 适配器，一次拿到 ASR+LLM+TTS+打断，是数字人实时对话的终态形态。

## 4. 风险

- ASR 非流式：端到端延迟 = 说完 + 上传 + 识别；实时对话应走 realtime 通道。
- TTS 返回 URL：需要下载再解码，比流式 TTS 多一跳；`pcm` 格式可省解码。
- `qwen4-max-preview` 的 thinking 会让首 token 变慢，数字人场景需显式关闭或限制。
- TokenPlan 额度独立于百炼主站（主站 `paraformer-realtime-v2` 免费额度已耗尽，TokenPlan 不受影响）。
