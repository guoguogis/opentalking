# Vidu S2-Avatar 数字人来源（云端渲染）

本文给要在 OpenTalking 里使用 **Vidu S2-Avatar 组件版** 的开发者看。读完你应该能：配好凭证、创建 `model: vidu` 的会话、让浏览器看到实时数字人画面，并知道哪些环节还没接线。

> Vidu 有「实时版」和「组件版」两种产品。OpenTalking 只接 **组件版**：Vidu 只负责渲染和推流，ASR / LLM / TTS 仍由 OpenTalking 负责。实时版会把对话与音色一并接管，评分、知识库、人设记忆落不到本系统，因此不接入。

## 1. 它是怎么工作的

```text
浏览器麦克风 ──► OpenTalking ASR ──► LLM ──► TTS（16 kHz PCM）
                                             │
                                             ▼
                          Vidu 控制 WebSocket（裸 PCM，24 kHz）
                                             │
                                             ▼
                         Vidu 云端渲染 ──► RTC 频道 ──► 浏览器 <video>
```

关键点：

- OpenTalking 的本地流水线**不产生视频帧**，`generate()` 返回空列表，只把音频送给 Vidu。
- 本地 WebRTC（aiortc）仍保留，用来把 **TTS 音频**送到浏览器；画面由厂商 RTC 频道提供。
- 因此 `vidu` 不需要 OmniRT，也不需要任何本地 GPU 模型。

## 2. 配置

`.env` 或环境变量：

```bash
# 必填：Vidu 控制台创建的 API Key（国内站）
VIDU_API_KEY=your_vidu_api_key

# 可选：国内站默认 https://api.vidu.cn；国际站改成 https://api.vidu.com
VIDU_BASE_URL=https://api.vidu.cn

# 可选：默认形象图（URL / data URI）。留空则使用 avatar bundle 的 reference.png|.jpg
VIDU_AVATAR_IMAGE_URI=

# 可选：RTC 供应商 artc（默认）/ trtc / agora / volcengine
VIDU_RTC_PROVIDER=artc

# 可选：平台托管 RTC 频道（默认 true）；false 时需要自备频道信息
OPENTALKING_VIDU_MANAGED_RTC=true

# 可选：本地切片长度（16 kHz 采样点），默认 4800 = 300 ms
OPENTALKING_VIDU_CHUNK_SAMPLES=4800

# 可选：会话空闲超时，Vidu 上限 7200 秒
OPENTALKING_VIDU_IDLE_TIMEOUT_SECONDS=7200
```

`configs/default.yaml` 里已经登记：

```yaml
models:
  vidu:
    backend: vidu
```

也可以用 `OPENTALKING_VIDU_BACKEND=vidu` 覆盖。

## 3. 启动与验证

```bash
# 后端
.venv/bin/python -m apps.unified.main --host 127.0.0.1 --port 8000
# 前端
cd apps/web && npm run dev
```

验证顺序：

1. `curl http://127.0.0.1:8000/models` —— `vidu` 应该出现且 `connected: true`，`reason: vidu_configured`。没有 API Key 时是 `not_configured`。
2. WebUI 选择 `vidu` 模型与任意 avatar bundle 创建会话。
3. 会话就绪后 `curl http://127.0.0.1:8000/sessions/<sid>/vidu` —— 返回 `live_id` 与 `rtc`（`app_id` / `channel_id` / `user_id` / `token`）。**在 worker 完成握手前这里是 409**。
4. 浏览器端通过 npm 打包的 `aliyun-rtc-sdk` 加入 RTC 频道（无需配置 CDN，内网可用），仅当模型为 `vidu` 时按需动态加载。

## 4. 前端 RTC SDK（已随 npm 打包）

阿里云 RTC Web SDK 通过 npm 依赖 `aliyun-rtc-sdk`（7.3.x）打包进 WebUI，仅在模型为 `vidu` 时动态加载（约 1.2 MB 独立 chunk）。其它厂商（腾讯 / 声网 / 火山）通过 `startViduPlayback({ adapter })` 接入：

```ts
import { startViduPlayback } from "./lib/viduRtc";

const handle = await startViduPlayback(sessionId, videoEl, {
  adapter: async ({ info, videoEl }) => {
    // 用你的 SDK 加入 info.rtc，并把远端视频挂到 videoEl
    return { leave: () => { /* 离开频道 */ } };
  },
});
```

**这一环尚未用真实凭证验证过**（本仓库没有 Vidu 账号与 RTC SDK 授权），首次联调请优先确认：入会成功、订阅到远端视频、`<video>` 有画面。

### 音频来源（口型同步关键）

Vidu 的 RTC 流只有画面，远端音频由阿里云 SDK 的**内部播放器**直出（不在 `<video>` 的 `srcObject` 里），而这一份音频正是它渲染口型时用的音轨，天生同步。因此 vidu 会话会让厂商音频做唯一音源，把本地 TTS 元素静音（`<audio ref={audioRef}>` → `muted`）。

如果现场环境里厂商音频放不出来（听到静音），用本地音轨兜底：

```bash
# apps/web/.env.local
VITE_VIDU_AUDIO_SOURCE=local
```

