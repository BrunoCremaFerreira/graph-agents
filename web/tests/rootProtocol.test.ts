/**
 * Contract tests (RED) for the three frames the root switch adds to the wire.
 *
 * The defect: the observed root is fixed at daemon boot, so watching a second
 * checkout means restarting the daemon and reloading the page. The bar behind
 * ctrl+L makes the browser ask for a different root -- and the browser cannot
 * read the disk, so every answer comes back over the same WebSocket the events
 * arrive on. Three new frames, three new parsers next to `parseMeta`:
 *
 *   completion — what Tab expands to, plus the directories still possible;
 *   reset      — the root changed: empty the graph, the new tree is coming;
 *   rootError  — the path was refused, with a reason to show the user.
 *
 * They are discriminated by `kind`, the field activity events do not carry, and
 * the discrimination is the load-bearing part: a `reset` mistaken for an event
 * would grow a node called "reset" instead of clearing the screen, and an event
 * mistaken for a `reset` would wipe the graph on every file save.
 *
 * Two degradation rules, both following `parseMeta`'s precedent that a missing
 * field must not cost the whole frame:
 *
 *  - `matches` is a HINT. Absent, mistyped, or holding junk items, the
 *    completion still has a `completed` path worth adopting, so the list
 *    degrades to `[]` and the junk is dropped item by item.
 *  - a `reset` is never rejected for its payload. Dropping one leaves the old
 *    project's nodes on screen while the new project's tree streams in on top
 *    of it -- two trees in one graph, which is worse than a nameless root. Only
 *    a frame that is not a reset at all returns null.
 *
 * Expected to FAIL until parseCompletion / parseReset / parseRootError exist in
 * src/protocol.ts. One failure reason per test.
 */

import { describe, it, expect } from "vitest";
import {
  parseCompletion,
  parseReset,
  parseRootError,
  parseEvent,
  parseMeta,
  type RootCompletion,
  type RootReset,
  type RootError,
} from "../src/protocol";

/** A well-formed completion reply: "/home/brn/pro" expanded one segment. */
function validCompletion(): Record<string, unknown> {
  return {
    kind: "completion",
    path: "/home/brn/pro",
    completed: "/home/brn/projects/",
    matches: ["/home/brn/projects/", "/home/brn/proto/"],
  };
}

/** A well-formed reset: the daemon switched roots. */
function validReset(): Record<string, unknown> {
  return { kind: "reset", root: "/home/brn/projects/other" };
}

/** A well-formed refusal of a typed path. */
function validRootError(): Record<string, unknown> {
  return { kind: "rootError", path: "/nope", reason: "no such directory" };
}

/** The HUD frame that already shares this socket. */
function validMeta(): Record<string, unknown> {
  return { kind: "meta", root: "~/projects/graph-agents", branch: "development" };
}

/** A well-formed activity event, for the cross-parser tests. */
function validEvent(): Record<string, unknown> {
  return {
    ts: 1754870400.5,
    agent: "sess-abc",
    type: "M",
    path: "src/api/users.ts",
    color: "FFAA00",
  };
}

