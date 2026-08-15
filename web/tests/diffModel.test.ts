/**
 * Contract tests (RED) for the parser behind the diff view.
 *
 * The defect: clicking a changed file shows its `git diff` as three colours of
 * TEXT and nothing else -- no line numbers, no full-width stripe, and no way to
 * tell a file header apart from a line of content that happens to begin with
 * `+++`. `diffLineClass` in `fileViewHud.ts` gets that last part actively wrong
 * today: it paints `--- a/x` as a deletion and `+++ b/x` as an addition, and
 * once there is a gutter those two headers would be handed LINE NUMBERS in a
 * file where they are not lines at all.
 *
 * The target is the Claude Code CLI's look: two gutters (old side, new side), a
 * stripe across added and removed rows, and the LANGUAGE's syntax colouring on
 * top of the stripe. All three need something the raw string does not have --
 * structure -- so the unified diff is parsed into rows here, in a pure module,
 * for the same reason as `statusList.ts`: `fileViewHud.ts` is DOM and untested
 * by doctrine, so every decision it must not make lives out here.
 *
 * `text` is the payload with the `+`/`-`/space marker STRIPPED, because `text`
 * is what eventually reaches the tokenizer: a grammar handed `-x = 1` sees a
 * unary minus where the file has an assignment. `raw` keeps the original line,
 * so nothing is lost.
 *
 * This module stops at rows. It deliberately does NOT rebuild "the old file"
 * and "the new file" by concatenating hunks: hunks are not contiguous, so
 * joining hunk 1 to hunk 3 invents an adjacency the file does not have, and a
 * hunk ending inside an unterminated string would poison the grammar for every
 * hunk after it. Splitting the work into (hunk x side) fragments is `fileDoc`'s
 * job.
 *
 * Five behaviours carry the weight, and each of them is a way a diff can lie
 * about line numbers:
 *
 *  - **A `@@` header restarts the numbering**, at its own two starts, including
 *    in the count-less form `@@ -1 +1 @@` (git omits a count of 1). A second
 *    hunk does NOT continue from the first: the file skipped everything in
 *    between.
 *  - **Each marker advances its own side.** Context advances both, a removal
 *    only the old side, an addition only the new -- and the side a row does not
 *    belong to gets `null`, not a repeated number.
 *  - **A line that belongs to neither file is `meta` and takes no number.**
 *    That covers the whole preamble, `\ No newline at end of file` (an
 *    annotation about the line above it) and the `Binary files ... differ` the
 *    daemon answers for a changed image. Numbering any of them shifts every
 *    line after it by one -- a silent off-by-one in exactly the file where the
 *    user is reading closely.
 *  - **Inside a hunk the FIRST CHARACTER classifies, nothing else.** The diff of
 *    a diff is a real file in this project, and its content lines begin with
 *    `---`, `+++` and `@@`. git guarantees column 0 is the marker inside a hunk,
 *    so a removal of `--- a/old.txt` arrives as `---- a/old.txt`; matching it as
 *    a header drops it out of the numbering entirely.
 *  - **The trailing newline is not a line.** `split("\n")` on content ending in
 *    `\n` yields a final `""`, and emitting it appends a phantom context row
 *    that gets a line number the file does not have.
 *
 * Expected to FAIL until src/diffModel.ts exists. One failure reason per test.
 */

import { describe, it, expect } from "vitest";
import { parseDiff } from "../src/diffModel";

/** A whole `git diff` of one file: preamble, one hunk, every marker. */
const DIFF = [
  "diff --git a/graphagents/normalize.py b/graphagents/normalize.py",
  "index 1a2b3c4..5d6e7f8 100644",
  "--- a/graphagents/normalize.py",
  "+++ b/graphagents/normalize.py",
  "@@ -10,3 +12,4 @@ def actor_of(payload):",
  " keep = 1",
  "-drop = 2",
  "+take = 2",
  "+extra = 3",
  " tail = 4",
  "",
].join("\n");

/** Row indices into {@link DIFF}, named so the assertions stay readable. */
const HUNK = 4;
const CONTEXT = 5;
const DEL = 6;
const ADD = 7;

