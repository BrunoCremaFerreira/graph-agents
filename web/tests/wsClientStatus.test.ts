/**
 * Contract tests (RED) for status/event routing in the WebSocket client.
 *
 * The git status panel adds a sixth kind of frame to the one socket the daemon
 * broadcasts on, and the client is the only place that sees all of them -- so it
 * is the only place that can misroute one. A `status` frame handed to `onEvent`
 * would be parsed as activity and grow a node called "status" in the graph, and
 * a status poll running every couple of seconds would keep it alive forever.
 *
 * Two compatibility constraints matter as much as the routing itself, exactly as
 * for `onMeta` (see wsClientMeta.test.ts): the existing
 * `createWsClient(onEvent, url)` call must keep compiling and working, and a
 * status frame arriving at a client given no status callback must be consumed in
 * silence -- an old page against a new daemon, where an exception in `onmessage`
 * would kill the frame handler for every event after it.
 *
 * Signature fixed by these tests (for the implementer):
 *   WsClientOptions gains `onStatus?: (status: GitStatus) => void`
 * i.e. the status sink rides in the SAME options object, no positional argument
 * added or reordered.
 *
 * Expected to FAIL until the client parses and routes status frames.
 */

import { describe, it, expect, afterEach, vi } from "vitest";
import { createWsClient } from "../src/wsClient";

/** Minimal stand-in for the browser WebSocket, capturing the live instance. */
class FakeSocket {
  static last: FakeSocket | null = null;

