/**
 * WebSocket client: the network edge. It connects to the daemon, validates
 * every inbound frame with {@link parseEvent} (never trusting the wire), and
 * hands well-formed {@link AgentEvent}s to a sink. Malformed frames are dropped
 * silently. Dropped connections reconnect with capped exponential backoff.
 */

import {
  parseCompletion,
  parseEvent,
  parseFileView,
  parseMeta,
  parseReset,
  parseRootError,
  parseStatus,
  type AgentEvent,
  type DaemonMeta,
  type FileView,
  type GitStatus,
  type RootCompletion,
  type RootError,
  type RootReset,
} from "./protocol";

export type EventSink = (event: AgentEvent) => void;
export type MetaSink = (meta: DaemonMeta) => void;
export type CompletionSink = (completion: RootCompletion) => void;
export type ResetSink = (reset: RootReset) => void;
export type RootErrorSink = (error: RootError) => void;
export type FileViewSink = (view: FileView) => void;
export type StatusSink = (status: GitStatus) => void;

export interface WsClientOptions {
  /** Backoff floor / ceiling in ms. */
  readonly minDelayMs?: number;
  readonly maxDelayMs?: number;
  /** Where meta frames go. Absent means meta frames are dropped in silence. */
  readonly onMeta?: MetaSink;
  /**
   * The root-switch frames. All three ride this options object rather than new
   * positional arguments, so `createWsClient(onEvent, url)` keeps compiling, and
   * all three are dropped in silence when absent: a page built before these
   * frames existed still has to survive a daemon that sends them.
   */
  readonly onCompletion?: CompletionSink;
  readonly onReset?: ResetSink;
  readonly onRootError?: RootErrorSink;
  /**
   * The daemon's answer to a click on a file. Optional and consumed either way,
   * for the same reason as the frames above.
   */
  readonly onFileView?: FileViewSink;
  /**
   * The working tree's uncommitted changes. Optional and consumed either way,
   * for the same reason as the frames above: an old page against a new daemon
   * would otherwise see this frame fall through to `parseEvent`.
   */
  readonly onStatus?: StatusSink;
}

/** Used only outside a browser (tests, SSR); real pages derive from location. */
const FALLBACK_URL = "ws://localhost:8080/ws";

/**
 * Resolve the daemon URL, preferring the page's own origin.
 *
 * The daemon answers HTTP and the WebSocket on one port, so deriving the URL
 * from `window.location` means whatever host/port reached the page also reaches
 * the socket. That is what makes a tunnelled setup (SSH or VS Code port
 * forwarding) work with a single forwarded port -- a hard-coded `localhost`
 * would resolve to the *viewer's* machine and silently never connect.
 * `VITE_WS_URL` still overrides, for a Vite dev server on a different port.
 */
export function resolveWsUrl(): string {
  const fromEnv = import.meta.env?.VITE_WS_URL;
  if (typeof fromEnv === "string" && fromEnv.length > 0) return fromEnv;

  const location = typeof window !== "undefined" ? window.location : undefined;
  if (location?.host) {
    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    return `${scheme}//${location.host}/ws`;
  }
  return FALLBACK_URL;
}

export class WsClient {
  private socket: WebSocket | null = null;
  private closed = false;
  private delay: number;
  private readonly minDelay: number;
  private readonly maxDelay: number;
  private readonly onMeta: MetaSink | undefined;
  private readonly onCompletion: CompletionSink | undefined;
  private readonly onReset: ResetSink | undefined;
  private readonly onRootError: RootErrorSink | undefined;
  private readonly onFileView: FileViewSink | undefined;
  private readonly onStatus: StatusSink | undefined;

