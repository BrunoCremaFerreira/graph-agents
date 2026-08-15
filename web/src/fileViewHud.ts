/**
 * The file viewer: a modal over the graph showing what a file contains.
 *
 * Presentation only — no domain logic. What a click asks for and what a late
 * answer is worth lives in {@link ./fileView}, what Escape means in
 * {@link ./fileViewKeys}, and WHAT to paint — rows, line numbers, gutter width,
 * which lines carry syntax tokens — in {@link ./fileDoc}. This module walks a
 * {@link FileDoc} and builds elements. DOM-bound, so it is not unit-tested: keep
 * it that thin, the way {@link ./searchHud} and {@link ./rootHud} are.
 *
 * Four rules are load-bearing here:
 *
 *  - **`textContent`, never `innerHTML`.** The body is an arbitrary file from
 *    the observed project — a diff of an HTML template, a hex dump, anything.
 *    Assigning it as markup would execute whatever a file happens to contain.
 *    A token's colour goes through the CSSOM (`el.style.color`) for the same
 *    reason, and it survives a future CSP that forbids inline style strings.
 *  - **The scroll position is captured and restored, not merely left alone.**
 *    The syntax tokens land in a second paint, milliseconds after the text; a
 *    `replaceChildren` repositions the scroll on its own while the new children
 *    have no layout yet, so reading a diff would jump to the top the moment it
 *    was coloured.
 *  - **A `null` `rows` is painted as ONE text node**, the fast path the panel
 *    always used: a hex dump, an error, a wait, and any file past the row cap,
 *    where 20 000 elements are the cost being avoided.
 *  - **An empty line keeps its height from CSS (`min-height`), not from a space
 *    smuggled into its text** — that space used to be copied out with the file.
 */

import type { FileViewState } from "./fileView";
import type { FileDoc, Row } from "./fileDoc";
import type { FileViewMode } from "./protocol";

/** Shown in the body while the daemon's answer is still travelling. */
const LOADING = "loading…";
/** Header note when the daemon cut the output short. */
const TRUNCATED = "output truncated";

/** How each mode is named in the header. */
const MODE_LABEL: Record<FileViewMode, string> = {
  diff: "git diff",
  text: "text",
  hex: "hex dump",
};

/** The marker column: what the reader sees instead of a stripped `+`/`-`. */
const SIGN: Partial<Record<Row["kind"], string>> = { add: "+", del: "-" };

export interface FileViewHud {
  /** Show the panel over the graph. */
  open(): void;
  /** Hide the panel and empty it. */
  close(): void;
  isOpen(): boolean;
  /**
   * Paint a state and the document built from it.
   *
   * `keepScroll` is for the repaint that only adds colour to text already on
   * screen; every other paint is a new file, which starts at its first line.
   */
  render(state: FileViewState, doc: FileDoc, keepScroll: boolean): void;
}

/** Bind the panel to `#file-view` (a header row and a scrollable body). */
export function createFileViewHud(container: HTMLElement): FileViewHud {
  const pathEl = container.querySelector<HTMLElement>("#file-view-path");
  const modeEl = container.querySelector<HTMLElement>("#file-view-mode");
  const langEl = container.querySelector<HTMLElement>("#file-view-lang");
  const truncEl = container.querySelector<HTMLElement>("#file-view-truncated");
  const bodyEl = container.querySelector<HTMLElement>("#file-view-body");

  /** One cell of a row: a `<span>` of one class, holding text and nothing else. */
  function cell(className: string, text: string): HTMLSpanElement {
    const el = document.createElement("span");
    el.className = className;
    el.textContent = text;
    return el;
  }

  /** The code column: the syntax tokens if they arrived, else the raw line. */
  function codeCell(row: Row): HTMLSpanElement {
    const el = document.createElement("span");
    el.className = "code";
    if (row.tokens === null) {
      el.textContent = row.text;
      return el;
    }
    for (const token of row.tokens) {
      const span = document.createElement("span");
      span.textContent = token.text;
      span.style.color = token.color;
      if (token.italic) span.style.fontStyle = "italic";
      if (token.bold) span.style.fontWeight = "bold";
      el.append(span);
    }
    return el;
  }

  /** Put plain text in the body as a single node, whatever it holds. */
  function paintPlain(text: string): void {
    if (bodyEl) bodyEl.textContent = text;
  }

  /** Build one element per row: two gutter columns, a sign, and the code. */
  function paintRows(doc: FileDoc, rows: readonly Row[]): void {
    if (!bodyEl) return;
    // One custom property per paint sizes both gutter columns; a `max-content`
    // column would be measured per row and would not align between them.
    bodyEl.style.setProperty("--gutter-ch", `${doc.gutterWidth}ch`);
    const fragment = document.createDocumentFragment();
    for (const row of rows) {
      const el = document.createElement("div");
      el.className = `row ${row.kind}`;
      el.append(
        cell("old", row.oldNo === null ? "" : String(row.oldNo)),
        cell("new", row.newNo === null ? "" : String(row.newNo)),
        cell("sign", SIGN[row.kind] ?? ""),
        codeCell(row),
      );
      fragment.append(el);
    }
    bodyEl.replaceChildren(fragment);
  }

  return {
    open(): void {
      container.hidden = false;
    },

    close(): void {
      container.hidden = true;
      // Emptied on the way out: the next file must never flash the previous
      // one's contents under its own name while its answer is in flight.
      if (pathEl) pathEl.textContent = "";
      if (modeEl) modeEl.textContent = "";
      if (langEl) langEl.textContent = "";
      if (truncEl) {
        truncEl.textContent = "";
        truncEl.hidden = true;
      }
      if (bodyEl) bodyEl.replaceChildren();
    },

    isOpen(): boolean {
      return !container.hidden;
    },

    render(state: FileViewState, doc: FileDoc, keepScroll: boolean): void {
      if (pathEl) pathEl.textContent = state.path;
      // No mode while the answer is still coming: the daemon, not the click,
      // decides whether this is a diff, text or a hex dump.
      if (modeEl) modeEl.textContent = state.loading ? "" : MODE_LABEL[state.mode] ?? "";
      if (langEl) {
        // "the daemon cut this short" and "we chose not to colour this" are
        // different facts, so the amber truncation note keeps its own span.
        const named = doc.note === "" ? doc.lang : `${doc.lang} · ${doc.note}`;
        langEl.textContent = doc.lang === null ? "" : named;
      }
      if (truncEl) {
        truncEl.textContent = TRUNCATED;
        truncEl.hidden = !state.truncated;
      }

      if (!bodyEl) return;
      // Captured BEFORE the children go: `replaceChildren` moves the scroll by
      // itself, so not zeroing it is not enough to hold the reader's place.
      const scroll = bodyEl.scrollTop;
      bodyEl.classList.toggle("error", state.error !== "");
      bodyEl.classList.toggle("rows", doc.rows !== null);
      if (state.error !== "") paintPlain(state.error);
      else if (state.loading) paintPlain(LOADING);
      else if (doc.rows !== null) paintRows(doc, doc.rows);
      else paintPlain(doc.plain);
      // A new file starts at its first line; only the colouring repaint of the
      // very text already on screen keeps where it was read to.
      bodyEl.scrollTop = keepScroll ? scroll : 0;
    },
  };
}
