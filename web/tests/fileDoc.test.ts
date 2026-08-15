/**
 * Contract tests (RED) for the render model behind the file viewer.
 *
 * The defect: `fileViewHud.ts` decides, in the DOM, what a file looks like --
 * one `textContent` for text and hex, a class per line prefix for a diff. Those
 * are decisions (how wide is the gutter? is this file big enough that colouring
 * it would stall the graph's animation loop? which fragments does the grammar
 * get to see?) living in the one module doctrine says is never tested, because
 * it needs a DOM the `node` test environment does not have.
 *
 * So the panel gets a MODEL, one for all three modes, and the painter becomes a
 * loop over `doc.rows`. `buildDoc` is the only entry point and it is a pure
 * READ of the state: it dispatches by mode, parses, budgets, sizes the gutter,
 * lists the tokenization requests AND stitches the tokens in once
 * `state.highlight` has arrived. Attaching tokens is not a step the caller
 * sequences -- a second exported function would let a caller paint a doc it
 * forgot to attach tokens to, and that bug is invisible (it just looks
 * uncoloured).
 *
 * Five decisions carry the weight:
 *
 *  - **One request per (hunk x side).** Concatenating "the whole old file" out
 *    of its hunks is wrong: hunks are not contiguous, so joining hunk 1 to
 *    hunk 3 invents an adjacency the file does not have, and a hunk ending
 *    inside an unterminated string would poison the grammar for every hunk
 *    after it. A fragment per hunk restarts the grammar at the boundary --
 *    which is all git's +-3 context lines ever licensed anyone to claim.
 *  - **`-1` is what makes that close.** A context line must EXIST in the old
 *    side's code for the grammar to see a coherent fragment, but its tokens
 *    come from the NEW side -- it is the same text, and the new side is the
 *    numbering the reader follows. So the old side lists it and discards it.
 *    Hence the one-line invariant that holds the whole design up:
 *    `code.split("\n").length === rows.length`.
 *  - **The budget is the graph's frame rate**, and it degrades in two steps,
 *    not one. Past the highlight budget the diff still gets rows, stripes and
 *    gutter and only loses colour; past `MAX_ROWS` it falls back to the single
 *    text node it uses today, because there the cost is ELEMENTS -- a 200 KB
 *    `.log` must not regress into 8 000 `<div>`s.
 *  - **Hex is untouched.** A dump is already column-aligned and has no
 *    language; a gutter would double its offsets and a grammar would colour
 *    them as code.
 *  - **A `highlight` whose shape disagrees paints NOTHING.** The tokenizer is a
 *    third-party wasm grammar over content the daemon may have cut mid-UTF-8;
 *    a chunk count or a line count that does not match means every row goes
 *    plain. Painting the wrong tokens on the right rows is worse than painting
 *    none: it is silently, plausibly wrong.
 *
 * No test here imports shiki, not even as a type: `CodeToken` is OURS, which is
 * what keeps this suite as fast as it is today.
 *
 * Expected to FAIL until src/fileDoc.ts exists. One failure reason per test.
 */

import { describe, it, expect } from "vitest";
import { buildDoc, MAX_ROWS, MAX_HIGHLIGHT_LINES, MAX_HIGHLIGHT_BYTES } from "../src/fileDoc";
import {
  createFileView,
  requestView,
  applyView,
  failView,
  type FileViewState,
} from "../src/fileView";
import type { FileViewMode } from "../src/protocol";

/** A Python file, so a known grammar is in play unless a test says otherwise. */
const PY = "rhizome_graph/normalize.py";

/** A panel waiting for the daemon's answer. */
function pending(path = PY): FileViewState {
  return requestView(createFileView(), path);
}

/** A panel showing the daemon's answer. */
function shown(mode: FileViewMode, content: string, path = PY): FileViewState {
  return applyView(pending(path), { path, mode, content, truncated: false, error: "" });
}

