/**
 * Contract tests (RED) for the `status` frame behind the HUD's git status panel.
 *
 * The defect: the graph shows a file lighting up when it changes, and nothing at
 * all about whether that change is still uncommitted. After a long session the
 * one question the user actually has -- "what is dirty right now?" -- can only be
 * answered by leaving the page for a terminal. The browser cannot read the disk
 * (nor run `git`), so the daemon pushes the answer down the same WebSocket the
 * events arrive on: `{kind:"status",repo,truncated,entries:[{path,state}]}`.
 *
 * This parser sits next to `parseMeta`, `parseFileView` and `parseReset` and
 * follows their contract exactly, because all of them share one socket:
 *
 *  - the `kind` gate is load-bearing in BOTH directions. A status frame routed
 *    as an activity event would grow a node called "status" in the graph; an
 *    activity event mistaken for a status frame would repaint the whole panel
 *    from a single file save. Each parser must refuse the other's frame.
 *  - `entries` DEGRADES rather than costing the frame, as `parseCompletion`'s
 *    `matches` does: absent or mistyped it becomes `[]`, and a junk item is
 *    dropped ONE AT A TIME. A newer daemon adding a fifth state must not blank
 *    the panel for the four files this page does understand -- a partial list is
 *    a smaller lie than an empty one, which reads as "the tree is clean".
 *  - `repo` and `truncated` are booleans or they are false. A truthy non-boolean
 *    would claim the output was cut when it was whole, or claim a git repo where
 *    there is none.
 *  - NEVER throws: this comes off the network.
 *
 * Expected to FAIL until parseStatus exists in src/protocol.ts. One failure
 * reason per test.
 */

import { describe, it, expect } from "vitest";
import {
  parseStatus,
  parseEvent,
  parseMeta,
  parseCompletion,
  parseReset,
  parseRootError,
  parseFileView,
  type GitStatus,
} from "../src/protocol";

/** A well-formed status frame: a dirty working tree with one file per state. */
function validStatus(): Record<string, unknown> {
  return {
    kind: "status",
    repo: true,
    truncated: false,
    entries: [
      { path: "web/src/renderer.ts", state: "modified" },
      { path: "web/src/statusHud.ts", state: "added" },
      { path: "docs/old.md", state: "deleted" },
      { path: "scratch.txt", state: "untracked" },
    ],
  };
}

/** The frames that already share this socket. */
function validEvent(): Record<string, unknown> {
  return {
    ts: 1754870400.5,
    agent: "sess-abc",
    type: "M",
    path: "web/src/renderer.ts",
    color: "FFAA00",
  };
}

function validMeta(): Record<string, unknown> {
  return { kind: "meta", root: "~/projects/rhizome-graph", branch: "development" };
}

function validCompletion(): Record<string, unknown> {
  return {
    kind: "completion",
    path: "/home/brn/pro",
    completed: "/home/brn/projects/",
    matches: ["/home/brn/projects/"],
  };
}

function validReset(): Record<string, unknown> {
  return { kind: "reset", root: "/home/brn/projects/other" };
}

function validRootError(): Record<string, unknown> {
  return { kind: "rootError", path: "/nope", reason: "no such directory" };
}

function validFileView(): Record<string, unknown> {
  return {
    kind: "fileView",
    path: "web/src/renderer.ts",
    mode: "diff",
    content: "@@ -1,3 +1,4 @@\n",
    truncated: false,
    error: "",
  };
}

