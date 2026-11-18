import { apiGet, apiPost } from "./api";

/**
 * No STUN by default: browser and worker share the intranet, so host candidates
 * connect in milliseconds. An unreachable STUN (the old default pointed at
 * stun.l.google.com) makes ICE gathering wait for its request timeouts, which
 * cost ~15s before the first frame in restricted networks. The server hands the
 * real list out via /sessions/webrtc/ice-config when one is configured.
 */
const DEFAULT_ICE_SERVERS: RTCIceServer[] = [];

type WebRtcIceConfigResponse = {
  iceServers?: RTCIceServer[];
  iceTransportPolicy?: RTCIceTransportPolicy;
};

async function loadIceConfig(): Promise<WebRtcIceConfigResponse> {
  try {
    const response = await apiGet<WebRtcIceConfigResponse>("/sessions/webrtc/ice-config");
    return {
      // An empty array is a valid answer (host candidates only) — do not
      // replace it with a default, that is exactly what made startup slow.
      iceServers: Array.isArray(response.iceServers) ? response.iceServers : DEFAULT_ICE_SERVERS,
      iceTransportPolicy:
        response.iceTransportPolicy === "relay" || response.iceTransportPolicy === "all"
          ? response.iceTransportPolicy
          : undefined,
    };
  } catch (error) {
    console.warn("Failed to load WebRTC ICE config, using host candidates only", error);
    return { iceServers: DEFAULT_ICE_SERVERS };
  }
}

async function waitForIceGatheringComplete(pc: RTCPeerConnection, timeoutMs = 8000): Promise<void> {
  if (pc.iceGatheringState === "complete") return;
  await new Promise<void>((resolve) => {
    const timeout = window.setTimeout(done, timeoutMs);
    function done() {
      window.clearTimeout(timeout);
      pc.removeEventListener("icegatheringstatechange", onStateChange);
      resolve();
    }
    function onStateChange() {
      if (pc.iceGatheringState === "complete") done();
    }
    pc.addEventListener("icegatheringstatechange", onStateChange);
  });
}

function requestVideoPlayback(videoEl: HTMLVideoElement) {
  videoEl.autoplay = true;
  videoEl.playsInline = true;
  const attempt = () => {
    void videoEl.play().catch(() => {
      // If the browser blocks autoplay with audio, retry muted so video still paints.
      videoEl.muted = true;
      void videoEl.play().catch(() => {});
    });
  };
  attempt();
  return attempt;
}

export type PlaybackHandle = { pc: RTCPeerConnection; remoteStream: MediaStream };

export type StartPlaybackOptions = {
  onRemoteStream?: (remoteStream: MediaStream) => void;
};

export async function startPlayback(
  sessionId: string,
  videoEl: HTMLVideoElement,
  options: StartPlaybackOptions = {},
): Promise<PlaybackHandle> {
  const { iceServers, iceTransportPolicy } = await loadIceConfig();
  const pc = new RTCPeerConnection({
    iceServers,
    iceTransportPolicy,
  });
  const mediaStream = new MediaStream();
  videoEl.srcObject = mediaStream;
  const ensurePlayback = requestVideoPlayback(videoEl);

  pc.ontrack = (ev) => {
    const track = ev.track;
    if (!track) return;
    const hasTrack = mediaStream.getTracks().some((t) => t.id === track.id);
    if (!hasTrack) {
      mediaStream.addTrack(track);
      options.onRemoteStream?.(mediaStream);
    }
    ensurePlayback();
  };

  const cleanup = () => {
    videoEl.pause();
    videoEl.srcObject = null;
  };
  pc.addEventListener("connectionstatechange", () => {
    if (
      pc.connectionState === "closed"
      || pc.connectionState === "failed"
      || pc.connectionState === "disconnected"
    ) {
      cleanup();
    }
  });
  pc.addEventListener("iceconnectionstatechange", () => {
    if (
      pc.iceConnectionState === "closed"
      || pc.iceConnectionState === "failed"
      || pc.iceConnectionState === "disconnected"
    ) {
      cleanup();
    }
  });

  pc.addTransceiver("video", { direction: "recvonly" });
  pc.addTransceiver("audio", { direction: "recvonly" });

  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  await waitForIceGatheringComplete(pc);

  const answer = await apiPost<{ sdp: string; type: RTCSdpType }>(
    `/sessions/${sessionId}/webrtc/offer`,
    { sdp: pc.localDescription?.sdp ?? "", type: pc.localDescription?.type ?? "offer" }
  );

  await pc.setRemoteDescription(new RTCSessionDescription(answer));
  ensurePlayback();
  options.onRemoteStream?.(mediaStream);
  return { pc, remoteStream: mediaStream };
}
