# 模拟实训数字人技术栈（含实时对话）

!!! abstract "TL;DR"
    三条业务需求 + 实时对话，最终是**"两条渲染路径 + 一个语音层"**：
    **实时对话走 SoulX-FlashHead（唯一明确支持"无限实时流式"的 1.3B 模型，单卡 4090 可 3 路并发）**；
    **高质量离线走 LiveAvatar / LongCat-Video-Avatar**；
    **语音层用三段式（ASR + 复刻 TTS）保音色可控，duplex 模型作为低延迟实验**。
    3×A800：`#1 语音层`、`#2 实时渲染 + 句子级备选`、`#3 高质量离线`。

> 编写日期：2026-11-12
> 一手来源：SoulX-FlashHead [官方 README](https://github.com/Soul-AILab/SoulX-FlashHead)（2026-02）、EchoMimicV3 [官方 README](https://github.com/antgroup/echomimic_v3)（2026-01）、[Tight Studio 2026-08 独立横评](https://tight.studio/zh-cn/blog/open-source-talking-avatar-models/)
> 前置：[3×A800 方案](3xa800-realtime-avatar-architectures.md)、[A800 全双工部署](duplex-voice-a800-deployment.md)

## 1. 需求 → 技术含义

| 需求 | 技术含义 | 结论 |
| --- | --- | --- |
| 模拟实训 | 角色扮演多轮对话 + 评分 + 报告 | 需要编排层与评分引擎，不是模型问题 |
| 自定义数字人 | 上传一张照片即成形象 | 生成式模型（单图 + 音频），**不再需要模板视频** |
| 语音驱动生成视频 | 音频 → 对口型视频 | audio2video，离线/句子级均可 |
| **实时对话** | 边说边出画面，可打断 | **必须是原生流式模型**，这一条直接淘汰大部分候选 |

## 2. 关键分野：流式 vs 非流式

这是选型的第一道闸门，官方文档口径：

| 模型 | 流式 | 官方实时指标 | 显存 |
| --- | --- | --- | --- |
| **SoulX-FlashHead Lite** | ✅ **原生无限流式** | 单卡 4090：**96 FPS**，或 **3 路并发实时（25+ FPS）** | ~6.4 GB |
| **SoulX-FlashHead Pro** | ✅ 流式 | 单卡 4090 仅 **10.8 FPS**；**实时需 2×RTX5090 + SageAttention** | 中等 |
| **SoulX-FlashTalk 14B** | ✅ 块级流式 | **0.87s 亚秒延迟、32 FPS**、超长视频稳定 | ~58 GB |
| EchoMimicV3-Flash | ❌ **非流式**（整段扩散） | 8 步、12GB、最高 768×768；流式由后续 EchoTorrent 论文跟进，未开源 | 12–34 GB |
| LiveAvatar | ❌ 非流式 | 质量最高但整段生成 | 61 GB |
| LongCat-Video-Avatar 1.5 | ❌ 非流式 | 表现力最强 | 47 GB |
| Wan2.2-S2V-14B | ❌ 非流式 | 780s / 3.6s 音频，16fps | 59 GB |

**结论：实时对话 = FlashHead（Lite 多路并发 / Pro 双卡）或 FlashTalk 14B。EchoMimic、LiveAvatar、LongCat 只能做非实时。**

## 3. 最终架构

```text
┌─ 层① 语音层（实时对话的"耳朵+嘴巴"）───────────────────────┐
│  A：三段式 = FunASR 流式 ASR + CosyVoice3/IndexTTS2 复刻 TTS │  ← 音色可控，推荐
│  B：duplex = MiniCPM-o 4.5（内建判停/打断，但音色受限）      │  ← 低延迟实验
│  输出：16kHz PCM（画面层）/ 24kHz（duplex 需重采样）          │
└────────────────────────────────────────────────────────────┘
┌─ 层② 实时渲染（模拟实训的在线对话）────────────────────────┐
│  SoulX-FlashHead Lite（单卡 3 路并发 25+fps）               │
│  输入：一张正面照 + TTS 音频流；输出：25fps 视频（WebRTC）    │
└────────────────────────────────────────────────────────────┘
┌─ 层③ 高质量渲染（案例库、课程回放、关键片段复盘）──────────┐
│  LiveAvatar（最清晰）/ LongCat（动作最有表现力）             │
│  也可用 FlashHead Pro（质量更高，但单卡 10.8fps，准实时）     │
└────────────────────────────────────────────────────────────┘
┌─ 层④ 业务编排（自研）──────────────────────────────────────┐
│  场景/人设/Rubric → 对话 → 评分 → 报告 → 关键片段重渲染       │
└────────────────────────────────────────────────────────────┘
```

**两条渲染路径共用同一个形象资产**（一张参考图 + 一套音色），这是这套架构最省事的地方：练习用轻模型、复盘用重模型，学员感知到的形象是一致的。

## 4. 实时对话的延迟预算

| 环节 | 三段式 + FlashHead Lite | duplex + FlashHead Lite |
| --- | --- | --- |
| 判停（VAD/端点） | 300–500ms | 模型内建（约省 250ms） |
| ASR 终稿 | 200–400ms | — |
| LLM 首 token | 250–500ms | 含在 duplex 内 |
| TTS 首包 | 150–300ms | 含在 duplex 内 |
| 画面首帧（流式） | 200–400ms（含预缓冲） | 同 |
| WebRTC | 30–80ms | 同 |
| **合计** | **≈1.1–2.2s** | **≈0.9–1.6s** |

注意：FlashHead Pro 在单卡 4090 只有 10.8fps，**不能进实时链路**；A800 上要实测它在单卡的帧率，若达不到 25fps 就只能放层③做准实时。

## 5. 3×A800 分配

| 卡 | 负载 | 显存 |
| --- | --- | --- |
| **A800 #1** | 语音层：FunASR 流式 ASR + CosyVoice3 复刻 TTS + 评分 LLM（qwen）+ RAG | 20–30GB，余量大 |
| **A800 #2** | 实时渲染：FlashHead Lite（6.4GB，可多实例开 3–6 路）+ EchoMimicV3-Flash（12–34GB，句子级备选） | 40–60GB |
| **A800 #3** | 高质量离线：LiveAvatar（61GB）或 LongCat（47GB） | 47–61GB，刚好 |

**如果语音层改走 duplex**：MiniCPM-o 4.5 单卡吃 0.85×80≈68GB，需要独占一张卡 → 把 #1 给它，#2 只放渲染（更宽裕），ASR/TTS 则被 duplex 取代。

**取舍**：实训场景通常要求"固定人设音色"（比如模拟某类客户/患者），**三段式能精确复刻音色，duplex 不能**。所以我建议：**生产走三段式，duplex 只做 A/B 实验**，用 #3 空出来的时段或第二实例验证。

## 6. 模拟实训的业务闭环

```text
场景配置（人设 + 背景 + 任务目标 + Rubric）
        ↓
学生进入 → FlashHead 实时渲染的数字人扮演角色
        ↓ 实时对话（层① + 层②）
语音 → ASR → LLM（人设+知识+RAG）→ TTS 复刻 → 流式视频
        ↓
对话结束 → LLM 按 Rubric 评分（流程完整性/合规/共情/话术）
        ↓
报告 + 关键片段用层③（LiveAvatar）重新高质量渲染后回放
```

**差异化点**：练习时用 FlashHead Lite 保流畅（3 路并发），复盘时用 LiveAvatar 把关键 10 秒重新生成，学员看到的回放画质明显更高——这是"模板重放"路线做不到的。

## 7. 编排层怎么选

| 选项 | 判断 |
| --- | --- |
| **OpenTalking** | ✅ 已支持 `direct_ws`/`omnirt` 接 FlashTalk/FlashHead，WebUI/会话/事件/知识库可直接复用；实时链路改造成本最低 |
| OpenAvatarChat | avatar handler 只有 LiteAvatar/LAM/MuseTalk，四个候选都要自己写 |
| 自研业务 + 模型 HTTP 服务 | 实训场景/评分/报告本来就要自研，但**实时对话的 WebRTC 与打断不要自己造**，用 OpenTalking 现成能力 |

**建议**：OpenTalking 管"实时对话 + 画面 + WebRTC + 事件"，自研系统管"场景 + 评分 + 报告 + 片段重渲染调度"。

## 8. 许可与合规

| 模型 | 许可 |
| --- | --- |
| EchoMimicV3 | ✅ **Apache-2.0**（README 明确声明，且声明不主张生成内容权利） |
| LiveAvatar | ✅ Apache-2.0（HF 模型卡） |
| FlashHead / FlashTalk | ⚠️ 待核实仓库 LICENSE（Soul App 开源） |
| LongCat-Video-Avatar 1.5 | ⚠️ 有独立 LICENSE，需逐条读 |
| Wan2.2-S2V | ⚠️ 以仓库 LICENSE 为准 |
| HunyuanVideo-Avatar | ❌ 社区许可受限，商用需申请 |
| SkyReels-A3 | ❌ 被指"伪开源"，避开 |
| 合规 | 肖像授权、声音授权、合成内容标识（2025-09-01 起施行） |

## 9. 实施路线（3 周）

| 周 | 目标 | 验收 |
| --- | --- | --- |
| W1 | 一张照片跑通 FlashHead Lite 流式 + EchoMimicV3-Flash 句子级，对比画质/帧率 | 两段视频 + 帧率实测表 |
| W2 | 接 OpenTalking：三段式语音 → FlashHead 流式 → WebRTC；打通打断 | 能实时对话，抢话 250ms 内停 |
| W3 | 接层③ LiveAvatar/LongCat 做关键片段重渲染；接评分 Rubric | 完整实训报告 + 高质量回放 |

## 10. 关键风险

| 风险 | 应对 |
| --- | --- |
| FlashHead Pro 在 A800 单卡达不到 25fps | 实时链路固定用 Lite；Pro 只放层③ |
| Lite 画质偏弱（近裁、身份弱） | 用 Pro 做复盘渲染补观感；或实测 EchoMimic 句子级的生成速度是否够"边生成边播" |
| 流式首帧抖动/嘴瓢 | FlashHead 的 TACC（8 秒时序音频上下文缓存）就是为这个设计的，保留默认；预缓冲 1–2 块 |
| 音色与形象不匹配 | 音色必须来自 TTS 复刻，生成式模型不改变输入音频（实测相关性 0.999+） |
| 长视频身份漂移 | 瓶颈在模型本身的稳定性；用 FlashHead 的蒸馏/长视频能力或分段生成 + 首帧锚定 |
