# OpenTalking 云端数字人 · 部署与排障 FAQ

> 版本：2026-11-17 ｜ 环境：`https://<内部域名>/opentalking/`（主机 `<部署主机>`）
> 记录今天从"部署完成"到"画面、口型、音画同步全部打通"过程中遇到的全部问题、判定依据与最终结论。

---

## 0. 一句话结论

今天所有"看不到画面 / 没有口型 / 声音先出来"的问题，**没有一个是代码写错**，而是三个独立原因叠加：

| # | 根因 | 判定依据 | 最终处置 |
|---|---|---|---|
| 1 | 云端口型模型链路未接通（PCM 突发发送 + 未挂载轨道） | 说话时无帧推进 | 实时节流推 PCM、补挂载兜底 |
| 2 | **你的终端收不到阿里 RTC 的媒体流**（信令通、媒体不通） | `remotes` 有推流端但 `hasStream=false / frames=0`，订阅卡在 `subscribing` | 换托管供应商 → **腾讯 TRTC**（已切换） |
| 3 | 声音走本地 TTS、画面走 RTC，两条链路延迟不同 | `/start` 早于首帧，且音轨与视频轨非同源 | **门控 `/start` 等首帧** + **改播 Vidu 自己的音轨** |

---

## 1. 部署与基础配置

### Q1.1 服务部署在哪？怎么访问？

- 主机：`<部署主机>`（EulerOS 2.0 SP9，8 vCPU / 15G，**无 GPU**）
- 应用目录：`/usr/local/opentalking/{app,web,logs,data,bin/ffmpeg}`
- 启动方式：systemd 服务 `opentalking`，`python -m apps.unified.main --host 127.0.0.1 --port 18100`（**单进程**，路由前缀由 nginx 反代）
- 对外地址：`https://<内部域名>/opentalking/`
- nginx：`/opentalking/api/` → `http://127.0.0.1:18100/`（含 WebSocket 升级、`proxy_buffering off`、`read_timeout 3600s`）；`/opentalking/` 为静态文件 + SPA 回退

### Q1.2 默认驱动模型是什么？怎么切？

`.env` 中：

```bash
OPENTALKING_DEFAULT_MODEL=vidu          # vidu=云端真人（TRTC）；mock=轻量模式（前端 2D 口型，零成本）
OPENTALKING_VIDU_RTC_PROVIDER=trtc      # 托管 RTC 供应商：artc(阿里) / trtc(腾讯)
```

前端「驱动模型」下拉可随时切换，不必重启。

### Q1.3 轻量模式和 vidu 有什么区别？

| | 轻量模式（mock） | vidu |
|---|---|---|
| 渲染位置 | 浏览器（分层嘴型贴图 + 音频能量驱动） | Vidu 云端渲染 |
| 是否需要 GPU | 否 | 否（云端） |
| 是否需要 RTC | 否 | 是（腾讯 TRTC） |
| 形象要求 | 仅 `client_renderer.type=light2d` 的形象有口型（MatesX01 / 视频形象 hd1 / DOGO 博士小狗）；照片形象是静图 | 任意形象，照片级真人 |
| 计费 | 免费 | Vidu 1 积分/秒 |

---

## 2. 画面/口型类问题

### Q2.1 点「开始对话」后只有静态图和声音，画面完全不动？

按以下顺序看，每一条都能在页面上直接验证：

1. 地址加 `?diag=1`，左上角出现画面健康角标：
   `w=1024x1024 rs=4 frames=401 fps=25.7 op=1 muted=1 stream=y stalled=n remote=1(cam=1)`
   - `frames` 持续增长 → 视频在跑，问题在形象/口型而不是通路
   - `frames` 一直是 0 → 媒体没到，见 Q3
2. 控制台执行 `__viduRtc.motionSample()`：返回 `framesAdvanced / lumaChanged`
3. 说话时 `frames` 增长但嘴不动 → 云端渲染没拿到音频（检查 PCM 是否在实时推送）

