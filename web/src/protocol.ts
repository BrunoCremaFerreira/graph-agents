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
 * A single activity event as received from the backend.
 *
 * Matches the JSON broadcast contract:
 * `{ ts, agent, type, path, color }` where `color` is a hex string without `#`.
 */
export interface AgentEvent {
  /** Unix time in seconds (float). */
  ts: number;
  /** Actor id (the backend's session-derived agent). */
  agent: string;
  /** Operation kind. */
  type: EventType;
  /** Path relative to the observed project root. */
  path: string;
  /** Hex color without a leading `#` (A->33FF33, M->FFAA00, D->FF3333). */
  color: string;
}

/** The three valid operation kinds, used for runtime validation. */
const EVENT_TYPES: ReadonlySet<string> = new Set<EventType>(["A", "M", "D"]);

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
 *   - NEVER throws: bad input from the network must be handled gracefully.
 *
 * @param raw The value received from the socket (already JSON-parsed or not).
 */
export function parseEvent(raw: unknown): AgentEvent | null {
  if (!isRecord(raw)) return null;

  const { ts, agent, type, path, color } = raw;

  if (!isFiniteNumber(ts)) return null;
  if (typeof agent !== "string") return null;
  if (typeof path !== "string") return null;
  if (typeof color !== "string") return null;
  if (typeof type !== "string" || !EVENT_TYPES.has(type)) return null;

  return { ts, agent, type: type as EventType, path, color };
}
