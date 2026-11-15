# 3×A800 80G 实时对话数字人方案

!!! abstract "TL;DR"
    3 张 A800 能同时跑「开源全双工语音大脑 + 私有画面模型 + 备用/扩容」，这是目前开源能做到的最强组合。
    推荐：**#1 MiniCPM-o 4.5 duplex（语音）× #2 OmniRT QuickTalk+MuseTalk（画面）× #3 A/B 与扩容**，
    编排层**继续用 OpenTalking**（改造成本最低），把 duplex 作为新 provider 接入。

> 编写日期：2026-11-12
> 前置：[A800 全双工部署](duplex-voice-a800-deployment.md)、[QuickTalk 端到端手册](quicktalk-cloud-pipeline-runbook.md)、[实时数字人实施方案](realtime-digital-human-plan.md)

## 1. 编排层选型（"类似 OpenTalking"的到底有哪些）

| 框架 | 定位 | 支持的数字人模型 | 端到端 duplex | 传输 | 许可 | 对你改造成本 |
| --- | --- | --- | --- | --- | --- | --- |
| **OpenTalking**（你在用） | 实时数字人编排 + WebUI + 会话/事件 | QuickTalk / MuseTalk / FlashTalk / Wav2Lip / FlashHead / FasterLivePortrait，走 OmniRT | ❌ 需新增 provider | aiortc WHEP + RTMP/WHIP | Apache-2.0 | **最低**（资产与 OmniRT 已就绪） |
| **OpenAvatarChat**（HumanAIGC） | 模块化对话数字人基座，单 PC 可跑全功能 | LiteAvatar / LAM(3D) / **MuseTalk** | ✅ **原生有 MiniCPM-o S2S 模式** | gradio-webrtc（含 TURN 文档） | 见仓库 | 中（**QuickTalk 要自写 handler**） |
| **LiveTalking**（lipku） | 直播带货向实时引擎 | MuseTalk / Wav2Lip / Ultralight / ER-NeRF | ❌ | WebRTC / RTMP / 虚拟摄像头 | 见仓库 | 中高（直播语义，非客服语义） |
| **Pipecat** | 通用实时语音 Agent 编排 | 无内置口型模型，靠 avatar 服务插件（HeyGen/Simli/Tavus/LemonSlice） | ✅ OpenAI Realtime 原生 | Daily / LiveKit / SmallWebRTC / WS | BSD-2 | 高（会话/前端/事件全要重写） |
| **LiveKit Agents** | WebRTC 房间模型 + Agent 调度 | 同上，avatar 插件生态最大 | ✅ | LiveKit 原生 | Apache-2.0 | 高 |
| **TEN Framework**（Agora） | 多模态 Agent + RTC | 非数字人专用 | 部分 | Agora RTC | Apache-2.0 | 高 |

**结论**：
- 你要的是"数字人 + 客服对话"，不是纯语音 Agent → **OpenTalking 或 OpenAvatarChat** 才是同类。
- 想要最少工作量拿到 duplex：**OpenAvatarChat 原生支持 MiniCPM-o S2S**（`chat_with_minicpm.yaml`），但它的 avatar 不支持 QuickTalk，而 QuickTalk 正是你已经私有化的资产。
- **所以最优解是：OpenTalking 保留为主编排，把 duplex 写成 provider**；OpenAvatarChat 只作为架构参考（它的 handler 分层很干净）。

## 2. 三个可行拓扑

### 方案 A：稳态优先（推荐首发）

```text
A800 #1 ── vLLM-Omni · MiniCPM-o 4.5 duplex   WS /v1/realtime?duplex=1
A800 #2 ── OmniRT · QuickTalk + MuseTalk      WS /v1/audio2video/{model}
A800 #3 ── 预留：第二路 duplex / 三段式兜底 / RAG+ASR 本地化
编排    ── OpenTalking（新增 speech_duplex provider + 音频桥）
```

| 项 | 值 |
| --- | --- |
| 并发上限 | duplex 约 4 路/卡（vLLM-Omni 官方 `max_num_seqs: 4`），画面层 QuickTalk 实测可 2–4 路 |
| 首音频预估 | 1.0–2.2s（duplex 内建判停，比三段式省 0.4–0.8s） |
| 首帧预估 | 再加 QuickTalk 150–250ms |
| 风险 | 最低：每张卡职责单一，出问题可单独回滚 |
| 适合 | 先跑通、要给业务方演示 |

### 方案 B：质量上限（Qwen3-Omni 双卡）

```text
A800 #1+#2 ── vLLM-Omni · Qwen3-Omni-30B-A3B duplex（TP=2）
A800 #3    ── OmniRT · QuickTalk（+MuseTalk）
```

- 能力最强（30B MoE，中文与推理上限高），但有两个代价：
  1. **A800 的 NVLink 带宽被限制**，TP=2 的跨卡效率低于 A100，首包延迟会变差；
  2. 社区 issue 显示默认配置下 2×A100 80GB 都报显存不足，你需要调 `max_model_len` / KV 比例，不属于"开箱即用"。
- 适合：把它当"质量天花板"做对照实验，不建议首发走生产。

### 方案 C：双引擎 A/B（选型期最有用）

```text
A800 #1 ── MiniCPM-o 4.5 duplex（引擎①）
A800 #2 ── OmniRT QuickTalk（画面，两引擎共用）
A800 #3 ── 私有三段式：FunASR streaming + qwen + CosyVoice（引擎②）
```

