/**
 * Wire protocol for events pushed from the backend over the WebSocket.
 *
 * STUB written by the tester agent for the RED phase of TDD. The `AgentEvent`
 * type fixes the shared contract; `parseEvent` is left unimplemented so the
 * specifying tests fail for the right reason. Implementation belongs to
 * `desenvolvedor-frontend`.
 */

/** Operation kind: Added, Modified, Deleted. */
export type EventType = "A" | "M" | "D";

/**
 * What produced the event.
 *
 * `hook`  — a Claude Code tool call: live activity with a known agent.
 * `seed`  — part of the project tree snapshot the daemon sends on connect.
 *           Backdrop: it must not flash, and it belongs to no agent.
 * `watch` — a change the filesystem watcher saw. Real activity, but the agent
 *           may be empty when it could not be attributed to one.
 */
export type EventOrigin = "hook" | "seed" | "watch";

/**
 * A single activity event as received from the backend.
 *
 * Matches the JSON broadcast contract:
 * `{ ts, agent, type, path, color }` where `color` is a hex string without `#`.
 */
export interface AgentEvent {
  /** Unix time in seconds (float). */
  ts: number;
  /** Actor id (the backend's session-derived agent); `""` when unattributed. */
  agent: string;
  /** Operation kind. */
  type: EventType;
  /** Path relative to the observed project root. */
  path: string;
  /** Hex color without a leading `#` (A->33FF33, M->FFAA00, D->FF3333). */
  color: string;
  /** Where the event came from. Absent on the wire means `"hook"`. */
  origin: EventOrigin;
  /**
   * Human-readable name of the actor (the hook's `agent_type`, e.g.
   * `"desenvolvedor-backend"`), for DISPLAY only. Never an identity: `agent`
   * remains the actor key and the seed of its color, so two subagents of the
   * same type stay two figures. Absent on the wire means `""`, which is also
   * the legitimate orchestrator case (its payload carries no `agent_type`).
   */
  label: string;
}

/** The three valid operation kinds, used for runtime validation. */
const EVENT_TYPES: ReadonlySet<string> = new Set<EventType>(["A", "M", "D"]);

/** Valid origins. Anything else on the wire degrades to `"hook"`. */
const EVENT_ORIGINS: ReadonlySet<string> = new Set<EventOrigin>(["hook", "seed", "watch"]);

/** Type guard narrowing an unknown value to a plain (non-array) object. */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Whether `value` is a real finite number (rejects NaN and strings). */
function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/**
 * Validate and parse a raw WebSocket message into an {@link AgentEvent}.
 *
 * Contract (see tests/protocol.test.ts):
 *   - Returns a fully-typed `AgentEvent` for a well-formed message.
 *   - Returns `null` for a non-object, a missing/mistyped field, or an invalid
 *     `type` value.
 *   - An absent or unrecognized `origin` degrades to `"hook"` rather than
 *     rejecting the event, so a page served from a newer or older daemon than
 *     the one broadcasting still draws everything it receives.
 *   - An absent or mistyped `label` degrades to `""` for the same reason: it is
 *     display text, never a reason to drop a frame. A page built against the
 *     daemon that broadcasts it still draws every event from one that does not
 *     (and vice versa) — just without a readable name for the actor.
 *   - NEVER throws: bad input from the network must be handled gracefully.
 *
 * @param raw The value received from the socket (already JSON-parsed or not).
 */
export function parseEvent(raw: unknown): AgentEvent | null {
  if (!isRecord(raw)) return null;

  const { ts, agent, type, path, color, origin, label } = raw;

  if (!isFiniteNumber(ts)) return null;
  if (typeof agent !== "string") return null;
  if (typeof path !== "string") return null;
  if (typeof color !== "string") return null;
  if (typeof type !== "string" || !EVENT_TYPES.has(type)) return null;

  const resolvedOrigin =
    typeof origin === "string" && EVENT_ORIGINS.has(origin)
      ? (origin as EventOrigin)
      : "hook";

  return {
    ts,
    agent,
    type: type as EventType,
    path,
    color,
    origin: resolvedOrigin,
    label: typeof label === "string" ? label : "",
  };
}

/**
 * What the daemon is observing, announced on the same socket as the events.
 *
 * Discriminated from {@link AgentEvent} by a `kind: "meta"` field the events do
 * not carry, so neither parser can accept the other's frame.
 */
export interface DaemonMeta {
  /** The observed project root, as the daemon wants it displayed. */
  root: string;
  /** Current git branch, or `null` when the root is not a git repository. */
  branch: string | null;
}

/**
 * Validate and parse a raw WebSocket message into a {@link DaemonMeta}.
 *
 * Contract (see tests/meta.test.ts):
 *   - Returns `null` for a non-object, for a missing/mistyped `root`, and for
 *     anything whose `kind` is not exactly `"meta"` (which is what keeps an
 *     activity event out).
 *   - A missing or mistyped `branch` degrades to `null` instead of rejecting
 *     the frame: `null` is the legitimate not-a-git-repo case anyway, and a
 *     page served by an older daemon must still show the path.
 *   - NEVER throws.
 *
 * @param raw The value received from the socket (already JSON-parsed or not).
 */
export function parseMeta(raw: unknown): DaemonMeta | null {
  if (!isRecord(raw)) return null;

  const { kind, root, branch } = raw;

  if (kind !== "meta") return null;
  if (typeof root !== "string") return null;

  return { root, branch: typeof branch === "string" ? branch : null };
}

/** Marker standing in for the elided middle of a truncated string. */
const ELISION = "…";

/**
 * Shorten `text` to at most `max` characters by eliding its MIDDLE.
 *
 * Clipping the tail (what CSS ellipsis does) would throw away the segment that
 * names the project — every checkout under `~/projects` renders identically.
 * Head and tail both survive here; the cut lands between them.
 *
 * When it has to cut, the result is exactly `max` characters long. `max <= 0`,
 * `NaN`, and empty text all yield `""` rather than throwing.
 */
export function truncateMiddle(text: string, max: number): string {
  if (text.length === 0) return "";
  if (!Number.isFinite(max) || max <= 0) return "";

  const limit = Math.floor(max);
  if (text.length <= limit) return text;
  if (limit <= ELISION.length) return text.slice(0, limit);

  const keep = limit - ELISION.length;
  const head = Math.ceil(keep / 2);
  const tail = keep - head;

  return text.slice(0, head) + ELISION + (tail > 0 ? text.slice(text.length - tail) : "");
}
