# 实时数字人实施方案（私有 QuickTalk / MuseTalk + 国内云 API）

!!! abstract "TL;DR"
    在**已有私有 OmniRT + QuickTalk / MuseTalk** 的前提下，把"最先进"用在正确的地方：
    **画面不换（私有的脸继续用），把"耳朵、嘴巴、判停"换成 2026 年的云侧先进能力，把编排层的分片粒度、打断逻辑和传输补齐。**
    目标：客服问答场景，首帧响应 P50 ≤ 1.6s、抢话停止 ≤ 250ms、单卡 4090 稳定 1–2 路并发。

> 编写日期：2026-11-12
> 适用前提（已确认）：场景＝实时对话/客服问答；部署＝OmniRT 统一服务；算力＝局域网 Linux GPU 单卡（4090/3090 级）；云 API 边界＝国内云（火山/阿里/腾讯）可用。
> 行业竞品与技术全景不重复展开，见 [实时数字人研究与路线](realtime-digital-human-research.md)（2026-04-27 快照）。

## 1. 结论先行

### 1.1 三条判断

1. **不要换画面模型。** 单卡 4090（24G）跑 FlashTalk-14B 级模型不现实，QuickTalk/MuseTalk 才是单卡实时正解。你的私有化投入是资产，不是包袱。
2. **"最先进"的落点在链路两端：耳朵（STT/判停）和嘴巴（流式 TTS）。** 2026 年真正换代的是"端到端全双工语音模型"，但它的音频输出必须能喂给自有 talking-head，且音色要可控 —— 所以在客服场景里它首选做 **A/B 模式**，而不是无条件替换主链路。
3. **当前最大的体验短板不是模型，是"判停/打断"和"分片粒度"。** 仓库里 `interrupt` 是命令式的（`runtime/task_consumer.py:1033` 收到 `interrupt` 命令才停），没有"用户抢话自动停"；MuseTalk 默认 `context_ms=320`、QuickTalk 默认 `slice_len=28` 帧 @25fps ≈ **1.12s 一块**，这两个数字直接决定首帧延迟。

### 1.2 与本仓现状的差距（读代码得出，非猜测）

| 能力 | 现状 | 位置 | 差距 |
| --- | --- | --- | --- |
| 音频上行 | ✅ 已有 `WS /sessions/{id}/speak_audio_stream`（PCM s16le 16k → 流式 STT → speak） | `apps/api/routes/sessions.py:1191` | 无端侧 VAD，靠"说完再上送" |
| 判停 / 打断 | ⚠️ 仅命令式打断 | `runtime/task_consumer.py:1033` | 无抢话自动检测，无 hangover 策略 |
| 合成分片 | ⚠️ MuseTalk 320ms/块；QuickTalk 28 帧≈1.12s/块 | `configs/default.yaml:111`、`providers/synthesis/audio2video_client.py:49-62` | 粒度过粗，首帧被拖慢 |
| TTS | ✅ 多 provider 可插拔（edge / dashscope / cosyvoice / indextts / f5） | `providers/tts/` | 首包延迟与音色质量未按客服场景标定 |
| 传输 | ✅ aiortc WHEP + streaming（RTMP/WHIP）双路径 | `providers/rtc/aiortc`、`configs/streaming/mediamtx.yml` | 单机客服只需 WHEP，但缺 TURN（外网坐席） |
| 字幕事件 | ✅ events + SSE | `opentalking/events/` | 无"音频播放位置对齐"的字幕时间轴 |
| 知识 / 记忆 | ✅ LightRAG + mem0 + BM25 | `providers/memory/`、`OPENTALKING_AGENT_KNOWLEDGE_ROOT` | 检索超时默认 2000ms，会吃掉首 token 预算 |
| 语音新一代 | ❌ 未接 | — | 无端到端语音 A/B 能力 |

## 2. 目标架构