/** One syntax token, in the shape `highlight.ts` converts shiki's into. */
function tok(text: string, color: string, italic = false, bold = false) {
  return { text, color, italic, bold };
}

/** Two lines of a Python file. */
const TEXT = "import os\nx = 1\n";

/** A diff with one removal, one addition and one context line. */
const SMALL_DIFF = ["@@ -1,2 +1,2 @@", "-x = 1", "+x = 2", " y = 3", ""].join("\n");

/** Row indices into the doc built from {@link SMALL_DIFF}. */
const HUNK_ROW = 0;
const DEL_ROW = 1;
const ADD_ROW = 2;
const CONTEXT_ROW = 3;

/** Two hunks far apart in the same file. */
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

/** The tokens of {@link SMALL_DIFF}'s OLD fragment: the removal, then context. */
const OLD_CHUNK = [[tok("x = 1", "#CE9178")], [tok("y = 3", "#808080", true)]];

/** ...and of the NEW fragment: the addition, then the same context line. */
const NEW_CHUNK = [[tok("x = 2", "#B5CEA8")], [tok("y = 3", "#9CDCFE")]];

/** One tokenized fragment: a list of lines, each a list of tokens. */
type Chunk = ReturnType<typeof tok>[][];

/** A state whose tokens have already arrived, as `applyTokens` leaves it. */
function highlighted(state: FileViewState, chunks: Chunk[]): FileViewState {
  return { ...state, highlight: chunks };
}

describe("buildDoc: the single-text-node fallback", () => {
  it("keeps a hex dump out of rows, because it is already column-aligned", () => {
    expect(buildDoc(shown("hex", "00000000: 8950 4e47  .PNG\n", "web/logo.png")).rows).toBe(null);
  });

  it("draws no gutter over a dump, whose own first column is already an offset", () => {
    expect(buildDoc(shown("hex", "00000000: 8950 4e47  .PNG\n", "web/logo.png")).gutter).toBe(
      false,
    );
  });

  it("names no language for a dump, which is bytes and not source", () => {
    expect(buildDoc(shown("hex", "00000000: 8950 4e47  .PNG\n", "web/logo.png")).lang).toBe(null);
  });

  it("asks for no tokenization of a dump", () => {
    expect(buildDoc(shown("hex", "00000000: 8950 4e47  .PNG\n", "web/logo.png")).requests).toEqual(
      [],
    );
  });

  it("builds no rows for a failure, whose body is a message and not a file", () => {
    expect(buildDoc(failView(pending(), "no such path")).rows).toBe(null);
  });

  it("builds no rows while the answer is still in flight", () => {
    expect(buildDoc(pending()).rows).toBe(null);
  });

  it("asks for no tokenization while the answer is still in flight", () => {
    expect(buildDoc(pending()).requests).toEqual([]);
  });

  it("prefers the error over the content, so a stale body never sits under a failure", () => {
    expect(buildDoc(failView(shown("text", TEXT), "not a text file")).rows).toBe(null);
  });
});

describe("buildDoc: a text file", () => {
  it("makes one plain row per line of the file", () => {
    expect(buildDoc(shown("text", TEXT)).rows?.map((row) => [row.kind, row.text])).toEqual([
      ["plain", "import os"],
      ["plain", "x = 1"],
    ]);
  });

  it("numbers the lines from one, on the new side, which is the only side a file has", () => {
    expect(buildDoc(shown("text", TEXT)).rows?.map((row) => [row.oldNo, row.newNo])).toEqual([
      [null, 1],
      [null, 2],
    ]);
  });

  it("draws a gutter, which is the question a file view is most often opened to answer", () => {
    expect(buildDoc(shown("text", TEXT)).gutter).toBe(true);
  });

  it("does not turn the trailing newline into a phantom last line with a number of its own", () => {
    expect(buildDoc(shown("text", "a\nb\n")).rows).toHaveLength(2);
  });

  it("keeps the last line of a file that does not end in a newline", () => {
    expect(buildDoc(shown("text", "a\nb")).rows).toHaveLength(2);
  });

  it("asks for the whole file as one fragment, mapping each line to its row", () => {
    expect(buildDoc(shown("text", TEXT)).requests).toEqual([
      { code: "import os\nx = 1", rows: [0, 1] },
    ]);
  });

  it("names the language the extension resolves to", () => {
    expect(buildDoc(shown("text", TEXT)).lang).toBe("python");
  });

  it("names no language for an extension outside the batch, there being no fallback grammar", () => {
    expect(buildDoc(shown("text", "all:\n\techo hi\n", "Makefile")).lang).toBe(null);
  });

  it("asks for no tokenization when there is no grammar to ask", () => {
    expect(buildDoc(shown("text", "all:\n\techo hi\n", "Makefile")).requests).toEqual([]);
  });

  it("still builds rows and a gutter without a grammar, since plain text is still read by line", () => {
    expect(buildDoc(shown("text", "all:\n\techo hi\n", "Makefile")).gutter).toBe(true);
  });
});

