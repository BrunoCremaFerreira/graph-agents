/**
 * What a click means to the file viewer panel.
 *
 * Same shape and same reason as {@link ./fileViewKeys}: the mapping is a
 * decision, and decisions taken in {@link ./fileViewHud} need a DOM — the test
 * environment is `node` — while `renderer.ts` needs a GL context. This binding
 * reads nothing but the id of the element that was clicked and whether the
 * panel is open, so a real `event.target.id` and a plain string both fit.
 *
 * It follows the same PRECEDENCE doctrine as `interpretFileViewKey`: every id
 * answers null while the panel is CLOSED. The graph underneath is full of
 * clickable file dots, and a close handler still acting with no panel on screen
 * would swallow the click that opens the next file.
 *
 * The backdrop is deliberately NOT a dismiss target. Click-outside-to-dismiss is
 * the easiest way to lose a file you were halfway through reading, and this
 * panel keeps no state to recover: the way out is Escape or the `×`, both of
 * which are aimed at. Making the backdrop close is a decision to take on
 * purpose, not a line to drift into.
 */

/** The id of the close button, declared once and shared with the markup. */
export const FILE_VIEW_CLOSE_ID = "file-view-close";

/** What the caller should do, or null to leave the click to the rest of the page. */
export type FileViewClickCommand = "close";

export function interpretFileViewClick(
  targetId: string,
  open: boolean,
): FileViewClickCommand | null {
  if (!open) return null;
  // Most nodes on the page carry no id at all, so the empty string is an
  // ordinary miss and every other id — panel, header, body — belongs to the
  // content being read.
  return targetId === FILE_VIEW_CLOSE_ID ? "close" : null;
}