describe("parseCompletion", () => {
  it("parses a well-formed completion reply", () => {
    const parsed = parseCompletion(validCompletion()) as RootCompletion;

    expect(parsed).not.toBeNull();
    expect(parsed.path).toBe("/home/brn/pro");
    expect(parsed.completed).toBe("/home/brn/projects/");
    expect(parsed.matches).toEqual(["/home/brn/projects/", "/home/brn/proto/"]);
  });

  it("keeps the path the reply answers, which is how a stale reply is recognised", () => {
    // The user keeps typing while the daemon answers; without `path` the client
    // cannot tell an answer to the current field from an answer to two
    // keystrokes ago, and adopting the wrong one overwrites what was typed.
    const raw = validCompletion();
    raw.path = "/home/brn/projects/gra";

    expect((parseCompletion(raw) as RootCompletion).path).toBe("/home/brn/projects/gra");
  });

  it("degrades a missing matches list to an empty one instead of rejecting the frame", () => {
    const raw = validCompletion();
    delete raw.matches;

    const parsed = parseCompletion(raw) as RootCompletion;

    expect(parsed).not.toBeNull();
    expect(parsed.matches).toEqual([]);
  });

  it.each([
    ["a string", "/home/brn/projects/"],
    ["a number", 3],
    ["an object", { 0: "/home/brn/projects/" }],
    ["null", null],
  ])("degrades a matches field that is not an array (%s) to an empty list", (_label, bad) => {
    const raw = validCompletion();
    raw.matches = bad;

    const parsed = parseCompletion(raw) as RootCompletion;

    expect(parsed).not.toBeNull();
    expect(parsed.matches).toEqual([]);
  });

  it("drops the non-string items of a matches list, keeping the usable ones", () => {
    // A candidate that is not a string reaches the DOM as "[object Object]";
    // the rest of the list is still worth showing.
    const raw = validCompletion();
    raw.matches = ["/home/brn/projects/", 42, null, { path: "/x" }, "/home/brn/proto/"];

    expect((parseCompletion(raw) as RootCompletion).matches).toEqual([
      "/home/brn/projects/",
      "/home/brn/proto/",
    ]);
  });

  it("returns null when completed is missing, since there is nothing to adopt", () => {
    const raw = validCompletion();
    delete raw.completed;

    expect(parseCompletion(raw)).toBeNull();
  });

  it("returns null when path is missing, since the reply cannot be matched to the field", () => {
    const raw = validCompletion();
    delete raw.path;

    expect(parseCompletion(raw)).toBeNull();
  });

  it.each([
    ["a number", 42],
    ["an object", { path: "/home" }],
    ["null", null],
  ])("returns null when completed has the wrong type (%s)", (_label, bad) => {
    const raw = validCompletion();
    raw.completed = bad;

    expect(parseCompletion(raw)).toBeNull();
  });

  it.each([
    ["missing", undefined],
    ["reset", "reset"],
    ["Completion", "Completion"],
    ["a number", 1],
  ])("returns null when kind is not \"completion\" (%s)", (_label, badKind) => {
    const raw = validCompletion();
    if (badKind === undefined) delete raw.kind;
    else raw.kind = badKind;

    expect(parseCompletion(raw)).toBeNull();
  });

  it.each([
    ["null", null],
    ["undefined", undefined],
    ["a number", 5],
    ["a string", "hello"],
    ["an array", [{ kind: "completion", path: "/a", completed: "/a" }]],
  ])("returns null for a non-object input (%s)", (_label, value) => {
    expect(parseCompletion(value)).toBeNull();
  });

  it("never throws on malformed input", () => {
    expect(() => parseCompletion(undefined)).not.toThrow();
    expect(() => parseCompletion("garbage")).not.toThrow();
    expect(() => parseCompletion({ kind: "completion" })).not.toThrow();
    expect(() => parseCompletion([])).not.toThrow();
  });
});

describe("parseReset", () => {
  it("parses a well-formed reset frame, carrying the root now being observed", () => {
    const parsed = parseReset(validReset()) as RootReset;

    expect(parsed).not.toBeNull();
    expect(parsed.root).toBe("/home/brn/projects/other");
  });

  it("degrades a missing root to an empty string rather than dropping the reset", () => {
    // Dropping it would leave the old project's nodes on screen while the new
    // project's tree arrives on top: two trees in one graph. Clearing without a
    // name is recoverable; not clearing is not.
    const raw = validReset();
    delete raw.root;

    const parsed = parseReset(raw) as RootReset;

    expect(parsed).not.toBeNull();
    expect(parsed.root).toBe("");
  });

  it.each([
    ["a number", 42],
    ["an object", { path: "/home" }],
    ["null", null],
  ])("degrades a root of the wrong type (%s) to an empty string", (_label, bad) => {
    const raw = validReset();
    raw.root = bad;

    const parsed = parseReset(raw) as RootReset;

    expect(parsed).not.toBeNull();
    expect(parsed.root).toBe("");
  });

  it.each([
    ["missing", undefined],
    ["meta", "meta"],
    ["Reset", "Reset"],
    ["a number", 1],
  ])("returns null when kind is not \"reset\" (%s)", (_label, badKind) => {
    const raw = validReset();
    if (badKind === undefined) delete raw.kind;
    else raw.kind = badKind;

    expect(parseReset(raw)).toBeNull();
  });

  it.each([
    ["null", null],
    ["undefined", undefined],
    ["a number", 5],
    ["a string", "reset"],
    ["an array", [{ kind: "reset", root: "/x" }]],
  ])("returns null for a non-object input (%s)", (_label, value) => {
    expect(parseReset(value)).toBeNull();
  });

  it("never throws on malformed input", () => {
    expect(() => parseReset(undefined)).not.toThrow();
    expect(() => parseReset("garbage")).not.toThrow();
    expect(() => parseReset([])).not.toThrow();
  });
});