  constructor(
    private readonly url: string,
    private readonly onEvent: EventSink,
    options: WsClientOptions = {},
  ) {
    this.minDelay = options.minDelayMs ?? 500;
    this.maxDelay = options.maxDelayMs ?? 8000;
    this.onMeta = options.onMeta;
    this.onCompletion = options.onCompletion;
    this.onReset = options.onReset;
    this.onRootError = options.onRootError;
    this.onFileView = options.onFileView;
    this.onStatus = options.onStatus;
    this.delay = this.minDelay;
  }

  /** Open the connection and keep it alive across drops. */
  connect(): void {
    this.closed = false;
    this.open();
  }

  /** Stop reconnecting and close the socket. */
  disconnect(): void {
    this.closed = true;
    this.socket?.close();
    this.socket = null;
  }

  /**
   * Write one request (a completion or a root switch) as JSON.
   *
   * SILENT when there is nothing to write to: no socket yet, one closed by
   * `disconnect`, or one mid-backoff after the daemon restarted. This is called
   * straight from a key handler, and an exception thrown out of it leaves the
   * page with a dead keyboard — for a keystroke that could simply be dropped.
   */
  send(payload: object): void {
    const socket = this.socket;
    if (!socket) return;
    if (socket.readyState !== WebSocket.OPEN) return;
    try {
      socket.send(JSON.stringify(payload));
    } catch {
      // A socket that died between the check and the write, or a payload that
      // cannot be stringified: still not worth breaking the page over.
    }
  }

  private open(): void {
    const socket = new WebSocket(this.url);
    this.socket = socket;

    socket.onopen = (): void => {
      this.delay = this.minDelay;
    };
    socket.onmessage = (msg: MessageEvent): void => {
      this.handleMessage(msg.data);
    };
    socket.onclose = (): void => this.scheduleReconnect();
    socket.onerror = (): void => socket.close();
  }

  private handleMessage(data: unknown): void {
    if (typeof data !== "string") return;
    let raw: unknown;
    try {
      raw = JSON.parse(data);
    } catch {
      return;
    }
    const meta = parseMeta(raw);
    if (meta) {
      // A frame the HUD claims never reaches the simulation, with or without a
      // meta sink: routing it on as an event would grow a node for the root.
      this.onMeta?.(meta);
      return;
    }
    // The root-switch frames are routed BEFORE parseEvent, and — like meta —
    // consumed whether or not a sink was given. A reset delivered as an event
    // would grow a node named after the new root instead of clearing the old
    // project, which is the exact opposite of what the frame asks for.
    const reset = parseReset(raw);
    if (reset) {
      this.onReset?.(reset);
      return;
    }
    const completion = parseCompletion(raw);
    if (completion) {
      this.onCompletion?.(completion);
      return;
    }
    const rootError = parseRootError(raw);
    if (rootError) {
      this.onRootError?.(rootError);
      return;
    }
    // Also before `parseEvent`, and also consumed without a sink: a file's diff
    // is an answer about a path, not a change to it, and routing it on would
    // flash the file in the graph every time someone opened it.
    const fileView = parseFileView(raw);
    if (fileView) {
      this.onFileView?.(fileView);
      return;
    }
    // Before `parseEvent` too, and consumed with or without a sink: a status
    // frame is a statement about the working tree, not a change to a path, so
    // routing it on would grow a node called "status" in the graph — and the
    // poll repeats every couple of seconds, keeping it there forever.
    const status = parseStatus(raw);
    if (status) {
      this.onStatus?.(status);
      return;
    }
    const event = parseEvent(raw);
    if (event) this.onEvent(event);
  }

  private scheduleReconnect(): void {
    if (this.closed) return;
    const wait = this.delay;
    this.delay = Math.min(this.maxDelay, this.delay * 2);
    window.setTimeout(() => {
      if (!this.closed) this.open();
    }, wait);
  }
}

/** Convenience factory used by `main.ts`. */
export function createWsClient(
  onEvent: EventSink,
  url = resolveWsUrl(),
  options: WsClientOptions = {},
): WsClient {
  return new WsClient(url, onEvent, options);
}
