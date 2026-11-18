# 驱动模型对接公网模型服务网关

记录 Gamma 环境（`https://ailab-gamma.xfusion.com/opentalking/`）把 5 个视频驱动模型
切到公网模型服务网关（`https://218.28.9.108:50787`，见 `model-service-api.md`）的配置、
代码改动与验收结果。

## 1. 目标架构

```
浏览器 ──WebRTC(aiortc)── OpenTalking(Gamma, CPU)
                              │  WSS + Bearer（经企业代理 CONNECT）
                              ▼
                    模型服务网关 218.28.9.108:50787
                    ├── /v1/audio2video/musetalk        (WS)
                    ├── /v1/audio2video/flashtalk       (WS)
                    ├── /v1/audio2video/quicktalk       (WS)
                    ├── /v1/audio2video/fasterliveportrait (WS)
                    ├── /ws/flashhead                   (WS，消息格式独立)
                    └── /tts/synthesize                 (HTTP，裸 PCM 24k/16bit/mono)
```

本机只做编排、TTS/STT/LLM、以及把远端回传的画面推给浏览器；推理全部在网关侧 GPU 上跑。

## 2. 环境变量（`/usr/local/opentalking/app/.env`）

```ini
# 统一网关入口（HTTP 与 WS 同端口，TLS 由网关自身终止）
OMNIRT_ENDPOINT=https://218.28.9.108:50787
OPENTALKING_OMNIRT_API_KEY=sk-litellm-master-key-123

# FlashHead 消息格式与 OmniRT 不同，单独给 WSS 地址
OPENTALKING_FLASHHEAD_WS_URL=wss://218.28.9.108:50787/ws/flashhead

# 视频类驱动模型后端
OPENTALKING_MUSETALK_BACKEND=omnirt
OPENTALKING_QUICKTALK_BACKEND=omnirt
OPENTALKING_FLASHTALK_BACKEND=omnirt
OPENTALKING_FASTERLIVEPORTRAIT_BACKEND=omnirt

# 远端读不到本机绝对路径，init 时不要发送 template_video / ref_frame_dir 等
OPENTALKING_AUDIO2VIDEO_STRIP_LOCAL_PATHS=1

# 可选：网关自带 CosyVoice3 TTS（切换 provider 时直接可用）
OPENTALKING_TTS_LOCAL_COSYVOICE_SERVICE_URL=https://218.28.9.108:50787/tts/synthesize
OPENTALKING_TTS_LOCAL_COSYVOICE_API_KEY=sk-litellm-master-key-123
```

模型 → 后端映射来自 `opentalking/core/model_config.py` 的 `_DEFAULT_MODEL_BACKENDS`，
其中 flashhead 固定走 `direct_ws`，其余四个默认已是 `omnirt`。

### 网络前提

Gamma 主机没有直连外网，所有出网必须走企业代理，systemd 单元里已注入：

```ini
Environment=HTTPS_PROXY=http://proxy-sys.xfusion.com:8080
Environment=HTTP_PROXY=http://proxy-sys.xfusion.com:8080
Environment=NO_PROXY=localhost,127.0.0.1,::1,10.25.31.93,.xfusion.com
```

`websockets>=15` 会自动读取 `HTTPS_PROXY` 走 CONNECT 隧道，所以 WS 客户端不需要额外改代码。
手工排障时 curl 必须显式带 `-x http://proxy-sys.xfusion.com:8080`，否则 `HTTP=000`。

## 3. 为对接做的两处代码修复

### 3.1 远端后端不再做本地资产预热（`apps/api/routes/avatars.py`）

UI 在 QuickTalk / Wav2Lip 启动前会 `POST /avatars/{id}/prewarm`，并且 **预热失败就拒绝开会话**。
原实现即使模型跑在远端（`omnirt` / `direct_ws` / `vidu`）也会先在本地构建 QuickTalk 缓存，
于是无 GPU、无 torch 的主机直接 500：

```
QuickTalk 准备失败：failed to prepare quicktalk cache: No module named 'torch'
```

现在远端后端直接返回 `status=ready`、`cache.status=remote`（`runtime_status=skipped`），
远端运行时用 `ref_image` 自己建缓存。如需恢复旧的本地预热行为，设置
`OPENTALKING_PREWARM_REMOTE_CACHE=1`。

### 3.2 FlashHead 忽略无关的 init 参数（`opentalking/providers/synthesis/flashhead/ws_client.py`）

合成 runner 对所有客户端统一传 wav2lip/quicktalk 的调参关键字，FlashHead 客户端签名里没有，
选 FlashHead 时 `POST /sessions` 直接 503：

```
TypeError: FlashHeadWSClient.init_session() got an unexpected keyword argument 'wav2lip_postprocess_mode'
```

现在 `init_session(..., **extra)` 收下并忽略这些参数（debug 日志列出被忽略的键）。

## 4. 验收结果（2026-11-18，Gamma 环境）

`GET /models`：

| 模型 | backend | connected |
| --- | --- | --- |
| flashtalk | omnirt | ✅ |
| musetalk | omnirt | ✅ |
| fasterliveportrait | omnirt | ✅ |
| flashhead | direct_ws | ✅ |
| quicktalk | omnirt | ✅ |
| vidu | vidu | ✅（默认模型，未改动） |
| wav2lip | local | ❌ 网关侧 `runtime_not_enabled`，本机也无 adapter |

端到端实测（真实浏览器，点选模型 → 开始对话 → 说话 → 统计解码帧数）：

