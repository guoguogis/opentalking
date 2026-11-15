# QuickTalk + 云端语音：端到端落地建议

!!! abstract "TL;DR"
    画面走私有 **QuickTalk**（动作保真、4090 上 46.9fps），语音与语义走**国内云端**（STT/TTS/LLM），
    **判停用云侧、抢话用端侧、执行停嘴在本地**。素材阶段决定上限：视频必须 25fps、8–15s、**首尾动作闭环**（QuickTalk 是正放倒放 ping-pong 循环）。

> 编写日期：2026-11-12
> 适用前提：单卡 4090/3090 + 局域网 OmniRT；国内云 API 可用；客服问答场景。
> 相关文档：[实时数字人实施方案](realtime-digital-human-plan.md)、[Avatar 资产格式](../docs/avatar-format.md)、[QuickTalk Local 部署](../avatar_models/deployment/quicktalk-local.md)。

## 1. 分工总表：谁在哪一层，为什么

| 环节 | 部署位置 | 选型 | 为什么放这里 |
| --- | --- | --- | --- |
| 轮次判定（"你说完了吗"） | 云端 STT | dashscope paraformer-realtime-v2（自带端点检测） | 要准；容忍 200–400ms |
| **抢话检测（"我要打断"）** | **浏览器端** | TEN-VAD（ONNX，16ms hop，AUC 0.917）或能量门限起步 | 要快；云端一个来回就 50–150ms，等不起 |
| 音频上行 | 本地编排层 | `WS /sessions/{id}/speak_audio_stream` | 已有实现 |
| STT | 云端 | 同上，流式增量 | 免维护、抗噪、支持热词 |
| LLM | 云端 | qwen-flash 起步（有知识库时注意检索超时） | 便宜、可换 |
| TTS | 云端 | cosyvoice-v3-flash / Qwen-TTS 流式 | 首包 150–300ms，本地克隆音色留给后续 |
| **停嘴执行** | **本地（必须）** | `POST /sessions/{id}/interrupt` → `runner.interrupt()` | 云端无法替你停帧；只有本地说停就停 |
| 口型渲染 | 私有 OmniRT | QuickTalk | 动作保真、单卡可跑 |
| 传输 | 本地 | WebRTC WHEP（aiortc） | 50–100ms，比 WS 传帧快一档 |

**一句话原则**：**"判断"可以上云，"停嘴"必须在本地。** 云端再快也有 RTT，抢话后的静音只能在编排层立刻执行。

## 2. 第 1 步：素材制作（决定 70% 的最终观感）

### 2.1 必须遵守的参数

| 项 | 要求 | 原因（代码/机制依据） |
| --- | --- | --- |
| 帧率 | **25fps**（拍摄可 50fps 再转） | 模型按 25fps 训练；非 25fps 会被 `_ensure_25fps` 二次转码，多一层重采样 |
| 时长 | **8–15 秒** | QuickTalk 用 **ping-pong** 循环（正放→倒放），太短重复感极强 |
| 画幅比 | 与输出一致（如 3:5 → 540×900） | 转换脚本用 `cv2.resize` **拉伸、不补边**，比例不一致会变形 |
| 长边 | ≤900px（默认被 `--max-long-edge 900` 缩到 900） | 1080×1920 会被压成 506×900；想要 540×900 就提前裁 3:5 |
| 机位 | 固定三脚架、正面、半身、眼睛在画面上 1/3 | 转头幅度大→人脸框跳变，口型错位 |
| 光照 | 双侧柔光 5000–6000K，不逆光不顶光 | 人脸检测/贴回质量直接受光影响 |
| 口型 | **素材里嘴保持中性微闭** | 素材本身有说话口型会和驱动口型"打架" |

### 2.2 动作设计（这是最容易翻车的地方）

QuickTalk 的循环是 `idx = offset if cycle % 2 == 0 else n-1-offset`（`opentalking/models/quicktalk/runtime.py:425-437`）——**正放一遍、倒放一遍**。

所以：

- ✅ 可逆小动作：呼吸起伏、轻微点头、微笑、微微侧身再回来、手指轻动
- ✅ 理想结构：**A → B → A**（首尾姿态几乎一致，接缝无感）
- ❌ 单向大动作：转身走开、手从画面外进来、挥手到画外、持续下蹲
  - 倒放时物理上是反的（"手放下"倒放成"手抬起"），观众会觉得"鬼畜"
- ❌ 手部遮挡嘴部、频繁走出画面、机位抖动

### 2.3 交付物清单

```text
raw/
├── main_take.mp4          # 8–15s，25fps，闭环动作，中性口型
├── idle_take.mp4          # 2–3s 静止呼吸，做 idle 帧
└── consent.pdf            # 肖像授权（商用必须）
```

## 3. 第 2 步：素材 → QuickTalk 资产

