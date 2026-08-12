/**
 * WebSocket client: the network edge. It connects to the daemon, validates
 * every inbound frame with {@link parseEvent} (never trusting the wire), and
 * hands well-formed {@link AgentEvent}s to a sink. Malformed frames are dropped
 * silently. Dropped connections reconnect with capped exponential backoff.
 */

import { parseEvent, type AgentEvent } from "./protocol";

export type EventSink = (event: AgentEvent) => void;

export interface WsClientOptions {
  /** Backoff floor / ceiling in ms. */
  readonly minDelayMs?: number;
  readonly maxDelayMs?: number;
}

const DEFAULT_URL = "ws://localhost:8765";

/** Resolve the daemon URL from Vite env, falling back to the local default. */
export function resolveWsUrl(): string {
  const fromEnv = import.meta.env?.VITE_WS_URL;
  return typeof fromEnv === "string" && fromEnv.length > 0 ? fromEnv : DEFAULT_URL;
}

export class WsClient {
  private socket: WebSocket | null = null;
  private closed = false;
  private delay: number;
  private readonly minDelay: number;
  private readonly maxDelay: number;

  constructor(
    private readonly url: string,
    private readonly onEvent: EventSink,
    options: WsClientOptions = {},
  ) {
    this.minDelay = options.minDelayMs ?? 500;
    this.maxDelay = options.maxDelayMs ?? 8000;
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
export function createWsClient(onEvent: EventSink, url = resolveWsUrl()): WsClient {
  return new WsClient(url, onEvent);
}