代价是嘴型会比声音慢一点（云端渲染延迟），只作为 fallback。

## 5. 行为细节

| 场景 | 行为 |
| --- | --- |
| 建会话 | `POST /live/s_avatar/component`，`managed_rtc` 由平台建房 |
| 握手 | 首帧 `conn_init`(type=1)；`NOT_READY` 自动退避重发，不重连 |
| 发送音频 | 16 kHz 重采样到 24 kHz，按 20 ms（960 字节）二进制帧发送 |
| 打断 | `interrupt()` 只发 `audio_interrupted`(type=7)，**不重建会话**（重建会结束计费并重新握手） |
| 挂断 | `close()` 发 `call_hangup`(type=5) |
| 空闲动作 | Vidu 自己渲染，本地跳过 idle 帧生成 |
| 强制挂断 | 服务端 `force_hangup`(type=6) 会被记录并标记会话结束 |
| 心跳 | 服务端约 5 s ping、15 s 无 pong 断线；库层自动回 pong |

## 6. 真实 API 已验证（2026-11-15）

生产端 `https://api.vidu.cn` 实测通过，两个字段事实值得记住：

1. **托管 RTC 的凭证在响应的 `managed_rtc` 字段里，不在 `rtc`**：

```json
{
  "live": {"id": "1019484231039983616", "status": "waiting"},
  "client_secret": "v1.eyJ…",
  "managed_rtc": {
    "app_id": "4f495dcd-…",
    "channel_id": "live-user-1019484231039983616",
    "user_id": "live-user-1003595675528605696-1019484231039983616",
    "token": "eyJhcHBpZCI6…",
    "token_expire_at": "1794822496",
    "provider": "artc"
  }
}
```

客户端已同时接受 `rtc` / `managed_rtc` / `customer_rtc` 三种形状。

2. **计费按 `billed_seconds` 结算**，可用 `GET /live/v1/lives/{live_id}` 查询；实测空转会话为 `0`，正常一轮对话 42 秒。

实测覆盖：create（3.7s）→ `conn_init_ack.success=true` → 推送 PCM → `audio_interrupted` → `call_hangup` → 会话 `status=ended`、`close_reason=user_end`。可用 `python scripts/probe_vidu_api.py` 复现。

## 7. 已知未完成

- **字幕与动作文本**：组件版支持 `type=9/10` 传转写文本驱动动作。客户端已提供 `send_transcription()`，但尚未接入 speak 流水线，所以当前只有口型跟随音频，手势来自 Vidu 的 idle/动作库。
- **录制与复盘**：本地不再有帧，`recording` / `offline_export` 对 `vidu` 会话不适用；如需录制请开启 `OPENTALKING_VIDU_ENABLE_RECORDING=true` 并用 Vidu 的录制查询接口。
- **真实链路验证**：create/握手/推流/打断/挂断已用生产 Key 实测通过；浏览器画面已在 Chromium/Edge 实测出帧（`videoWidth=1024`、`readyState=4`、连续两个会话均正常）。
- **英文文档**：`docs/en/model-deployment/vidu-avatar.md` 尚未同步。

## 8. 排错

| 现象 | 检查 |
| --- | --- |
| `/models` 里 vidu 为 `not_configured` | 没配 `VIDU_API_KEY` |
| 建会话成功但 `/sessions/{id}/vidu` 一直 409 | worker 握手失败，查后端日志 `Vidu conn_init`、`Vidu create session failed` |
| 浏览器有声音没画面 | 看控制台是否有 `Vidu RTC bye`、`setRemoteViewConfig failed`；确认 `aliyun-rtc-sdk` 版本与 `viduRtc.ts` 的 API 一致（7.3.x） |
| 控制台报 `token may be invalid` | `joinChannel` 必须直接传 Vidu 下发的 `token` 字符串（它本身就是 base64 的 AliRtcAuthInfo，内含厂商签名的 timestamp）；自己用当前时间拼 `AliRtcAuthInfo` 会被 roomserver 拒绝 |
| 页面有声音但舞台一直是黑的 | 画面 `<video>` 必须常驻挂载（不能只在 `!showStart` 时渲染），否则启动阶段 `ref` 为空会静默跳过整个 RTC 入会 |
| 回答时听到两个声音 | 一条音轨只能有一个出声的播放器：`<audio ref={audioRef}>` 是唯一的音频出口，舞台 `<video>` 必须 `muted`（同一 `MediaStream` 挂到两个未静音元素会出现错位双声）；同时 Vidu 会把我们送去的音频回灌进它的 RTC 流，客户端已 `setDefaultSubscribeAllRemoteAudioStreams(false)` 且画面元素 `muted` |
| 提示 `already joined` | Aliyun RTC 引擎是页面级单例：开第二个会话前必须 `await leaveChannel()`。`viduRtc.ts` 已跟踪 `joinedChannelId`、等待上一次 leave 完成，并在报 already joined 时自动 leave 后重试一次 |
| 画面卡顿/断流 | 检查 RTC token 是否过期（`token_expire_at`），以及网络到厂商 RTC 的连通性 |
| 每轮说话都重建会话 | 说明打断走了 FlashTalk 的 reset 分支，确认 `model_type == "vidu"` |