```text
┌──────────────────────── 坐席浏览器 ────────────────────────┐
│ 麦克风 16k PCM ──WS──► 端侧 VAD/RMS + 200ms 预卷缓冲        │
│                        └─ 抢话判定 → 立即发 interrupt        │
│ WebRTC(WHEP) ◄──── 音视频轨道 ──────────────────────────────┤
└──────────────────────────────┬──────────────────────────────┘
                               │ 局域网 / HTTPS + TURN（外网时）
┌──────────────────────────────┴──────────────────────────────┐
│ OpenTalking 编排层（apps.unified，单进程，建议与 GPU 同机）    │
│                                                             │
│  STT: dashscope paraformer-realtime（流式，增量）            │
│        ↓                                                    │
│  LLM: qwen 系 + LightRAG 知识 + mem0 记忆（检索超时 300ms）   │
│        ↓ 句子级流式                                          │
│  TTS: 流式 provider（首句优先，见 P0-2）                      │
│        ↓ 按模型粒度切音频                                     │
│  SessionRunner → audio2video_client.generate(chunk)          │
└──────────────────────────────┬──────────────────────────────┘
                               │ ws://gpu:9000/v1/audio2video/{model}
┌──────────────────────────────┴──────────────────────────────┐
│ OmniRT（单卡 4090/3090）                                     │
│  musetalk-1.5（自由形象、512²、≥25fps）                       │
│  quicktalk（模板视频、720×900、≥25fps）                       │
└─────────────────────────────────────────────────────────────┘
```

**关键取舍**：编排层与 OmniRT **同机部署**。少一次跨机 RTT（20–40ms），且 4090 显存里同时装不下两套编排 + 两套模型时，同机更容易看清资源账。

## 3. 延迟预算（客服目标：首帧 P50 ≤ 1.6s）

| 环节 | 目标值 | 依据 / 调法 |
| --- | --- | --- |
| 端侧判停（说完→触发） | 300–500ms | 静音阈值 400–600ms + 语义兜底（字数≥4） |
| 流式 STT 终稿 | 200–400ms | dashscope 实时流式，边说边出中间结果 |
| LLM 首 token | 250–500ms | qwen-flash 起步；检索超时压到 300ms，并行不阻塞 |
| 首句 TTS 首包 | 150–300ms | 流式解码 + 首句短切分（12–18 字） |
| OmniRT 首帧 | 80–200ms | 取决于分片粒度（P0-3） |
| WebRTC 传输 | 30–80ms | 局域网 WHEP |
| **合计** | **≈1.0–1.9s** | 现状大概率落在 2.5–3.5s，差距主要在首帧与判停 |

对照参考（厂商/第三方实测口径，**非本方案承诺**，来源见 §9）：
- 阿里 `qwen3.8-omni-flash-realtime`：语音进→首音频 **1.37s**、打断停止 **71ms**、约 0.0025 元/轮（社区实测）。
- 豆包实时语音模型 3.0（Seeduplex）：官方口径判停延迟较半双工缩短约 **250ms**、打断延迟缩短约 **300ms**（厂商口径）。
- 传输层协议级区间：WebRTC **50–100ms**，WebSocket **100–300ms**（微软口径）——这是本方案选 WHEP 的直接依据。
- 一个硬约束：判停(250ms 级) + TTS 首包(100–300ms) + 渲染追帧(25fps = 40ms/帧) + 传输(50–300ms) 四项已在 **500ms 量级**吃满。**跨网 RTT 一旦超过 ~80ms，就只能靠 prebuffer 换稳定性** —— 这也是编排层要与 GPU 同机部署的原因。

## 4. 升级项（按优先级）

### P0-1 真·barge-in：抢话自动停（预计 2–3 天）

**问题**：现在只能靠前端按钮或 API 发 `interrupt`，用户抢话时数字人不会自己闭嘴。

**做法**：
1. 前端在 `speak_audio_stream` 上行前插入 gate：10–20ms 帧的 RMS 能量 + 噪声底自适应；连续 ≥150ms 超过阈值判定为"用户在说话"。
2. 若该 session 当前处于 speaking 状态 → 立即 `POST /sessions/{id}/interrupt`，并清空 OmniRT 侧未消费音频。
3. 打断后保留 **200ms 预卷**（pre-roll），把打断瞬间的音频补回 STT，避免"你…"半个字丢掉。
4. 阈值/静音时长做成前端可调参数，按现场噪声标定。

**VAD 选型（有基准数据，别凭感觉选）**：2026-10 的公开受控基准（LibriVAD-small，2214 条含噪语音、9 类噪声、SNR −5~20dB）：

