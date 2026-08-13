/**
 * The recent-activity list in the bottom-left HUD: newest change on top.
 *
 * Presentation only. Every decision about what belongs in the list — seed
 * filtering, collapsing repeats, ordering, the cap — lives in the pure
 * {@link createEventLog}; this module only paints it, because the test
 * environment is `node` and a DOM-bound module cannot be unit-tested. Keep it
 * that thin.
 *
 * Two details are load-bearing and easy to lose in a refactor:
 *
 *  - **No full re-render.** One DOM node per entry, updated in place: a push
 *    either bumps the counter on the top node or inserts a single node and
 *    drops the overflow from the end. Rebuilding the list on every event would
 *    thrash during a burst of writes and reset the reader's scroll position.
 *  - **Scroll anchoring.** Inserting at the top shifts everything below it
 *    down, so when the reader has scrolled away from the top we add the height
 *    we inserted back onto `scrollTop`; otherwise the line being read jumps out
 *    from under the cursor whenever an agent saves a file.
 *
 * Wheel scrolling needs no event handling here: the zoom listener is bound to
 * the canvas element, so a wheel over this list never reaches it. The list only
 * has to opt back into hit-testing (`pointer-events: auto`), since `#hud` sets
 * `pointer-events: none` so drags pass through to the canvas.
 */

import { createEventLog, splitPath, type LogEntry } from "./eventLog";
import type { AgentEvent, EventType } from "./protocol";

/** Operation glyph. The single splash of colour in an otherwise grey HUD. */
const GLYPH: Record<EventType, string> = { A: "+", M: "~", D: "−" };

/** Class suffix per operation, so CSS owns the actual colours. */
const OP_CLASS: Record<EventType, string> = { A: "a", M: "m", D: "d" };

export interface EventHud {
  /** Offer an event to the list. Dropped events (seed) change nothing. */
  push(event: AgentEvent): void;
}

/** Build the `<li>` for an entry: glyph, dimmed directory, file name, count. */
function buildRow(entry: LogEntry): HTMLLIElement {
  const row = document.createElement("li");

  const op = document.createElement("span");
  op.className = `op ${OP_CLASS[entry.type] ?? ""}`;
  op.textContent = GLYPH[entry.type] ?? "?";

  const { dir, name } = splitPath(entry.path);
  const dirEl = document.createElement("span");
  dirEl.className = "dir";
  dirEl.textContent = dir;

  const nameEl = document.createElement("span");
  nameEl.className = "name";
  nameEl.textContent = name;

  const rep = document.createElement("span");
  rep.className = "rep";

  row.append(op, dirEl, nameEl, rep);
  row.title = entry.path;
  return row;
}

/** Show `×N` once an entry has folded repeats; stay silent at one. */
function paintCount(row: Element, count: number): void {
  const rep = row.querySelector(".rep");
  if (rep) rep.textContent = count > 1 ? `×${count}` : "";
}

/**
 * Bind the activity list to an `<ol>` element.
 *
 * @param listEl The `<ol id="log">` to fill.
 * @param max Entry cap, forwarded to the underlying log.
 */
export function createEventHud(listEl: HTMLElement, max?: number): EventHud {
  const log = createEventLog(max);

  return {
    push(event: AgentEvent): void {
      const previousTop = log.entries()[0];
      if (!log.push(event)) return;

      const entries = log.entries();
      const top = entries[0];
      if (!top) return;

      // Same entry object back on top => the event folded into it.
      if (top === previousTop) {
        const row = listEl.firstElementChild;
        if (row) paintCount(row, top.count);
        return;
      }

      const row = buildRow(top);
      paintCount(row, top.count);
      listEl.insertBefore(row, listEl.firstChild);

      // Keep the reader's line still: the insertion pushed it down by `height`.
      if (listEl.scrollTop > 0) listEl.scrollTop += row.offsetHeight;

      while (listEl.childElementCount > entries.length && listEl.lastElementChild) {
        listEl.removeChild(listEl.lastElementChild);
      }
    },
  };
}
