/**
 * Contract tests (RED) for the readable agent label on the wire.
 *
 * The defect: the HUD names an actor with `shortAgentName(agent)`, which cuts
 * the id at its last `-`. That was tolerable while `agent` was a session id, but
 * a tool call made by a SUBAGENT carries an OPAQUE `agent_id`, and chopping an
 * opaque id yields a slab of hexadecimal on screen -- two subagents look like
 * two random strings instead of "developer-backend" and
 * "developer-frontend". The hook payload also carries `agent_type`, the
 * human-readable name, so the daemon will start broadcasting it as a new
 * `label` field.
 *
 * Two things must stay straight, and confusing them is the easy mistake:
 *   - `agent` is the IDENTITY: the actor key and the seed of the actor's color.
 *     It stays the opaque id. Nothing here may push the label into that role,
 *     or two subagents of the same type would collapse into one actor.
 *   - `label` is TEXT. It is display-only, may be empty, and must never decide
 *     whether an event is drawable.
 *
 * Hence the degradation rules, mirroring what `origin` already does: a missing
 * or mistyped `label` degrades to `""` instead of rejecting the frame, so a
 * freshly built page served by an older daemon still draws every event it
 * receives -- just without a pretty name. An empty label is the legitimate
 * ORCHESTRATOR case: the main session's payload carries no `agent_type` at all.
 *
 * Expected to FAIL until `AgentEvent.label` exists and `parseEvent` fills it.
 * One failure reason per test.
 */

import { describe, it, expect } from "vitest";
import { parseEvent, type AgentEvent } from "../src/protocol";

/** A well-formed frame from a subagent: opaque id plus readable type. */
function subagentRaw(): Record<string, unknown> {
  return {
    ts: 1754870400.5,
    agent: "a1b2c3d4e5f60718",
    type: "M",
    path: "rhizome_graph/normalize.py",
    color: "FFAA00",
    origin: "hook",
    label: "developer-backend",
  };
}

describe("parseEvent: the readable agent label", () => {
  it("keeps a well-formed label so the screen can name the subagent", () => {
    const parsed = parseEvent(subagentRaw());

    expect(parsed).not.toBeNull();
    expect((parsed as AgentEvent).label).toBe("developer-backend");
  });

  it("keeps the opaque agent id as the identity, distinct from the label", () => {
    // The actor key and its color come from `agent`; if the label ever replaced
    // it, every subagent of a given type would merge into a single figure.
    const parsed = parseEvent(subagentRaw());

    expect((parsed as AgentEvent).agent).toBe("a1b2c3d4e5f60718");
  });

  it("degrades a missing label to an empty string instead of rejecting the event", () => {
    // A new page against an older daemon: everything must still be drawable.
    const raw = subagentRaw();
    delete raw.label;

    const parsed = parseEvent(raw);

    expect(parsed).not.toBeNull();
    expect((parsed as AgentEvent).label).toBe("");
  });

  it("accepts an empty label, which is the orchestrator with no agent_type", () => {
    const raw = subagentRaw();
    raw.label = "";

    const parsed = parseEvent(raw);

    expect(parsed).not.toBeNull();
    expect((parsed as AgentEvent).label).toBe("");
  });

  it.each([
    ["a number", 42],
    ["null", null],
    ["an object", { name: "developer-backend" }],
    ["an array", ["developer-backend"]],
    ["a boolean", true],
  ])("degrades a label of the wrong type (%s) to an empty string", (_case, badLabel) => {
    const raw = subagentRaw();
    raw.label = badLabel;

    const parsed = parseEvent(raw);

    expect(parsed).not.toBeNull();
    expect((parsed as AgentEvent).label).toBe("");
  });

  it("still parses the rest of the event when the label is garbage", () => {
    // Display text must never cost us the node: path, type and color survive.
    const raw = subagentRaw();
    raw.label = { junk: true };

    const parsed = parseEvent(raw) as AgentEvent;

    expect(parsed.path).toBe("rhizome_graph/normalize.py");
    expect(parsed.type).toBe("M");
    expect(parsed.color).toBe("FFAA00");
    expect(parsed.origin).toBe("hook");
  });

  it("never throws on a hostile label", () => {
    expect(() => parseEvent({ ...subagentRaw(), label: Symbol("x") })).not.toThrow();
  });
});

describe("parseEvent: a label does not weaken the rest of the validation", () => {
  it.each([
    ["ts", "not-a-number"],
    ["agent", 123],
    ["path", 42],
    ["color", 0xffaa00],
    ["type", 7],
  ])("still returns null when %s has the wrong type, label present", (field, badValue) => {
    const raw = subagentRaw();
    raw[field] = badValue;

    expect(parseEvent(raw)).toBeNull();
  });

  it("still returns null for an invalid type value when a label is present", () => {
    const raw = subagentRaw();
    raw.type = "X";

    expect(parseEvent(raw)).toBeNull();
  });

  it.each(["ts", "agent", "type", "path", "color"])(
    "still returns null when the required field %s is missing, label present",
    (field) => {
      const raw = subagentRaw();
      delete raw[field];

      expect(parseEvent(raw)).toBeNull();
    },
  );

  it("does not accept a meta frame just because it carries a label", () => {
    // The HUD frame shares the socket; a `label` on it must not make it look
    // like an event. Mirror of the parseMeta side in tests/meta.test.ts.
    expect(
      parseEvent({
        kind: "meta",
        root: "~/projects/rhizome-graph",
        branch: "development",
        label: "developer-backend",
      }),
    ).toBeNull();
  });
});
