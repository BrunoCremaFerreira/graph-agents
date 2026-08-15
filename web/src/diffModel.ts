/**
 * The unified diff, parsed into rows.
 *
 * The panel used to paint a diff as three colours of TEXT: one class per line
 * prefix and nothing else. That cannot carry line numbers, cannot carry a
 * stripe, and got the headers actively wrong — `--- a/x` painted as a deletion,
 * `+++ b/x` as an addition — which is harmless while they are only colours and
 * a lie the moment they are handed line numbers.
 *
 * So the diff is parsed here, in a pure module, for the same reason as
 * {@link ./statusList}: {@link ./fileViewHud} is DOM and untested by doctrine,
 * so the decisions it must not make live out here.
 *
 * `text` is the payload with the `+`/`-`/space marker STRIPPED, because `text`
 * is what eventually reaches the tokenizer: a grammar handed `-x = 1` sees a
 * unary minus where the file has an assignment. `raw` keeps the line exactly as
 * git wrote it.
 *
 * This module stops at rows. It deliberately does NOT rebuild "the old file"
 * and "the new file" out of the hunks — hunks are not contiguous, so joining
 * them invents an adjacency the file does not have. Cutting the work into
 * (hunk × side) fragments is {@link ./fileDoc}'s job.
 *
 * Four rules carry the weight, and each is a way a diff can lie about line
 * numbers:
 *
 *  - **A `@@` header restarts the numbering**, at its own two starts, including
 *    in the count-less form `@@ -1 +1 @@` that git writes for a length of 1.
 *  - **Each marker advances its own side**, and the side a row does not belong
 *    to gets `null`, never a repeated number.
 *  - **A line belonging to neither file is `meta` and takes no number** — the
 *    whole preamble, `\ No newline at end of file`, `Binary files … differ`.
 *    Numbering any of them shifts every line after it by one.
 *  - **Inside a hunk the FIRST CHARACTER classifies, nothing else.** The diff
 *    of a diff is a real file here: git guarantees column 0 is the marker, so a
 *    removal of `--- a/old.txt` arrives as `---- a/old.txt`.
 */

/** What a row is: content of one side, both sides, or neither. */
export type RowKind = "meta" | "hunk" | "add" | "del" | "context" | "plain";

/** One line of a diff, placed in the old file, the new one, or neither. */
export interface DiffRow {
  readonly kind: RowKind;
  /** Line number in the old file, or `null` when it is not in it. */
  readonly oldNo: number | null;
  /** Line number in the new file, or `null` when it is not in it. */
  readonly newNo: number | null;
  /** The payload, marker stripped — what the tokenizer is handed. */
  readonly text: string;
  /** The line exactly as git wrote it. */
  readonly raw: string;
}

/** `@@ -10,3 +12,4 @@ heading`, and the count-less `@@ -7 +9 @@`. */
const HUNK_HEADER = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

/** Parse a whole `git diff` answer into rows. Empty content yields no rows. */
export function parseDiff(content: string): readonly DiffRow[] {
  const lines = content.split("\n");
  // `split` on content ending in "\n" yields a final "" that is not a line;
  // emitting it appends a phantom context row with a number of its own.
  if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();

  const rows: DiffRow[] = [];
  let inHunk = false;
  let oldNo = 0;
  let newNo = 0;

  for (const raw of lines) {
    const header = HUNK_HEADER.exec(raw);
    // A header is only a header at the start of a hunk or between hunks; inside
    // one, column 0 is the marker, so a context line reading `@@ -1 +1 @@`
    // arrives as ` @@ -1 +1 @@` and never reaches this branch.
    if (header && (!inHunk || raw.startsWith("@@"))) {
      inHunk = true;
      oldNo = Number(header[1]);
      newNo = Number(header[2]);
      rows.push({ kind: "hunk", oldNo: null, newNo: null, text: raw, raw });
      continue;
    }

    if (!inHunk) {
      rows.push({ kind: "meta", oldNo: null, newNo: null, text: raw, raw });
      continue;
    }

    const marker = raw.charAt(0);
    if (marker === "+") {
      rows.push({ kind: "add", oldNo: null, newNo, text: raw.slice(1), raw });
      newNo += 1;
    } else if (marker === "-") {
      rows.push({ kind: "del", oldNo, newNo: null, text: raw.slice(1), raw });
      oldNo += 1;
    } else if (marker === "\\") {
      // `\ No newline at end of file` annotates the line above it and is not a
      // line of either file: it consumes no number on either side.
      rows.push({ kind: "meta", oldNo: null, newNo: null, text: raw, raw });
    } else if (marker === " " || raw === "") {
      rows.push({ kind: "context", oldNo, newNo, text: raw.slice(1), raw });
      oldNo += 1;
      newNo += 1;
    } else {
      // Anything else in column 0 means the hunk ended: the next file's
      // preamble in a multi-file diff, or a trailer.
      inHunk = false;
      rows.push({ kind: "meta", oldNo: null, newNo: null, text: raw, raw });
    }
  }

  return rows;
}
