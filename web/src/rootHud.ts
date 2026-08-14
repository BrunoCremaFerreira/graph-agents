/**
 * The observed-root bar at the top of the screen (ctrl+L): a path field, the
 * completion candidates, and the daemon's refusal when there is one.
 *
 * Presentation only — no domain logic. What a keystroke means lives in
 * {@link ./rootKeys}, what the bar holds and how a late completion is handled in
 * {@link ./rootPrompt}; this module shows a field, reports what was typed, and
 * paints two lines of text. DOM-bound, so it is not unit-tested: keep it that
 * thin, the way {@link ./searchHud} and {@link ./contextHud} are.
 */

/**
 * How many candidates are painted at most.
 *
 * A directory with hundreds of children would otherwise fill the screen with a
 * list nobody reads, over the graph the bar exists to re-aim.
 */
const MAX_SHOWN_MATCHES = 12;

export interface RootHud {
  /** Show the field, focused, with its text selected. */
  open(): void;
  /** Hide the field and forget what was typed. */
  close(): void;
  isOpen(): boolean;
  /** What is currently typed. */
  text(): string;
  /** Put `value` in the field (a completion the daemon just answered). */
  setText(value: string): void;
  /** Paint the candidates, capped; an empty list hides the line. */
  setMatches(matches: readonly string[]): void;
  /** Paint a refusal; an empty message hides the line. */
  setError(message: string): void;
  /** Called on every keystroke in the field, with the new text. */
  onTextChange(callback: (text: string) => void): void;
}

/** Bind the bar to `#root-bar` (an input, a match line and an error line). */
export function createRootHud(container: HTMLElement): RootHud {
  const input = container.querySelector<HTMLInputElement>("#root-input");
  const matchesEl = container.querySelector<HTMLElement>("#root-matches");
  const errorEl = container.querySelector<HTMLElement>("#root-error");
  // A free function, not `this.text()`: the returned methods are handed to
  // callbacks and would lose their receiver.
  const readText = (): string => input?.value ?? "";

  function clearLines(): void {
    if (matchesEl) {
      matchesEl.textContent = "";
      matchesEl.hidden = true;
    }
    if (errorEl) {
      errorEl.textContent = "";
      errorEl.hidden = true;
    }
  }

  return {
    open(): void {
      container.hidden = false;
      // Selected, not just focused: this is an address bar, so the next
      // keystroke replaces the prefilled root instead of appending to it.
      input?.focus();
      input?.select();
    },

    close(): void {
      container.hidden = true;
      if (input) input.value = "";
      clearLines();
      // Give the keyboard back to the page: a focused field inside a hidden
      // box would keep swallowing keys.
      input?.blur();
    },

    isOpen(): boolean {
      return !container.hidden;
    },

    text: readText,

    setText(value: string): void {
      // Only on a real change: assigning to `value` can drop the caret to the
      // end, and this runs on the same keystrokes the user is typing through.
      if (input && input.value !== value) input.value = value;
    },

    setMatches(matches: readonly string[]): void {
      if (!matchesEl) return;
      const shown = matches.slice(0, MAX_SHOWN_MATCHES);
      const rest = matches.length - shown.length;
      matchesEl.textContent = rest > 0 ? `${shown.join("  ")}  +${rest} more` : shown.join("  ");
      matchesEl.hidden = matches.length === 0;
    },

    setError(message: string): void {
      if (!errorEl) return;
      errorEl.textContent = message;
      errorEl.hidden = message === "";
    },

    onTextChange(callback: (text: string) => void): void {
      input?.addEventListener("input", () => callback(input.value));
    },
  };
}