| 方案 | ROC AUC | 说明 |
| --- | --- | --- |
| TEN VAD @16ms hop | **0.917** | 对 20–50ms 窗长几乎不敏感（0.916–0.919），首选 |
| Silero VAD | 原生 32ms 输入时与 TEN 持平；**20ms 窗掉到 0.63** | 该论文认为与固定分帧协议有关，未做消融 |
| RMS 能量基线（即 P0-1 的起步方案） | 0.76 | **噪声环境下明显不够**，只适合先跑通 |

所以 P0-1 的路径是：**RMS 先上线拿数据 → 正式版换 TEN VAD**（前端约 1–2MB ONNX）。注意该基准论文**未测流式延迟与判停行为**，VAD 准确率不等于打断手感，仍需实测。
进阶选项：LiveKit Agents 用 transformer 做**语义轮次检测**来降低误打断，适合后续替换纯声学判停。

**验收**：用户在数字人说 1s 时插话 → 画面停止 ≤ 250ms；不出现"嘴动但没声音"的残留帧；打断后 1.5s 内能正常进入下一轮。

### P0-2 流式 TTS：首包压到 300ms 内（预计 2 天）

**做法**：
1. 句子级流式已具备（`pipeline/speak/`），重点是**首句切分**：首句按 12–18 字或第一个逗号切，别等句号。
2. 首句用低延迟 provider 先出声，后续句子并发预取；**但音色必须统一** —— 客服场景下不要混用 edge 与克隆音色，宁可用单一 provider。
3. 音色方案二选一：
   - **要克隆音色** → CosyVoice3 流式 + `local_cosyvoice_load_trt`/`fp16` 加速（配置项已在 `configs/default.yaml:73-82`）；
   - **要最低延迟、音色可接受** → Qwen3-TTS 或云端 Qwen/火山 TTS 流式。
4. 采样率统一 16000（与 talking-head 对齐），避免重采样引入额外缓冲。

**首包延迟参考**（口径混杂：厂商自述＋第三方横评，非统一基准，仅用于排序，不作为选型承诺）：

| 方案 | 首包延迟 | 备注 |
| --- | --- | --- |
| Qwen3-TTS | 97ms | 第三方横评汇总 |
| CosyVoice 2 (0.5B) | 150ms | 第三方横评汇总 |
| Fish Audio S2 | <150ms | 官方指标 |
| Qwen TTS Realtime | <200ms | 云服务 SLA |
| 豆包 TTS 流式 | <300ms | 社区实测 |
| Azure TTS | 350ms（P95 480ms） | 第三方 50 次实测 |
| edge-tts（当前默认） | 420ms（P95 580ms） | 第三方 50 次实测 |

**结论：默认的 edge-tts 是这张表里最慢的一档**，客服场景应换掉；CosyVoice 系（本地、可控音色、150ms 级）是性价比最高的替换目标。

**验收**：首句 TTS 首包 P50 < 300ms、P95 < 500ms；连续 20 轮无爆音、无截断。

### P0-3 分片粒度与并发调参（预计 1–2 天，收益最大）

这是单卡场景最"划算"的改动，全部靠环境变量，不需要改代码：

```bash
# MuseTalk：默认 320ms 一块，首帧等于被拖了一块
OPENTALKING_MUSETALK_CONTEXT_MS=200        # 试 160 / 200 / 240 三档
OPENTALKING_MUSETALK_OVERLAP_FRAMES=2      # context 变小后拼接抖动靠 overlap 补
OPENTALKING_MUSETALK_PREBUFFER_CHUNKS=1    # 起播平滑 vs 首帧延迟的权衡
OPENTALKING_MUSETALK_SILENCE_GATE=0.04     # 保留静音闭嘴

# QuickTalk：默认 slice_len=28 帧 @25fps ≈ 1.12s/块，首帧太慢
OPENTALKING_QUICKTALK_SLICE_LEN=10         # 10 帧 = 400ms；试 8 / 10 / 12
OPENTALKING_QUICKTALK_FPS=25
OPENTALKING_QUICKTALK_WORKER_CACHE=1
```