/** The preamble lines git emits, none of which is a line of either file. */
const PREAMBLE = [
  "diff --git a/x b/x",
  "index 1a2b3c4..5d6e7f8 100644",
  "new file mode 100644",
  "deleted file mode 100644",
  "similarity index 95%",
  "rename from old/name.py",
  "rename to new/name.py",
];

/** Two hunks of the same file, far apart. */
const TWO_HUNKS = [
  "@@ -1,2 +1,2 @@",
  " a",
  "-b",
  "+B",
  "@@ -50,2 +60,2 @@",
  " x",
  "-y",
  "+Y",
  "",
].join("\n");

/** A removal, its no-newline annotation, and the lines that follow it. */
const NO_NEWLINE = [
  "@@ -1,2 +1,2 @@",
  "-old last",
  "\\ No newline at end of file",
  "+new last",
  " after",
  "",
].join("\n");

/** The diff of a diff: content lines that look exactly like headers. */
const NESTED = ["@@ -1,2 +1,2 @@", "---- a/old.txt", "++++ b/new.txt", " @@ -1 +1 @@", ""].join(
  "\n",
);

/** The whole answer for a binary file that changed. */
const BINARY = "Binary files a/web/logo.png and b/web/logo.png differ\n";

describe("parseDiff: the preamble", () => {
  it("treats every `git diff` preamble line as meta rather than content", () => {
    expect(PREAMBLE.map((line) => parseDiff(`${line}\n`)[0].kind)).toEqual(
      PREAMBLE.map(() => "meta"),
    );
  });

  it("reads `--- a/x` before any hunk as meta, not as a removed line", () => {
    // What `diffLineClass` gets wrong today, and what would otherwise be given
    // a line number the moment the gutter exists.
    expect(parseDiff(DIFF)[2].kind).toBe("meta");
  });

  it("reads `+++ b/x` before any hunk as meta, not as an added line", () => {
    expect(parseDiff(DIFF)[3].kind).toBe("meta");
  });

  it("gives a meta line no line number on either side, since it belongs to neither file", () => {
    const row = parseDiff(DIFF)[0];

    expect([row.oldNo, row.newNo]).toEqual([null, null]);
  });

  it("keeps a meta line's text identical to its raw line, there being no marker to strip", () => {
    expect(parseDiff(DIFF)[1].text).toBe("index 1a2b3c4..5d6e7f8 100644");
  });
});

describe("parseDiff: the hunk header", () => {
  it("marks the `@@` line as a hunk, which the panel paints as a separator", () => {
    expect(parseDiff(DIFF)[HUNK].kind).toBe("hunk");
  });

  it("gives the hunk header no line numbers of its own", () => {
    const row = parseDiff(DIFF)[HUNK];

    expect([row.oldNo, row.newNo]).toEqual([null, null]);
  });

  it("keeps the whole `@@` line as the hunk row's text, so the panel prints the header it saw", () => {
    expect(parseDiff(DIFF)[HUNK].text).toBe("@@ -10,3 +12,4 @@ def actor_of(payload):");
  });

  it("preserves the section heading after the second `@@`, which names the function being changed", () => {
    expect(parseDiff(DIFF)[HUNK].raw).toContain("def actor_of(payload):");
  });

  it("starts the old side at the header's first number", () => {
    expect(parseDiff(DIFF)[CONTEXT].oldNo).toBe(10);
  });

  it("starts the new side at the header's second number", () => {
    expect(parseDiff(DIFF)[CONTEXT].newNo).toBe(12);
  });

  it("numbers from the count-less form too, where git omits a length of 1", () => {
    const rows = parseDiff(["@@ -7 +9 @@", "-only old", "+only new", ""].join("\n"));

    expect([rows[1].oldNo, rows[2].newNo]).toEqual([7, 9]);
  });

  it("restarts the numbering at a second hunk instead of continuing from the first", () => {
    // Everything between the hunks was skipped; carrying the counter forward
    // would number line 50 as line 3.
    const rows = parseDiff(TWO_HUNKS);

    expect([rows[5].oldNo, rows[5].newNo]).toEqual([50, 60]);
  });
});

