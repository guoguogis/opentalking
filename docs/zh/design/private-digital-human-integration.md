# 私有化数字人服务对接（OmniRT / audio2video 网关）

对接目标：`https://218.28.9.108:50787`（HTTP 与 WebSocket 同端口，TLS 由网关终止）。

## 1. 配置（已写入 `.env`）

```env
OMNIRT_ENDPOINT=https://218.28.9.108:50787
OPENTALKING_OMNIRT_ENDPOINT=https://218.28.9.108:50787   # 实际生效的是这条（带前缀）
OPENTALKING_OMNIRT_API_KEY=sk-litellm-master-key-123
OPENTALKING_MUSETALK_BACKEND=omnirt
OPENTALKING_QUICKTALK_BACKEND=omnirt
OPENTALKING_FLASHHEAD_WS_URL=wss://218.28.9.108:50787/ws/flashhead
OPENTALKING_TTS_LOCAL_COSYVOICE_SERVICE_URL=https://218.28.9.108:50787/tts/synthesize
OPENTALKING_TTS_LOCAL_COSYVOICE_API_KEY=sk-litellm-master-key-123
OPENTALKING_AUDIO2VIDEO_STRIP_LOCAL_PATHS=1
NO_PROXY=218.28.9.108
```

`.env` 里同时存在 `OMNIRT_ENDPOINT` 与 `OPENTALKING_OMNIRT_ENDPOINT` 两个键，**后者优先**；
只改前者不生效（已踩过）。

## 2. 代码改动

| 文件 | 改动 | 原因 |
|---|---|---|
| `providers/tts/local_cosyvoice/adapter.py` | 新增 `_service_auth_headers()`，POST 时带 `Authorization: Bearer` | 网关 `/tts/synthesize` 需要鉴权，原适配器不发 header（文档第 8 节已提示） |
| `providers/synthesis/flashtalk/ws_client.py` | 新增 `_remote_gateway_target()`；开启 `OPENTALKING_AUDIO2VIDEO_STRIP_LOCAL_PATHS=1` 时不再发送 `template_video` / `ref_frame_dir` / `prepared_cache_dir` / `quicktalk_face_cache` 等本地绝对路径 | 远端网关读不到本机路径，会报 `template_video is outside allowed frame roots` |
| `core/config.py` | 新增 `musetalk_backend` 设置项 | `resolve_model_backend()` 按 `{model}_backend` 取后端，缺字段时 musetalk 无法切到 omnirt |
| `providers/tts/providers.py` | 修复 `SUPPORTED_TTS_PROVIDERS`（误写成 tuple，导致 `openai_compatible` 被判为不支持、`/health` 500） | 回归修复 |

## 3. 验证结果

后端 `/models`（网关实测）：

```
flashtalk: backend=omnirt connected=True reason=omnirt
musetalk:  backend=omnirt connected=True reason=omnirt
quicktalk: backend=omnirt connected=True reason=omnirt
flashhead: backend=direct_ws connected=True reason=direct_ws
```

协议级验证（`scripts/probe_audio2video.py`，直连网关）：

| 模型 | init | chunk_samples | 每块耗时 | 每块音频 | RTF |
|---|---|---|---|---|---|
| QuickTalk | 3.9 s（冷启 11.8 s） | 2560（160 ms） | 634–949 ms | 160 ms | **≈4.0–5.9× 慢于实时** |
| MuseTalk | 2.3 s | 16000（1 s） | 3975–5572 ms | 1000 ms | **≈4.0–5.6× 慢于实时** |
| FlashHead | 3.3 s | 15360（0.96 s） | 2102–3923 ms | 960 ms | **≈2.2–4.1× 慢于实时** |

帧格式校验通过：`VIDX` + 帧数 + JPEG 列表，解码正常
（QuickTalk 512×512 → 实际返回 416×704；首块 `lookahead_chunks=1` 返回 0 帧，符合文档）。

## 4. 结论与限制

- **接入是通的**：鉴权、模型清单、WS init、音频推送、帧回收全部可用。
- **离线视频生成已验证通过**：
  `POST /video-creation/jobs`（model=quicktalk, avatar=anchor, audio_source=tts_text）
  产出 `data/exports/videos/2026-11-17/<export_id>/recording.mp4` —
  ffprobe 实测 **512×512 / 3.36 s / 84 帧 / 含音轨**，全流程走私有化网关。
- **当前吞吐约 0.2–0.45× 实时**（RTF 2.2–5.9），远低于文档标注的 140–210 ms/块。
  同样的请求在文档环境是 1.2× 实时，说明该实例 GPU 侧负载高或配置较低。
- 因此：
  - **实时对话**：功能可用但性能不足（音频产出速度远超渲染速度），
    不建议作为实时链路；实测 `speak` 会长时间落后。
  - **离线成片 / 视频生成**：可用，且这是当前最合适的用法。
- 前端模型下拉无需改代码：`App` 拉取 `/models` 后 `SettingsPanel` 全量渲染，
  quicktalk / musetalk / flashhead 会自动出现并带连通角标。

## 5. 自检命令

```powershell
# 网关探活
.\.venv\Scripts\python.exe -c "import httpx;print(httpx.get('https://218.28.9.108:50787/gw-health',trust_env=False).text)"
# 协议与吞吐
$env:PROBE_MODEL="quicktalk"; .\.venv\Scripts\python.exe scripts\probe_audio2video.py
```

## 6. 后续可选方向

1. 提升网关侧吞吐（换卡 / 降分辨率 / 关闭 `wav2lip_postprocess_mode=easy_improved`）后再评估实时。
2. 用 `flashhead`（文档标注 0.63–0.71 s/0.96 s 音频，≈1.5× 实时）实测对比。
3. 实时对话继续走 Vidu；私有化网关专用于离线视频生成。
