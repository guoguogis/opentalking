# DUIX 离线视频生成接入

DUIX 是一套**离线任务队列**：提交（submit）→ 轮询（query）→ 下载成品，和实时驱动模型
（WS 推流、边说话边出画面）完全不同。本文记录接入方式、配置项，以及当前的**网络阻塞点**。

## 1. 与实时模型的区别

| | 实时驱动模型（flashtalk / musetalk / quicktalk / flashhead / vidu） | DUIX |
| --- | --- | --- |
| 交互 | 音频一块块推给模型，画面实时回来 | 一次性提交整段音频 + 驱动视频，稍后取成品 |
| 入口 | 「实时对话」工作台 | 「视频创作」工作台（`model=duix`） |
| 结果 | 浏览器里的视频流 | 下载的 MP4，进「资产库」 |
| 并发 | 各模型自己的槽位 | **单任务串行**，忙时 `10001` |
| 输出尺寸 | 客户端可指定 | 由驱动视频决定（实测 1184×896） |

因此 DUIX **不在** `/models` 的实时模型列表（`models` / `statuses`）里，而是单独通过
`offline_models` / `offline_statuses` 暴露，前端只有「视频创作」的模型选择器能看到它。

## 2. 接口

```bash
# 提交
curl -sk -X POST https://218.28.9.108:50787/duix/easy/submit \
  -H 'Authorization: Bearer sk-litellm-master-key-123' \
  -H 'Content-Type: application/json' \
  -d '{"code":"task-001","audio_url":"http://.../s.wav","video_url":"http://.../d.mp4"}'

# 查询（status: 1 运行中 / 2 成功 / 3 失败；progress 0-100）
curl -sk -H 'Authorization: Bearer ...' 'https://218.28.9.108:50787/duix/easy/query?code=task-001'

# 下载
curl -sk -H 'Authorization: Bearer ...' -o r.mp4 https://218.28.9.108:50787/duix-files/task-001-r.mp4
```

- 响应外层统一 `{"code": 10000|10001|10003..., "data": {...}}`：`10000` 成功、**`10001` 忙碌**、
  `10004` 任务不存在。
- `audio_url` / `video_url` 由**渲染器自己下载**，不是我们推过去的。
- 任务成功后**立即从内存删除**，`query` 只能拿到一次结果，必须马上下载。

### 端口说明

- **50787**：带 SK 的统一网关，`/duix/*` 与 `/duix-files/*` 都在这里，我们用这个。
- 50637：同一套服务的另一个前置，`/duix/*` 前面挂了 nginx Basic Auth，只带 Bearer 会 401。
- `http://7.6.57.96:8383`：内网直连，仅厂商侧可达。

## 3. 代码结构

| 文件 | 作用 |
| --- | --- |
| `opentalking/providers/offline/duix.py` | `DuixClient`：submit / query / wait / download、`10001` 退避重试、结果 URL 推导 |
| `opentalking/providers/offline/duix_assets.py` | `DuixAssetPublisher`：把音频和驱动视频发布成渲染器可下载的 URL（随机 token 目录 + TTL 清理） |
| `opentalking/video_creation_duix.py` | `DuixVideoCreationService`：定位驱动视频 → 转 16k mono wav → 提交 → 轮询（回报进度）→ 下载 → 登记为 export |
| `apps/api/routes/duix.py` | `GET /duix/assets/{token}/{name}`（匿名下载，token 即能力）、`GET /duix/status` |
| `apps/api/routes/video_creation.py` | `model=duix` 分支：**支持 `audio_source=upload` 与 `tts_text`**，sync / async 两种执行 |
| `apps/api/routes/models.py` | `/models` 增加 `offline_models` / `offline_statuses` |

驱动视频取形象自带的 `avatars/<id>/source/*.mp4`（没有就 400 并说明），所以**只有带原始视频的
形象**能用 DUIX；`audio_source=voice_clone / duo_dialog / reference_video` 暂不支持。

进度通过 job 的 `progress` / `progress_stage` 上报：`publishing_inputs → submitting →
rendering（远端 0-100%）→ downloading → exporting → done`。

## 4. 配置

```ini
# 网关地址；留空则用 OMNIRT_ENDPOINT + /duix
OPENTALKING_DUIX_BASE_URL=
OPENTALKING_DUIX_API_KEY=          # 留空回落到 OMNIRT_API_KEY
# 渲染器下载素材用的前缀（必须它网络内可达）
OPENTALKING_DUIX_ASSET_BASE_URL=https://ailab-gamma.xfusion.com/opentalking/api
OPENTALKING_DUIX_ASSET_DIR=        # 默认 <data>/duix_assets
OPENTALKING_DUIX_ASSET_TTL_SEC=3600
OPENTALKING_DUIX_TASK_TIMEOUT_SEC=1800
OPENTALKING_DUIX_POLL_INTERVAL_SEC=2
OPENTALKING_DUIX_BUSY_RETRY_SEC=15
```

## 5. 验证

- `/duix/status`：`{"connected": true, "base_url": "https://218.28.9.108:50787/duix"}`（已通）。
- `/models`：`offline_statuses: [{"id":"duix","connected":true,"reason":"configured"}]`（已通）。
- 素材路由：`GET /duix/assets/<token>/speech.wav` → 200；目录穿越和未知 token → 404（已通）。
- 整条链路（用本地协议替身，见 `.deploy/test_duix_stub.py`）：提交→忙碌重试→进度
  0.02→1.00→下载→export 登记全部通过，`model=duix`、`duration=2.0`。
- **真实渲染未跑通**，原因见下。

## 6. 阻塞点：渲染器拉不到我们的素材

实测结论（每一项都验证过）：

| 检查 | 结果 |
| --- | --- |
| `https://ailab-gamma.xfusion.com/opentalking/...` 作为 audio_url | `status=3, msg="audio download failed"` |
| `http://10.25.31.93/opentalking/...`（内网 IP，80 端口） | 6 分钟后同样 download failed，**nginx access.log 里没有任何请求** |
| 公网 `https://www.w3schools.com/...` | 同样 download failed → 渲染器没有公网出口 |
| `http://218.28.9.108:50787/gw-health`（它自己的网关） | 能下载（不再报 download failed） |

即：DUIX 渲染器**只能访问自己所在的厂商网络**（至少能到 218.28.9.108:50787），
到 10.25.31.93 没有路由。网关侧也没有任何上传接口（`/duix-files/` 只读：GET 可用、
PUT 405、`/duix/upload` 404、`/upload` 404、`/v1/files` 404）。

要打通只差一个「素材交接通道」，三选一：

1. **厂商提供上传接口/对象存储**（推荐）：我们 `POST` 音频和视频过去，拿回可下载 URL 再
   `submit`。改动只在 `DuixAssetPublisher.publish()` 一处。
2. **放通网络**：让渲染器能访问 `ailab-gamma.xfusion.com`（或 10.25.31.93:443）。
   代码侧一行环境变量即可（当前 `OPENTALKING_DUIX_ASSET_BASE_URL` 已指向公网域名）。
3. **共享存储**：给一个双方都能读写的目录/HTTP 静态路径，我们把素材写进去、把 URL 传给它。

另外若有 multipart 直传接口（字段名告知即可），`submit` 改成表单上传也能 15 行内搞定。
