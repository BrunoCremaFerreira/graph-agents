/**
 * What a key press means to the search box.
 *
 * The mapping is a decision, and decisions taken inside `renderer.ts` need a GL
 * context and cannot be tested -- so it lives in a pure module, the way
 * {@link ./view} and {@link ./labels} do. It reads nothing but the fields below,
 * so a real `KeyboardEvent` and a plain object both fit.
 *
 * The trap this exists to avoid: once the field is open the user is TYPING. A
 * bare "f" is a character, not a command; a handler reacting to unmodified
 * letters would re-fire the shortcut on every keystroke of "footer.ts" and make
 * the field unusable. Only the modified shortcut and the navigation keys mean
 * anything, and the navigation keys only while the field is open.
 */

/** The slice of a keyboard event the binding looks at. */
export interface SearchKeyEvent {
  readonly key: string;
  readonly ctrlKey: boolean;
  readonly metaKey: boolean;
}

/** What the caller should do, or null to leave the key to the page. */
export type SearchCommand = "open" | "next" | "close";

export function interpretSearchKey(event: SearchKeyEvent, open: boolean): SearchCommand | null {
  if (event.ctrlKey || event.metaKey) {
    // The browser reports `key` with the modifiers already applied, so a stray
    // shift or caps lock would otherwise silently disable the shortcut. cmd is
    // the same shortcut: it is what a mac user reaches for.
    return event.key.toLowerCase() === "f" ? "open" : null;
  }

  // Everything below is a command only while the field is showing; closed,
  // these keys belong to the rest of the page.
  if (!open) return null;
  if (event.key === "F3" || event.key === "Enter") return "next";
  if (event.key === "Escape") return "close";
  return null;
}