describe("buildDoc: the gutter width", () => {
  it("sizes a two-line file at one character", () => {
    expect(buildDoc(shown("text", TEXT)).gutterWidth).toBe(1);
  });

  it("sizes a twelve-line file at two characters", () => {
    expect(buildDoc(shown("text", "x\n".repeat(12))).gutterWidth).toBe(2);
  });

  it("sizes a short diff by its largest line number, not by its row count", () => {
    expect(buildDoc(shown("diff", SMALL_DIFF)).gutterWidth).toBe(1);
  });

  it("sizes a diff that reaches line 1204 at four characters", () => {
    // A hunk far into the file: the numbers, not the rows, are what must fit.
    const far = `@@ -1200,5 +1200,5 @@\n${" x\n".repeat(5)}`;

    expect(buildDoc(shown("diff", far)).gutterWidth).toBe(4);
  });
});

describe("buildDoc: a diff", () => {
  it("carries the parsed rows through, marker stripped", () => {
    expect(buildDoc(shown("diff", SMALL_DIFF)).rows?.map((row) => [row.kind, row.text])).toEqual([
      ["hunk", "@@ -1,2 +1,2 @@"],
      ["del", "x = 1"],
      ["add", "x = 2"],
      ["context", "y = 3"],
    ]);
  });

  it("keeps both line numbers of a context row, which is why the gutter has two columns", () => {
    const row = buildDoc(shown("diff", SMALL_DIFF)).rows?.[CONTEXT_ROW];

    expect([row?.oldNo, row?.newNo]).toEqual([2, 2]);
  });

  it("asks for one fragment per side of a hunk, the old one first", () => {
    expect(buildDoc(shown("diff", SMALL_DIFF)).requests).toEqual([
      { code: "x = 1\ny = 3", rows: [DEL_ROW, -1] },
      { code: "x = 2\ny = 3", rows: [ADD_ROW, CONTEXT_ROW] },
    ]);
  });

  it("discards the context line on the old side, whose tokens come from the new one", () => {
    // It is in the code so the grammar sees a coherent fragment, and mapped to
    // -1 so its tokens are thrown away.
    expect(buildDoc(shown("diff", SMALL_DIFF)).requests[0].rows).toEqual([DEL_ROW, -1]);
  });

  it("asks for four fragments over two hunks, never one concatenated document per side", () => {
    // Hunks are not contiguous: joining them invents adjacency, and an
    // unterminated string in one would poison every hunk after it.
    expect(buildDoc(shown("diff", TWO_HUNKS)).requests).toHaveLength(4);
  });

  it("keeps each hunk's fragments to that hunk's own lines", () => {
    expect(buildDoc(shown("diff", TWO_HUNKS)).requests).toEqual([
      { code: "a\nb", rows: [-1, 2] },
      { code: "a\nB", rows: [1, 3] },
      { code: "x\ny", rows: [-1, 6] },
      { code: "x\nY", rows: [5, 7] },
    ]);
  });

  it("holds the invariant that every fragment has one row entry per line of code", () => {
    // The one line that holds the whole design up: a chunk of tokens comes back
    // per line, and it is `rows` that says where each of them lands.
    const { requests } = buildDoc(shown("diff", TWO_HUNKS));

    expect(requests.map((req) => req.code.split("\n").length)).toEqual(
      requests.map((req) => req.rows.length),
    );
  });

  it("omits a side that has no lines at all, since an empty fragment breaks that invariant", () => {
    // A newly created file: every line is an addition, so the old side of the
    // hunk is empty. `"".split("\n")` is one line, and `rows` would be zero.
    const created = ["@@ -0,0 +1,2 @@", "+a", "+b", ""].join("\n");

    expect(buildDoc(shown("diff", created)).requests).toEqual([
      { code: "a\nb", rows: [1, 2] },
    ]);
  });

  it("names the language from the path, a diff being a diff OF something", () => {
    expect(buildDoc(shown("diff", SMALL_DIFF)).lang).toBe("python");
  });
});