**注意**：`slice_len` 降低会增加 WS 往返次数与 GPU 小批次开销，**需同时看 fps 是否掉**；`context_ms` 降到 160 以下容易出现口型抖动与拼接痕迹。这两组参数必须用 benchmark 跑组合，不要凭感觉。

**验收**（参考第三方实测水位，留出安全余量）：

| 模型 | 参考吞吐（同社区口径） | 本方案目标（单卡 4090） |
| --- | --- | --- |
| MuseTalk 1.5 | 4090 **72fps** / 3090 45fps / 3080Ti 42fps | ≥45fps（2 路并发仍 ≥25fps） |
| QuickTalk | 3090 720×900@25fps、吞吐 35fps、显存约 **3.8GiB** | ≥30fps，显存 ≤6GiB |

- MuseTalk 只重绘 **256×256 人脸区域**，别按整帧分辨率去规划算力。
- **资产硬约束**：MuseTalk 按 25fps 训练，底片必须转成 25fps，否则唇音整体漂移 —— 这条要写进形象资产验收清单。
- 1 路会话显存占用目标 <8GB（QuickTalk 实际约 4GB，可据此评估 2 路可行性）；2 路并发 fps 掉幅 <20%。

### P1-1 传输与字幕（预计 3 天）

- 单机客服直接用 **WHEP（aiortc）**，不要上 mediamtx/RTMP；只有做直播推流或跨房 SFU 才用 `OPENTALKING_STREAMING_ENABLED=1`。
- 坐席在外网时必须部署 **TURN（coturn）**，否则企业网络下 WebRTC 建连失败率很高。
- 字幕时间轴：把 STT 增量文本、LLM token、TTS 起止时间戳打成一个 `subtitle` 事件，前端按音频播放位置推进字幕，避免"字比嘴快"。事件 schema 在 `opentalking/events/schemas.py`。

### P1-2 客服知识链路（预计 3 天）

- 接 LightRAG（`OPENTALKING_AGENT_KNOWLEDGE_ROOT`）+ mem0 记忆，问答走"检索 → 拼 prompt → LLM"。
- **关键**：`recall_timeout_ms` 默认 2000ms（`configs/default.yaml:88`），客服首句绝对等不起，压到 **300–500ms**，超时就直接用通用回复兜底。
- 知识库用 `scripts/benchmark_knowledge_upload.py` 做一次容量/延迟标定。

### P2-1 端到端语音 A/B（可选，预计 5 天）

按 `OPENTALKING_SPEECH_MODE=cascade|e2e` 双模式：

| 维度 | 三段式（主链路） | 端到端（A/B） |
| --- | --- | --- |
| 首音频 | 1.0–1.9s（本方案优化后） | 参考 1.37s（阿里实测） |
| 音色 | 完全可控（克隆/自有音色） | 受限于模型内置音色（除非厂商支持复刻） |
| 打断 | 自建 VAD + interrupt | 模型内建，71–300ms 级 |
| 字幕 | 天然精确 | 字幕走 `transcript.delta` 近似跟随（业界标准解法，非音素级时间戳） |
| 成本 | TTS+LLM+GPU 折旧 | 按 token 计费，约 1 分钱/分钟 |

**字幕/口型对齐这件事已经有标准解法**：行业通行的做法是**同时开 text + audio 双模态** —— 阿里 `qwen3.8-omni-flash-realtime` 原生输出文本+音频双路；OpenAI 在 `response.output_audio.delta` 之外并行推 `response.output_audio_transcript.delta`；Gemini Live 有 `inputAudioTranscription` / `outputAudioTranscription` / `explicitVadSignal`。**但 transcript delta 是增量文本、不是音素级时间戳**，所以字幕只能做"近似跟随"；口型对齐对 MuseTalk/QuickTalk 不构成问题——它们本来就直接吃音频波形。

**成熟度风险（必须知道）**：`gpt-realtime-2` 在社区出现把「应答词 + 下一步」拆成两轮、导致 5–20 秒静默的案例，已有人 A/B 回滚到 1.5。端到端上线前务必做多轮真实对话回归，别只看首音频指标。

**判断**：如果预置音色能满足业务（例如标准客服音），端到端更"先进"且更省事；如果必须是你复刻的真人音色，就别急着上。

### P2-2 云兜底形象（可选，预计 3 天）

