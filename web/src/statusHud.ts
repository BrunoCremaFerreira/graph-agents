/**
 * The git status panel in the bottom-right corner: what is still uncommitted.
 *
 * Presentation only. Every decision — visibility, order, the cap, the glyph and
 * the class of each row — belongs to the pure {@link buildStatusList}; this
 * module only paints what it returns, because the test environment is `node`
 * and a DOM-bound module cannot be unit-tested. If you find yourself choosing
 * an order or a cut-off here, you are in the wrong file.
 *
 * Two details are load-bearing:
 *
 *  - **A full repaint is fine here, unlike in `eventHud.ts`.** The daemon
 *    dedupes the status frame, so one arrives only when the working tree really
 *    changed — not once per file save. What is NOT fine is losing the reader's
 *    place, so `scrollTop` is restored around the repaint.
 *  - **One delegated listener on the `<ol>`,** not one per row. The rows are
 *    thrown away and rebuilt on every frame, so per-row listeners would be
 *    re-bound wholesale each time, for no gain.
 */

import { splitPath } from "./eventLog";
import { buildStatusList, type StatusRow } from "./statusList";
import type { GitStatus } from "./protocol";

export interface StatusHud {
  /** Paint the last status frame, or hide the panel when there is none. */
  render(status: GitStatus | null): void;
  /**
   * Empty the panel and hide it, because the daemon switched roots: the rows
   * name files of a project that is no longer on screen.
   */
  clear(): void;
}

/** Build the `<li>` for a row: glyph, dimmed directory, file name. */
function buildRow(row: StatusRow): HTMLLIElement {
  const item = document.createElement("li");

  const op = document.createElement("span");
  op.className = `op ${row.cssClass}`;
  op.textContent = row.glyph;

  const { dir, name } = splitPath(row.path);
  const dirEl = document.createElement("span");
  dirEl.className = "dir";
  dirEl.textContent = dir;

  const nameEl = document.createElement("span");
  nameEl.className = "name";
  nameEl.textContent = name;

  item.append(op, dirEl, nameEl);
  item.title = row.path;
  // The delegated handler reads the path back off the row it was given.
  item.dataset.path = row.path;
  return item;
}

/** `N alterações`, plus what the cap left out. */
function countText(total: number, hidden: number): string {
  const base = total === 1 ? "1 alteração" : `${total} alterações`;
  return hidden > 0 ? `${base} · +${hidden} ocultos` : base;
}

/**
 * Bind the git status panel to its container.
 *
 * @param rootEl The `<div id="status">` wrapper; its `hidden` attribute is what
 *   keeps the panel off screen over a clean tree.
 * @param onPick Called with the path of a clicked row, to open the same viewer
 *   a click in the graph opens.
 * @param max Row cap, forwarded to the underlying model.
 */
export function createStatusHud(
  rootEl: HTMLElement,
  onPick: (path: string) => void,
  max?: number,
): StatusHud {
  const listEl = rootEl.querySelector("#status-list");
  const countEl = rootEl.querySelector("#status-count");

  listEl?.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const row = target.closest("li");
    const path = row instanceof HTMLElement ? row.dataset.path : undefined;
    if (path) onPick(path);
  });

  return {
    render(status: GitStatus | null): void {
      const model = buildStatusList(status, max);
      rootEl.hidden = !model.visible;
      if (countEl) countEl.textContent = countText(model.total, model.hidden);
      if (!listEl) return;

      // The rows are replaced wholesale, which resets the scroll to the top;
      // without this the list jumps under the reader every time the tree
      // changes.
      const scroll = listEl.scrollTop;
      listEl.replaceChildren(...model.rows.map(buildRow));
      listEl.scrollTop = scroll;
    },

    clear(): void {
      rootEl.hidden = true;
      if (countEl) countEl.textContent = "";
      if (!listEl) return;
      listEl.replaceChildren();
      listEl.scrollTop = 0;
    },
  };
}
