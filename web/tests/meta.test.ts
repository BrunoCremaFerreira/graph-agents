/**
 * Contract tests (RED) for parseMeta, the HUD's half of the wire protocol.
 *
 * The defect: the screen shows a graph with no idea *which* project it is. Two
 * terminals watching two checkouts render the same anonymous cloud of nodes,
 * and a demo recording is unattributable after the fact. The daemon will start
 * announcing the observed root and its git branch on the same WebSocket, so the
 * page needs a second parser next to `parseEvent` -- discriminated by a `kind`
 * field the events do not carry.
 *
 * Two properties are load-bearing and easy to get wrong:
 *   - `branch: null` is LEGITIMATE, not an error. It is the "observed directory
 *     is not a git repo" case, which must still reach the screen with its path.
 *   - a MISSING `branch` must degrade to null rather than reject the message:
 *     a freshly built page served by an older daemon still shows the path.
 * And the pair must not confuse each other: neither parser may accept the
 * other's message, or a meta frame would try to spawn a node named "meta".
 *
 * Expected to FAIL until `parseMeta` / `DaemonMeta` exist in src/protocol.ts.
 */

import { describe, it, expect } from "vitest";
import { parseMeta, type DaemonMeta } from "../src/protocol";

/** A well-formed meta frame as the daemon will send it. */
function validMeta(): Record<string, unknown> {
  return {
    kind: "meta",
    root: "~/projects/graph-agents",
    branch: "development",
  };
}

/**
 * A well-formed event frame, for the cross-parser test below. The mirror
 * assertion -- parseEvent must reject a meta frame -- lives in
 * tests/protocol.test.ts so it keeps running even while parseMeta is missing.
 */
function validEvent(): Record<string, unknown> {
  return {
    ts: 1754870400.5,
    agent: "sess-abc",
    type: "M",
    path: "src/api/users.ts",
    color: "FFAA00",
  };
}

/** A subagent's event frame: opaque id plus the readable `label`. */
function labelledEvent(): Record<string, unknown> {
  return { ...validEvent(), agent: "a1b2c3d4e5f60718", label: "desenvolvedor-backend" };
}

describe("parseMeta", () => {
  it("parses a well-formed meta frame into root and branch", () => {
    const parsed = parseMeta(validMeta());

    expect(parsed).not.toBeNull();
    const meta = parsed as DaemonMeta;
    expect(meta.root).toBe("~/projects/graph-agents");
    expect(meta.branch).toBe("development");
  });

  it("accepts a null branch as the legitimate not-a-git-repo case", () => {
    const raw = validMeta();
    raw.branch = null;

    const parsed = parseMeta(raw);

    expect(parsed).not.toBeNull();
    expect((parsed as DaemonMeta).root).toBe("~/projects/graph-agents");
    expect((parsed as DaemonMeta).branch).toBeNull();
  });

  it("degrades a missing branch to null instead of rejecting the frame", () => {
    const raw = validMeta();
    delete raw.branch;

    const parsed = parseMeta(raw);

    expect(parsed).not.toBeNull();
    expect((parsed as DaemonMeta).root).toBe("~/projects/graph-agents");
    expect((parsed as DaemonMeta).branch).toBeNull();
  });

  it.each([
    ["a number", 42],
    ["an object", { name: "main" }],
    ["an array", ["main"]],
  ])("degrades a branch of the wrong type (%s) to null", (_label, badBranch) => {
    const raw = validMeta();
    raw.branch = badBranch;

    const parsed = parseMeta(raw);

    expect(parsed).not.toBeNull();
    expect((parsed as DaemonMeta).branch).toBeNull();
  });

  it("returns null when root is missing", () => {
    const raw = validMeta();
    delete raw.root;

    expect(parseMeta(raw)).toBeNull();
  });

  it.each([
    ["a number", 42],
    ["an object", { path: "/home" }],
    ["an array", ["/home"]],
    ["null", null],
  ])("returns null when root has the wrong type (%s)", (_label, badRoot) => {
    const raw = validMeta();
    raw.root = badRoot;

    expect(parseMeta(raw)).toBeNull();
  });

  it("returns null when kind is missing", () => {
    const raw = validMeta();
    delete raw.kind;

    expect(parseMeta(raw)).toBeNull();
  });

  it.each([
    ["event", "event"],
    ["Meta", "Meta"],
    ["a number", 1],
  ])("returns null when kind is not \"meta\" (%s)", (_label, badKind) => {
    const raw = validMeta();
    raw.kind = badKind;

    expect(parseMeta(raw)).toBeNull();
  });

  it.each([
    ["null", null],
    ["undefined", undefined],
    ["a number", 5],
    ["a string", "hello"],
    ["an array", [{ kind: "meta", root: "/x" }]],
  ])("returns null for a non-object input (%s)", (_label, value) => {
    expect(parseMeta(value)).toBeNull();
  });

  it("never throws on malformed input", () => {
    expect(() => parseMeta(undefined)).not.toThrow();
    expect(() => parseMeta("garbage")).not.toThrow();
    expect(() => parseMeta({ kind: "meta" })).not.toThrow();
    expect(() => parseMeta([])).not.toThrow();
  });
});

describe("the two parsers never accept each other's frames", () => {
  it("parseMeta returns null for a valid event frame", () => {
    expect(parseMeta(validEvent())).toBeNull();
  });

  it("parseMeta returns null for an event carrying an agent label", () => {
    // `label` is display text on an EVENT. It must not be mistaken for part of
    // the HUD frame, or a subagent's edit would rewrite the project name.
    expect(parseMeta(labelledEvent())).toBeNull();
  });
});
