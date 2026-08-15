/**
 * Contract tests (RED) for the `fileView` frame the file viewer adds to the wire.
 *
 * The defect: the graph shows that a file changed and never what changed in it.
 * The browser cannot read the disk, so clicking a file asks the daemon
 * (`{kind:"file",path}`) and the answer comes back over the same WebSocket the
 * events arrive on: `{kind:"fileView",path,mode,content,truncated,error}`, where
 * `mode` is the daemon's fallback chain -- the `git diff` of the file, else its
 * text, else a hex dump when it is binary.
 *
 * This parser sits next to `parseMeta` and `parseCompletion` and follows their
 * contract exactly, because the frames share one socket:
 *
 *  - discrimination by `kind` is load-bearing. A `fileView` mistaken for an
 *    activity event would grow a node named after a diff hunk; an event mistaken
 *    for a `fileView` would drop a modal on screen mid-session.
 *  - `path` is required. A pane of text that names no file cannot be matched to
 *    the click that asked for it, and `applyView` would paint one file's diff
 *    under another file's name.
 *  - everything else DEGRADES rather than costing the frame, as `parseMeta`'s
 *    `branch` does. An unknown `mode` from a newer daemon still has content
 *    worth showing, so it falls back to `"text"` -- the one rendering that is
 *    never actively wrong; a missing `content` or `error` becomes `""`; a
 *    non-boolean `truncated` becomes `false`. Dropping the frame instead would
 *    leave the panel spinning on `loading` forever, with no reply ever coming.
 *
 * Expected to FAIL until parseFileView exists in src/protocol.ts. One failure
 * reason per test.
 */

import { describe, it, expect } from "vitest";
import {
  parseFileView,
  parseEvent,
  parseMeta,
  parseCompletion,
  parseReset,
  parseRootError,
  type FileView,
} from "../src/protocol";

