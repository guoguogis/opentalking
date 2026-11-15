/**
 * Vidu S2-Avatar playback (数字人组件版).
 *
 * Vidu renders the avatar in the cloud and pushes the video into an RTC
 * channel, so the browser must join that channel with the vendor RTC SDK.
 * Everything else in the conversation (ASR / LLM / TTS) still runs through
 * OpenTalking's own WebRTC audio track.
 *
 * The Aliyun RTC Web SDK is bundled from npm (`aliyun-rtc-sdk`) so the WebUI
 * works on an isolated LAN without reaching `g.alicdn.com`. Its real API is:
 *
 *   const engine = AliRtcEngine.getInstance();       // singleton, not createInstance
 *   engine.on("videoSubscribeStateChanged", (uid, _old, next) => { if (next === 3) … });
 *   engine.setRemoteViewConfig(videoEl, uid, 1); // 1 == camera track
 *   await engine.joinChannel({ appId, channelId, userId, token, timestamp });
 *
 * Supply `adapter` to plug a different provider (trtc / agora / volcengine).
 */
import { apiGet } from "./api";

export type ViduRtcCredential = {
  app_id?: string;
  channel_id?: string;
  user_id?: string;
  token?: string;
  token_expire_at?: string;
  provider?: string;
  managed?: boolean;
};

export type ViduRtcInfo = {
  session_id: string;
  provider: string;
  live_id: string;
  rtc: ViduRtcCredential;
  fps?: number;
};

export type ViduRtcHandle = {
  info: ViduRtcInfo;
  leave: () => void;
};

export class ViduRtcUnavailableError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ViduRtcUnavailableError";
  }
}

/** Optional hook so a different vendor SDK can be wired without editing App.tsx. */
export type ViduRtcAdapter = (context: {
  info: ViduRtcInfo;
  videoEl: HTMLVideoElement;
}) => Promise<{ leave: () => void }>;

type SdkModule = typeof import("aliyun-rtc-sdk");
type RtcEngine = InstanceType<SdkModule["default"]>;

/** AliRtcVideoTrack.AliRtcVideoTrackCamera — the avatar stream Vidu publishes. */
const VIDEO_TRACK_CAMERA = 1;
/** AliRtcSubscribeState: 3 == subscribed, safe to render. */
const SUBSCRIBE_STATE_SUBSCRIBED = 3;

/**
 * Which track carries the reply audio for vidu sessions.
 *
 * ``vidu``  (default) — the vendor stream. Aliyun plays remote audio through its
 *                       own audio pipeline (not the <video> srcObject), and that
 *                       copy is the one the rendered mouth was synced to.
 * ``local`` — our own WebRTC TTS track. Louder/earlier, but the mouth lags
 *             behind it, so it is only a fallback when the vendor audio is
 *             unavailable.
 *
 * Switch with ``VITE_VIDU_AUDIO_SOURCE=local``.
 */
const AUDIO_SOURCE = ((import.meta.env.VITE_VIDU_AUDIO_SOURCE as string | undefined) ?? "vidu").trim();

export function viduUsesLocalAudio(): boolean {
  return AUDIO_SOURCE === "local";
}

let sdkPromise: Promise<SdkModule> | null = null;
let engineCache: RtcEngine | null = null;

/** Channel this page currently occupies; the SDK engine is a process-wide singleton. */
let joinedChannelId = "";
/** In-flight leave, so a new join never races the previous channel teardown. */
let pendingLeave: Promise<void> | null = null;

/**
 * Leave whatever channel the singleton engine still holds.
 * `leaveChannel()` is async and throws when we are not joined, hence the guard.
 */
async function leaveChannelSafe(engine: RtcEngine): Promise<void> {
  try {
    await engine.leaveChannel();
  } catch {
    // Not joined, or already leaving — nothing to do.
  }
  joinedChannelId = "";
}

