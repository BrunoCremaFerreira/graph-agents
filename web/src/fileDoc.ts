/**
 * The render model behind the file viewer: what the panel paints, decided here.
 *
 * {@link ./fileViewHud} used to decide it in the DOM — one `textContent` for
 * text and hex, a class per line prefix for a diff. Those are decisions (how
 * wide is the gutter? is this file big enough that colouring it would stall the
 * graph's animation loop? which fragments does the grammar get to see?) living
 * in the one module doctrine says is never tested, because it needs a DOM the
 * `node` test environment does not have. So the panel gets a MODEL, one for all
 * three modes, and the painter becomes a loop over {@link FileDoc.rows}.
 *
 * `buildDoc` is the only entry point, and it is a pure READ of the state: it
 * dispatches by mode, parses, budgets, sizes the gutter, lists the tokenization
 * requests AND stitches the tokens in once `state.highlight` has arrived. A
 * second exported "attach" step would let a caller paint a doc it forgot to
 * attach tokens to — a bug that just looks uncoloured.
 *
 * Five decisions carry the weight:
 *
 *  - **One request per (hunk × side).** Concatenating "the whole old file" out
 *    of its hunks is wrong: hunks are not contiguous, and a hunk ending inside
 *    an unterminated string would poison the grammar for every hunk after it.
 *  - **`-1` is what makes that close.** A context line must EXIST in the old
 *    side's code for the grammar to see a coherent fragment, but its tokens
 *    come from the NEW side — same text, and the new side is the numbering the
 *    reader follows. Hence the invariant that holds the design up:
 *    `code.split("\n").length === rows.length`.
 *  - **The budget is the graph's frame rate**, and it degrades in two steps:
 *    past the highlight budget a diff keeps its rows, stripes and gutter and
 *    only loses colour; past {@link MAX_ROWS} it falls back to the single text
 *    node the panel used to use, because there the cost is ELEMENTS.
 *  - **Hex is untouched.** A dump is already column-aligned and has no
 *    language: a gutter would double its offsets.
 *  - **A `highlight` whose shape disagrees paints NOTHING.** A chunk count or a
 *    line count that does not match means every row goes plain. The wrong
 *    tokens on the right rows are worse than none: silently, plausibly wrong.
 */

import { parseDiff, type DiffRow, type RowKind } from "./diffModel";
import { languageForPath, type LanguageId } from "./language";
import type { CodeToken, FileViewState } from "./fileView";

export type { CodeToken } from "./fileView";

/** Above this many rows the panel would be built out of that many elements. */
export const MAX_ROWS = 20000;

/** Lines the tokenizer is allowed: ~28 000 spans, ~60–100 ms of building. */
export const MAX_HIGHLIGHT_LINES = 4000;

/** Bytes the tokenizer is allowed: half the daemon's own 256 KiB cap. */
export const MAX_HIGHLIGHT_BYTES = 131072;

/** Why the content is on screen without colour. */
const TOO_LARGE = "too large to highlight";

/** One line of the panel. */
export interface Row {
  readonly kind: RowKind;
  /** Line number in the old file, or `null`. */
  readonly oldNo: number | null;
  /** Line number in the new file, or `null`. */
  readonly newNo: number | null;
  /** The text to paint, diff marker already stripped. */
  readonly text: string;
  /** Its syntax tokens, or `null` for a plain line. */
  readonly tokens: readonly CodeToken[] | null;
}

/** A fragment to tokenize, and where each of its lines lands. */
export interface HighlightRequest {
  /** The fragment's lines, joined by `"\n"`. */
  readonly code: string;
  /** Per line of {@link code}: the {@link Row} index, or `-1` to discard. */
  readonly rows: readonly number[];
}

/** Everything the painter needs, and nothing it has to decide. */
export interface FileDoc {
  /** The rows, or `null` for the single-text-node fast path. */
  readonly rows: readonly Row[] | null;
  /** Whether line numbers are drawn. */
  readonly gutter: boolean;
  /** How wide the gutter must be, in `ch`. */
  readonly gutterWidth: number;
  /** The grammar the path resolves to, or `null`. */
  readonly lang: LanguageId | null;
  /** The fragments to tokenize; empty when there is nothing to colour. */
  readonly requests: readonly HighlightRequest[];
  /** `""`, or why the content was not coloured. */
  readonly note: string;
  /** The whole body, for the fast path where {@link rows} is `null`. */
  readonly plain: string;
}

/** A mutable row under construction; frozen into a {@link Row} by returning it. */
interface MutableRow {
  kind: RowKind;
  oldNo: number | null;
  newNo: number | null;
  text: string;
  tokens: readonly CodeToken[] | null;
}

/** The single-text-node answer: hex, an error, a wait, or too many rows. */
function plainDoc(plain: string, lang: LanguageId | null, note: string): FileDoc {
  return { rows: null, gutter: false, gutterWidth: 0, lang, requests: [], note, plain };
}

