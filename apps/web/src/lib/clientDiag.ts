import { buildApiUrl } from "./api";

/**
 * Post one browser-side diagnostic snapshot to the server log.
 *
 * The cloud avatar is rendered by the vendor's RTC SDK inside the page, so a
 * failed channel join or a frozen picture is invisible from the server. This
 * beacon is what lets the operator read the user's actual numbers (frame count,
 * overlay opacity, stream presence) out of `service.log`.
 */
export function reportClientDiag(event: string, data: Record<string, unknown> = {}): void {
  try {
    void fetch(buildApiUrl("client-diag"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        event,
        at: new Date().toISOString(),
        href: typeof location === "object" ? location.href : "",
        ...data,
      }),
      keepalive: true,
    }).catch(() => undefined);
  } catch {
    // Diagnostics must never break the UI.
  }
}