- 用 OpenTalking 现有的 session/事件模型，同一套前端切两个引擎，跑同一批客服脚本做 A/B。
- **这是我最推荐的"前两周"形态**：先用数据回答"duplex 到底比三段式好多少"，再决定生产形态。
- 之后 A800#3 可以转成方案 A 的扩容位。

## 3. 推荐架构图（方案 A + C 合并后的生产形态）

```text
                 ┌────────── 浏览器/坐席（WebRTC WHEP）──────────┐
                 │  麦克风 PCM 16k ↑ ｜ ↓ 音视频轨道              │
                 └───────────────┬───────────────────────────────┘
                                 │
                  ┌──────────────▼──────────────┐
                  │ OpenTalking 编排层（CPU 机） │
                  │  speech_duplex provider      │
                  │  audio_bridge（24k→16k/切块）│
                  │  session / 事件 / 知识 / 记忆 │
                  └───┬───────────────────┬──────┘
                      │duplex WS           │audio2video WS
          ┌───────────▼─────────┐   ┌─────▼──────────────────┐
          │ A800#1 MiniCPM-o4.5 │   │ A800#2 OmniRT          │
          │ （听+想+说）         │   │  QuickTalk / MuseTalk  │
          └─────────────────────┘   └────────────────────────┘
          ┌─────────────────────┐
          │ A800#3 扩容/兜底     │
          └─────────────────────┘
```

**关键组件：`audio_bridge`**（必须自己写，约 200 行）
1. 收 `response.audio.delta`（base64，24kHz）→ 解码 PCM
2. 重采样到 16kHz
3. 按 QuickTalk `slice_len`（10 帧 @25fps = 400ms）切块
4. 逐块调 `OmniRTAudio2VideoClient.generate()`
5. 打断时双向清空：duplex 侧 `output_audio_buffer.clear` + 本地 `runner.interrupt()`

## 4. 改造工作量（用 OpenTalking 做基座）

| 任务 | 估时 | 说明 |
| --- | --- | --- |
| `speech_duplex` provider（OpenAI Realtime 协议客户端） | 3–5 天 | 复用现有 provider 注册机制，`OPENTALKING_SPEECH_PROVIDER=duplex` |
| `audio_bridge`（重采样 + 切块 + 打断联动） | 3 天 | 方案核心，也是唯一硬验证点 |
| 字幕/事件对接（transcript delta → subtitle 事件） | 1–2 天 | 现有 events schema 扩展 |
| 前端：双引擎切换 + 打断 UI 状态 | 2 天 | 已有 WebUI，加开关 |
| 部署与压测 | 3 天 | 3 卡拓扑、并发、长稳 |

合计 **2 周左右**能出可用版本（不含模型调优）。

## 5. 与 OpenAvatarChat 的取舍（一句话版）

- 选 OpenTalking 的理由：**QuickTalk 资产与 OmniRT 抽象已就绪**，且你能保留 WebUI/事件/会话/知识库这条完整产品线。
- 选 OpenAvatarChat 的理由：模块化分层更干净、原生 MiniCPM-o S2S、开箱即用的 LiteAvatar/LAM/MuseTalk；但**要自己写 QuickTalk handler**，且 RTC 是 gradio-webrtc（生产 WebRTC 需要额外加 TURN）。
- 折中建议：**架构上照抄 OpenAvatarChat 的 handler 分层**（VAD / ASR / LLM / TTS / Avatar / RTC 六层），落地仍用 OpenTalking。

## 6. 许可与合规（商用必查）

| 项 | 状态 |
| --- | --- |
| MiniCPM-o 4.5 | **Apache-2.0** ✅ |
| vLLM-Omni | Apache-2.0 ✅ |
| QuickTalk / MuseTalk 权重 | 以各自仓库许可为准，商用前单独确认 |
| 必须排除 | Nemotron VoiceChat 11B（OpenMDW 仅研究用）、Moshi/Hertz-dev（仅英/法，且非中文场景） |
| 合规 | 肖像授权书、合成内容标识（《人工智能生成合成内容标识办法》2025-09-01 起施行） |

## 7. 实施路线（4 周）

| 周 | 目标 | 验收 |
| --- | --- | --- |
| W1 | A800#1 起 MiniCPM-o duplex；跑通官方 `barge_in_client.py` | 中文音色可接受、打断可复现 |
| W2 | 写 `audio_bridge`，duplex 音频驱动 QuickTalk | 数字人跟着开源模型说话，口型同步 |
| W3 | 接入 OpenTalking（provider + 事件 + 前端），A800#3 起三段式做 A/B | 同一批脚本两引擎对比数据 |
| W4 | 并发（4/8 路）与 30 分钟长稳；出选型结论 | 上线配置 + 回滚预案 |

## 8. 主要风险

| 风险 | 应对 |
| --- | --- |
| MiniCPM-o 中文全双工效果未经验证 | W1 就用真实客服话术测，不要用英文 demo 外推 |
| 首音频延迟不达预期 | duplex 的官方基准是离线吞吐口径，必须自测实时会话；不达标就回退三段式（协议一致，改 endpoint 即可） |
| A800 NVLink 限制 TP 效率 | 方案 B 谨慎；优先单卡模型 + 多实例 |
| 两套打断不同步 | 打断必须做成"一个事件、两处执行"，写进代码而不是文档 |
| 显存竞争 | 语音层与画面层分卡；不要在同卡上 co-location 生产 |