  onopen: (() => void) | null = null;
  onmessage: ((msg: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(public readonly url: string) {
    FakeSocket.last = this;
  }

  close(): void {
    /* nothing to tear down */
  }

  /** Deliver a raw frame exactly as the browser would. */
  deliver(data: unknown): void {
    this.onmessage?.({ data });
  }
}

/** Connect a client and return the socket it opened. */
function connect(
  onEvent: (event: unknown) => void,
  options?: Record<string, unknown>,
): FakeSocket {
  FakeSocket.last = null;
  vi.stubGlobal("WebSocket", FakeSocket as unknown as typeof WebSocket);
  const client = createWsClient(
    onEvent as never,
    "ws://localhost:8080/ws",
    options as never,
  );
  client.connect();
  const socket = FakeSocket.last;
  if (!socket) throw new Error("client did not open a socket");
  return socket;
}

const STATUS_FRAME = JSON.stringify({
  kind: "status",
  repo: true,
  truncated: false,
  entries: [
    { path: "web/src/renderer.ts", state: "modified" },
    { path: "scratch.txt", state: "untracked" },
  ],
});

const EVENT_FRAME = JSON.stringify({
  ts: 1754870400.5,
  agent: "sess-abc",
  type: "M",
  path: "src/api/users.ts",
  color: "FFAA00",
});

const META_FRAME = JSON.stringify({
  kind: "meta",
  root: "~/projects/rhizome-graph",
  branch: "development",
});

const RESET_FRAME = JSON.stringify({ kind: "reset", root: "/home/brn/projects/other" });

const COMPLETION_FRAME = JSON.stringify({
  kind: "completion",
  path: "/home/brn/pro",
  completed: "/home/brn/projects/",
  matches: ["/home/brn/projects/"],
});

const ROOT_ERROR_FRAME = JSON.stringify({
  kind: "rootError",
  path: "/nope",
  reason: "no such directory",
});

const FILE_VIEW_FRAME = JSON.stringify({
  kind: "fileView",
  path: "web/src/renderer.ts",
  mode: "diff",
  content: "@@ -1,3 +1,4 @@\n",
  truncated: false,
  error: "",
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("WebSocket client: status frame routing", () => {
  it("hands a status frame to the status callback, parsed", () => {
    const onEvent = vi.fn();
    const onStatus = vi.fn();
    const socket = connect(onEvent, { onStatus });

    socket.deliver(STATUS_FRAME);

    expect(onStatus).toHaveBeenCalledTimes(1);
    expect(onStatus).toHaveBeenCalledWith({
      repo: true,
      truncated: false,
      entries: [
        { path: "web/src/renderer.ts", state: "modified" },
        { path: "scratch.txt", state: "untracked" },
      ],
    });
  });

  it("does not feed a status frame to the event callback", () => {
    // Routed as an event it would grow a node called "status" in the graph, and
    // the poll would keep it there forever.
    const onEvent = vi.fn();
    const onStatus = vi.fn();
    const socket = connect(onEvent, { onStatus });

    socket.deliver(STATUS_FRAME);

    expect(onEvent).not.toHaveBeenCalled();
  });

  it("does not feed an activity event to the status callback", () => {
    const onEvent = vi.fn();
    const onStatus = vi.fn();
    const socket = connect(onEvent, { onStatus });

    socket.deliver(EVENT_FRAME);

    expect(onStatus).not.toHaveBeenCalled();
  });

  it("consumes a status frame in silence when no status callback was given", () => {
    const onEvent = vi.fn();
    const socket = connect(onEvent);

    expect(() => socket.deliver(STATUS_FRAME)).not.toThrow();
    expect(onEvent).not.toHaveBeenCalled();
  });

  it("keeps delivering events after a status frame with no status callback", () => {
    const onEvent = vi.fn();
    const socket = connect(onEvent);

    socket.deliver(STATUS_FRAME);
    socket.deliver(EVENT_FRAME);

    expect(onEvent).toHaveBeenCalledTimes(1);
  });

  it("drops a malformed status frame without calling any sink", () => {
    const onEvent = vi.fn();
    const onStatus = vi.fn();
    const onMeta = vi.fn();
    const socket = connect(onEvent, { onStatus, onMeta });

    expect(() =>
      socket.deliver(JSON.stringify({ kind: "status", entries: "not a list" })),
    ).not.toThrow();

    // `entries` degrades, so this one is still a status frame; what must never
    // happen is it reaching the graph or the HUD's caption.
    expect(onEvent).not.toHaveBeenCalled();
    expect(onMeta).not.toHaveBeenCalled();
  });

  it("survives a status frame that is not an object at all", () => {
    const onEvent = vi.fn();
    const onStatus = vi.fn();
    const socket = connect(onEvent, { onStatus });

    expect(() => socket.deliver("[1,2,3]")).not.toThrow();
    expect(onEvent).not.toHaveBeenCalled();
    expect(onStatus).not.toHaveBeenCalled();
  });

  it("keeps delivering events after a malformed status frame", () => {
    const onEvent = vi.fn();
    const onStatus = vi.fn();
    const socket = connect(onEvent, { onStatus });

    socket.deliver(JSON.stringify({ kind: "status", entries: 7, repo: "yes" }));
    socket.deliver(EVENT_FRAME);

    expect(onEvent).toHaveBeenCalledTimes(1);
  });
});

describe("WebSocket client: the frames that already worked keep working", () => {
  it("still routes a valid event to the event callback", () => {
    const onEvent = vi.fn();
    const onStatus = vi.fn();
    const socket = connect(onEvent, { onStatus });

    socket.deliver(EVENT_FRAME);

    expect(onEvent).toHaveBeenCalledTimes(1);
    expect((onEvent.mock.calls[0][0] as { path: string }).path).toBe("src/api/users.ts");
  });

  it.each([
    ["meta", "onMeta", META_FRAME],
    ["reset", "onReset", RESET_FRAME],
    ["completion", "onCompletion", COMPLETION_FRAME],
    ["rootError", "onRootError", ROOT_ERROR_FRAME],
    ["fileView", "onFileView", FILE_VIEW_FRAME],
  ])("still routes a %s frame to its own sink, and never to onStatus", (_label, sinkName, frame) => {
    const onEvent = vi.fn();
    const onStatus = vi.fn();
    const sink = vi.fn();
    const socket = connect(onEvent, { onStatus, [sinkName]: sink });

    socket.deliver(frame);

    expect(sink).toHaveBeenCalledTimes(1);
    expect(onStatus).not.toHaveBeenCalled();
    expect(onEvent).not.toHaveBeenCalled();
  });
});