| 模型 | 输出分辨率 | 首帧 | 说话后 2s 内新增帧 | 结论 |
| --- | --- | --- | --- | --- |
| musetalk | 512×512 | 7.2s | +49 | 通过 |
| fasterliveportrait | 448×448 | 4.8s | +50 | 通过 |
| flashhead | 512×512 | 7.5s | +48 | 通过 |
| flashtalk | 448×768 | 8.6s | +50 | 通过 |
| quicktalk | 512×512 | 12.9s（首次含网关冷启动） | +50 | 通过 |

首帧时间包含网关侧模型冷启动；同一模型第二次连接明显更快。

## 5. FlashHead 按会话指定分辨率

FlashHead 的 VAE 步长是 `vae_stride=[8, 32, 32]`，只要宽高是 **32 的倍数**就能原生生成，
不再固定 512×512。`session.create` 的 `config` 新增三个可选字段：

```json
{"type":"session.create",
 "inputs":{"image_b64":"..."},
 "config":{"seed":9999,"width":640,"height":640,"fit":"native"}}
```

- `fit="native"`：按目标分辨率直接生成，宽高必须是 32 的倍数
- `fit="resize"`：先按 512×512 生成再缩放到目标尺寸，任意宽高可用，但不会增加细节
- 范围 64–2048；`session.created.video` 会回显 `width/height`、`render_width/render_height`、`fit`

### 应用侧策略（`opentalking/providers/synthesis/flashhead/ws_client.py`）

分辨率取值优先级：显式参数 → `OPENTALKING_FLASHHEAD_WIDTH/HEIGHT/FIT` → 形象自身分辨率
（`manifest.json` 的 `width/height`，由 `synthesis_runner._flashhead_render_config()` 传入）
→ settings 默认 → 512×512。

`fit` 默认 `auto`：**两边都是 32 的倍数就用 native，否则用 resize**；若显式要求 `native`
但尺寸不合规，会打 warning 并回落到 resize（避免会话直接失败）。只有在需要 `resize` 或显式指定
`fit` 时才会把 `fit` 字段发给服务端，保证与旧版 FlashHead 兼容。

```ini
# 想让所有 FlashHead 会话都用固定尺寸时（覆盖形象自身分辨率）
OPENTALKING_FLASHHEAD_WIDTH=640
OPENTALKING_FLASHHEAD_HEIGHT=640
OPENTALKING_FLASHHEAD_FIT=native
```

### 实测（网关 + 应用内）

客户端级（`/tmp/test_flashhead_size.py`，真实解码首帧尺寸为准）：

| 请求 | fit 解析 | 回显 render | 首帧解码尺寸 | 结果 |
| --- | --- | --- | --- | --- |
| 512×512（默认） | native | 512×512 | 512×512 | 通过 |
| 704×1248（形象原生） | native | 704×1248 | 704×1248 | 通过 |
| 720×1280（720 非 32 倍数） | resize | 512×512 | 720×1280 | 通过 |
| 显式 640×640 | native | 640×640 | 640×640 | 通过 |
| 720×1280 + 强制 native | 回落 resize（有 warning） | 512×512 | 720×1280 | 通过 |

应用内端到端（浏览器里选形象 → 选 FlashHead → 说话）：

| 形象 | 形象分辨率 | 日志 | 舞台分辨率 | 首帧 | 说话后 2s 新增帧 |
| --- | --- | --- | --- | --- | --- |
| MatesX01(动态) | 704×1248 | `fit=native render 704x1248` | 704×1248 | 5.9s | +50 |
| 古风小生 | 720×1280 | `fit=resize render 512x512` | 720×1280 | 5.7s | +49 |

注意 720 不是 32 的倍数，`1280×720` / `720×1280` 只能走 resize；要 native 请用
1280×704 / 1280×736 这类尺寸。2048×2048 以上或低于 64 的边会被钳制。

## 6. 日常运维

```bash
# 改了 .env 之后
systemctl restart opentalking && sleep 8 && curl -s http://127.0.0.1:18100/models

# 只看网关相关日志
grep -aiE 'omnirt|flashhead|quicktalk|musetalk|flashtalk' /usr/local/opentalking/logs/service.log | tail -30
```

排障顺序：`/models` 里 `connected` 是否为 true → 服务日志有没有 WS 连接/鉴权错误 →
代理是否放行 `218.28.9.108:50787` 的 CONNECT。

## 6. 已知限制

- **并发**：MuseTalk / QuickTalk 单路独占 GPU；FlashTalk / FlashHead 单槽位串行，第二路排队；
  fasterliveportrait 的音频驱动与视频克隆共用运行时。
- **单会话时长**：网关侧单个 WS 会话上限 1800s，超时强制断开。
- **wav2lip**：网关侧运行时未启用，本方案不覆盖。
- **TTS**：当前仍用百炼 `qwen-audio-3.0-tts-plus`（`openai_compatible`）。网关 CosyVoice3
  可作为替代（已实测：3.6s 正常音频、`x-audio-sample-rate: 24000`），但它的音色名是
  `indextts-xiaoxiao-cn` 这类，不在本地 CosyVoice 音色表里，切过去要先解决音色校验。
- **认证**：网关只认 `Authorization: Bearer sk-litellm-master-key-123`，TLS 证书签发给 IP，
  不能加 `-k` 之外的绕过假设；浏览器侧不直连网关，密钥不下发到前端。
- **FlashHead 大尺寸代价**：1280×704 native 单块 1.32s（24 帧/块），已低于实时，只适合离线；
  实时对话建议 ≤768×1280 且优先 32 的倍数走 native，非 32 倍数的竖屏走 resize（512 放大）。
- **FlashHead 一会话两次建连**：应用会为预缓冲（首个语音块提前渲染）再起一个 WS 会话，
  在单槽位串行的网关上是排队关系，若并发紧张可观察日志确认排队是否成为瓶颈。