GPU 排队/故障/超并发时，切换云端实时数字人（国内可用火山「实时互动数字人 API」、腾讯云智能数智人；海外 LiveAvatar/Simli/Akool 延迟更低但有跨境 RTT）。做成 `fallback_chain` 的第二档，只保证"能出声能出画"，不追求画质一致。

## 5. 配置清单（可直接抄）

```bash
# ---- 编排层与推理层 ----
OPENTALKING_DEFAULT_MODEL=musetalk
OMNIRT_ENDPOINT=http://<gpu-lan-ip>:9000
OPENTALKING_MUSETALK_BACKEND=omnirt
OPENTALKING_QUICKTALK_BACKEND=omnirt
# 如需鉴权
OMNIRT_API_KEY=<key>

# ---- 分片粒度（P0-3，按 benchmark 结论定档）----
OPENTALKING_MUSETALK_CONTEXT_MS=200
OPENTALKING_MUSETALK_OVERLAP_FRAMES=2
OPENTALKING_MUSETALK_PREBUFFER_CHUNKS=1
OPENTALKING_QUICKTALK_SLICE_LEN=10
OPENTALKING_QUICKTALK_FPS=25

# ---- 声音 ----
OPENTALKING_TTS_DEFAULT_PROVIDER=cosyvoice   # 或 dashscope / edge（试听对比后定）
OPENTALKING_TTS_STREAMING_DECODE=1
OPENTALKING_TTS_SAMPLE_RATE=16000

# ---- 对话 ----
OPENTALKING_LLM_PROVIDER=openai_compatible
OPENTALKING_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENTALKING_LLM_MODEL=qwen-flash              # 客服先跑 qwen-flash，再对比更强模型
OPENTALKING_MEMORY_RECALL_TIMEOUT_MS=400      # 默认 2000，会吃光首 token 预算

# ---- 传输 ----
OPENTALKING_STREAMING_ENABLED=0               # 单机客服不需要 RTMP/WHIP
```

## 6. 验收与压测

| 指标 | 目标 | 测法 |
| --- | --- | --- |
| 首帧响应（说完→首帧） | P50 ≤1.6s / P95 ≤2.4s | `scripts/benchmark_opentalking_e2e.py` + 前端打点 |
| 抢话停止 | ≤250ms | 人工 + 录屏逐帧核对 |
| 持续帧率 | ≥25fps，抖动 <10% | `/models` + 前端 stats |
| 显存占用 | 1 路 <8GB，2 路 <16GB | `nvidia-smi` 采样 |
| 长会话稳定性 | 30 分钟无累积延迟 | 连续对话回归 |
| 音画同步 | 口型偏差 <80ms | 录屏抽帧 |

已有工具：`scripts/run_opentalking_e2e_benchmark.sh`、`configs/benchmark/opentalking-e2e.yaml`、参考数据见 [Benchmark](../reference/benchmark.md)。

## 7. 排期（3 周可交付第一版）

| 周 | 内容 | 交付物 |
| --- | --- | --- |
| W1 | P0-3 调参基线与组合扫描；P0-1 抢话打断；P0-2 首句 TTS | 延迟基线报告 + 可用的打断体验 |
| W2 | P1-1 传输/字幕对齐；P1-2 知识链路与检索超时压降 | 端到端客服 demo（含知识问答） |
| W3 | 并发压测（2/4 路）、长稳、P2-1 端到端 A/B 接通 | 选型结论 + 上线配置 |

## 8. 风险与回滚

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| `context_ms` 调小导致口型抖动 | 观感变差 | 同步调 `overlap_frames`；逐档 benchmark，不行回退 240/320 |
| `slice_len` 调小导致 GPU 小批次效率下降 | fps 掉 | 看 fps 与显存曲线，找拐点；必要时只对首句用小分片 |
| 端侧 VAD 误触发 | 数字人频繁被打断 | 加 hangover + 最短语音时长；改用 TEN-VAD |
| 检索超时压太狠 | 答非所问 | 保持"检索命中即用、超时兜底话术"双分支 |
| 外网坐席无 TURN | WebRTC 建连失败 | 部署 coturn，并做建连成功率监控 |

## 9. 数据可信度声明（哪些能当承诺，哪些不能）