### 3.1 重要提醒：WebUI 上传视频**不会**自动生成 QuickTalk 资产

通过 `POST /avatars/custom` 上传视频时，服务端只做一件事：把视频抽成帧序列做成 **Wav2Lip** 动态资产（`apps/api/routes/avatars.py:1206-1217`）。
QuickTalk 的模板视频与 face cache 只在**上传图片**的分支里生成（`avatars.py:1246`）。

**但是**上传视频会把原视频存到 `source/source_video.mp4` 并写进 `metadata.source_video`，而 `opentalking-prepare-cache` 的素材解析顺序里**第二优先级就是 `metadata.source_video`**（`apps/cli/prepare_cache.py:102-122`）。

所以正确路径是：**先上传，再补跑一次 prepare-cache**。

### 3.2 三条落地路径（选一条）

**路径 A：WebUI 上传视频 → 补跑缓存（最省事）**

```bash
# 1) WebUI 形象库上传视频，得到 avatar id（例如 custom-host-01）
# 2) 生成 QuickTalk 模板 + face cache
opentalking-prepare-cache \
  --model quicktalk \
  --avatars-root examples/avatars \
  --avatar custom-host-01 \
  --quicktalk-asset-root models/quicktalk \
  --device cuda:0 \
  --max-long-edge 900 \
  --verify
```

**路径 B：手工放素材（可控性最强）**

```bash
# 目标目录：examples/avatars/<id>/quicktalk/template_<W>x<H>.mp4
ffmpeg -y -i raw/main_take.mp4 \
  -vf "fps=25,scale=540:900:force_original_aspect_ratio=increase,crop=540:900" \
  -c:v libx264 -crf 18 -pix_fmt yuv420p -an \
  examples/avatars/host-01/quicktalk/template_540x900.mp4
# manifest.json 里 width=540, height=900
```

**路径 C：放在 avatar 根目录当"源视频"**，让解析器自己找

素材解析优先级（`prepare_cache.py:102-122`）：
1. `metadata.quicktalk.template_video` / `.source_video`
2. `metadata.template_video` / `metadata.source_video`
3. 目录下的 `idle.mp4` / `source.mp4`
4. 图片兜底：`metadata.source_image` / `reference.png` / `preview.png`（→ 静态模板）

### 3.3 产物与命名规则

```text
examples/avatars/host-01/
├── manifest.json                     # width/height 决定 W×H
├── preview.png
├── source/source_video.mp4           # 原始视频（保留，便于重跑）
└── quicktalk/
    ├── template_540x900.mp4          # 25fps 模板
    └── face_cache_v3_540x900.npz     # 人脸检测缓存（首轮省 1–2s）
```

- 目标尺寸 = `manifest.width/height`，长边超过 `--max-long-edge` 就等比缩到 900，并取偶数（`_target_video_size`）。
- **换素材必须加 `--overwrite`**：face cache 命中条件是帧数完全一致（`runtime.py:281` 的 `len(cached) == len(self.frames)`），改了视频却不加 `--overwrite` 会命中过期缓存。

## 4. 第 3 步：云端语音接线 + 打断

### 4.1 双 VAD 分工（本方案的核心设计）

```text
麦克风 16k PCM ──┬─► 端侧 TEN-VAD（16ms 帧）
                 │      └─ 判定"正在说话" → 若数字人正在说 → 立刻 interrupt（本地，≤100ms 发出）
                 │      └─ 同时保留 200ms 预卷，补回被打断的那个字
                 │
                 └─► WS /speak_audio_stream ──► 云端 STT（判停/端点）
                                                  └─ 出终稿 → LLM → TTS → QuickTalk
```

- 端侧 VAD：**只负责"打断"**，宁可稍敏感（能忍误打断，不能忍不打断）。
- 云侧 VAD：**负责"轮次结束"**，宁可稍迟钝（别把思考中的停顿当说完）。

### 4.2 打断链路时序（目标 250ms 内画面停）

| t | 事件 |
| --- | --- |
| 0ms | 用户开口 |
| 30–150ms | 端侧 VAD 连续判定，达阈值 |
| +20ms | 前端发 `POST /sessions/{id}/interrupt` |
| +50–120ms | `runner.interrupt()` 置位，OmniRT 侧未消费音频丢弃，帧下发停止 |
| 拿到预卷后 | 把打断瞬间音频补进 STT，开始新一轮 |

### 4.3 关键参数（可直接抄）

