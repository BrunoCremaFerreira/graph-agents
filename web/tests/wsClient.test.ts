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
 *
 * ---
 *
 * Second defect, specified below: the socket has been one-way. Everything the
 * page knew arrived on it and nothing was ever sent back, which is fine while
 * the observed root is fixed at daemon boot -- and impossible once ctrl+L lets
 * the page ask for a different one. The browser cannot read the disk, so both
 * halves of the feature (Tab completion and the switch itself) are requests the
 * client has to WRITE, and three new answers it has to route:
 *
 *   completion — what Tab expands to;
 *   reset      — the root changed, empty the graph before the new tree lands;
 *   rootError  — the path was refused, with a reason for the bar to show.
 *
 * They ride the same options object as `onMeta`, for the same reason: no
 * positional argument is added or reordered, so `createWsClient(onEvent, url)`
 * in main.ts keeps compiling. Two properties are easy to get wrong and cost the
 * whole session when they are:
 *
 *  - `send` must be SILENT when there is no open socket. The daemon restarts,
 *    the client is mid-backoff, and the user hits Tab: an exception thrown out
 *    of the key handler leaves the page with a dead keyboard, for a keystroke
 *    that could simply have been dropped.
 *  - a `reset` must never reach `onEvent`. Routed as an event it would grow a
 *    node named after the new root instead of clearing the old project, which
 *    is the exact opposite of what the frame asks for.
 */

import { describe, it, expect, afterEach, vi } from "vitest";
import { createWsClient, resolveWsUrl } from "../src/wsClient";

/** Pretend the page was served from `origin`. */
function servedFrom(origin: string): void {
  const url = new URL(origin);
  vi.stubGlobal("window", {
    location: { protocol: url.protocol, host: url.host, hostname: url.hostname },
  });
}

/**
 * Minimal stand-in for the browser WebSocket, as in tests/wsClientMeta.ts, plus
 * the two things a two-way socket needs: what was written to it, and whether it
 * was open at the time. `send` refuses when the socket is not OPEN, exactly as a
 * real one does while CONNECTING -- a client that writes blindly fails here
 * rather than in the user's session.
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

  /** Frames written by the client, in order, exactly as handed over. */
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

  /** Deliver a raw frame exactly as the browser would. */
  deliver(data: unknown): void {
    this.onmessage?.({ data });
  }
}

/** A client that has not connected yet, with the fake WebSocket installed. */
function makeClient(
  onEvent: (event: unknown) => void = () => {},
  options?: Record<string, unknown>,
) {
  FakeSocket.last = null;
  vi.stubGlobal("WebSocket", FakeSocket as unknown as typeof WebSocket);
  return createWsClient(onEvent as never, "ws://localhost:8080/ws", options as never);
}

/** Connect a client and return it together with the socket it opened. */
function connected(
  onEvent: (event: unknown) => void = () => {},
  options?: Record<string, unknown>,
) {
  const client = makeClient(onEvent, options);
  client.connect();
  const socket = FakeSocket.last;
  if (!socket) throw new Error("client did not open a socket");
  return { client, socket };
}

const COMPLETION_FRAME = JSON.stringify({
  kind: "completion",
  path: "/home/brn/pro",
  completed: "/home/brn/projects/",
  matches: ["/home/brn/projects/", "/home/brn/proto/"],
});

const RESET_FRAME = JSON.stringify({
  kind: "reset",
  root: "/home/brn/projects/other",
});

const ROOT_ERROR_FRAME = JSON.stringify({
  kind: "rootError",
  path: "/nope",
  reason: "no such directory",
});

const FILE_VIEW_FRAME = JSON.stringify({
  kind: "fileView",
  path: "src/api/users.ts",
  mode: "diff",
  content: "@@ -1 +1 @@\n-old\n+new\n",
  truncated: false,
  error: "",
});

/** `kind` says fileView but there is no path to match the click against. */
const BROKEN_FILE_VIEW_FRAME = JSON.stringify({
  kind: "fileView",
  mode: "diff",
  content: "@@ -1 +1 @@\n-old\n+new\n",
});

const EVENT_FRAME = JSON.stringify({
  ts: 1754870400.5,
  agent: "sess-abc",
  type: "M",
  path: "src/api/users.ts",
  color: "FFAA00",
});

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