**原因（今天实际遇到的）**：早期是 PCM 一次性突发发完（48 块 / 14.4 秒在 50ms 内发完），Vidu 来不及按实时渲染；已改为**按实时节流**每 100ms 送 100ms 音频，日志形如 `Vidu PCM sent: ... took=0.300s audio=0.30s rms=2100 peak=9000`。

### Q2.2 是不是域名 / nginx 禁用了某些功能？

**不是。** 判定方法：RTC 的信令（WSS）和入会都成功，说明 HTTPS/域名/反代全程正常；媒体走的是 UDP，不经过 nginx，因此 nginx 无法"只禁视频不禁音频"。

### Q2.3 声音先出来、画面后出来，怎么解决？

**根因**：两条链路不同源、延迟不同 ——

- 声音：本地 TTS → 浏览器音频（快）
- 画面：PCM → Vidu 渲染 → TRTC 编码发布 → 浏览器（慢 0.5~1.5s，首次出画还要 5~7s）

**处置（已上线）**：

1. **改用 Vidu 自己的音轨**：`VITE_VIDU_AUDIO_SOURCE=vidu`（构建期）。人声由渲染端与画面一起编码发布，**帧级同步**；本地 TTS 音轨自动静音避免双声。
   兜底：8 秒内厂商音轨未到（自动播放策略拦截等）→ **自动回退**本地 TTS，不会变成哑巴。
2. **门控 `/start`**：会话不再"先出声"——`POST /sessions/{id}/start`（开 STT/agent/TTS）必须等云端数字人**出第一帧**，最多 12s，超时才放行并打日志。
3. **加快出画**：TRTC 解码轨道 150ms 快轮询（原来 1.5s）、页面选定 vidu 即预加载 1.2MB TRTC SDK chunk。

**实测**：出画 7.3s → **5.7s**；`/start` 从"早于首帧"变为"晚于首帧"（6.40s > 5.73s）。

---

## 3. 网络与 RTC 通路（今天最花时间的部分）

### Q3.1 为什么你们测试能看到口型，我在自己电脑上就是不行？

因为**测试是在另一台机器上做的**，而且这台机器是"正常对照组"：

| 位置 | 是否有浏览器 | 作用 |
|---|---|---|
| 自动化测试机 `<调试机>` | 有（headless Chrome + CDP） | 出截图、帧计数、嘴部开合数据 |
| 服务器 `<部署主机>` | **没有浏览器** | 只跑 FastAPI，从不渲染视频；提供日志/会话/配置证据 |
| 你自己的笔记本 | 有 | 只有遥测数据（关键：那几次 `frames=0`） |

两台都在公司网内、都能跑 STUN，但只有测试机收到媒体 → 说明是**终端侧差异**，不是代码问题。

### Q3.2 怎么 30 秒判断是不是我这台机器/网络的问题？

**内置自检页**（只读，不改变任何配置）：

```
https://<内部域名>/opentalking/api/selftest.html
```

它测三件事并把结果自动上报到服务端日志：STUN 能否拿到公网地址（UDP 出网）、浏览器是否支持 H.264、到本服务 API 的连通性。

对照参考（正常机器的结果）：

```
WebRTC H.264        = 支持 ✔
STUN stun.miwifi    = 可出网 UDP ✔ 公网 203.0.113.11
STUN stun.bilibili  = 可出网 UDP ✔ 公网 203.0.113.12
本服务 API          = 正常 ✔ 97ms
```

### Q3.3 上网行为管理拦了 `badjs.weixinbridge.com`，跟这个有关吗？

**无关。** 那是微信前端的错误上报域名（纯 HTTP 埋点），RTC 媒体不经过它。但它间接说明链路上有 **URL 过滤/应用控制**设备 —— 这类设备若同时开了应用识别，就可能对 UDP 媒体做限制，症状正好是"信令通、媒体不通"。

### Q3.4 需要给网络同事开什么？在服务器上开吗？

**不是服务器**。服务器只需要 HTTPS 出网到 `api.vidu.cn`（一直是通的），媒体不经服务器。

