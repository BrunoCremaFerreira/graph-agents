/**
 * The search box at the top of the screen: an input and a match counter.
 *
 * Presentation only — no domain logic. What a keystroke means lives in
 * {@link ./searchKeys}, what a query matches and where the camera goes in
 * {@link ./search}; this module shows a field, reports what was typed, and
 * paints a count. DOM-bound, so it is not unit-tested: keep it that thin, the
 * way {@link ./contextHud} and {@link ./eventHud} are.
 */

/** Shown while a query is typed that nothing answers. */
const NO_MATCHES = "no matches";

export interface SearchHud {
  /** Show the field, focused, with any previous text selected. */
  open(): void;
  /** Hide the field and forget what was typed. */
  close(): void;
  isOpen(): boolean;
  /** What is currently typed. */
  query(): string;
  /** Paint `activeIndex + 1 / matchCount`, or the empty/no-match states. */
  setStatus(matchCount: number, activeIndex: number): void;
  /** Called on every keystroke in the field, with the new text. */
  onQueryChange(callback: (query: string) => void): void;
}

/** Bind the box to `#search` (an input plus a count span). */
export function createSearchHud(container: HTMLElement): SearchHud {
  const input = container.querySelector<HTMLInputElement>("#search-input");
  const countEl = container.querySelector<HTMLElement>("#search-count");
  // A free function, not `this.query()`: the returned methods are handed to
  // callbacks and would lose their receiver.
  const readQuery = (): string => input?.value ?? "";

  return {
    open(): void {
      container.hidden = false;
      // Selected, not just focused: reopening over an old query lets the next
      // keystroke replace it instead of appending to it.
      input?.focus();
      input?.select();
    },

    close(): void {
      container.hidden = true;
      if (input) input.value = "";
      if (countEl) countEl.textContent = "";
      // Give the keyboard back to the page: a focused field inside a hidden
      // box would keep swallowing keys.
      input?.blur();
    },

    isOpen(): boolean {
      return !container.hidden;
    },

    query: readQuery,

    setStatus(matchCount: number, activeIndex: number): void {
      if (!countEl) return;
      // Nothing typed yet is not "no results": the box has just opened.
      if (readQuery().trim() === "") countEl.textContent = "";
      else if (matchCount === 0) countEl.textContent = NO_MATCHES;
      // The index is 0-based inside the model and 1-based on screen.
      else countEl.textContent = `${activeIndex + 1} / ${matchCount}`;
    },

    onQueryChange(callback: (query: string) => void): void {
      input?.addEventListener("input", () => callback(input.value));
    },
  };
}