describe("parseRootError", () => {
  it("parses a well-formed refusal into the path and the reason", () => {
    const parsed = parseRootError(validRootError()) as RootError;

    expect(parsed).not.toBeNull();
    expect(parsed.path).toBe("/nope");
    expect(parsed.reason).toBe("no such directory");
  });

  it("degrades a missing reason to an empty string instead of swallowing the refusal", () => {
    // Without the frame the bar would close as if the root had been accepted,
    // and the graph would sit on the old project pretending to be the new one.
    const raw = validRootError();
    delete raw.reason;

    const parsed = parseRootError(raw) as RootError;

    expect(parsed).not.toBeNull();
    expect(parsed.reason).toBe("");
  });

  it.each([
    ["a number", 42],
    ["an object", { message: "nope" }],
    ["null", null],
  ])("degrades a reason of the wrong type (%s) to an empty string", (_label, bad) => {
    const raw = validRootError();
    raw.reason = bad;

    expect((parseRootError(raw) as RootError).reason).toBe("");
  });

  it("returns null when path is missing, since the refusal names no attempt", () => {
    const raw = validRootError();
    delete raw.path;

    expect(parseRootError(raw)).toBeNull();
  });

  it.each([
    ["missing", undefined],
    ["rooterror", "rooterror"],
    ["error", "error"],
    ["a number", 1],
  ])("returns null when kind is not \"rootError\" (%s)", (_label, badKind) => {
    const raw = validRootError();
    if (badKind === undefined) delete raw.kind;
    else raw.kind = badKind;

    expect(parseRootError(raw)).toBeNull();
  });

  it.each([
    ["null", null],
    ["undefined", undefined],
    ["a number", 5],
    ["a string", "rootError"],
    ["an array", [{ kind: "rootError", path: "/x", reason: "y" }]],
  ])("returns null for a non-object input (%s)", (_label, value) => {
    expect(parseRootError(value)).toBeNull();
  });

  it("never throws on malformed input", () => {
    expect(() => parseRootError(undefined)).not.toThrow();
    expect(() => parseRootError("garbage")).not.toThrow();
    expect(() => parseRootError([])).not.toThrow();
  });
});

describe("no parser accepts another's frame", () => {
  it("parseCompletion returns null for a reset frame", () => {
    expect(parseCompletion(validReset())).toBeNull();
  });

  it("parseCompletion returns null for a rootError frame", () => {
    expect(parseCompletion(validRootError())).toBeNull();
  });

  it("parseCompletion returns null for a meta frame", () => {
    expect(parseCompletion(validMeta())).toBeNull();
  });

  it("parseCompletion returns null for an activity event", () => {
    expect(parseCompletion(validEvent())).toBeNull();
  });

  it("parseReset returns null for a completion frame", () => {
    expect(parseReset(validCompletion())).toBeNull();
  });

  it("parseReset returns null for a rootError frame", () => {
    expect(parseReset(validRootError())).toBeNull();
  });

  it("parseReset returns null for a meta frame, even though both carry a root", () => {
    // The two frames are one word apart and mean opposite things: meta relabels
    // the HUD, reset throws the whole graph away.
    expect(parseReset(validMeta())).toBeNull();
  });

  it("parseReset returns null for an activity event, so a file save never wipes the graph", () => {
    expect(parseReset(validEvent())).toBeNull();
  });

  it("parseRootError returns null for a completion frame", () => {
    expect(parseRootError(validCompletion())).toBeNull();
  });

  it("parseRootError returns null for a reset frame", () => {
    expect(parseRootError(validReset())).toBeNull();
  });

  it("parseRootError returns null for an activity event", () => {
    expect(parseRootError(validEvent())).toBeNull();
  });
});

describe("the existing parsers refuse the new frames", () => {
  it("parseEvent returns null for a completion frame, so no node is named after a path prefix", () => {
    expect(parseEvent(validCompletion())).toBeNull();
  });

  it("parseEvent returns null for a reset frame", () => {
    expect(parseEvent(validReset())).toBeNull();
  });

  it("parseEvent returns null for a rootError frame", () => {
    expect(parseEvent(validRootError())).toBeNull();
  });

  it("parseMeta returns null for a reset frame, so a switch does not relabel the HUD twice", () => {
    expect(parseMeta(validReset())).toBeNull();
  });

  it("parseMeta returns null for a completion frame", () => {
    expect(parseMeta(validCompletion())).toBeNull();
  });

  it("parseMeta returns null for a rootError frame", () => {
    expect(parseMeta(validRootError())).toBeNull();
  });
});