describe("parseDiff: numbering inside a hunk", () => {
  it("numbers a context line on both sides, because it exists in both files", () => {
    const row = parseDiff(DIFF)[CONTEXT];

    expect([row.oldNo, row.newNo]).toEqual([10, 12]);
  });

  it("gives a removed line no new-side number, since it is not in the new file", () => {
    expect(parseDiff(DIFF)[DEL].newNo).toBe(null);
  });

  it("advances only the old side across a removal", () => {
    expect(parseDiff(DIFF)[DEL].oldNo).toBe(11);
  });

  it("gives an added line no old-side number, since it was not in the old file", () => {
    expect(parseDiff(DIFF)[ADD].oldNo).toBe(null);
  });

  it("advances only the new side across an addition", () => {
    const rows = parseDiff(DIFF);

    expect([rows[ADD].newNo, rows[ADD + 1].newNo]).toEqual([13, 14]);
  });

  it("resumes both counters on the context line after a removal and two additions", () => {
    // Old: 10 keep, 11 drop, 12 tail. New: 12 keep, 13 take, 14 extra, 15 tail.
    const row = parseDiff(DIFF)[9];

    expect([row.oldNo, row.newNo]).toEqual([12, 15]);
  });
});

describe("parseDiff: the marker", () => {
  it("strips the `-` from a removed line, so the tokenizer is handed code and not a unary minus", () => {
    expect(parseDiff(DIFF)[DEL].text).toBe("drop = 2");
  });

  it("strips the `+` from an added line", () => {
    expect(parseDiff(DIFF)[ADD].text).toBe("take = 2");
  });

  it("strips the leading space from a context line, which is a marker and not indentation", () => {
    expect(parseDiff(DIFF)[CONTEXT].text).toBe("keep = 1");
  });

  it("keeps the marker in `raw`, the line exactly as git wrote it", () => {
    expect(parseDiff(DIFF)[DEL].raw).toBe("-drop = 2");
  });

  it("classifies a content line starting with `---` inside a hunk by its first character", () => {
    // The diff of a diff: git wrote `-` + `--- a/old.txt`. Inside a hunk column
    // 0 is always the marker, so this is a removal, not a header.
    expect(parseDiff(NESTED)[1].kind).toBe("del");
  });

  it("classifies a content line starting with `+++` inside a hunk as an addition, not a header", () => {
    expect(parseDiff(NESTED)[2].kind).toBe("add");
  });

  it("classifies a context line that looks like a hunk header as context, and does not renumber on it", () => {
    const row = parseDiff(NESTED)[3];

    expect([row.kind, row.oldNo, row.newNo]).toEqual(["context", 2, 2]);
  });
});

describe("parseDiff: the annotations", () => {
  it("marks `\\ No newline at end of file` as meta, since it is not a line of either file", () => {
    expect(parseDiff(NO_NEWLINE)[2].kind).toBe("meta");
  });

  it("gives the no-newline annotation no line number of its own", () => {
    const row = parseDiff(NO_NEWLINE)[2];

    expect([row.oldNo, row.newNo]).toEqual([null, null]);
  });

  it("does not let the annotation consume a number, which would shift every line after it", () => {
    // The `+new last` following it is still new line 1, and the context after
    // that is old 2 / new 2.
    const rows = parseDiff(NO_NEWLINE);

    expect([rows[3].newNo, rows[4].oldNo, rows[4].newNo]).toEqual([1, 2, 2]);
  });

  it("turns a whole binary answer into a single meta row, which is all the daemon sent", () => {
    expect(parseDiff(BINARY).map((row) => row.kind)).toEqual(["meta"]);
  });
});

describe("parseDiff: the edges of the string", () => {
  it("does not turn the trailing newline into a phantom context row at the end", () => {
    // `split("\n")` yields a final "" that would be numbered as a real line.
    expect(parseDiff(DIFF)).toHaveLength(10);
  });

  it("returns no rows at all for empty content", () => {
    expect(parseDiff("")).toEqual([]);
  });
});