本方案里的数字按可信度分三档，**排期与验收只能挂靠"已核实"那档**：

**已核实（有公开一手来源）**
- MuseTalk 只重绘 256×256 人脸区域、25fps 训练约束、实时模式默认 batch_size 20（MuseTalk 官方 README）。
- WebRTC 50–100ms / WebSocket 100–300ms 的协议级区间（微软文档口径）。
- 厂商 API 存在性与模态能力：豆包 Seeduplex、qwen3.8-omni-flash-realtime（文本+音频双输出）、Gemini Live（含 `explicitVadSignal`/`avatarConfig`）、qwen-omni 支持 WS/WebRTC/AOQ。
- TEN VAD vs Silero vs RMS 的 AUC 基准（LibriVAD-small 受控实验）。

**仅厂商/第三方口径（用于排序，不作承诺）**
- 各家 TTS 首包延迟、Seeduplex 的 250/300ms 改善、qwen 实测的 71ms 打断停止、QuickTalk 的 35fps/3.8GiB、MuseTalk 的 72fps@4090。
- 这些数字的测试条件与你的数据/形象/网络不同，**必须自己复测**。

**明确未找到可靠数据（不要在评审里引用）**
1. IndexTTS / F5-TTS 的官方首包延迟。
2. webrtc-vad 的准确率/延迟对比。
3. GPT-Realtime-2、Gemini 3.8 Live 的官方延迟毫秒数。
4. "打断后停止出声"延迟的行业统一基准。
5. TTS chunk 大小对数字人口型稳定性的量化研究（只知方向：chunk 越小首帧越快、拼接越容易抖）。
6. "音画同步/追帧"的权威工程方案文档（业界通行做法是预缓存 + 时间戳对齐 + idle 帧，但无可引用实现说明）。

## 10. 待确认（需要你拍板的三件事）

1. **音色**：客服形象必须用真人复刻音色，还是可以用现成音色？—— 决定 P2-1 端到端能不能上主链路。
2. **并发**：第一期几路并发？—— 1–2 路可以单卡单进程；4 路以上要考虑 OmniRT 侧队列与第二张卡。
3. **形象**：QuickTalk（模板视频）和 MuseTalk（自由形象）谁是主、谁是备？—— 决定默认模型与 fallback 链。

## 11. 主要来源

- 豆包 Seeduplex 官方博客：<https://seed.bytedance.com/en/blog/introducing-seed-full-duplex-speech-llm-attentive-listening-robust-interference-suppression-enabling-more-natural-interaction>
- 豆包实时语音 3.0 API 上线报道：<https://www.itsdw.cn/news/11485.html>
- qwen3.8-omni-flash-realtime 模型页：<https://www.alibabacloud.com/help/zh/model-studio/qwen3-8-omni-flash-realtime>
- 阿里云开发者社区：实时语音实测（首音频 1.37s / 打断 71ms）：<https://developer.aliyun.com/article/1772614>
- OpenAI Realtime over WebSocket（`turn_detection` / transcript delta）：<https://learn.microsoft.com/zh-cn/azure/foundry/openai/how-to/realtime-audio-websockets>
- gpt-realtime-2 拆轮问题社区帖：<https://community.openai.com/t/gpt-realtime-2-splits-acknowledgment-next-step-into-separate-turns-causing-5-20s-caller-silence-rollback-to-gpt-realtime-1-5-confirmed-as-an-a-b-fix/1381276/3>
- Gemini Live 会话配置：<https://pub.dev/documentation/gemini_live/latest/gemini_live/LiveConnectConfig-class.html>
- MuseTalk 官方 README：<https://github.com/TMElyralab/MuseTalk>
- LiveTalking README（各卡实测 fps）：<https://github.com/lipku/LiveTalking>
- LiveKit Agents（语义轮次检测）：<https://github.com/livekit/agents>
- Pipecat（传输层与 S2S 覆盖）：<https://github.com/pipecat-ai/pipecat>
- VAD 受控基准论文：<https://arxiv.org/abs/2610.14607>
- Silero VAD：<https://github.com/snakers4/silero-vad>
- 开源 TTS 横评（2026）：<https://juejin.cn/post/7649764223339495475>
