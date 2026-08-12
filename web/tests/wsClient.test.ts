/**
 * Contract tests (RED) for resolveWsUrl.
 *
 * The daemon now answers HTTP and the WebSocket on the SAME port, so the client
 * must derive its URL from the page's own origin. That makes remote setups
 * (SSH / VS Code port forwarding) work with a single forwarded port instead of
 * silently failing to connect to a hard-coded localhost:8765.
 *
 * Expected to FAIL until resolveWsUrl derives the URL from window.location.
 * One failure reason per test.
 */

import { describe, it, expect, afterEach, vi } from "vitest";
import { resolveWsUrl } from "../src/wsClient";

/** Pretend the page was served from `origin`. */
function servedFrom(origin: string): void {
  const url = new URL(origin);
  vi.stubGlobal("window", {
    location: { protocol: url.protocol, host: url.host, hostname: url.hostname },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("resolveWsUrl", () => {
  it("derives a ws:// URL on the same host and port as the page", () => {
    servedFrom("http://localhost:8080");

    expect(resolveWsUrl()).toBe("ws://localhost:8080/ws");
  });

  it("keeps the forwarded port the page was actually served from", () => {
    // A tunnel may expose the daemon on any local port; follow the page.
    servedFrom("http://localhost:9999");

    expect(resolveWsUrl()).toBe("ws://localhost:9999/ws");
  });

  it("uses wss:// when the page is served over https", () => {
    servedFrom("https://example.com");

    expect(resolveWsUrl()).toBe("wss://example.com/ws");
  });

  it("follows a remote host rather than assuming localhost", () => {
    servedFrom("http://192.168.100.2:8080");

    expect(resolveWsUrl()).toBe("ws://192.168.100.2:8080/ws");
  });

  it("lets VITE_WS_URL override the derived URL", () => {
    servedFrom("http://localhost:8080");
    vi.stubEnv("VITE_WS_URL", "ws://elsewhere:1234/ws");

    expect(resolveWsUrl()).toBe("ws://elsewhere:1234/ws");
  });
});