describe("WsClient.send", () => {
  it("writes the payload to the socket as JSON", () => {
    const { client, socket } = connected();

    client.send({ kind: "complete", path: "/home/brn/pro" });

    expect(socket.sent).toEqual([JSON.stringify({ kind: "complete", path: "/home/brn/pro" })]);
  });

  it("writes every request in the order they were made", () => {
    // Tab, Tab, Enter: the daemon answers each by `path`, so a reordered pair
    // would be matched against the wrong field contents.
    const { client, socket } = connected();

    client.send({ kind: "complete", path: "/home/brn/pro" });
    client.send({ kind: "setRoot", path: "/home/brn/projects/other" });

    expect(socket.sent).toEqual([
      JSON.stringify({ kind: "complete", path: "/home/brn/pro" }),
      JSON.stringify({ kind: "setRoot", path: "/home/brn/projects/other" }),
    ]);
  });

  it("stays silent when it was never connected, instead of throwing at the key handler", () => {
    const client = makeClient();

    expect(() => client.send({ kind: "complete", path: "/x" })).not.toThrow();
  });

  it("stays silent after disconnect, when there is no socket left to write to", () => {
    const { client } = connected();
    client.disconnect();

    expect(() => client.send({ kind: "complete", path: "/x" })).not.toThrow();
  });

  it("drops the request while the socket is not open, rather than writing into a dead socket", () => {
    // The daemon restarted and the client is mid-backoff; a Tab pressed now is
    // a keystroke worth losing, not a page worth breaking.
    const { client, socket } = connected();
    socket.readyState = FakeSocket.CLOSED;

    expect(() => client.send({ kind: "complete", path: "/x" })).not.toThrow();
    expect(socket.sent).toEqual([]);
  });

  it("resumes writing once the socket is open again", () => {
    const { client, socket } = connected();
    socket.readyState = FakeSocket.CONNECTING;
    client.send({ kind: "complete", path: "/dropped" });

    socket.readyState = FakeSocket.OPEN;
    client.send({ kind: "complete", path: "/kept" });

    expect(socket.sent).toEqual([JSON.stringify({ kind: "complete", path: "/kept" })]);
  });
});

describe("WebSocket client routing of the root frames", () => {
  it("hands a completion frame to the completion callback, parsed", () => {
    const onCompletion = vi.fn();
    const { socket } = connected(vi.fn(), { onCompletion });

    socket.deliver(COMPLETION_FRAME);

    expect(onCompletion).toHaveBeenCalledTimes(1);
    expect(onCompletion).toHaveBeenCalledWith({
      path: "/home/brn/pro",
      completed: "/home/brn/projects/",
      matches: ["/home/brn/projects/", "/home/brn/proto/"],
    });
  });

  it("hands a reset frame to the reset callback, parsed", () => {
    const onReset = vi.fn();
    const { socket } = connected(vi.fn(), { onReset });

    socket.deliver(RESET_FRAME);

    expect(onReset).toHaveBeenCalledTimes(1);
    expect(onReset).toHaveBeenCalledWith({ root: "/home/brn/projects/other" });
  });

  it("hands a rootError frame to the error callback, parsed", () => {
    const onRootError = vi.fn();
    const { socket } = connected(vi.fn(), { onRootError });

    socket.deliver(ROOT_ERROR_FRAME);

    expect(onRootError).toHaveBeenCalledTimes(1);
    expect(onRootError).toHaveBeenCalledWith({
      path: "/nope",
      reason: "no such directory",
    });
  });

  it("never feeds a reset frame to the event callback", () => {
    // Routed as an event it would add a node named after the new root, which is
    // the opposite of emptying the graph.
    const onEvent = vi.fn();
    const { socket } = connected(onEvent, { onReset: vi.fn() });

    socket.deliver(RESET_FRAME);

    expect(onEvent).not.toHaveBeenCalled();
  });

  it("never feeds a completion frame to the event callback", () => {
    const onEvent = vi.fn();
    const { socket } = connected(onEvent, { onCompletion: vi.fn() });

    socket.deliver(COMPLETION_FRAME);

    expect(onEvent).not.toHaveBeenCalled();
  });

  it("never feeds a rootError frame to the event callback", () => {
    const onEvent = vi.fn();
    const { socket } = connected(onEvent, { onRootError: vi.fn() });

    socket.deliver(ROOT_ERROR_FRAME);

    expect(onEvent).not.toHaveBeenCalled();
  });

  it("does not mistake an activity event for a reset, so a file save never wipes the graph", () => {
    const onReset = vi.fn();
    const onEvent = vi.fn();
    const { socket } = connected(onEvent, { onReset });

    socket.deliver(EVENT_FRAME);

    expect(onReset).not.toHaveBeenCalled();
  });

  it("still routes an activity event to the event callback with the root sinks wired", () => {
    const onEvent = vi.fn();
    const { socket } = connected(onEvent, {
      onCompletion: vi.fn(),
      onReset: vi.fn(),
      onRootError: vi.fn(),
    });

    socket.deliver(EVENT_FRAME);

    expect(onEvent).toHaveBeenCalledTimes(1);
    expect((onEvent.mock.calls[0][0] as { path: string }).path).toBe("src/api/users.ts");
  });

  it("drops the root frames in silence when no such callback was given", () => {
    // A page built before these frames existed still has to survive a daemon
    // that sends them; an exception in onmessage kills every event after it.
    const onEvent = vi.fn();
    const { socket } = connected(onEvent);

    expect(() => {
      socket.deliver(COMPLETION_FRAME);
      socket.deliver(RESET_FRAME);
      socket.deliver(ROOT_ERROR_FRAME);
    }).not.toThrow();
    expect(onEvent).not.toHaveBeenCalled();
  });

  it("keeps delivering events after a reset frame with no reset callback", () => {
    const onEvent = vi.fn();
    const { socket } = connected(onEvent);

    socket.deliver(RESET_FRAME);
    socket.deliver(EVENT_FRAME);

    expect(onEvent).toHaveBeenCalledTimes(1);
  });
});

