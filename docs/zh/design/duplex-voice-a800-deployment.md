# A800 上部署开源全双工语音层（配 QuickTalk）

!!! abstract "TL;DR"
    A800 80GB 足够单卡跑 **MiniCPM-o 4.5**（9B，Apache-2.0，vLLM-Omni 原生全双工），
    官方单卡布局只需预留 85% 显存（约 68GB），余量充足。
    **Qwen3-Omni 30B 单卡仍然不现实**（社区 issue 显示 2×A100 80GB 都报显存不足）。
    建议：A800 独占语音层，QuickTalk 留在 4090；打通后音频需 **24kHz → 16kHz 重采样 + 400ms 切块** 才能喂 QuickTalk。

> 编写日期：2026-11-12
> 前置阅读：[QuickTalk + 云端语音端到端手册](quicktalk-cloud-pipeline-runbook.md)、[实时数字人实施方案](realtime-digital-human-plan.md)
> 依据：vLLM-Omni 官方 recipe（[MiniCPM-o 4.5](https://github.com/vllm-project/vllm-omni/blob/main/recipes/OpenBMB/MiniCPM-o-4_5.md)）、[Full-Duplex API 文档](https://docs.vllm.ai/projects/vllm-omni/en/latest/serving/full_duplex_api/)

## 1. 选型结论：A800 80GB 能放什么

| 模型 | 规模 | 单卡 A800 80GB | 判定 |
| --- | --- | --- | --- |
| **MiniCPM-o 4.5** | 9B | 官方单卡布局预留 85%（≈68GB） | ✅ **首选** |
| Step-Audio-2-mini | 7B 级 | 显存宽裕 | ✅ 备选（许可 Apache-2.0） |
| Qwen3-Omni 30B-A3B | 30B MoE | 默认配置 2×A100 80GB 仍 OOM | ❌ 单卡别试；要上就 2 卡或量化 |
| Nemotron VoiceChat 11B | 11B | 官方建议 80GB，能跑 | ❌ **OpenMDW 仅研究用，不能商用** |
| Moshi / Hertz-dev | 7B / 8.5B | 能跑 | ❌ 只支持英/法 |

## 2. 部署拓扑

```text
┌────────────────────── A800 80GB ──────────────────────┐
│ vLLM-Omni · MiniCPM-o 4.5（默认单卡三级流水线）        │
│   Thinker  (GPU0, util 0.55)                          │
│   Talker   (GPU0, util 0.15, 走 V1 runner)            │
│   Code2Wav (GPU0, util 0.15)                          │
│ 对外：WS /v1/realtime?duplex=1（OpenAI Realtime 协议） │
└────────────────────────┬──────────────────────────────┘
                         │ response.audio.delta (24kHz base64)
                         ▼
┌─────────────────── 编排层（CPU/任意机）────────────────┐
│ 重采样 24k→16k → 按 400ms 切块（slice_len 10 @25fps）  │
│ → OmniRT /v1/audio2video/quicktalk → 帧 → WebRTC WHEP  │
└────────────────────────┬──────────────────────────────┘
                         ▼
┌──────────── 4090 / 3090（画面层，独立）──────────────┐
│ OmniRT · QuickTalk（峰值显存约 1.8GB，46.9fps@4090）  │
└───────────────────────────────────────────────────────┘
```

**为什么语音层和画面层分卡**：MiniCPM-o 三个 stage 已经要 0.85 的显存预算，再塞 QuickTalk 会让三级上下文互相抢 SM，官方 recipe 明确说单卡 co-location 是"用吞吐换硬件密度"。分卡后两边都能满负荷。

如果你**只有一张 A800**：把 MiniCPM-o 的 utilization 降到 0.78 左右（≈62GB），给 QuickTalk 留 16GB+，然后必须实测 Talker 的音频 TTFP 是否劣化。

## 3. 安装与启动（A800 单卡）

```bash
# 1) 环境：Linux + Python 3.10+，vLLM / vLLM-Omni >= 0.21.0
uv venv --python 3.11 .venv-omni
source .venv-omni/bin/activate
uv pip install vllm
uv pip install vllm-omni

# 2) Code2Wav 的声码器依赖（注意不是上游 Step-Audio2 的包）
uv pip install stepaudio2-minicpmo

# 3) 启动 duplex 服务（默认单卡布局会自动加载，不需要 --deploy-config）
vllm serve openbmb/MiniCPM-o-4_5 --omni \
    --trust-remote-code \
    --chat-template vllm_omni/transformers_utils/chat_templates/minicpmo45_native.jinja \
    --chat-template-content-format openai \
    --host 0.0.0.0 --port 8099
```

多点显存时的两个开关（写在自定义 deploy overlay 里）：

```yaml
# 示例思路：单卡 co-location 时下调预算
platforms:
  cuda:
    stages:
      - gpu_memory_utilization: 0.50   # Thinker
      - gpu_memory_utilization: 0.12   # Talker
      - gpu_memory_utilization: 0.12   # Code2Wav
```

**有 2 张 A800 时**（推荐用于生产吞吐）直接换官方 2 卡布局：

```bash
vllm serve openbmb/MiniCPM-o-4_5 --omni \
    --deploy-config vllm_omni/deploy/minicpmo_4_5_2gpu.yaml \
    --trust-remote-code --port 8099
```

## 4. A800 专属注意点

| 项 | 影响 | 应对 |
| --- | --- | --- |
| **Ampere（SM80）没有 FP8** | vLLM-Omni 的 FP8 W8A8 量化路径在 A800 不可用 | 80GB 跑 9B 不需要量化；真要用就用 W8A16 / AWQ / GGUF |
| **A800 NVLink 带宽被限制** | 多卡 TP 通信效率低于 A100 | 优先单卡三阶段或 2 卡阶段拆分，避免 TP 摊大 |
| Talker 只能跑 V1 runner | CUDA 上 Thinker/Code2Wav 走 Model Runner V2，Talker 仍 V1（多帧 decode 只支持 V1） | 不要全局加 `--enforce-eager`，否则会破坏 Thinker 的图重放 |
| 启动时显存竞争 | 三个 stage 同卡三个 CUDA context | 保留官方 `max_num_seqs: 4`，别盲目调高 |
| `--trust-remote-code` 必需 | HF 仓是自定义 config 类 | 内网部署时提前审计代码 |

## 5. 打通 QuickTalk 的三个技术点（这是容易翻车的地方）

1. **采样率**：MiniCPM-o 输出 **24kHz 单声道**，QuickTalk 吃 **16kHz** → 必须重采样，否则音画节奏整体错位。
2. **切块粒度**：duplex 是 20ms 级增量流，QuickTalk 的 `generate()` 按 `slice_len`（默认 10 帧 @25fps = **400ms**）吃音频 → 需要缓冲成 400ms 再送，否则每块都太短、口型抖。
3. **打断联动**：duplex 的打断是四步 —— 停本地播放 → 上报 `playback.ack` 最终位置 → `conversation.item.truncate` → `output_audio_buffer.clear`；而在你这边还要同时 `POST /sessions/{id}/interrupt` 停 QuickTalk 的帧下发并清空 OmniRT 未消费队列。**两套打断必须同时触发**，否则会出现"声音停了嘴还在动"。

## 6. 验收清单

| # | 检查 | 方法 |
| --- | --- | --- |
| 1 | 服务起得来 | `curl localhost:8099/health`、`/v1/models` |
| 2 | 能力协商 | 连上后读 `session.created.session.capabilities`，确认 `supports_barge_in`、`chunk_period_ms` |
| 3 | 中文音色 | `--ref-audio` 传你的参考音色 WAV，主观打分 |
| 4 | 官方打断用例 | 跑 `examples/online_serving/barge_in_client.py`，确认第一条回复被取消 |
| 5 | 端到端联动 | 打断时 QuickTalk 画面 ≤250ms 停 + 无残留口型帧 |
| 6 | 显存 | `nvidia-smi` 连续采样 30 分钟，看三级是否稳定不涨 |
| 7 | 延迟 | 记录"说完→首音频"和"说完→首帧"，与三段式基线对比 |

## 7. 风险与回退

- **中文效果未验证**：MiniCPM-o 是 OpenBMB 系（中文母语场景），但全双工中文的打断手感、方言/口音鲁棒性必须实测，不要用英文 demo 结论外推。
- **音频 TTFP 不一定达标**：官方 Daily-Omni 批处理基准里"平均音频 TTFP 3.24s"是**离线吞吐场景**，不等于实时会话的首音频延迟；实时延迟要在 duplex 会话里单独测。
- **回退路径**：语音层随时可退回云端 API（OpenAI Realtime 兼容协议一致，改 endpoint 即可），画面层不动。这条回退路径就是选 duplex 协议而不是私有协议的价值。