describe("parseStatus", () => {
  it("parses a well-formed frame", () => {
    const parsed = parseStatus(validStatus());

    expect(parsed).not.toBeNull();
    const status = parsed as GitStatus;
    expect(status.repo).toBe(true);
    expect(status.truncated).toBe(false);
    expect(status.entries).toEqual([
      { path: "web/src/renderer.ts", state: "modified" },
      { path: "web/src/statusHud.ts", state: "added" },
      { path: "docs/old.md", state: "deleted" },
      { path: "scratch.txt", state: "untracked" },
    ]);
  });

  it.each([["untracked"], ["modified"], ["added"], ["deleted"]])(
    "keeps the state the daemon reported (%s), since the four are shown differently",
    (state) => {
      const raw = validStatus();
      raw.entries = [{ path: "a.ts", state }];

      expect((parseStatus(raw) as GitStatus).entries).toEqual([{ path: "a.ts", state }]);
    },
  );

  it("preserves the order the daemon sent, leaving presentation to statusList", () => {
    const raw = validStatus();
    raw.entries = [
      { path: "z.ts", state: "untracked" },
      { path: "a.ts", state: "modified" },
    ];

    expect((parseStatus(raw) as GitStatus).entries.map((e) => e.path)).toEqual([
      "z.ts",
      "a.ts",
    ]);
  });

  it("reports a clean tree as an empty list, not as a dropped frame", () => {
    const raw = validStatus();
    raw.entries = [];

    const status = parseStatus(raw) as GitStatus;

    expect(status).not.toBeNull();
    expect(status.entries).toEqual([]);
    expect(status.repo).toBe(true);
  });

  it("degrades a missing entries list to an empty one", () => {
    const raw = validStatus();
    delete raw.entries;

    expect((parseStatus(raw) as GitStatus).entries).toEqual([]);
  });

  it.each([
    ["a string", "modified"],
    ["a number", 3],
    ["an object", { "a.ts": "modified" }],
    ["null", null],
  ])("degrades a non-array entries (%s) to an empty list", (_label, bad) => {
    const raw = validStatus();
    raw.entries = bad;

    const status = parseStatus(raw) as GitStatus;

    expect(status).not.toBeNull();
    expect(status.entries).toEqual([]);
  });

  it.each([
    ["a string", "web/src/a.ts"],
    ["a number", 7],
    ["null", null],
    ["an array", ["a.ts", "modified"]],
  ])("drops an item that is not an object (%s) without losing the frame", (_label, bad) => {
    const raw = validStatus();
    raw.entries = [bad, { path: "a.ts", state: "modified" }];

    expect((parseStatus(raw) as GitStatus).entries).toEqual([
      { path: "a.ts", state: "modified" },
    ]);
  });

  it("drops an item with no path, since a row that names no file cannot be shown", () => {
    const raw = validStatus();
    raw.entries = [{ state: "modified" }, { path: "a.ts", state: "added" }];

    expect((parseStatus(raw) as GitStatus).entries).toEqual([
      { path: "a.ts", state: "added" },
    ]);
  });

  it.each([
    ["a number", 42],
    ["null", null],
    ["an object", { path: "a.ts" }],
  ])("drops an item whose path has the wrong type (%s)", (_label, bad) => {
    const raw = validStatus();
    raw.entries = [{ path: bad, state: "modified" }, { path: "a.ts", state: "modified" }];

    expect((parseStatus(raw) as GitStatus).entries).toEqual([
      { path: "a.ts", state: "modified" },
    ]);
  });

  it.each([
    ["a state this page does not know", "renamed"],
    ["a capitalised state", "Modified"],
    ["a porcelain code", "??"],
    ["an empty string", ""],
  ])("drops an item whose state is outside the four words (%s)", (_label, bad) => {
    const raw = validStatus();
    raw.entries = [{ path: "new.ts", state: bad }, { path: "a.ts", state: "modified" }];

    expect((parseStatus(raw) as GitStatus).entries).toEqual([
      { path: "a.ts", state: "modified" },
    ]);
  });

  it.each([
    ["missing", undefined],
    ["a number", 1],
    ["null", null],
    ["an object", { state: "modified" }],
  ])("drops an item whose state is not a string (%s)", (_label, bad) => {
    const raw = validStatus();
    const item: Record<string, unknown> = { path: "new.ts" };
    if (bad !== undefined) item.state = bad;
    raw.entries = [item, { path: "a.ts", state: "deleted" }];

    expect((parseStatus(raw) as GitStatus).entries).toEqual([
      { path: "a.ts", state: "deleted" },
    ]);
  });

  it("keeps every valid item around a run of junk, dropping one at a time", () => {
    const raw = validStatus();
    raw.entries = [
      { path: "keep-1.ts", state: "modified" },
      "junk",
      { path: "bad", state: "renamed" },
      null,
      { path: "keep-2.ts", state: "untracked" },
    ];

    expect((parseStatus(raw) as GitStatus).entries).toEqual([
      { path: "keep-1.ts", state: "modified" },
      { path: "keep-2.ts", state: "untracked" },
    ]);
  });

  it("keeps repo true when the daemon says the root is a git repository", () => {
    expect((parseStatus(validStatus()) as GitStatus).repo).toBe(true);
  });

  it("reports repo false for a root that is not a git repository", () => {
    const raw = validStatus();
    raw.repo = false;
    raw.entries = [];

    expect((parseStatus(raw) as GitStatus).repo).toBe(false);
  });

  it.each([
    ["missing", undefined],
    ["the string \"true\"", "true"],
    ["1", 1],
    ["null", null],
    ["an object", {}],
  ])("degrades a non-boolean repo (%s) to false", (_label, bad) => {
    const raw = validStatus();
    if (bad === undefined) delete raw.repo;
    else raw.repo = bad;

    const status = parseStatus(raw) as GitStatus;

    expect(status).not.toBeNull();
    expect(status.repo).toBe(false);
  });

  it("keeps a truncation flag, so the panel can say the list was cut", () => {
    const raw = validStatus();
    raw.truncated = true;

    expect((parseStatus(raw) as GitStatus).truncated).toBe(true);
  });

  it.each([
    ["missing", undefined],
    ["the string \"true\"", "true"],
    ["1", 1],
    ["null", null],
    ["an object", {}],
  ])("degrades a non-boolean truncated (%s) to false", (_label, bad) => {
    // A truthy non-boolean would put a "list cut" notice over a list that is
    // whole, which is a lie about what the user is reading.
    const raw = validStatus();
    if (bad === undefined) delete raw.truncated;
    else raw.truncated = bad;

    expect((parseStatus(raw) as GitStatus).truncated).toBe(false);
  });

  it.each([
    ["missing", undefined],
    ["gitStatus", "gitStatus"],
    ["Status", "Status"],
    ["meta", "meta"],
    ["a number", 1],
  ])("returns null when kind is not \"status\" (%s)", (_label, badKind) => {
    const raw = validStatus();
    if (badKind === undefined) delete raw.kind;
    else raw.kind = badKind;

    expect(parseStatus(raw)).toBeNull();
  });

  it.each([
    ["null", null],
    ["undefined", undefined],
    ["a number", 5],
    ["a string", "status"],
    ["an array", [{ kind: "status", entries: [] }]],
  ])("returns null for a non-object input (%s)", (_label, value) => {
    expect(parseStatus(value)).toBeNull();
  });

  it("never throws on malformed input", () => {
    expect(() => parseStatus(undefined)).not.toThrow();
    expect(() => parseStatus("garbage")).not.toThrow();
    expect(() => parseStatus({ kind: "status" })).not.toThrow();
    expect(() => parseStatus({ kind: "status", entries: [undefined, null] })).not.toThrow();
    expect(() => parseStatus([])).not.toThrow();
  });
});