要放通的是**你终端所在网段的出口**：

```
目标：*.rtn.aliyuncs.com（gw / prs / rs.rtn.aliyuncs.com，解析到 223.109.x.x 等阿里 RTC 边缘）
放通：TCP 443（信令）
      UDP 全端口（媒体端口是动态高位，实测出现过 1106 / 5000 / 10010 / 10018 / 10019 / 10111）
并关闭：对该目标的应用识别拦截、限速、审计
```

⚠️ **按端口放通无效**：实测媒体连的是 `223.109.36.90:1106`，每次可能不同，必须**按目标 IP 放通 UDP**。

### Q3.5 换成腾讯 TRTC 就通了 —— 怎么做的？踩了什么坑？

Vidu 的 `managed_rtc.provider` 是**封闭枚举**：`artc`（阿里，默认）/ `trtc`（腾讯）/ `agora`（声网）/ `volcengine`（火山）。切换只需改一行配置：

```bash
OPENTALKING_VIDU_RTC_PROVIDER=trtc
```

后端本来就支持，工作量全在前端适配（`apps/web/src/lib/viduTrtc.ts`，按需 `import("trtc-sdk-v5")`；`startViduPlayback` 检测到 `provider==="trtc"` 自动接管，对外仍暴露 `window.__viduRtc`，所以诊断角标/看门狗/遥测一行都不用改）。

两个必踩的坑：

1. **房间号类型**
   `'EnterRoomConfig' must be an integer but go a string, use 'strRoomId' instead`
   Vidu 给的是字符串房间 `live-user-1020...`，必须走 `strRoomId`，不能塞 `roomId`。
2. **渲染目标**
   `startRemoteVideo({view})` 是**往 view 里塞它自己的 `<video>`**。直接传我们的 `<video>` 当容器，元素永远 `w=0`、心跳全是 0。
   正确做法：先用隐藏 holder 让 SDK 收流，再 `getVideoTrack()` 把轨道绑到我们自己的元素上（全链路单一渲染目标）。

**凭证映射**：`app_id`→SDKAppID，`channel_id`→strRoomId，`user_id`→userId，`token`→userSig。

### Q3.6 能在云主机上自建 RTC 顶替阿里云吗？

**不能。** provider 枚举封闭 + 各家私有协议，Vidu 没有"推到我自己的 RTC 服务"这个选项。

能做的替代是**中继**（浏览器从此只连我们域名，不碰第三方 RTC）：

```
Vidu → 阿里 RTC → [云主机当观众收流] → ffmpeg → HTTPS 分发（WS-fMP4 / LL-HLS / MJPEG）→ 浏览器
```

可行性已核实：`ffmpeg 7.0.2 + libx264` ✔、8 vCPU/9G 可用 ✔、服务器 UDP 出网 ✔（缺 Chromium，可装）。
代价：延迟 +1~3s；中继是单向的，**"打断"要重做**。

### Q3.7 换供应商/中继，能不能省钱？

不能。Vidu 仍按 **1 积分/秒** 计费（余额 < 45 会报 `CreditInsufficient`）。换供应商只换媒体出口路径。

---

## 4. 计费与并发

### Q4.1 计费与并发规则？

- 一条 OpenTalking 会话 = 一条 Vidu live，**1 积分/秒**，`on_live` 状态开始计费
- 账号只允许 **1 路** 并发（`OPENTALKING_VIDU_MAX_LIVES=1`），第二路返回 429 / "云端数字人正忙"
- 结束会话必须真正释放：日志出现 `Vidu live released: ... active_lives=0`
- 幂等/防漏：`OPENTALKING_VIDU_END_WAIT_SEC`、`LIVE_RELEASE_SEC`、`BUSY_RETRY_SEC`

### Q4.2 怎么确认没有残留的计费会话？

```bash
# 服务端
grep -a 'Vidu live released' /usr/local/opentalking/logs/service.log | tail -3
# 本仓库工具
python .deploy/vidu_active_check.py     # 输出 on_live count: 0 才算干净
```

---

