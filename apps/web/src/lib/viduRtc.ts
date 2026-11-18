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

import { startTrtcPlayback } from "./viduTrtc";

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

/** AliRtcVideoTrack.AliRtcVideoTrackCamera — camera stream (value 1). */
const VIDEO_TRACK_CAMERA = 1;
/** AliRtcVideoTrack.AliRtcVideoTrackScreen — screen-share stream (value 2). */
const VIDEO_TRACK_SCREEN = 2;
/** AliRtcVideoTrack.AliRtcVideoTrackNo — publisher has no video (value 0). */
const VIDEO_TRACK_NONE = 0;
/** AliRtcSubscribeState: 3 == subscribed, safe to render. */
const SUBSCRIBE_STATE_SUBSCRIBED = 3;

/**
 * How long to wait for the first decoded frame before giving up on a join.
 * Vidu starts pushing a little after the control-channel handshake, and a join
 * that lands before the publisher exists parks the vendor SDK in a retry loop.
 */
const FRAME_WAIT_MS = 6_000;
/** Rejoin attempts when the first join produced no frame in FRAME_WAIT_MS. */
const VIDEO_REJOIN_ATTEMPTS = 2;
/** No new decoded frame for this long while attached ⇒ the picture is frozen. */
const STALL_AFTER_MS = 2_500;

/**
 * True once the element can actually paint something.
 *
 * ``videoWidth > 0`` alone is not enough: a <video> whose play() was blocked by
 * the autoplay policy holds a decoded frame but never renders it, and a stream
 * that stalled straight after attach still reports a frame — both painted an
 * opaque black rectangle over the stage.
 */
function hasPaintableFrame(videoEl: HTMLVideoElement): boolean {
  if (videoEl.readyState < 2 || videoEl.videoWidth <= 0) return false;
  return !isBlankFrame(videoEl);
}

/**
 * Detects a uniformly black frame.
 *
 * Vidu sometimes publishes nothing for a while: the SDK still hands us a single
 * black frame, which is indistinguishable from "no picture" for the user. Sample
 * a small thumbnail instead of trusting the counters.
 */
function isBlankFrame(videoEl: HTMLVideoElement): boolean {
  try {
    const canvas = document.createElement("canvas");
    canvas.width = 32;
    canvas.height = 32;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    if (!ctx) return false;
    ctx.drawImage(videoEl, 0, 0, 32, 32);
    const data = ctx.getImageData(0, 0, 32, 32).data;
    let maxLuma = 0;
    for (let i = 0; i < data.length; i += 4) {
      const luma = data[i] * 0.299 + data[i + 1] * 0.587 + data[i + 2] * 0.114;
      if (luma > maxLuma) maxLuma = luma;
    }
    return maxLuma < 8;
  } catch {
    // Tainted canvas / detached element: fall back to the cheap signal.
    return false;
  }
}

/** Resolve true once the element paints; `nudge` retries a blocked play(). */
async function waitForFirstFrame(
  videoEl: HTMLVideoElement,
  budgetMs: number,
  nudge?: () => void
): Promise<boolean> {
  const deadline = Date.now() + budgetMs;
  while (Date.now() < deadline) {
    if (hasPaintableFrame(videoEl)) return true;
    nudge?.();
    await new Promise((resolve) => window.setTimeout(resolve, 150));
  }
  return hasPaintableFrame(videoEl);
}

/**
 * Keep watching for the first painted frame long after the join budget expired.
 *
 * Vidu sometimes parks the vendor SDK in its own "PC begin restore" loop and
 * only pushes frames ~20-30s in; the caller uses this to fade the overlay in
 * whenever the picture finally shows up instead of leaving it hidden forever.
 */
function watchForLateFrame(
  videoEl: HTMLVideoElement,
  onFrame: () => void,
  nudge?: () => void,
  budgetMs = 180_000
): void {
  const deadline = Date.now() + budgetMs;
  const tick = () => {
    if (!videoEl.isConnected) return;
    if (hasPaintableFrame(videoEl)) {
      onFrame();
      return;
    }
    if (Date.now() > deadline) return;
    nudge?.();
    window.setTimeout(tick, 400);
  };
  window.setTimeout(tick, 400);
}