/** A well-formed answer: the diff of a file the user clicked. */
function validFileView(): Record<string, unknown> {
  return {
    kind: "fileView",
    path: "web/src/renderer.ts",
    mode: "diff",
    content: "@@ -1,3 +1,4 @@\n+const x = 1;\n",
    truncated: false,
    error: "",
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

describe("parseFileView", () => {
  it("parses a well-formed answer", () => {
    const parsed = parseFileView(validFileView()) as FileView;

    expect(parsed).not.toBeNull();
    expect(parsed.path).toBe("web/src/renderer.ts");
    expect(parsed.mode).toBe("diff");
    expect(parsed.content).toBe("@@ -1,3 +1,4 @@\n+const x = 1;\n");
    expect(parsed.truncated).toBe(false);
    expect(parsed.error).toBe("");
  });

  it.each([["diff"], ["text"], ["hex"]])(
    "keeps the mode the daemon chose (%s), since the three are not rendered the same way",
    (mode) => {
      const raw = validFileView();
      raw.mode = mode;

      expect((parseFileView(raw) as FileView).mode).toBe(mode);
    },
  );

  it("degrades a missing mode to text rather than dropping the frame", () => {
    const raw = validFileView();
    delete raw.mode;

    expect((parseFileView(raw) as FileView).mode).toBe("text");
  });

  it.each([
    ["an unknown name from a newer daemon", "image"],
    ["a capitalised name", "Diff"],
    ["a number", 3],
    ["null", null],
  ])("degrades an unusable mode (%s) to text, which is never actively wrong", (_label, bad) => {
    const raw = validFileView();
    raw.mode = bad;

    const parsed = parseFileView(raw) as FileView;

    expect(parsed).not.toBeNull();
    expect(parsed.mode).toBe("text");
  });

  it("degrades a missing content to an empty string, so the panel stops loading either way", () => {
    // Rejecting the frame would leave the viewer spinning forever, because no
    // second reply is coming for that click.
    const raw = validFileView();
    delete raw.content;

    const parsed = parseFileView(raw) as FileView;

    expect(parsed).not.toBeNull();
    expect(parsed.content).toBe("");
  });

  it.each([
    ["a number", 42],
    ["an object", { text: "hi" }],
    ["null", null],
  ])("degrades a content of the wrong type (%s) to an empty string", (_label, bad) => {
    const raw = validFileView();
    raw.content = bad;

    expect((parseFileView(raw) as FileView).content).toBe("");
  });

  it("degrades a missing error to an empty string, which is the success case", () => {
    const raw = validFileView();
    delete raw.error;

    expect((parseFileView(raw) as FileView).error).toBe("");
  });

  it("keeps the reason when the daemon refused to read the file", () => {
    // "not a text file" / "no such path" is the only thing the user gets when
    // the content is empty on purpose.
    const raw = validFileView();
    raw.content = "";
    raw.error = "no such path";

    expect((parseFileView(raw) as FileView).error).toBe("no such path");
  });

  it("keeps a truncation flag, so the panel can say the output was cut", () => {
    const raw = validFileView();
    raw.truncated = true;

    expect((parseFileView(raw) as FileView).truncated).toBe(true);
  });

  it("degrades a missing truncated flag to false", () => {
    const raw = validFileView();
    delete raw.truncated;

    expect((parseFileView(raw) as FileView).truncated).toBe(false);
  });

  it.each([
    ["the string \"true\"", "true"],
    ["1", 1],
    ["null", null],
    ["an object", {}],
  ])("degrades a non-boolean truncated (%s) to false", (_label, bad) => {
    // A truthy non-boolean would put an "output cut" notice over content that is
    // whole, which is a lie about what the user is reading.
    const raw = validFileView();
    raw.truncated = bad;

    expect((parseFileView(raw) as FileView).truncated).toBe(false);
  });

  it("returns null when path is missing, since the answer cannot be matched to the click", () => {
    const raw = validFileView();
    delete raw.path;

    expect(parseFileView(raw)).toBeNull();
  });

  it.each([
    ["a number", 42],
    ["an object", { path: "a.ts" }],
    ["null", null],
  ])("returns null when path has the wrong type (%s)", (_label, bad) => {
    const raw = validFileView();
    raw.path = bad;

    expect(parseFileView(raw)).toBeNull();
  });

  it.each([
    ["missing", undefined],
    ["file", "file"],
    ["FileView", "FileView"],
    ["meta", "meta"],
    ["a number", 1],
  ])("returns null when kind is not \"fileView\" (%s)", (_label, badKind) => {
    const raw = validFileView();
    if (badKind === undefined) delete raw.kind;
    else raw.kind = badKind;

    expect(parseFileView(raw)).toBeNull();
  });

  it.each([
    ["null", null],
    ["undefined", undefined],
    ["a number", 5],
    ["a string", "fileView"],
    ["an array", [{ kind: "fileView", path: "a.ts" }]],
  ])("returns null for a non-object input (%s)", (_label, value) => {
    expect(parseFileView(value)).toBeNull();
  });

  it("never throws on malformed input", () => {
    expect(() => parseFileView(undefined)).not.toThrow();
    expect(() => parseFileView("garbage")).not.toThrow();
    expect(() => parseFileView({ kind: "fileView" })).not.toThrow();
    expect(() => parseFileView([])).not.toThrow();
  });
});

describe("parseFileView refuses the frames that already share the socket", () => {
  it("returns null for an activity event, so a file save never opens a viewer", () => {
    expect(parseFileView(validEvent())).toBeNull();
  });

  it("returns null for a meta frame", () => {
    expect(parseFileView(validMeta())).toBeNull();
  });

  it("returns null for a completion frame, even though both carry a path", () => {
    expect(parseFileView(validCompletion())).toBeNull();
  });

  it("returns null for a reset frame", () => {
    expect(parseFileView(validReset())).toBeNull();
  });

  it("returns null for a rootError frame, even though both carry a path and an error", () => {
    expect(parseFileView(validRootError())).toBeNull();
  });
});

describe("the existing parsers refuse the fileView frame", () => {
  it("parseEvent returns null, so no node is named after a diff hunk", () => {
    expect(parseEvent(validFileView())).toBeNull();
  });

  it("parseMeta returns null, so a viewed file does not relabel the HUD", () => {
    expect(parseMeta(validFileView())).toBeNull();
  });

  it("parseCompletion returns null, so a file's content is never typed into the root bar", () => {
    expect(parseCompletion(validFileView())).toBeNull();
  });

  it("parseReset returns null, so opening a file does not wipe the graph", () => {
    expect(parseReset(validFileView())).toBeNull();
  });

  it("parseRootError returns null, so a file's error does not accuse the observed root", () => {
    expect(parseRootError(validFileView())).toBeNull();
  });
});