```bash
# ---- 云端语音 ----
OPENTALKING_STT_DEFAULT_PROVIDER=dashscope          # paraformer-realtime-v2，已有 key 可用
OPENTALKING_TTS_DEFAULT_PROVIDER=cosyvoice          # cosyvoice-v3-flash，首包 150ms 级
OPENTALKING_TTS_STREAMING_DECODE=1
OPENTALKING_TTS_SAMPLE_RATE=16000                   # 与 QuickTalk 输入对齐，免重采样
OPENTALKING_LLM_MODEL=qwen-flash
OPENTALKING_MEMORY_RECALL_TIMEOUT_MS=400            # 默认 2000ms，会吃光首 token 预算

# ---- QuickTalk ----
OPENTALKING_DEFAULT_MODEL=quicktalk
OPENTALKING_QUICKTALK_BACKEND=omnirt                # 或 local
OMNIRT_ENDPOINT=http://<gpu-lan-ip>:9000
OPENTALKING_QUICKTALK_WORKER_CACHE=1                # 多会话复用常驻 worker
OPENTALKING_QUICKTALK_FPS=25
OPENTALKING_QUICKTALK_SLICE_LEN=10                  # 28 帧≈1.12s/块太慢；10 帧=400ms
OPENTALKING_QUICKTALK_MAX_TEMPLATE_SECONDS=12       # 与素材时长匹配，控制冷启动
```

## 5. 第 4 步：联调检查清单（按顺序打勾）

| # | 检查点 | 命令 / 判定 |
| --- | --- | --- |
| 1 | 素材合规 | `ffprobe -v error -show_entries stream=width,height,r_frame_rate -show_entries format=duration raw/main_take.mp4` → 25fps、比例匹配、8–15s |
| 2 | 首尾帧闭环 | 抽首末帧目视对比，姿态差异越小越好（ping-pong 接缝） |
| 3 | 资产生成 | prepare-cache 输出 `status=hit/prepared`，文件存在 |
| 4 | 模型可用 | `curl -s http://127.0.0.1:8000/models \| jq '.statuses[] \| select(.id=="quicktalk")'` → `connected:true` |
| 5 | 首轮不卡 | 第二次建会话时 `v3_template_cache=asset_hit` 出现在日志 |
| 6 | 单句出声 | `POST /sessions/{id}/speak`，看首帧时间与口型是否同步 |
| 7 | 打断 | 数字人说 1s 时插话，录屏逐帧数画面停止时间 ≤250ms |
| 8 | 长句循环 | 让数字人说 30s，观察 ping-pong 动作是否违和（这是素材问题，不是模型问题） |

## 6. 延迟预算与实测方法

| 环节 | 目标 |
| --- | --- |
| 端侧 VAD 判停 | 300–500ms |
| 云端 STT 终稿 | 200–400ms |
| LLM 首 token | 250–500ms |
| 云端 TTS 首句首包 | 150–300ms |
| QuickTalk 首帧（4090，slice 10 帧） | 150–250ms |
| WebRTC 传输 | 30–80ms |
| **首帧合计** | **≈1.1–2.0s** |

实测：`scripts/benchmark_opentalking_e2e.py`（配置 `configs/benchmark/opentalking-e2e.yaml`），先跑 mock 基线，再逐项替换成云端 provider 做对照。

## 7. 常见坑

| 现象 | 根因 | 处理 |
| --- | --- | --- |
| 长句说着突然"动作倒回去" | ping-pong 循环 + 单向大动作素材 | 重拍闭环动作，或缩短单次播报长度 |
| 画面被横向/纵向拉扁 | 素材比例≠manifest 比例，`cv2.resize` 不补边 | 用 `scale`+`crop` 预裁到目标比例 |
| 唇音整体漂移 | 素材不是 25fps | 源就出 25fps |
| 换了视频但仍用旧缓存 | face cache 命中按帧数一致 | `--overwrite` 重跑 |
| 首轮卡 2–3s | face cache 未预生成 | 提前跑 prepare-cache，或开 `WORKER_CACHE=1` |
| 素材里本来在说话 | 素材口型与驱动口型叠加 | 重拍中性口型 |
| 打断后出现"半张嘴静帧" | 停帧未清 OmniRT 残留队列 | 打断时同时清队列（`queue_max_frames`） |
| 云端 TTS 与 QuickTalk 采样率不一致 | 重采样引入缓冲 | 统一 16000 |
| 数字人一直在动 | 没有 idle 帧机制 | 准备 idle 素材，静音时切 idle |
| 知识库检索拖慢首 token | `recall_timeout_ms=2000` | 压到 400ms + 兜底话术 |

## 8. 一周排期

| 天 | 任务 | 产出 |
| --- | --- | --- |
| D1 | 拍素材（3 个 take，各 10s，闭环动作）+ 合规授权 | 可用 raw 素材 |
| D2 | 资产转换 + prepare-cache + 单句出声验证 | 能出声的 QuickTalk 数字人 |
| D3 | 接云端 STT/TTS，打通完整一轮对话 | 端到端可对话 |
| D4 | 端侧 VAD + 打断链路 | 抢话体验 |
| D5 | 延迟打点 + 参数扫描（slice_len / context） | 基线报告 |
| D6–7 | 长稳测试（30 分钟连续）、并发测试、素材返修 | 可上线配置 |
