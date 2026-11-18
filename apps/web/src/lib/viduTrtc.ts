/**
 * Tencent TRTC adapter for Vidu's managed RTC.
 *
 * Vidu can host the avatar channel on four providers. `artc` (Aliyun) is bundled
 * in `viduRtc.ts`; this module covers `trtc` (Tencent) so a network that blocks
 * one provider's media edge can fall back to another without touching the rest
 * of the app.
 *
 * Credential mapping (verified against Vidu create-session responses 2026-11):
 *   app_id     -> TRTC SDKAppID
 *   channel_id -> TRTC room id (string form, e.g. "live-user-1020...")
 *   user_id    -> TRTC userId
 *   token      -> TRTC userSig
 */

import type { ViduRtcInfo } from "./viduRtc";

type TrtcRemoteVideoConfig = {
  userId: string;
  streamType: never;
  view: HTMLElement;
};

type TrtcLike = {
  create: () => TrtcInstance;
  EVENT: Record<string, string>;
  TYPE: Record<string, number>;
  setLogLevel?: (level: number) => void;
};

type TrtcInstance = {
  on: (event: string, handler: (payload: never) => void) => void;
  off?: (event: string, handler: (payload: never) => void) => void;
  enterRoom: (config: Record<string, unknown>) => Promise<void>;
  exitRoom: () => Promise<void>;
  destroy: () => void;
  startRemoteVideo: (config: TrtcRemoteVideoConfig) => Promise<void>;
  getVideoTrack?: (config: { userId?: string; streamType?: unknown; processed?: boolean }) => MediaStreamTrack | null;
  getAudioTrack?: (config: { userId?: string; processed?: boolean }) => MediaStreamTrack | null;
  updateRemoteVideo?: (config: TrtcRemoteVideoConfig) => Promise<void>;
};

export type TrtcPlaybackOptions = {
  onFirstFrame?: () => void;
  onStall?: (stalled: boolean) => void;
  /** Called once the room is entered, mainly for diagnostics. */
  onJoined?: () => void;
  /**
   * Subscribe to the vendor's own audio track. Vidu renders the mouth from the
   * PCM we paced to it, so the only copy that is guaranteed in sync with the
   * picture is the one it publishes back.
   */
  vendorAudio?: boolean;
  /** Fires when the vendor audio track is actually bound (or given up on). */
  onVendorAudio?: (ready: boolean) => void;
};

const STALL_AFTER_MS = 2_500;
const ATTACH_RETRY_MS = 1_500;
const ATTACH_RETRIES = 20;

const videoStateOf = (el: HTMLVideoElement) => {
  const stream = el.srcObject as MediaStream | null;
  const quality = el.getVideoPlaybackQuality ? el.getVideoPlaybackQuality() : undefined;
  return {
    w: el.videoWidth,
    h: el.videoHeight,
    readyState: el.readyState,
    paused: el.paused,
    currentTime: Number(el.currentTime.toFixed(2)),
    frames: quality ? quality.totalVideoFrames : -1,
    opacity: window.getComputedStyle(el).opacity,
    hasStream: Boolean(stream),
  };
};

/**
 * Join the Tencent room Vidu prepared and paint its video into `videoEl`.
 *
 * The vendor publishes one remote user; we attach to its main stream. A
 * publisher can appear before its track is decodable, and `REMOTE_VIDEO_AVAILABLE`
 * is not always delivered on every browser, so the attach is retried until the
 * element really owns a playing stream.
 */
