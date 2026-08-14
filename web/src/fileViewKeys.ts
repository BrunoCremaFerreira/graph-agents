/**
 * What a key press means to the file viewer panel.
 *
 * Same shape and same reason as {@link ./searchKeys} and {@link ./rootKeys}: the
 * mapping is a decision, and decisions taken inside `renderer.ts` need a GL
 * context and cannot be unit-tested. It reads nothing but `key`, so a real
 * `KeyboardEvent` and a plain object both fit.
 *
 * What this exists to settle is PRECEDENCE, not the key. Escape is spoken for
 * twice already — the search box closes with it, the root bar discards with it —
 * and once a modal covers the graph the key is the modal's. So the caller
 * consults this binding FIRST and falls through to the others when it declines,
 * which is why every key answers null while the panel is closed: a handler
 * claiming Escape with no panel on screen would keep the search box and the root
 * bar open forever.
 */

/** The slice of a keyboard event the binding looks at. */
export interface FileViewKeyEvent {
  readonly key: string;
}

/** What the caller should do, or null to leave the key to the rest of the page. */
export type FileViewCommand = "close";

export function interpretFileViewKey(
  event: FileViewKeyEvent,
  open: boolean,
): FileViewCommand | null {
  if (!open) return null;
  // The panel is read-only: every other key, arrows included, belongs to the
  // content being scrolled and to nothing here.
  return event.key === "Escape" ? "close" : null;
}
