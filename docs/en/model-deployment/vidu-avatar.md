# Vidu S2-Avatar source (cloud rendering)

How to run OpenTalking with the **Vidu S2-Avatar component product**. You send
PCM, Vidu renders the avatar and pushes it into an RTC channel; ASR / LLM / TTS
stay in OpenTalking.

> Vidu ships two products. OpenTalking only wires the **component** one. The
> realtime product also takes over dialogue and voice, which would take rubric
> scoring, knowledge and persona memory away from this system.

## How it works

```text
browser mic -> OpenTalking ASR -> LLM -> TTS (16 kHz PCM)
                                          |
                                          v
                       Vidu control WebSocket (raw PCM, 24 kHz)
                                          |
                                          v
                    Vidu cloud render -> RTC channel -> browser <video>
```

- The local pipeline produces **no frames**: `generate()` returns an empty list.
- The local WebRTC (aiortc) connection stays because it carries the **TTS
  audio** to the browser; the picture comes from the vendor RTC channel.
- No OmniRT and no local GPU model are involved.

## Configuration

```bash
VIDU_API_KEY=your_vidu_api_key                     # required
VIDU_BASE_URL=https://api.vidu.cn                  # https://api.vidu.com for the global site
VIDU_AVATAR_IMAGE_URI=                             # optional; falls back to bundle reference.png|.jpg
VIDU_RTC_PROVIDER=artc                             # artc | trtc | agora | volcengine
OPENTALKING_VIDU_MANAGED_RTC=true                  # platform-hosted RTC channel
OPENTALKING_VIDU_CHUNK_SAMPLES=4800                # 16 kHz samples per chunk (300 ms)
OPENTALKING_VIDU_IDLE_TIMEOUT_SECONDS=7200
```

`configs/default.yaml` already registers `models.vidu.backend: vidu`; override
with `OPENTALKING_VIDU_BACKEND=vidu` if needed.

## Verify

```bash
curl http://127.0.0.1:8000/models                  # vidu: connected, reason=vidu_configured
curl http://127.0.0.1:8000/sessions/<sid>/vidu     # live_id + rtc credentials (409 until the worker hands off)
```

The Aliyun RTC Web SDK is bundled from the npm dependency `aliyun-rtc-sdk` (7.3.x) and lazy-loaded in its own ~1.2 MB chunk only for `vidu` sessions.

For other providers (trtc / agora / volcengine) pass `adapter` to `startViduPlayback`.

## Behaviour

| Case | Behaviour |
| --- | --- |
| Session create | `POST /live/s_avatar/component` with `managed_rtc` |
| Handshake | `conn_init` first frame; `NOT_READY` retried with backoff, no reconnect |
| Audio | 16 kHz resampled to 24 kHz, sent as 20 ms (960 byte) binary frames |
| Barge-in | `interrupt()` sends `audio_interrupted` (type=7); the live session is **not** rebuilt |
| Hangup | `close()` sends `call_hangup` (type=5) |
| Idle | rendered by Vidu; local idle frame generation is skipped |

### Audio source (matters for lip sync)

Vidu's RTC stream carries video only; remote audio is played by Aliyun's **internal** player, not through the `<video>` `srcObject`. That copy is the track the mouth was rendered against, so it is naturally in sync. Vidu sessions therefore use the vendor audio as the single source and mute the local TTS sink (`<audio ref={audioRef}>`).

If the vendor audio cannot play in a given environment (silence), fall back to the local track:

```bash
# apps/web/.env.local
VITE_VIDU_AUDIO_SOURCE=local
```

The tradeoff is that the mouth lags the audio by the cloud render latency, so use it only as a fallback.

## Verified against the live API (2026-11-15)

Two field facts worth remembering:

1. **Managed-RTC credentials come back under `managed_rtc`, not `rtc`**:

```json
{
  "live": {"id": "1019484231039983616", "status": "waiting"},
  "client_secret": "v1.eyJ…",
  "managed_rtc": {
    "app_id": "4f495dcd-…",
    "channel_id": "live-user-1019484231039983616",
    "user_id": "live-user-1003595675528605696-1019484231039983616",
    "token": "eyJhcHBpZCI6…",
    "token_expire_at": "1794822496",
    "provider": "artc"
  }
}
```

The client now accepts `rtc`, `managed_rtc` and `customer_rtc` shapes.

2. **Billing is reported as `billed_seconds`** via `GET /live/v1/lives/{live_id}`; an idle probe session billed `0`, a normal exchange billed 42 s.

Verified flow: create (3.7 s) → `conn_init_ack.success=true` → PCM push → `audio_interrupted` → `call_hangup` → `status=ended`, `close_reason=user_end`. Reproduce with `python scripts/probe_vidu_api.py`.

Browser playback was verified end to end in Chromium/Edge: `videoWidth=1024`, `readyState=4`, `paused=false`, and two consecutive sessions on the same page both rendered.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `/models` shows `vidu` as `not_configured` | `VIDU_API_KEY` is missing |
| Session created but `/sessions/{id}/vidu` keeps returning 409 | worker handshake failed; look for `Vidu create session failed` in the backend log |
| Console shows `token may be invalid` | pass Vidu's `token` string straight to `joinChannel` — it is itself base64 `AliRtcAuthInfo` with the vendor-signed timestamp; a locally built `AliRtcAuthInfo` is rejected |
| Stage stays black while audio works | the playback `<video>` must always be mounted (not only when `!showStart`), otherwise `ref` is null during startup and the whole RTC join is skipped silently |
| Console shows `already joined` | the Aliyun engine is a page-wide singleton: the previous channel must be left (`await leaveChannel()`) before joining again; `viduRtc.ts` tracks `joinedChannelId` and retries once after leaving |
| Two voices while the bot replies | One track, one audible player: `<audio ref={audioRef}>` is the only audio sink and the stage `<video>` must stay `muted` (the same `MediaStream` on two unmuted elements plays the reply twice with an offset). Vidu also echoes our audio into its RTC stream, so the client calls `setDefaultSubscribeAllRemoteAudioStreams(false)` and keeps the overlay `muted`. |
| Each turn rebuilds the live session | barge-in fell into the FlashTalk reset branch; confirm `model_type == "vidu"` |

## Not done yet

- `type=9/10` transcription-based gesture driving: `send_transcription()` exists but is not wired into the speak pipeline.
- Recording / offline export: there are no local frames for `vidu` sessions; use Vidu's recording API.
- Browser RTC playback is verified with a production key in Chromium/Edge; other RTC providers (trtc / agora / volcengine) still need adapters.
- This page is a translation of `docs/zh/model-deployment/vidu-avatar.md`; keep both in sync.

