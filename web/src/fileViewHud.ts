/**
 * The file viewer: a modal over the graph showing what a file contains.
 *
 * Presentation only — no domain logic. What a click asks for and what a late
 * answer is worth lives in {@link ./fileView}, what Escape means in
 * {@link ./fileViewKeys}; this module hides and shows a panel and paints a
 * string into it. DOM-bound, so it is not unit-tested: keep it that thin, the
 * way {@link ./searchHud}, {@link ./rootHud} and {@link ./contextHud} are.
 *
 * Two rules are load-bearing here:
 *
 *  - **`textContent`, never `innerHTML`.** The body is an arbitrary file from
 *    the observed project — a diff of an HTML template, a hex dump, anything.
 *    Assigning it as markup would execute whatever a file happens to contain.
 *  - **The diff's colours are the only decoration.** One class per line prefix
 *    and nothing else; text and hex dumps go in raw, so what is on screen is
 *    exactly what the daemon read.
 */

import type { FileViewState } from "./fileView";
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

/** Class per diff line prefix. CSS owns the actual colours. */
function diffLineClass(line: string): string {
  // Hunk headers first: `@@` is the only three-way split, and a `---`/`+++`
  // file header colouring as a deletion/addition is what git itself does.
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "del";
  return "";
}

export interface FileViewHud {
  /** Show the panel over the graph. */
  open(): void;
  /** Hide the panel and empty it. */
  close(): void;
  isOpen(): boolean;
  /** Paint a state: the header, and the body or why there is none. */
  render(state: FileViewState): void;
}

/** Bind the panel to `#file-view` (a header row and a scrollable body). */
export function createFileViewHud(container: HTMLElement): FileViewHud {
  const pathEl = container.querySelector<HTMLElement>("#file-view-path");
  const modeEl = container.querySelector<HTMLElement>("#file-view-mode");
  const truncEl = container.querySelector<HTMLElement>("#file-view-truncated");
  const bodyEl = container.querySelector<HTMLElement>("#file-view-body");

  /** Put plain text in the body, whatever it holds. */
  function paintPlain(text: string): void {
    if (bodyEl) bodyEl.textContent = text;
  }

  /** Put a diff in the body, one element per line, coloured by its prefix. */
  function paintDiff(content: string): void {
    if (!bodyEl) return;
    const fragment = document.createDocumentFragment();
    for (const line of content.split("\n")) {
      const row = document.createElement("div");
      const cls = diffLineClass(line);
      if (cls) row.className = cls;
      // An empty line still needs its height, or the diff loses its shape.
      row.textContent = line === "" ? " " : line;
      fragment.append(row);
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
      if (truncEl) {
        truncEl.textContent = "";
        truncEl.hidden = true;
      }
      if (bodyEl) bodyEl.replaceChildren();
    },

    isOpen(): boolean {
      return !container.hidden;
    },

    render(state: FileViewState): void {
      if (pathEl) pathEl.textContent = state.path;
      // No mode while the answer is still coming: the daemon, not the click,
      // decides whether this is a diff, text or a hex dump.
      if (modeEl) modeEl.textContent = state.loading ? "" : MODE_LABEL[state.mode] ?? "";
      if (truncEl) {
        truncEl.textContent = TRUNCATED;
        truncEl.hidden = !state.truncated;
      }

      if (!bodyEl) return;
      // A new file starts at its first line, not where the last one was read to.
      bodyEl.scrollTop = 0;
      bodyEl.classList.toggle("error", state.error !== "");
      if (state.error !== "") paintPlain(state.error);
      else if (state.loading) paintPlain(LOADING);
      else if (state.mode === "diff") paintDiff(state.content);
      else paintPlain(state.content);
    },
  };
}