/**
 * Load the vendor SDK on demand: it is ~1.4 MB and only Vidu sessions need it,
 * so it stays in its own chunk instead of inflating the main bundle.
 */
function loadSdk(): Promise<SdkModule> {
  if (!sdkPromise) sdkPromise = import("aliyun-rtc-sdk");
  return sdkPromise;
}

async function getEngine(): Promise<RtcEngine> {
  if (engineCache) return engineCache;
  const sdk = await loadSdk();
  engineCache = sdk.default.getInstance();
  return engineCache;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

/**
 * Wait until the worker has published the Vidu live session credentials.
 * The runner publishes them right after the control-channel handshake, which
 * can happen a few seconds after the Session becomes ready.
 */
export async function waitForViduRtcInfo(
  sessionId: string,
  options: { timeoutMs?: number; intervalMs?: number } = {}
): Promise<ViduRtcInfo> {
  const timeoutMs = options.timeoutMs ?? 60_000;
  const intervalMs = options.intervalMs ?? 800;
  const deadline = Date.now() + timeoutMs;
  let lastError: unknown = null;
  while (Date.now() < deadline) {
    try {
      return await apiGet<ViduRtcInfo>(`/sessions/${sessionId}/vidu`);
    } catch (error) {
      lastError = error;
      await sleep(intervalMs);
    }
  }
  throw new ViduRtcUnavailableError(
    `Vidu live session was not ready within ${Math.round(timeoutMs / 1000)}s: ${
      lastError instanceof Error ? lastError.message : String(lastError)
    }`
  );
}

/**
 * Join the vendor RTC channel and paint the rendered avatar into `videoEl`.
 *
 * The default path targets the Aliyun RTC (artc) provider, which is Vidu's
 * default. Supply `adapter` for trtc / agora / volcengine.
 */
export async function startViduPlayback(
  sessionId: string,
  videoEl: HTMLVideoElement,
  options: { timeoutMs?: number; adapter?: ViduRtcAdapter; onInfo?: (info: ViduRtcInfo) => void } = {}
): Promise<ViduRtcHandle> {
  const info = await waitForViduRtcInfo(sessionId, { timeoutMs: options.timeoutMs });
  options.onInfo?.(info);

  if (options.adapter) {
    const handle = await options.adapter({ info, videoEl });
    return { info, leave: handle.leave };
  }

  const { app_id: appId, channel_id: channelId, user_id: userId, token } = info.rtc;
  if (!appId || !channelId || !userId || !token) {
    throw new ViduRtcUnavailableError(
      `Vidu returned incomplete RTC credentials for session ${sessionId}: ${JSON.stringify(info.rtc)}`
    );
  }
  if (info.provider && info.provider !== "artc") {
    throw new ViduRtcUnavailableError(
      `Vidu RTC provider "${info.provider}" needs a custom adapter; only artc (Aliyun RTC) is bundled.`
    );
  }

  videoEl.autoplay = true;
  videoEl.playsInline = true;
  // The overlay is the single audible sink for vidu sessions: Vidu's own audio
  // is synced to the mouth it rendered, while our local TTS track runs ahead of
  // it (playing that instead makes the lips look disconnected).
  videoEl.muted = false;

  const engine = await getEngine();
  let attachedUid = "";

  const playVideo = () => {
    void videoEl.play().catch((error: unknown) => {
      // AbortError just means setRemoteViewConfig swapped the stream mid-play().
      if (error instanceof DOMException && error.name === "AbortError") return;
      console.warn("Vidu RTC video play() was blocked", error);
    });
  };

  try {
    engine.setDefaultSubscribeAllRemoteVideoStreams(true);
    // Vidu publishes the audio it rendered the lips against, so its stream is
    // already A/V-synced. Subscribe it unless the local-audio fallback is on;
    // App.tsx mutes our own TTS sink for vidu sessions so the reply is heard
    // exactly once.
    engine.setDefaultSubscribeAllRemoteAudioStreams(!viduUsesLocalAudio());
  } catch {
    // Not fatal: the explicit subscription state handler below still fires.
  }

  const onSubscribeStateChanged = (uid: string, _old: number, next: number) => {
    if (next !== SUBSCRIBE_STATE_SUBSCRIBED || !uid) return;
    attachedUid = uid;
    try {
      engine.setRemoteViewConfig(videoEl, uid, VIDEO_TRACK_CAMERA);
    } catch (error) {
      console.warn("Vidu RTC setRemoteViewConfig failed", error);
      return;
    }
    playVideo();
  };
  const onBye = (code: number) => {
    console.warn(`Vidu RTC bye, code=${code}`);
  };

  engine.on("videoSubscribeStateChanged", onSubscribeStateChanged);
  engine.on("bye", onBye);

  // The Aliyun engine is a process-wide singleton: reusing it for a second
  // session fails with "already joined" unless the previous channel is fully
  // released, and leaveChannel() is async, so wait out any in-flight leave.
  if (pendingLeave) {
    await pendingLeave.catch(() => undefined);
    pendingLeave = null;
  }
  if (joinedChannelId) {
    await leaveChannelSafe(engine);
    await sleep(120);
  }

  const join = async (payload: string | Parameters<RtcEngine["joinChannel"]>[0]) => {
    await engine.joinChannel(payload);
    joinedChannelId = channelId;
  };

  try {
    // Vidu returns an AliRTC "token" that is itself the base64-encoded auth info
    // (appid / channelid / userid / nonce / timestamp / token). The room server
    // validates the join against the timestamp baked into that blob, so hand the
    // vendor-signed string to joinChannel instead of rebuilding the fields —
    // a locally generated timestamp is rejected with "token may be invalid".
    await join(token);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (/already joined/i.test(message)) {
      // Stale channel from a previous session that never tore down cleanly.
      await leaveChannelSafe(engine);
      await sleep(200);
      await join(token);
    } else {
      const tokenTs = Number(info.rtc.token_expire_at);
      const fallbackTimestamp =
        Number.isFinite(tokenTs) && tokenTs > 0 ? tokenTs : Math.floor(Date.now() / 1000);
      try {
        await join({
          appId,
          channelId,
          userId,
          token,
          nonce: "",
          timestamp: fallbackTimestamp,
        });
      } catch (fallbackError) {
        engine.off("videoSubscribeStateChanged", onSubscribeStateChanged);
        engine.off("bye", onBye);
        throw new ViduRtcUnavailableError(
          `Failed to join Vidu RTC channel ${channelId}: ` +
            `${message} / ${fallbackError instanceof Error ? fallbackError.message : String(fallbackError)}`
        );
      }
    }
  }

  if (attachedUid) {
    // The overlay is the audible sink for vidu sessions (vendor A/V is already
    // in sync); audio must stay subscribed.
    try {
      engine.muteRemoteAudioPlaying(attachedUid, false);
    } catch {
      // Ignore: muting is a best-effort hint.
    }
  }

  return {
    info,
    leave: () => {
      try {
        engine.off("videoSubscribeStateChanged", onSubscribeStateChanged);
        engine.off("bye", onBye);
      } catch (error) {
        console.warn("Vidu RTC listener cleanup failed", error);
      }
      try {
        if (attachedUid) {
          engine.setRemoteViewConfig(null, attachedUid, VIDEO_TRACK_CAMERA);
        }
      } catch {
        // Ignore: the view is dropped anyway when we leave the channel.
      }
      try {
        // leaveChannel() returns a promise; the next startViduPlayback awaits it
        // so a fresh join never races the teardown of this channel.
        pendingLeave = leaveChannelSafe(engine);
      } catch (error) {
        console.warn("Vidu RTC leaveChannel failed", error);
      }
      videoEl.pause();
      videoEl.srcObject = null;
    },
  };
}