## 5. 排查工具速查（今天新增的）

| 工具 | 用法 | 用途 |
|---|---|---|
| 画面健康角标 | 页面 URL 加 `?diag=1` | 实时看 `w/h/frames/fps/op/stalled/remote` |
| 画面探针 | 控制台 `__viduRtc.state()` / `.motionSample()` / `.remoteUsers()` / `.stats()` | 判断有没有帧、有没有动、厂商计数器 |
| 卡死看门狗 | 自动（2.5s 无新帧） | 页面顶部黄条提示"云端画面已停止" |
| 客户端遥测 | 自动上报 → `POST /opentalking/api/client-diag` → `logs/service.log` 搜 `client diag` | 拿到**用户真实浏览器**的帧数/轨道/挂载原因，不用截图 |
| 自检页 | `/opentalking/api/selftest.html` | STUN / H.264 / API 连通性一次性体检 |
| 模型连通性 | `curl -s localhost:18100/models` | 每个驱动模型 `connected/reason` |

日志里区分"自动化测试"和"真实用户"：看 UA — `HeadlessChrome/…` 是自动化，`Chrome/…` 是真人操作。客户端 IP 全部显示为出口代理地址，不能用来区分。

---

## 6. 今天的修复清单（可直接对照代码）

| 文件 | 改动 |
|---|---|
| `opentalking/providers/synthesis/vidu/client.py` | PCM **实时节流**（`OPENTALKING_VIDU_PCM_LEAD_SEC`，默认 0.2s）；`send_transcription()` 发 type=9/10 文本帧；`close()` 按终态等待再释放并发槽 |
| `opentalking/pipeline/speak/synthesis_runner.py` | 会话切换时重建 vidu 会话；说话前回传用户/回复文本 |
| `apps/web/src/lib/viduRtc.ts` | 阿里面向：轨道选择（camera/screen）、`ensureAttached()` 兜底轮询、快轮询、`muted` 属性、卡死看门狗、`__viduRtc` 诊断面 |
| `apps/web/src/lib/viduTrtc.ts` | **新增**：腾讯 TRTC 适配器（strRoomId、隐藏 holder + `getVideoTrack` 绑定、厂商音轨、手势解除静音） |
| `apps/web/src/App.tsx` | `/start` 门控等首帧；厂商音轨 8s 兜底回退；遥测覆盖全模式；预加载 RTC SDK；卡死黄条 |
| `apps/api/routes/client_diag.py`、`selftest.py` | 客户端遥测接收端；浏览器自检页（挂在 API 前缀下，绕开静态目录的 SPA 回退 403） |
| `.deploy/*.py`、`.deploy/probe_*.js` | 部署/校验/自动化验收脚本（帧计数、嘴部开合、音画时序、媒体端点抓取） |

---

## 7. 方案矩阵（以后遇到同类问题直接查表）

| 现象 | 最可能原因 | 首选动作 |
|---|---|---|
| 有声音、无画面，`frames=0` 且 `remotes` 有推流端 | 终端到 RTC 边缘的 UDP 媒体被拦 | 换供应商（`trtc`）→ 仍不行则中继或找网络放通 |
| 有画面但嘴不动 | 云端没收到/没实时收到 PCM | 查 `Vidu PCM sent` 的 `took/audio` 是否 1:1 |
| 画面冻结成静图 | live 已结束 | 看门狗黄条 → 结束会话重新开始 |
| 声音先于画面 | 音频/视频不同源 | 用厂商音轨 + `/start` 门控 |
| 第二路会话 429 | 并发 1 路 | 先结束旧会话，确认 `on_live=0` |
| 字幕有、语音无 | TTS 通道异常 | 查 `tts_worker_wall_ms`、`OPENTALKING_TTS_VOICE` |
| 轻量模式下是静图 | 形象没有 light2d 分层素材 | 换 MatesX01 / 视频形象 hd1 / DOGO 博士小狗 |
| 服务 502 且 restart 失败 | 新模块未推送到服务器 | `py_compile` 校验 + 补齐文件后重启 |
