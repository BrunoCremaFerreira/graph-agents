/**
 * What a key press means to the observed-root bar.
 *
 * Same shape and same reason as {@link ./searchKeys}: the mapping is a decision,
 * and decisions taken inside `renderer.ts` need a GL context and cannot be
 * unit-tested. It reads nothing but the fields below, so a real `KeyboardEvent`
 * and a plain object both fit.
 *
 * The trap here is the search box's, one key worse. Once the bar is open the
 * user is TYPING A PATH: the bare `l` of `web/src/labels.ts` is a character, not
 * a re-open, and Tab / Enter / Escape mean complete / apply / discard only while
 * the bar is showing. Closed, Tab must still move focus and Escape must still
 * reach whatever else listens for it -- the search box also answers Escape, and
 * a root handler claiming it while closed would keep the search open forever.
 */

/** The slice of a keyboard event the binding looks at. */
export interface RootKeyEvent {
  readonly key: string;
  readonly ctrlKey: boolean;
  readonly metaKey: boolean;
}

/** What the caller should do, or null to leave the key to the page. */
export type RootCommand = "open" | "complete" | "submit" | "cancel";

export function interpretRootKey(event: RootKeyEvent, open: boolean): RootCommand | null {
  if (event.ctrlKey || event.metaKey) {
    // The browser reports `key` with the modifiers already applied, so a stray
    // shift or caps lock would otherwise silently disable the shortcut. cmd is
    // the same shortcut: it is what a mac user reaches for. It still answers
    // while the bar is open, so clicking on the graph does not strand the field
    // without focus.
    return event.key.toLowerCase() === "l" ? "open" : null;
  }

  // Everything below is a command only while the bar is showing; closed, these
  // keys belong to the rest of the page.
  if (!open) return null;
  if (event.key === "Tab") return "complete";
  if (event.key === "Enter") return "submit";
  if (event.key === "Escape") return "cancel";
  return null;
}