describe("buildDoc: the budget", () => {
  it("stops highlighting above 4 000 rows, which is about 28 000 spans to build", () => {
    expect(MAX_HIGHLIGHT_LINES).toBe(4000);
  });

  it("stops highlighting above 128 KiB, half the daemon's own cap", () => {
    expect(MAX_HIGHLIGHT_BYTES).toBe(131072);
  });

  it("falls back to a single text node above 20 000 rows, where the cost is elements", () => {
    expect(MAX_ROWS).toBe(20000);
  });

  it("still highlights a file exactly at the row budget", () => {
    expect(buildDoc(shown("text", "x = 1\n".repeat(MAX_HIGHLIGHT_LINES))).requests).toHaveLength(1);
  });

  it("asks for nothing one row above the highlight budget", () => {
    expect(buildDoc(shown("text", "x = 1\n".repeat(MAX_HIGHLIGHT_LINES + 1))).requests).toEqual([]);
  });

  it("still highlights content exactly at the byte budget", () => {
    expect(buildDoc(shown("text", "a".repeat(MAX_HIGHLIGHT_BYTES))).requests).toHaveLength(1);
  });

  it("asks for nothing one character above the byte budget, even on a single line", () => {
    // The minified bundle case: one line, no rows to speak of, and 256 KiB of
    // it. Rows alone would never catch it.
    expect(buildDoc(shown("text", "a".repeat(MAX_HIGHLIGHT_BYTES + 1))).requests).toEqual([]);
  });

  it("still gives a diff over the budget its rows, so the stripe and the numbers survive", () => {
    const big = `@@ -1,${MAX_HIGHLIGHT_LINES + 1} +1,${MAX_HIGHLIGHT_LINES + 1} @@\n${" x = 1\n".repeat(
      MAX_HIGHLIGHT_LINES + 1,
    )}`;

    expect(buildDoc(shown("diff", big)).rows).toHaveLength(MAX_HIGHLIGHT_LINES + 2);
  });

  it("still names the language over the budget, so the header can say which colouring was skipped", () => {
    expect(buildDoc(shown("text", "x = 1\n".repeat(MAX_HIGHLIGHT_LINES + 1))).lang).toBe("python");
  });

  it("says why it did not highlight, since silence is indistinguishable from a broken grammar", () => {
    expect(buildDoc(shown("text", "x = 1\n".repeat(MAX_HIGHLIGHT_LINES + 1))).note).toBe(
      "too large to highlight",
    );
  });

  it("says nothing when it did highlight", () => {
    expect(buildDoc(shown("text", TEXT)).note).toBe("");
  });

  it("still builds rows for a file exactly at the row cap", () => {
    expect(buildDoc(shown("text", "x\n".repeat(MAX_ROWS))).rows).toHaveLength(MAX_ROWS);
  });

  it("drops to a single text node one row above the cap, rather than opening 20 001 elements", () => {
    expect(buildDoc(shown("text", "x\n".repeat(MAX_ROWS + 1))).rows).toBe(null);
  });

  it("drops the gutter with the rows above the cap, there being no rows to align", () => {
    expect(buildDoc(shown("text", "x\n".repeat(MAX_ROWS + 1))).gutter).toBe(false);
  });

  it("asks for no tokenization above the cap either", () => {
    expect(buildDoc(shown("text", "x\n".repeat(MAX_ROWS + 1))).requests).toEqual([]);
  });

  it("applies the same cap to a huge diff, where the element count costs exactly the same", () => {
    const huge = `@@ -1,${MAX_ROWS + 1} +1,${MAX_ROWS + 1} @@\n${" x\n".repeat(MAX_ROWS + 1)}`;

    expect(buildDoc(shown("diff", huge)).rows).toBe(null);
  });
});

