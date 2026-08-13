/**
 * The footnote under the activity list: "changes are arriving, but nobody is
 * credited for them".
 *
 * Presentation only. Whether attribution was ever proven is decided by the pure
 * {@link createAttributionMonitor}; this module only paints that answer, because
 * the test environment is `node` and a DOM-bound module cannot be unit-tested.
 * Keep it that thin.
 *
 * The visibility rule is the load-bearing part, and it is deliberately
 * conservative: the note appears only when real (non-seed) activity has already
 * reached the list AND nothing has been attributed. On a freshly opened, idle
 * page "no attribution" would simply be a lie — there is nothing to attribute
 * yet. Once attribution is proven the note is gone for good, which the monitor's
 * own latch guarantees.
 *
 * Styling is the same dead grey as the rest of the HUD: no border, no
 * background, no alert glyph. It is a footnote, not an error.
 */

/** Short, in the tone of the other HUD lines; a question, not an accusation. */
const NOTICE = "no agent attribution — capture hooks not installed?";

export interface AttributionHud {
  /**
   * Repaint from the current state.
   *
   * @param hasActivity Whether the activity list holds at least one entry.
   * @param attributed Whether an attributed event was ever seen.
   */
  update(hasActivity: boolean, attributed: boolean): void;
}

/** Bind the note to its element, which starts empty and hidden in the markup. */
export function createAttributionHud(element: HTMLElement): AttributionHud {
  element.textContent = NOTICE;

  return {
    update(hasActivity: boolean, attributed: boolean): void {
      element.hidden = attributed || !hasActivity;
    },
  };
}