/**
 * Coverage backfill: `onFileView` was added to the options object and routed
 * alongside onMeta/onCompletion/onReset/onRootError, but — unlike those four —
 * arrived with no test of its own. It carries the same two hazards they do, and
 * the second is the expensive one: a fileView frame is an ANSWER about a path,
 * not a change to it, so leaking it into `onEvent` would flash the file in the
 * graph every time someone merely opened it, and the flash would be
 * indistinguishable from a real write.
 */
describe("WebSocket client routing of the fileView frame", () => {
  it("hands a fileView frame to the file-view callback, parsed", () => {
    const onFileView = vi.fn();
    const { socket } = connected(vi.fn(), { onFileView });

    socket.deliver(FILE_VIEW_FRAME);

    expect(onFileView).toHaveBeenCalledTimes(1);
    expect(onFileView).toHaveBeenCalledWith({
      path: "src/api/users.ts",
      mode: "diff",
      content: "@@ -1 +1 @@\n-old\n+new\n",
      truncated: false,
      error: "",
    });
  });

  it("never feeds a fileView frame to the event callback", () => {
    // Reading a file is not touching it: routed on as an event, every click
    // would light the node up as though the file had just been written.
    const onEvent = vi.fn();
    const { socket } = connected(onEvent, { onFileView: vi.fn() });

    socket.deliver(FILE_VIEW_FRAME);

    expect(onEvent).not.toHaveBeenCalled();
  });

  it("consumes a fileView frame with no callback registered, instead of falling through to the event sink", () => {
    // A page built before the panel existed still has to survive a daemon that
    // sends the frame; the frame is swallowed, not reinterpreted as activity.
    const onEvent = vi.fn();
    const { socket } = connected(onEvent);

    expect(() => socket.deliver(FILE_VIEW_FRAME)).not.toThrow();
    expect(onEvent).not.toHaveBeenCalled();
  });

  it("keeps delivering events after a fileView frame with no file-view callback", () => {
    const onEvent = vi.fn();
    const { socket } = connected(onEvent);

    socket.deliver(FILE_VIEW_FRAME);
    socket.deliver(EVENT_FRAME);

    expect(onEvent).toHaveBeenCalledTimes(1);
  });

  it("drops a fileView frame that names no file, reaching neither the panel nor the graph", () => {
    // An answer with no path cannot be matched to the click that asked for it,
    // and painting it would put one file's diff under another file's name.
    const onFileView = vi.fn();
    const onEvent = vi.fn();
    const { socket } = connected(onEvent, { onFileView });

    expect(() => socket.deliver(BROKEN_FILE_VIEW_FRAME)).not.toThrow();
    expect(onFileView).not.toHaveBeenCalled();
    expect(onEvent).not.toHaveBeenCalled();
  });

  it("does not mistake an activity event for a fileView answer", () => {
    const onFileView = vi.fn();
    const onEvent = vi.fn();
    const { socket } = connected(onEvent, { onFileView });

    socket.deliver(EVENT_FRAME);

    expect(onFileView).not.toHaveBeenCalled();
    expect(onEvent).toHaveBeenCalledTimes(1);
  });
});
