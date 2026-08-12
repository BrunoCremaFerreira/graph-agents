/**
 * Contract tests (RED) for meta/event routing in the WebSocket client.
 *
 * The daemon is about to send two kinds of frame down one socket: activity
 * events and a single HUD frame describing the observed root and its git
 * branch. The client is the only place that sees both, so it is the only place
 * that can misroute them -- feeding a meta frame to the simulation would add a
 * phantom node, and dropping it silently would leave the HUD blank forever.
 *
 * Two compatibility constraints matter as much as the routing itself: the
 * existing two-argument `createWsClient(onEvent, url)` call in main.ts must keep
 * compiling and working, and a meta frame arriving at a client that was given no
 * meta callback must be discarded in silence -- an exception in `onmessage`
 * would kill the frame handler for every event after it.
 *
 * Signature fixed by these tests (for the implementer):
 *   createWsClient(onEvent: EventSink, url?: string, options?: WsClientOptions)
 *   WsClientOptions gains `onMeta?: (meta: DaemonMeta) => void`
 * i.e. the meta sink rides in the SAME options object the class already takes
 * for backoff tuning, so no positional argument is added or reordered.
 *
 * Expected to FAIL until the client parses and routes meta frames.
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

const META_FRAME = JSON.stringify({
  kind: "meta",
  root: "~/projects/graph-agents",
  branch: "development",
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
});

describe("WebSocket client frame routing", () => {
  it("hands a meta frame to the meta callback, parsed", () => {
    const onEvent = vi.fn();
    const onMeta = vi.fn();
    const socket = connect(onEvent, { onMeta });

    socket.deliver(META_FRAME);

    expect(onMeta).toHaveBeenCalledTimes(1);
    expect(onMeta).toHaveBeenCalledWith({
      root: "~/projects/graph-agents",
      branch: "development",
    });
  });

  it("does not feed a meta frame to the event callback", () => {
    const onEvent = vi.fn();
    const onMeta = vi.fn();
    const socket = connect(onEvent, { onMeta });

    socket.deliver(META_FRAME);

    expect(onEvent).not.toHaveBeenCalled();
  });

  it("still routes a valid event to the event callback", () => {
    const onEvent = vi.fn();
    const onMeta = vi.fn();
    const socket = connect(onEvent, { onMeta });

    socket.deliver(EVENT_FRAME);

    expect(onEvent).toHaveBeenCalledTimes(1);
    expect((onEvent.mock.calls[0][0] as { path: string }).path).toBe("src/api/users.ts");
  });

  it("does not feed an event to the meta callback", () => {
    const onEvent = vi.fn();
    const onMeta = vi.fn();
    const socket = connect(onEvent, { onMeta });

    socket.deliver(EVENT_FRAME);

    expect(onMeta).not.toHaveBeenCalled();
  });

  it("drops a meta frame in silence when no meta callback was given", () => {
    const onEvent = vi.fn();
    const socket = connect(onEvent);

    expect(() => socket.deliver(META_FRAME)).not.toThrow();
    expect(onEvent).not.toHaveBeenCalled();
  });

  it("keeps delivering events after a meta frame with no meta callback", () => {
    const onEvent = vi.fn();
    const socket = connect(onEvent);

    socket.deliver(META_FRAME);
    socket.deliver(EVENT_FRAME);

    expect(onEvent).toHaveBeenCalledTimes(1);
  });

  it("drops malformed JSON in silence, touching neither callback", () => {
    const onEvent = vi.fn();
    const onMeta = vi.fn();
    const socket = connect(onEvent, { onMeta });

    expect(() => socket.deliver("{not json at all")).not.toThrow();
    expect(onEvent).not.toHaveBeenCalled();
    expect(onMeta).not.toHaveBeenCalled();
  });

  it("drops a non-string frame in silence", () => {
    const onEvent = vi.fn();
    const onMeta = vi.fn();
    const socket = connect(onEvent, { onMeta });

    expect(() => socket.deliver(new ArrayBuffer(8))).not.toThrow();
    expect(onEvent).not.toHaveBeenCalled();
    expect(onMeta).not.toHaveBeenCalled();
  });
});