describe("buildDoc: stitching the tokens in", () => {
  const diff = shown("diff", SMALL_DIFF);

  it("leaves every row plain while no tokens have arrived", () => {
    expect(buildDoc(diff).rows?.map((row) => row.tokens)).toEqual([null, null, null, null]);
  });

  it("paints a removed line from the OLD fragment, the only one that still contains it", () => {
    const doc = buildDoc(highlighted(diff, [OLD_CHUNK, NEW_CHUNK]));

    expect(doc.rows?.[DEL_ROW].tokens).toEqual(OLD_CHUNK[0]);
  });

  it("paints an added line from the NEW fragment", () => {
    const doc = buildDoc(highlighted(diff, [OLD_CHUNK, NEW_CHUNK]));

    expect(doc.rows?.[ADD_ROW].tokens).toEqual(NEW_CHUNK[0]);
  });

  it("paints a context line from the new side, whose numbering the reader is following", () => {
    const doc = buildDoc(highlighted(diff, [OLD_CHUNK, NEW_CHUNK]));

    expect(doc.rows?.[CONTEXT_ROW].tokens).toEqual(NEW_CHUNK[1]);
  });

  it("leaves a hunk header uncoloured, since it is not a line of either file", () => {
    const doc = buildDoc(highlighted(diff, [OLD_CHUNK, NEW_CHUNK]));

    expect(doc.rows?.[HUNK_ROW].tokens).toBe(null);
  });

  it("carries the italic flag through, because Dark+ italicises some scopes", () => {
    const text = highlighted(shown("text", TEXT), [
      [[tok("import os", "#C586C0", true)], [tok("x = 1", "#9CDCFE")]],
    ]);

    expect(buildDoc(text).rows?.[0].tokens?.[0].italic).toBe(true);
  });

  it("paints a text file from its single fragment, line for line", () => {
    const chunk = [[tok("import os", "#C586C0")], [tok("x = 1", "#9CDCFE")]];

    expect(buildDoc(highlighted(shown("text", TEXT), [chunk])).rows?.map((row) => row.tokens)).toEqual(
      chunk,
    );
  });

  it("paints nothing at all when the number of fragments does not match", () => {
    // One chunk for a two-fragment request: the second half would land on rows
    // chosen by index, which is plausible and wrong.
    const doc = buildDoc(highlighted(diff, [OLD_CHUNK]));

    expect(doc.rows?.map((row) => row.tokens)).toEqual([null, null, null, null]);
  });

  it("paints nothing at all when a fragment came back with the wrong number of lines", () => {
    const doc = buildDoc(highlighted(diff, [[OLD_CHUNK[0]], NEW_CHUNK]));

    expect(doc.rows?.map((row) => row.tokens)).toEqual([null, null, null, null]);
  });

  it("ignores tokens it never asked for, such as those left over an over-budget file", () => {
    const over = shown("text", "x = 1\n".repeat(MAX_HIGHLIGHT_LINES + 1));
    const doc = buildDoc(highlighted(over, [[[tok("x = 1", "#9CDCFE")]]]));

    expect(doc.rows?.[0].tokens).toBe(null);
  });
});