/**
 * Which track carries the reply audio for vidu sessions.
 *
 * ``local`` (default) — our own WebRTC TTS track, played by ``<audio>`` in App.
 *                       The browser autoplay policy blocks audible playback the
 *                       app cannot control, and the vendor copy goes through the
 *                       Aliyun SDK's own audio pipeline — which is exactly what
 *                       left vidu sessions silent while the picture rendered.
 *                       This track carries the same TTS audio that was pushed to
 *                       Vidu, so it is always in sync with the reply text.
 * ``vidu``  — the vendor stream (mouth-synced, but silent whenever the SDK's
 *             audio pipeline is blocked). Opt in with
 *             ``VITE_VIDU_AUDIO_SOURCE=vidu``.
 */
const AUDIO_SOURCE = ((import.meta.env.VITE_VIDU_AUDIO_SOURCE as string | undefined) ?? "local").trim();

export function viduUsesLocalAudio(): boolean {
  return AUDIO_SOURCE !== "vidu";
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
  options: {
    timeoutMs?: number;
    adapter?: ViduRtcAdapter;
    onInfo?: (info: ViduRtcInfo) => void;
    /** Fires once the overlay actually has a decoded frame to paint. */
    onFirstFrame?: () => void;
    /**
     * Fires when the overlay stops receiving frames (a frozen picture). Vidu
     * ends a live on its own, and the element then keeps painting the last
     * decoded frame forever, which looks like a still image.
     */
    onStall?: (stalled: boolean) => void;
  } = {}
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
  if (info.provider === "trtc") {
    // Tencent-hosted channel: the credential set is the same shape, only the
    // SDK differs, so swap the transport and keep the rest of the app intact.
    const handle = await startTrtcPlayback(info, videoEl, {
      onFirstFrame: options.onFirstFrame,
      onStall: options.onStall,
      vendorAudio: !viduUsesLocalAudio(),
      onVendorAudio: (ready: boolean) => {
        window.dispatchEvent(new CustomEvent("vidu-audio-state", { detail: { ready } }));
      },
    });
    return { info, leave: handle.leave };
  }
  if (info.provider && info.provider !== "artc") {
    throw new ViduRtcUnavailableError(
      `Vidu RTC provider "${info.provider}" needs a custom adapter; artc (Aliyun) and trtc (Tencent) are bundled.`
    );
  }

  videoEl.autoplay = true;
  videoEl.playsInline = true;
  // The overlay is the single audible sink for vidu sessions: Vidu's own audio
  // is synced to the mouth it rendered, while our local TTS track runs ahead of
  // it (playing that instead makes the lips look disconnected).
  //
  // Start muted: an unmuted <video> may not autoplay before the user has
  // interacted with the page, and a blocked play() leaves the element paused on
  // a black frame forever (the overlay is opaque, so the whole stage went dark).
  // Audio is restored as soon as playback is running and a gesture allows it.
  videoEl.muted = true;

  const engine = await getEngine();
  let attachedUid = "";

  /** Last observed RTC facts, read by `window.__viduRtc.state()`. */
  let debugState: Record<string, unknown> = {};
  const updateDebugState = (patch: Record<string, unknown>) => {
    debugState = { ...debugState, ...patch, at: Date.now() };
  };

  let gestureUnmuteArmed = false;
  const armGestureUnmute = () => {
    if (gestureUnmuteArmed || viduUsesLocalAudio()) return;
    gestureUnmuteArmed = true;
    const resume = () => {
      window.removeEventListener("pointerdown", resume, true);
      window.removeEventListener("keydown", resume, true);
      if (!videoEl.isConnected) return;
      // Playing already (muted); turning the volume on is now allowed because
      // the page has sticky user activation.
      videoEl.muted = false;
      void videoEl.play().catch(() => undefined);
    };
    window.addEventListener("pointerdown", resume, true);
    window.addEventListener("keydown", resume, true);
  };

  const playVideo = () => {
    const attempt = videoEl.play();
    if (!attempt) return;
    void attempt.catch((error: unknown) => {
      // AbortError just means setRemoteViewConfig swapped the stream mid-play().
      if (error instanceof DOMException && error.name === "AbortError") return;
      if (!videoEl.muted) {
        // Autoplay with sound was refused: paint the picture muted (always
        // allowed) and bring the audio back on the next user gesture.
        videoEl.muted = true;
        void videoEl.play().catch(() => undefined);
        gestureUnmuteArmed = false;
        armGestureUnmute();
        return;
      }
      console.warn("Vidu RTC video play() was blocked", error);
    });
    if (!viduUsesLocalAudio()) armGestureUnmute();
  };

  try {
    engine.setDefaultSubscribeAllRemoteVideoStreams(true);
    // Always subscribe the vendor stream, audio included: with
    // `setDefaultSubscribeAllRemoteAudioStreams(false)` the SDK stops pulling
    // the publisher at all and our element froze on a single (black) frame.
    // Audibility is controlled separately below via muteRemoteAudioPlaying.
    engine.setDefaultSubscribeAllRemoteAudioStreams(true);
  } catch {
    // Not fatal: the explicit subscription state handler below still fires.
  }

  const applyVendorAudioState = (uid: string) => {
    if (!uid || viduUsesLocalAudio()) return;
    try {
      // Vendor-audio mode: the overlay is the audible sink. In local-audio mode
      // we deliberately do NOT touch the vendor mute flag — muting the remote
      // user made the SDK stop delivering its video track as well (measured:
      // frame counter froze at 1 and the stage stayed black).
      engine.muteRemoteAudioPlaying(uid, false);
    } catch {
      // Muting is a best-effort hint.
    }
  };

  /** What the publisher currently offers, straight from the SDK. */
  const readPublishedTracks = (uid: string): { camera: boolean; screen: boolean } => {
    try {
      const info = engine.getUserInfo(uid);
      return {
        camera: Boolean(info?.hasCamera || info?.hasCameraLarge || info?.hasCameraSmall),
        screen: Boolean(info?.hasScreenShare),
      };
    } catch {
      return { camera: false, screen: false };
    }
  };

  /**
   * Pick the track the vendor actually publishes.
   *
   * Vidu's renderer is a server-side compositor, so depending on the account it
   * shows up either as a camera track or as a screen-share track. Binding the
   * view to the wrong one leaves the element showing the single placeholder
   * frame the SDK decoded first — the exact "static picture, no lip movement"
   * symptom — so query the publisher instead of assuming camera.
   */
  const pickVideoTrack = (uid: string): number => {
    const { camera, screen } = readPublishedTracks(uid);
    if (camera) return VIDEO_TRACK_CAMERA;
    if (screen) return VIDEO_TRACK_SCREEN;
    return VIDEO_TRACK_CAMERA;
  };

  const attachRemoteView = (uid: string, reason: string, preferredTrack?: number) => {
    if (!uid) return;
    attachedUid = uid;
    const tracks = readPublishedTracks(uid);
    // getUserInfo can still be empty when remoteTrackAvailableNotify fires on a
    // real (non-headless) browser, so fall back to the track the event carried
    // instead of silently binding "no track" and never trying again.
    const track =
      tracks.camera
        ? VIDEO_TRACK_CAMERA
        : tracks.screen
          ? VIDEO_TRACK_SCREEN
          : typeof preferredTrack === "number" && preferredTrack > 0
            ? preferredTrack
            : pickVideoTrack(uid);
    try {
      engine.setRemoteViewConfig(videoEl, uid, track);
    } catch (error) {
      console.warn("Vidu RTC setRemoteViewConfig failed", error);
      return;
    }
    updateDebugState({ uid, track, reason, ...tracks });
    applyVendorAudioState(uid);
    playVideo();
  };

  let attachTimer: ReturnType<typeof setTimeout> | null = null;
  const scheduleAttach = (uid: string, reason: string, preferredTrack?: number) => {
    if (attachTimer) clearTimeout(attachTimer);
    attachTimer = setTimeout(() => {
      attachTimer = null;
      attachRemoteView(uid, reason, preferredTrack);
    }, 120);
  };

  /**
   * Keep re-asserting the view until the element really owns a stream.
   *
   * On Chrome the publisher shows up in getOnlineRemoteUsers() (so the channel
   * is fine) while `srcObject` stays null: the vendor SDK needs a second, later
   * setRemoteViewConfig() before it hands over the MediaStream. Without this the
   * overlay keeps showing the placeholder frame — "static picture, no lip
   * movement" with a perfectly healthy RTC channel.
   */
  let ensureTimer: ReturnType<typeof setInterval> | null = null;
  const ensureAttached = () => {
    if (ensureTimer) return;
    let tries = 0;
    ensureTimer = setInterval(() => {
      tries += 1;
      const stop = () => {
        if (ensureTimer) clearInterval(ensureTimer);
        ensureTimer = null;
      };
      if (tries > 20) {
        stop();
        return;
      }
      if (videoEl.srcObject) {
        if (videoEl.paused) playVideo();
        stop();
        return;
      }
      // Neither remoteTrackAvailableNotify nor videoSubscribeStateChanged is
      // guaranteed to fire (measured on a real Chrome: the channel was joined
      // and the publisher visible, yet no event ever reached us), so discover
      // the publisher here instead of waiting for a callback that never comes.
      let uid = attachedUid;
      if (!uid) {
        try {
          const uids: string[] = engine.getOnlineRemoteUsers?.() ?? [];
          uid = uids.find((candidate) => readPublishedTracks(candidate).camera) ?? uids[0] ?? "";
        } catch {
          uid = "";
        }
      }
      if (!uid) {
        if (tries > 12) stop();
        return;
      }
      attachRemoteView(uid, `ensure#${tries}`, tries % 2 === 0 ? VIDEO_TRACK_SCREEN : VIDEO_TRACK_CAMERA);
    }, 1_500);
  };

  // Diagnostic handle for E2E probes: exposes the vendor engine so a test can
  // read remote-user track availability and decoded frame counts instead of
  // guessing from screenshots.
  /** Reusable scratch canvas for the frame fingerprint below. */
  const probeCanvas = document.createElement("canvas");
  const sampleFrame = (): { frames: number; luma: number; w: number } => {
    const q = videoEl.getVideoPlaybackQuality ? videoEl.getVideoPlaybackQuality() : undefined;
    let luma = -1;
    if (videoEl.videoWidth > 0) {
      try {
        probeCanvas.width = 48;
        probeCanvas.height = 48;
        const ctx = probeCanvas.getContext("2d", { willReadFrequently: true });
        if (ctx) {
          ctx.drawImage(videoEl, 0, 0, 48, 48);
          const data = ctx.getImageData(0, 0, 48, 48).data;
          let sum = 0;
          for (let i = 0; i < data.length; i += 4) sum += data[i] * 3 + data[i + 1] * 5 + data[i + 2] * 7;
          luma = sum % 1_000_000_007;
        }
      } catch {
        luma = -2;
      }
    }
    return { frames: q ? q.totalVideoFrames : -1, luma, w: videoEl.videoWidth };
  };
  const videoState = () => {
    const q = videoEl.getVideoPlaybackQuality ? videoEl.getVideoPlaybackQuality() : undefined;
    return {
      w: videoEl.videoWidth,
      h: videoEl.videoHeight,
      readyState: videoEl.readyState,
      paused: videoEl.paused,
      muted: videoEl.muted,
      currentTime: Number(videoEl.currentTime.toFixed(2)),
      frames: q ? q.totalVideoFrames : -1,
      dropped: q ? q.droppedVideoFrames : -1,
      opacity: typeof getComputedStyle === "function" ? getComputedStyle(videoEl).opacity : "?",
      hasStream: Boolean(videoEl.srcObject),
      videoTracks: (videoEl.srcObject as MediaStream | null)?.getVideoTracks
        ? ((videoEl.srcObject as MediaStream).getVideoTracks() as MediaStreamTrack[]).map(
            (track) => track.readyState
          )
        : [],
    };
  };

  updateDebugState({ channelId });
  (window as unknown as Record<string, unknown>).__viduRtc = {
    engine,
    videoEl,
    state: () => ({ channelId, attachedUid, ...debugState, video: videoState() }),
    /** Two frame probes `ms` apart: proves the picture is actually moving. */
    motionSample: async (ms = 600) => {
      const before = sampleFrame();
      await new Promise((resolve) => setTimeout(resolve, ms));
      const after = sampleFrame();
      return {
        before,
        after,
        framesAdvanced: after.frames > before.frames,
        lumaChanged: after.luma !== before.luma,
        video: videoState(),
      };
    },
    remoteUsers: () => {
      try {
        const uids = engine.getOnlineRemoteUsers?.() ?? [];
        return uids.map((uid: string) => ({ uid, ...readPublishedTracks(uid) }));
      } catch {
        return [];
      }
    },
    /** Raw vendor counters: proves whether media actually reaches the client. */
    stats: () => {
      const result: Record<string, unknown> = {};
      const eng = engine as unknown as Record<string, (arg?: unknown) => unknown>;
      try {
        if (attachedUid && typeof eng.getRemoteVideoStats === "function") {
          result.remoteVideo = eng.getRemoteVideoStats(attachedUid);
        }
      } catch (error) {
        result.remoteVideoError = String(error);
      }
      try {
        if (typeof eng.getStats === "function") result.stats = eng.getStats();
      } catch (error) {
        result.statsError = String(error);
      }
      return result;
    },
    forceAttach: (uid: string, track: number) => {
      try {
        engine.setRemoteViewConfig(videoEl, uid, track);
        void videoEl.play().catch(() => undefined);
        return true;
      } catch (error) {
        return String(error);
      }
    },
  };

  const onTrackAvailable = (uid: string, _audio: number, videoTrack: number) => {
    if (!uid) return;
    updateDebugState({ availability: videoTrack });
    if (videoTrack === VIDEO_TRACK_NONE) return;
    // Re-bind so a screen-share publisher is rendered too; a camera-only bind
    // would keep the element frozen on the first decoded frame.
    scheduleAttach(uid, `remoteTrackAvailable(${videoTrack})`);
  };

  const onSubscribeStateChanged = (uid: string, _old: number, next: number) => {
    if (next !== SUBSCRIBE_STATE_SUBSCRIBED || !uid) return;
    attachRemoteView(uid, "videoSubscribeStateChanged");
  };
  const onBye = (code: number) => {
    console.warn(`Vidu RTC bye, code=${code}`);
  };

  engine.on("videoSubscribeStateChanged", onSubscribeStateChanged);
  engine.on("remoteTrackAvailableNotify", onTrackAvailable);
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
        engine.off("remoteTrackAvailableNotify", onTrackAvailable);
        engine.off("bye", onBye);
        throw new ViduRtcUnavailableError(
          `Failed to join Vidu RTC channel ${channelId}: ` +
            `${message} / ${fallbackError instanceof Error ? fallbackError.message : String(fallbackError)}`
        );
      }
    }
  }

  if (attachedUid) {
    // Re-assert after the join: some SDK versions reset the mute flag when the
    // publisher re-announces itself.
    applyVendorAudioState(attachedUid);
  }

  // The runner publishes RTC credentials as soon as the control-channel
  // handshake completes, but Vidu only starts pushing frames shortly after.
  // Joining inside that gap leaves the vendor SDK in its own "PC begin
  // restore" retry loop, which was measured taking ~27s before the first
  // frame appeared. Rejoining once the publisher is live cuts that to ~3s.
  let framesReady = await waitForFirstFrame(videoEl, FRAME_WAIT_MS, playVideo);
  for (let attempt = 0; !framesReady && attempt < VIDEO_REJOIN_ATTEMPTS; attempt += 1) {
    await leaveChannelSafe(engine);
    joinedChannelId = "";
    await sleep(300);
    try {
      await join(token);
    } catch (error) {
      console.warn("Vidu RTC rejoin failed", error);
      break;
    }
    framesReady = await waitForFirstFrame(videoEl, FRAME_WAIT_MS, playVideo);
  }
  if (!framesReady) {
    console.warn("Vidu RTC joined but the first video frame has not arrived yet", {
      channelId,
      waitedMs: FRAME_WAIT_MS * (VIDEO_REJOIN_ATTEMPTS + 1),
    });
  }
  if (options.onFirstFrame) {
    if (framesReady) options.onFirstFrame();
    else watchForLateFrame(videoEl, options.onFirstFrame, playVideo);
  }
  if (!framesReady) ensureAttached();

  // Stall watchdog: Vidu keeps the last decoded frame on screen after the live
  // ends, so "no new frames for a few seconds" is the only reliable signal that
  // the picture is frozen rather than simply a motionless avatar.
  let stallTimer: ReturnType<typeof setInterval> | undefined;
  if (options.onStall) {
    const onStall = options.onStall;
    let lastFrames = -1;
    let lastAdvanceAt = Date.now();
    let stalled = false;
    stallTimer = setInterval(() => {
      const q = videoEl.getVideoPlaybackQuality ? videoEl.getVideoPlaybackQuality() : undefined;
      const frames = q ? q.totalVideoFrames : -1;
      if (frames < 0 || videoEl.videoWidth === 0) return;
      if (frames !== lastFrames) {
        lastFrames = frames;
        lastAdvanceAt = Date.now();
        if (stalled) {
          stalled = false;
          onStall(false);
        }
        return;
      }
      if (!stalled && Date.now() - lastAdvanceAt > STALL_AFTER_MS) {
        stalled = true;
        console.warn("Vidu overlay stopped receiving frames", { frames, stalledMs: Date.now() - lastAdvanceAt });
        onStall(true);
      }
    }, 1000);
  }

  return {
    info,
    leave: () => {
      if (stallTimer) clearInterval(stallTimer);
      if (ensureTimer) {
        clearInterval(ensureTimer);
        ensureTimer = null;
      }
      try {
        engine.off("videoSubscribeStateChanged", onSubscribeStateChanged);
        engine.off("remoteTrackAvailableNotify", onTrackAvailable);
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