/** The lines of a file, without the phantom last one a trailing `\n` yields. */
function contentLines(content: string): string[] {
  const lines = content.split("\n");
  if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();
  return lines;
}

/** Widest line number, in characters — one column width for every row. */
function gutterWidthOf(rows: readonly MutableRow[]): number {
  let largest = 0;
  for (const row of rows) {
    if (row.oldNo !== null && row.oldNo > largest) largest = row.oldNo;
    if (row.newNo !== null && row.newNo > largest) largest = row.newNo;
  }
  return String(largest).length;
}

/** One fragment per side of one hunk, in the order old-then-new. */
function hunkRequests(rows: readonly MutableRow[]): HighlightRequest[] {
  const requests: HighlightRequest[] = [];
  let oldCode: string[] = [];
  let oldRows: number[] = [];
  let newCode: string[] = [];
  let newRows: number[] = [];

  function flush(): void {
    // An empty side is dropped: `"".split("\n")` is one line against zero row
    // entries, which is exactly the invariant this design rests on.
    if (oldCode.length > 0) requests.push({ code: oldCode.join("\n"), rows: oldRows });
    if (newCode.length > 0) requests.push({ code: newCode.join("\n"), rows: newRows });
    oldCode = [];
    oldRows = [];
    newCode = [];
    newRows = [];
  }

  rows.forEach((row, index) => {
    if (row.kind === "hunk") {
      flush();
      return;
    }
    if (row.kind === "del") {
      oldCode.push(row.text);
      oldRows.push(index);
    } else if (row.kind === "add") {
      newCode.push(row.text);
      newRows.push(index);
    } else if (row.kind === "context") {
      // In the old side's code so the grammar sees a coherent fragment, and
      // mapped to -1 so its tokens are thrown away in favour of the new side's.
      oldCode.push(row.text);
      oldRows.push(-1);
      newCode.push(row.text);
      newRows.push(index);
    }
  });
  flush();

  return requests;
}

/**
 * Paint the tokens onto the rows they were asked for, or paint none at all.
 *
 * The tokenizer is a third-party wasm grammar over content the daemon may have
 * cut mid-UTF-8. A chunk count or a line count that disagrees with what was
 * requested means the answer describes something else, and every row stays
 * plain rather than taking colours chosen by index.
 */
function stitch(
  rows: MutableRow[],
  requests: readonly HighlightRequest[],
  chunks: readonly (readonly (readonly CodeToken[])[])[],
): void {
  if (chunks.length !== requests.length) return;
  for (let i = 0; i < requests.length; i += 1) {
    if (chunks[i].length !== requests[i].rows.length) return;
  }
  // Only once every fragment checks out: a later request overrides an earlier
  // one, which is how a context line ends up wearing the new side's colours.
  for (let i = 0; i < requests.length; i += 1) {
    const map = requests[i].rows;
    for (let line = 0; line < map.length; line += 1) {
      const target = map[line];
      if (target >= 0 && target < rows.length) rows[target].tokens = chunks[i][line];
    }
  }
}

/** The whole model for one panel state — the painter decides nothing else. */
export function buildDoc(state: FileViewState): FileDoc {
  // The error wins over the content, so a stale body never sits under a failure.
  if (state.error !== "") return plainDoc(state.error, null, "");
  if (state.loading) return plainDoc("", null, "");
  // A dump has no language and its own first column is already an offset.
  if (state.mode === "hex") return plainDoc(state.content, null, "");

  const lang = languageForPath(state.path);
  const isDiff = state.mode === "diff";

  const rows: MutableRow[] = isDiff
    ? parseDiff(state.content).map((row: DiffRow) => ({
        kind: row.kind,
        oldNo: row.oldNo,
        newNo: row.newNo,
        text: row.text,
        tokens: null,
      }))
    : contentLines(state.content).map((text, index) => ({
        kind: "plain" as RowKind,
        oldNo: null,
        // A file has only one side, and it is the one the reader is on.
        newNo: index + 1,
        text,
        tokens: null,
      }));

  if (rows.length > MAX_ROWS) return plainDoc(state.content, lang, lang ? TOO_LARGE : "");

  const overBudget =
    rows.length > MAX_HIGHLIGHT_LINES || state.content.length > MAX_HIGHLIGHT_BYTES;

  let requests: readonly HighlightRequest[] = [];
  if (lang !== null && !overBudget) {
    if (isDiff) requests = hunkRequests(rows);
    else if (rows.length > 0) {
      requests = [{ code: rows.map((row) => row.text).join("\n"), rows: rows.map((_, i) => i) }];
    }
  }

  if (state.highlight !== null) stitch(rows, requests, state.highlight);

  return {
    rows,
    gutter: true,
    gutterWidth: gutterWidthOf(rows),
    lang,
    requests,
    note: lang !== null && overBudget ? TOO_LARGE : "",
    plain: state.content,
  };
}