describe("parseStatus refuses the frames that already share the socket", () => {
  it("returns null for an activity event, so one file save never repaints the panel", () => {
    expect(parseStatus(validEvent())).toBeNull();
  });

  it("returns null for a meta frame", () => {
    expect(parseStatus(validMeta())).toBeNull();
  });

  it("returns null for a completion frame", () => {
    expect(parseStatus(validCompletion())).toBeNull();
  });

  it("returns null for a reset frame", () => {
    expect(parseStatus(validReset())).toBeNull();
  });

  it("returns null for a rootError frame", () => {
    expect(parseStatus(validRootError())).toBeNull();
  });

  it("returns null for a fileView frame, even though both carry a truncated flag", () => {
    expect(parseStatus(validFileView())).toBeNull();
  });
});

describe("the existing parsers refuse the status frame", () => {
  it("parseEvent returns null, so no node is ever named after a status frame", () => {
    expect(parseEvent(validStatus())).toBeNull();
  });

  it("parseMeta returns null, so a dirty tree does not relabel the HUD", () => {
    expect(parseMeta(validStatus())).toBeNull();
  });

  it("parseCompletion returns null, so a status frame is never typed into the root bar", () => {
    expect(parseCompletion(validStatus())).toBeNull();
  });

  it("parseReset returns null, so a status poll does not wipe the graph", () => {
    expect(parseReset(validStatus())).toBeNull();
  });

  it("parseRootError returns null, so a dirty tree does not accuse the observed root", () => {
    expect(parseRootError(validStatus())).toBeNull();
  });

  it("parseFileView returns null, so a status frame does not open the file viewer", () => {
    expect(parseFileView(validStatus())).toBeNull();
  });
});