export async function startTrtcPlayback(
  info: ViduRtcInfo,
  videoEl: HTMLVideoElement,
  options: TrtcPlaybackOptions = {}
): Promise<{ leave: () => Promise<void> }> {
  const { app_id: appId, channel_id: channelId, user_id: userId, token } = info.rtc;
  const sdkAppId = Number(appId);
  if (!Number.isFinite(sdkAppId) || !channelId || !userId || !token) {
    throw new Error(
      `Vidu returned incomplete TRTC credentials: ${JSON.stringify({
        appId,
        channelId,
        userId,
        hasToken: Boolean(token),
      })}`
    );
  }

  videoEl.autoplay = true;
  videoEl.playsInline = true;
  // Local TTS is the audible sink for cloud sessions (same rule as artc), so the
  // element starts muted: an unmuted <video> may refuse to autoplay and then
  // stays paused on a black frame behind an opaque overlay.
  videoEl.muted = true;

  const sdkModule = (await import("trtc-sdk-v5")) as unknown as {
    default?: TrtcLike;
  } & TrtcLike;
  const TRTC: TrtcLike = sdkModule.default ?? sdkModule;
  const trtc = TRTC.create();

  let remoteUserId = "";
  let attachedStreamType = 0;
  let lastFrames = -1;
  let lastProgressAt = performance.now();
  let stalled = false;
  let firstFrameFired = false;
  let audioReady = false;
  let attachTimer: ReturnType<typeof setInterval> | null = null;
  let stallTimer: ReturnType<typeof setInterval> | null = null;
  const mainStreamType = TRTC.TYPE?.STREAM_TYPE_MAIN ?? 0;

  let gestureUnmuteArmed = false;
  const armGestureUnmute = () => {
    if (gestureUnmuteArmed || !options.vendorAudio) return;
    gestureUnmuteArmed = true;
    const resume = () => {
      window.removeEventListener("pointerdown", resume, true);
      window.removeEventListener("keydown", resume, true);
      gestureUnmuteArmed = false;
      if (!videoEl.isConnected) return;
      videoEl.muted = false;
      videoEl.volume = 1;
      void videoEl.play().catch(() => undefined);
    };
    window.addEventListener("pointerdown", resume, true);
    window.addEventListener("keydown", resume, true);
  };

  const playVideo = () => {
    const attempt = videoEl.play();
    if (!attempt) return;
    void attempt.catch((error: unknown) => {
      if (error instanceof DOMException && error.name === "AbortError") return;
      if (!videoEl.muted) {
        // Unmuted playback refused by the autoplay policy: paint muted (always
        // allowed) and restore sound on the next user gesture.
        console.warn("TRTC unmuted play() blocked; retrying on next gesture", error);
        videoEl.muted = true;
        void videoEl.play().catch(() => undefined);
        armGestureUnmute();
        return;
      }
      console.warn("TRTC video play() was blocked", error);
    });
  };

  // The SDK renders remote video by appending its own <video> into the element
  // you hand it, so the overlay element itself would stay empty (w=0) and every
  // downstream check - diag pill, stall watchdog, heartbeat - would read zeros.
  // Receive into an offscreen holder and bind the decoded track to our element
  // instead, which keeps one single video sink for the whole app.
  const holder = document.createElement("div");
  holder.style.cssText =
    "position:fixed;left:-9999px;top:0;width:1px;height:1px;overflow:hidden;pointer-events:none";
  document.body.appendChild(holder);

  const bindTrack = (): boolean => {
    if (!remoteUserId) return false;
    let track: MediaStreamTrack | null = null;
    try {
      track = trtc.getVideoTrack?.({ userId: remoteUserId, streamType: attachedStreamType as never }) ?? null;
    } catch {
      track = null;
    }
    if (!track) return false;
    const current = videoEl.srcObject as MediaStream | null;
    const currentVideo = current && current.getVideoTracks ? current.getVideoTracks()[0] : null;
    const currentAudio = current && current.getAudioTracks ? current.getAudioTracks()[0] : null;
    const vendorAudioTrack = options.vendorAudio ? bindAudio() : null;
    if (currentVideo !== track || (vendorAudioTrack && currentAudio !== vendorAudioTrack)) {
      const stream = new MediaStream([track]);
      if (vendorAudioTrack) stream.addTrack(vendorAudioTrack);
      videoEl.srcObject = stream;
    }
    playVideo();
    return true;
  };

  /** Vendor audio track for the same publisher, bound to the overlay element. */
  const bindAudio = (): MediaStreamTrack | null => {
    if (!remoteUserId || !options.vendorAudio) return null;
    try {
      const track = trtc.getAudioTrack?.({ userId: remoteUserId }) ?? null;
      if (track && !audioReady) {
        audioReady = true;
        // Playing vendor audio makes the overlay the audible sink; the app mutes
        // its local TTS sink in that case, so sync is exact by construction.
        videoEl.muted = false;
        videoEl.volume = 1;
        options.onVendorAudio?.(true);
      }
      return track;
    } catch {
      return null;
    }
  };

  const attach = async (uid: string, streamType: number | string, reason: string) => {
    if (!uid) return;
    remoteUserId = uid;
    attachedStreamType = streamType as number;
    try {
      await trtc.startRemoteVideo({
        userId: uid,
        streamType: streamType as never,
        view: holder,
      });
      lastProgressAt = performance.now();
    } catch (error) {
      console.warn(`TRTC startRemoteVideo failed (${reason})`, error);
    }
    if (bindTrack()) return;
    // The decoded track usually shows up a few hundred ms after the subscription
    // is set up. Poll fast first, so the picture is not delayed by the coarse
    // 1.5s retry cadence that exists to survive genuine failures.
    let fastTries = 0;
    const fast = setInterval(() => {
      fastTries += 1;
      if (bindTrack() || fastTries > 20) clearInterval(fast);
    }, 150);
  };

  const markFirstFrame = () => {
    if (firstFrameFired) return;
    firstFrameFired = true;
    options.onFirstFrame?.();
  };

  // --- diagnostics surface: same shape as the artc module -------------------
  const debug = {
    engine: trtc,
    videoEl,
    provider: "trtc",
    channelId,
    state: () => ({
      provider: "trtc",
      channelId,
      attachedUid: remoteUserId,
      streamType: attachedStreamType,
      stall: stalled,
      video: videoStateOf(videoEl),
    }),
    remoteUsers: () => (remoteUserId ? [{ uid: remoteUserId, camera: true, screen: false }] : []),
    motionSample: async (ms = 600) => {
      const canvas = document.createElement("canvas");
      canvas.width = 64;
      canvas.height = 64;
      const ctx = canvas.getContext("2d", { willReadFrequently: true });
      const sample = () => {
        if (!ctx || videoEl.videoWidth === 0) return { luma: -1, frames: -1 };
        ctx.drawImage(videoEl, 0, 0, 64, 64);
        const data = ctx.getImageData(0, 0, 64, 64).data;
        let sum = 0;
        for (let i = 0; i < data.length; i += 4) sum += data[i] * 3 + data[i + 1] * 5 + data[i + 2] * 7;
        const quality = videoEl.getVideoPlaybackQuality ? videoEl.getVideoPlaybackQuality() : undefined;
        return { luma: sum % 1_000_000_007, frames: quality ? quality.totalVideoFrames : -1 };
      };
      const before = sample();
      await new Promise((resolve) => setTimeout(resolve, ms));
      const after = sample();
      return {
        before,
        after,
        framesAdvanced: after.frames > before.frames,
        lumaChanged: after.luma !== before.luma,
        video: videoStateOf(videoEl),
      };
    },
    leave: async () => {
      try {
        await trtc.exitRoom();
      } catch {
        /* the room may already be gone */
      }
      try {
        trtc.destroy();
      } catch {
        /* ignore */
      }
    },
  };
  (window as unknown as { __viduRtc?: typeof debug }).__viduRtc = debug;

  // --- room events ---------------------------------------------------------
  trtc.on(TRTC.EVENT.REMOTE_VIDEO_AVAILABLE, (payload: never) => {
    const event = payload as unknown as { userId?: string; streamType?: number };
    void attach(event.userId ?? "", event.streamType ?? mainStreamType, "remote-video-available");
  });
  trtc.on(TRTC.EVENT.REMOTE_AUDIO_AVAILABLE, () => {
    if (bindAudio()) bindTrack();
  });
  trtc.on(TRTC.EVENT.ERROR, (payload: never) => {
    console.error("TRTC error", payload);
  });
  trtc.on(TRTC.EVENT.KICKED_OUT, (payload: never) => {
    console.warn("TRTC kicked out", payload);
  });

  // Audio stays on our own TTS track, so do not subscribe to the vendor's.
  //
  // Vidu hands out a *string* room id ("live-user-1020..."), and the SDK rejects
  // it in `roomId` ("must be an integer ... or use 'strRoomId'"), so route it to
  // the string field instead of truncating it into a different room.
  const numericRoomId = /^\d+$/.test(channelId) ? Number(channelId) : Number.NaN;
  await trtc.enterRoom({
    sdkAppId,
    userId,
    userSig: token,
    autoReceiveAudio: Boolean(options.vendorAudio),
    scene: "rtc",
    ...(Number.isFinite(numericRoomId) ? { roomId: numericRoomId } : { strRoomId: channelId }),
  });
  options.onJoined?.();

  // The publisher id is not known until it appears; poke until frames arrive.
  let tries = 0;
  attachTimer = setInterval(() => {
    tries += 1;
    if (tries > ATTACH_RETRIES) {
      if (attachTimer) clearInterval(attachTimer);
      attachTimer = null;
      return;
    }
    if (videoEl.videoWidth > 0 && videoEl.srcObject) {
      if (attachTimer) clearInterval(attachTimer);
      attachTimer = null;
      return;
    }
    if (remoteUserId) {
      if (!bindTrack()) void attach(remoteUserId, attachedStreamType || mainStreamType, `retry#${tries}`);
    }
  }, ATTACH_RETRY_MS);

  stallTimer = setInterval(() => {
    const quality = videoEl.getVideoPlaybackQuality ? videoEl.getVideoPlaybackQuality() : undefined;
    const frames = quality ? quality.totalVideoFrames : videoEl.currentTime * 1e3;
    if (frames > lastFrames) {
      lastFrames = frames;
      lastProgressAt = performance.now();
      if (stalled) {
        stalled = false;
        options.onStall?.(false);
      }
      markFirstFrame();
      return;
    }
    if (!stalled && performance.now() - lastProgressAt > STALL_AFTER_MS) {
      stalled = true;
      console.warn("TRTC overlay stopped receiving frames");
      options.onStall?.(true);
    }
  }, 1_000);

  playVideo();
  if (videoEl.videoWidth > 0) markFirstFrame();

  return {
    leave: async () => {
      if (attachTimer) clearInterval(attachTimer);
      if (stallTimer) clearInterval(stallTimer);
      attachTimer = null;
      stallTimer = null;
      await debug.leave();
      try {
        holder.remove();
      } catch {
        /* ignore */
      }
      try {
        if (videoEl.srcObject) videoEl.srcObject = null;
      } catch {
        /* ignore */
      }
    },
  };
}
