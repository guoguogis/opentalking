# OpenTalking 接入 Vidu S2-Avatar 可行性评估

!!! abstract "结论"
    **技术上可行，且与现有架构不冲突**——因为 OpenTalking 的画面层本来就是"音频进 → 帧出"，而 Vidu 组件版正好吃裸 PCM。
    但它是**新增一条 `cloud_avatar` 旁路**，不是"加一个 provider"：画面渲染、帧缓存、fps 调参全部外移，**RTC 传输要从 aiortc 换成厂商 RTC SDK**。
    工作量：**生产可用 4–5 周（1 人）**；只做单路 demo **1.5–2 周**。
    最大风险不在后端，在**前端 RTC SDK 替换**。

> 编写日期：2026-11-14
> 依据：Vidu 官方文档（[组件版](https://platform.vidu.cn/docs/api-reference/digital-human/realtime-avatar-component)、[实时版](https://platform.vidu.cn/docs/api-reference/digital-human/realtime-avatar-standard)）、[参数说明](https://platform.vidu.cn/vidu-stream/doc/s2-avatar/realtime/parameters)
> 现状参考：[3×A800 方案](3xa800-realtime-avatar-architectures.md)、[MiniCPM-o 改造评估](training-simulation-avatar-stack.md)

## 1. Vidu 有两种模式，必须先选

| | **组件版**（自带 RTC + 自带语音） | **实时版**（Vidu 全包） |
| --- | --- | --- |
| 端点 | `POST /live/v1/external-lives` | `POST /live/v1/lives` |
| Vidu 负责 | **只做渲染 + 推流** | ASR + 对话 + TTS + 渲染 |
| 你发什么 | **裸 PCM 音频**（二进制帧）+ 转写文本（文本帧） | 文本（`text_msg`） |
| RTC | 你提供频道（`customer_rtc`）或平台托管（`managed_rtc`） | 平台下发 `rtc.token` / `rtc.user_id` |
| 计费（中国区 2026-09 媒体口径） | 约 **1.9 元/分钟** | 约 **2.8 元/分钟** |
| 对 OpenTalking 的意义 | ✅ **保留 LLM/TTS/评分/记忆** | ❌ 业务能力被 Vidu 接管 |

**必须选组件版**，否则 Rubric 评分、知识库、人设记忆全部落不到你的系统里，等于放弃 OpenTalking 的价值。

## 2. 组件版协议细节（决定工作量）

```text
① POST /live/v1/external-lives  （带 customer_rtc 或 managed_rtc）
   → live.id + WS 建连凭证
② WS /live/v1/external-lives/{live_id}/stream
   首帧必须发 conn_init(type=1) → 收 conn_init_ack.success=true
   若 NOT_READY：等一会重发 conn_init，不用重连
③ 运行期（两条链路并行，缺一不可）
   WS（控制）  ：二进制帧=裸 PCM；文本帧=JSON 信令
   RTC（媒体） ：Vidu 把渲染好的数字人推进你的频道，客户端订阅渲染
```

| type | 名称 | 方向 | 用途 |
| --- | --- | --- | --- |
| 1 / 2 | `conn_init` / `conn_init_ack` | C→S / S→C | 握手，`success=true` 开始计费 |
| 5 / 6 | `call_hangup` / `force_hangup` | C→S / S→C | 挂断 |
| **7** | **`audio_interrupted`** | C→S | **打断当前回复**（VAD 检测用户开口时发） |
| 9 | `input_transcription` | C→S | 用户转写文本 |
| 10 | `output_transcription` | S→C | 模型回复文本 → **字幕来源** |

**硬性约束**：单会话最长 **7200s**；服务端约 5s 一次 ping，**15s 无响应判断线**；断线后需用**同一 `live_id` + `conn_id`** 在宽限期重连并重发 `conn_init`，超期会话结束停止计费；维护可能以 close code `1012` 断开。

**RTC 可选四家**：`artc`（阿里，默认）/ `trtc`（腾讯）/ `agora`（声网）/ `volcengine`（火山）。**不是只能锁死阿里**——如果你们已有火山/声网账号，可以少谈一次商务。

## 3. 与 OpenTalking 的映射

| 现有能力 | 接 Vidu 后 |
| --- | --- |
| ASR（FunASR/dashscope） | ✅ 保留（用户语音自己转写，再喂 type=9） |
| LLM + 人设 + RAG + 记忆 | ✅ 保留（`_build_agent_context` / `_memory` 不动） |
| TTS（edge/CosyVoice/Qwen） | ✅ 保留，输出 PCM 直接发 WS |
| **画面渲染（OmniRT / FlashHead / QuickTalk）** | ❌ **整体外移给 Vidu** |
| **WebRTC 传输（aiortc `/webrtc/{id}/offer`）** | ❌ **换成厂商 RTC SDK** |
| 帧缓存 / fps 调参 / idle 视频 / ping-pong 素材规范 | ❌ 全部不再需要（净减少一堆复杂度） |
| 打断 `runner.interrupt()` | ⚠️ 保留，但要多发一个 `audio_interrupted`(type=7) |
| 字幕事件 | ⚠️ 数据源换成 type=10 |
| 录像（recording/offline_export） | ⚠️ 需改：本地没有帧了，只能录 RTC 远端流或让 Vidu 侧导出 |

**净效果**：删掉一整层本地渲染，但换上一套厂商 RTC 客户端与生命周期管理。**不是纯减法。**

## 4. 改造点与工作量

| # | 改造点 | 内容 | 估时 |
| --- | --- | --- | --- |
| 1 | `providers/avatars/vidu/` | HTTP 建会话 + WS 客户端（握手/PCM/信令/ping-pong/重连/挂断），约 800–1200 行 | **5–7 天** |
| 2 | Runner 接线 | `speak()` 的 TTS 输出改走 WS；`speak_uploaded_pcm` 直接转发；新增 `avatar_backend=vidu` 分支 | **3–4 天** |
| 3 | **前端 RTC 替换** | 装厂商 RTC Web SDK，订阅远端流绑 `<video>`；后端下发频道凭证；自动播放策略、TLS、跨域 | **5–8 天** ⚠️ |
| 4 | 打断 / 字幕 / 生命周期 | type=7 双向联动；type=9/10 → 字幕事件；idle_timeout、7200s、1012 重连、计费埋点 | **4–5 天** |
| 5 | 联调与压测 | 端到端时延、弱网、断线重连、多会话并发、长时间稳定（>1h） | **4–5 天** |
| | **合计** | | **≈4–5 周** |

**缩水版（只做单路 demo，不做多会话/重连/计费/录像）**：砍掉 #4 一半、#5 一半 → **1.5–2 周**。

## 5. 风险

| 风险 | 说明 | 应对 |
| --- | --- | --- |
| **RTC SDK 前端改动**（最大） | 现有前端吃 aiortc 的 track，现在要换成厂商 SDK；浏览器自动播放、鉴权票据、内网 TLS 都可能卡 | 先花 2 天做最小订阅 demo，验证再排期 |
| **跨网延迟叠加** | 你的 TTS PCM → 公网 WS → 云端渲染 → RTC → 浏览器，比本地 WebRTC 多 1–2 跳 | 先测首帧与口语延迟，达标再推 |
| **计费按分钟**（关键商业风险） | 组件版约 1.9 元/分钟 → **1 小时≈114 元/路**；每天 8h×22 天 ≈ **2 万元/月/路** | 见 §6 |
| 7200s 上限 | 长会话必须主动轮换 | 会话管理里做续接 |
| 数据出境 | 用国际站（.com）涉及出境；国内走 .cn | 实训用 .cn |
| 录像缺失 | 本地不再有帧，复盘素材来源要重新设计 | 用 RTC 服务端录制或客户端录制 |

## 6. 成本对比（这是决定因素）

| 方案 | 单位成本 | 实训 1 路 × 8h/天 × 22 天 | 备注 |
| --- | --- | --- | --- |
| Vidu 组件版 | ≈1.9 元/分钟 | **≈2.0 万元/月** | 不含并发扩容费（国际站报价约 $2,080/月/并发） |
| 自建 FlashHead（3×A800） | GPU 摊销 | ≈0（已购硬件） | 但要人天调优 + 电费 |
| HeyGen LiveAvatar | $0.01/分钟起 | ≈$105/月 | 跨境延迟 + 数据出境 |

**结论：只要并发路数 × 使用时长上去，Vidu 按分钟计费远贵于自建。**它适合"上线初期 / 高价值少量会话 / 合规兜底"，不适合作为 7×24 实训的主力通道。

## 7. 四个选项的决策矩阵

| 选项 | 工作量 | 保留业务控制 | 单位成本 | 适用 |
| --- | --- | --- | --- | --- |
| **A. Vidu 组件版** | 4–5 周 | ✅ 全部保留 | 1.9 元/分钟 | 国内合规 + 自有语音 + 可接受计费 |
| B. Vidu 实时版 | 1.5–2 周 | ❌ 评分/知识/记忆被接管 | 2.8 元/分钟 | 只想快速有个能对话的数字人 |
| **C. 自建 FlashHead** | 2–3 周 | ✅ 全部保留 | 硬件摊销 | **长期主力（推荐）** |
| D. HeyGen API | 2–3 天 | ⚠️ 画面外移，业务仍自有 | $0.01/分钟 | 最快出 demo / 兜底 |

## 8. 建议路径

1. **不要用 Vidu 替代自建主力**，成本结构不成立。
2. **先用 D（HeyGen）或 B（Vidu 实时版）在 2 周内出可对话 demo**，验证产品形态。
3. **主力走 C（自建 FlashHead）**，A800 已经在手，边际成本接近零。
4. **Vidu 组件版作为"合规兜底通道"**：当客户要求数据不出境/私有化受限、或某天 GPU 全满时切过去。按这个定位，工作量可压到 **1.5–2 周**（只做单路、不做计费与多会话）。

## 9. 若决定做，验收清单

| # | 检查 | 标准 |
| --- | --- | --- |
| 1 | 建会话 + 握手 | `conn_init_ack.success=true` |
| 2 | PCM 驱动 | 你的 TTS 音频能让云端数字人开口，口型同步 |
| 3 | 首帧延迟 | 与自建方案对比，记录差值 |
| 4 | 打断 | 本地 `interrupt()` → 250ms 内云端停嘴（type=7） |
| 5 | 字幕 | type=10 能落到现有字幕事件，不错位 |
| 6 | 弱网 | 断网 10s 内恢复重连（同 live_id/conn_id） |
| 7 | 长会话 | 连续 1h 无漂移、无 1012 意外断连 |
| 8 | 计费 | 埋点能统计每会话秒数，与账单对齐 |
