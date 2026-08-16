/**
 * Contract tests (RED) for the token on every outbound frame.
 *
 * The defect: the daemon authorizes commands (`complete`, `setRoot`, `file`) by
 * the peer's IP address, and that address lies. A WebSocket handshake is not
 * subject to the same-origin policy, so any page open in a browser on this host
 * can connect to `ws://127.0.0.1:8080/ws` and read files through it; and a
 * loopback-side proxy -- this repo's own `vite.config.ts`, `host: true` with a
 * `/ws` proxy -- makes a LAN connection arrive as loopback. The daemon's answer
 * is a boot token injected into the page it serves, required on every command.
 *
 * The page's side of that has ONE rule, and it is the reason these tests are
 * about `WsClient` rather than about its callers: `send` is the single
 * chokepoint. `main.ts` writes three different requests from three different
 * key and pointer handlers, and a token added at those call sites is a token
 * the fourth request (whenever it is written) will not have. So `main.ts` stays
 * exactly as it is, and the client tokenizes what passes through it.
 *
 * The client also resolves the token itself, from the injected global or from
 * the Vite variable, for the same reason: an option threaded in from `main.ts`
 * is another thing a call site can forget.
 *
 * The empty-token case is specified too, and it is not an afterthought: with no
 * token available the frame must go out EXACTLY as it does today. A `token: ""`
 * would be a field an older daemon does not recognize, and the dev-server setup
 * has to keep working.
 *
 * Expected to FAIL until `send` carries the token. One property per test.
 */

import { describe, it, expect, afterEach, vi } from "vitest";
import { createWsClient } from "../src/wsClient";

/**
 * Minimal stand-in for the browser WebSocket, as in `tests/wsClient.test.ts`:
 * what was written to it, and whether it was open at the time.
 */
class FakeSocket {
  static last: FakeSocket | null = null;
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  onopen: (() => void) | null = null;
  onmessage: ((msg: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  readonly sent: unknown[] = [];
  readyState: number = FakeSocket.OPEN;

  constructor(public readonly url: string) {
    FakeSocket.last = this;
  }

  send(data: unknown): void {
    if (this.readyState !== FakeSocket.OPEN) {
      throw new Error("InvalidStateError: socket is not open");
    }
    this.sent.push(data);
  }

  close(): void {
    this.readyState = FakeSocket.CLOSED;
  }
}

/** Pretend the daemon injected `token` into the page it served. */
function pageCarrying(token: string): void {
  vi.stubGlobal("window", {
    location: { protocol: "http:", host: "localhost:8080", hostname: "localhost" },
    __RHIZOME_TOKEN__: token,
  });
}

/** A page the daemon never touched (Vite dev server, or an older daemon). */
function pageCarryingNothing(): void {
  vi.stubGlobal("window", {
    location: { protocol: "http:", host: "localhost:8080", hostname: "localhost" },
  });
}

/**
 * Connect a client the way `main.ts` does -- no options object, nothing about
 * tokens at the call site -- and return the socket it opened.
 */
function connected() {
  FakeSocket.last = null;
  vi.stubGlobal("WebSocket", FakeSocket as unknown as typeof WebSocket);
  const client = createWsClient(() => {}, "ws://localhost:8080/ws");
  client.connect();
  const socket = FakeSocket.last;
  if (!socket) throw new Error("client did not open a socket");
  return { client, socket };
}

/** The frames written to the socket, parsed back from the wire format. */
function written(socket: FakeSocket): unknown[] {
  return socket.sent.map((frame) => JSON.parse(frame as string));
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("WsClient.send with a token in the page", () => {
  it("carries the token on a file request", () => {
    pageCarrying("s3cret-token");
    const { client, socket } = connected();

    client.send({ kind: "file", path: "src/app.ts" });

    expect(written(socket)).toEqual([
      { kind: "file", path: "src/app.ts", token: "s3cret-token" },
    ]);
  });

  it("carries the token on a completion request", () => {
    pageCarrying("s3cret-token");
    const { client, socket } = connected();

    client.send({ kind: "complete", path: "~/pro" });

    expect(written(socket)).toEqual([
      { kind: "complete", path: "~/pro", token: "s3cret-token" },
    ]);
  });

  it("carries the token on a root switch", () => {
    // The most privileged of the three: it repoints the daemon at any directory
    // on the host, which is the first half of reading any file on it.
    pageCarrying("s3cret-token");
    const { client, socket } = connected();

    client.send({ kind: "setRoot", path: "/srv/other" });

    expect(written(socket)).toEqual([
      { kind: "setRoot", path: "/srv/other", token: "s3cret-token" },
    ]);
  });

  it("tokenizes a request no call site has been written for yet", () => {
    // The point of putting this in `send`: whatever the fourth command turns
    // out to be, it is already authenticated.
    pageCarrying("s3cret-token");
    const { client, socket } = connected();

    client.send({ kind: "somethingNew", path: "x" });

    expect((written(socket)[0] as { token: string }).token).toBe("s3cret-token");
  });

  it("leaves the rest of the payload exactly as the caller wrote it", () => {
    // The daemon echoes `path` back and the page matches it against the field;
    // a payload rebuilt sloppily would break that match.
    pageCarrying("s3cret-token");
    const { client, socket } = connected();

    client.send({ kind: "complete", path: "  ~/pro  " });

    expect(written(socket)[0]).toMatchObject({ kind: "complete", path: "  ~/pro  " });
  });

  it("tokenizes every request, not only the first", () => {
    pageCarrying("s3cret-token");
    const { client, socket } = connected();

    client.send({ kind: "complete", path: "~/pro" });
    client.send({ kind: "setRoot", path: "/srv/other" });

    expect(written(socket).map((frame) => (frame as { token: string }).token)).toEqual([
      "s3cret-token",
      "s3cret-token",
    ]);
  });

  it("reads the token from the build-time variable when the page carries none", () => {
    // Vite serves the HTML in dev and proxies `/ws`, so nothing injected the
    // global and the daemon behind the proxy still wants a token.
    pageCarryingNothing();
    vi.stubEnv("VITE_RHIZOME_TOKEN", "dev-token");
    const { client, socket } = connected();

    client.send({ kind: "file", path: "src/app.ts" });

    expect(written(socket)).toEqual([
      { kind: "file", path: "src/app.ts", token: "dev-token" },
    ]);
  });
});

describe("WsClient.send with no token available", () => {
  it("writes the frame exactly as it does today", () => {
    // An older daemon would not know the field, and `main.ts` must not have to
    // care which one it is talking to.
    pageCarryingNothing();
    const { client, socket } = connected();

    client.send({ kind: "complete", path: "~/pro" });

    // No `token` key at all -- not an empty one. `toEqual` over the parsed
    // frame is what pins that: a `token: ""` on the wire would fail here.
    expect(written(socket)).toEqual([{ kind: "complete", path: "~/pro" }]);
  });

  it("does not throw when there is no window at all", () => {
    // Tests, SSR, a bundle evaluated outside a browser: resolving the token
    // must not be the thing that breaks the client.
    vi.stubGlobal("WebSocket", FakeSocket as unknown as typeof WebSocket);

    expect(() => {
      const client = createWsClient(() => {}, "ws://localhost:8080/ws");
      client.connect();
      client.send({ kind: "complete", path: "~/pro" });
    }).not.toThrow();
  });
});
