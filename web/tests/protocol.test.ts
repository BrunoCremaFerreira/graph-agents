/**
 * Contract tests (RED) for parseEvent.
 *
 * They specify validation of raw WebSocket messages into AgentEvent. Expected
 * to FAIL until `developer-frontend` implements parseEvent (currently a
 * NotImplementedError stub). One failure reason per test.
 */

import { describe, it, expect } from "vitest";
import { parseEvent, AgentEvent } from "../src/protocol";

function validRaw(): Record<string, unknown> {
  return {
    ts: 1754870400.5,
    agent: "sess-abc",
    type: "M",
    path: "src/api/users.ts",
    color: "FFAA00",
  };
}

describe("parseEvent", () => {
  it("parses a well-formed message into an AgentEvent", () => {
    const parsed = parseEvent(validRaw());

    expect(parsed).not.toBeNull();
    const event = parsed as AgentEvent;
    expect(event.ts).toBe(1754870400.5);
    expect(event.agent).toBe("sess-abc");
    expect(event.type).toBe("M");
    expect(event.path).toBe("src/api/users.ts");
    expect(event.color).toBe("FFAA00");
  });

  it.each(["ts", "agent", "type", "path", "color"])(
    "returns null when the required field %s is missing",
    (field) => {
      const raw = validRaw();
      delete raw[field];

      expect(parseEvent(raw)).toBeNull();
    },
  );

  it.each([
    ["ts", "not-a-number"],
    ["agent", 123],
    ["path", 42],
    ["color", 0xffaa00],
    ["type", 7],
  ])("returns null when field %s has the wrong type", (field, badValue) => {
    const raw = validRaw();
    raw[field] = badValue;

    expect(parseEvent(raw)).toBeNull();
  });

  it("returns null for an invalid type value", () => {
    const raw = validRaw();
    raw.type = "X";

    expect(parseEvent(raw)).toBeNull();
  });

  it.each([
    ["null", null],
    ["undefined", undefined],
    ["a number", 5],
    ["a string", "hello"],
    ["an array", [1, 2, 3]],
  ])("returns null for a non-object input (%s)", (_label, value) => {
    expect(parseEvent(value)).toBeNull();
  });

  it("never throws on malformed input", () => {
    expect(() => parseEvent(undefined)).not.toThrow();
    expect(() => parseEvent("garbage")).not.toThrow();
    expect(() => parseEvent({ type: "Z" })).not.toThrow();
  });

  it("rejects the daemon's meta frame, which is not an event", () => {
    // The HUD frame shares the socket with events. If parseEvent ever accepted
    // one, the graph would grow a node for the observed root's own path.
    // Mirror of the parseMeta side in tests/meta.test.ts.
    expect(
      parseEvent({ kind: "meta", root: "~/projects/graph-agents", branch: "development" }),
    ).toBeNull();
  });
});
