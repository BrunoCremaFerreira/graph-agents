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
      parseEvent({ kind: "meta", root: "~/projects/rhizome-graph", branch: "development" }),
    ).toBeNull();
  });
});

/**
 * A fourth operation kind: `R`, an agent READING a file.
 *
 * The defect it exists for is that reading is most of what an agent does, and
 * none of it was visible: an agent could spend a minute walking a package it
 * never wrote a byte to, and the graph showed a dead tree with a figure standing
 * still. The daemon now emits `R` with the violet `AA66FF`, on the same socket
 * as everything else.
 *
 * The validation is the part worth pinning here. `EVENT_TYPES` is a closed set
 * on purpose, and adding a member to it is exactly the change that tends to be
 * made by loosening the check instead of widening the set -- after which a
 * status frame, a typo, or a lowercase `r` from a daemon speaking some other
 * dialect all reach the simulation and grow a node in the graph.
 */
describe("parseEvent: the read event", () => {
  it("parses a read event, keeping its type as R", () => {
    const raw = { ...validRaw(), type: "R", color: "AA66FF", path: "src/api/users.ts" };

    const parsed = parseEvent(raw);

    expect(parsed).not.toBeNull();
    const event = parsed as AgentEvent;
    expect(event.type).toBe("R");
    expect(event.color).toBe("AA66FF");
    expect(event.path).toBe("src/api/users.ts");
  });

  it("carries agent, timestamp, origin and label through a read exactly as through a write", () => {
    const parsed = parseEvent({
      ts: 1754870400.5,
      agent: "sub-42",
      type: "R",
      path: "docs/guide.md",
      color: "AA66FF",
      origin: "watch",
      label: "developer-tester",
    });

    expect(parsed).toMatchObject({
      ts: 1754870400.5,
      agent: "sub-42",
      path: "docs/guide.md",
      origin: "watch",
      label: "developer-tester",
    });
  });

  it.each(["X", "r", "RR", "R ", "READ", ""])(
    "still returns null for the unknown type %j, so the new member did not open the gate",
    (type) => {
      const raw = { ...validRaw(), type };

      expect(parseEvent(raw)).toBeNull();
    },
  );
});
