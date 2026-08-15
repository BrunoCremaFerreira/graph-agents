/**
 * Contract tests (RED) for the file viewer's click binding.
 *
 * The defect this guards is that the viewer has exactly one way out today --
 * Escape, through `fileViewKeys.ts` -- and a modal covering the whole graph with
 * no visible affordance to dismiss it reads as a page that has hung. The `×`
 * button in `#file-view-head` is that affordance, and deciding WHICH click it is
 * has to be pure for the same reason the key binding is: `fileViewHud.ts` is
 * DOM-bound and `renderer.ts` needs a GL context, so neither is reachable from a
 * `node` test environment. This module reads nothing but the id of the element
 * that was clicked and whether the panel is open, so a real
 * `event.target.id` and a plain string both fit, and the suite stays free of
 * jsdom and of mocks.
 *
 * It follows the same PRECEDENCE doctrine as `interpretFileViewKey`: every input
 * answers null while the panel is CLOSED, because a handler that acts with no
 * panel on screen is a bug -- the graph is full of clickable file dots and a
 * stale close handler would swallow them.
 *
 * Expected to FAIL until src/fileViewClicks.ts exists. One failure reason per test.
 */

import { describe, it, expect } from "vitest";
import { FILE_VIEW_CLOSE_ID, interpretFileViewClick } from "../src/fileViewClicks";

const OPEN = true;
const CLOSED = false;

describe("interpretFileViewClick", () => {
  it("closes the panel when the close button is clicked while it is showing", () => {
    expect(interpretFileViewClick(FILE_VIEW_CLOSE_ID, OPEN)).toBe("close");
  });

  it("matches the close button by the id the module itself exports, so the id lives in one place", () => {
    // The HUD paints the button and index.html declares it; if this literal ever
    // has to be edited in three files, one of them will be missed.
    expect(FILE_VIEW_CLOSE_ID).toBe("file-view-close");
    expect(interpretFileViewClick("file-view-close", OPEN)).toBe("close");
  });

  it("ignores the close button's id while the panel is closed, so a hidden button cannot act", () => {
    expect(interpretFileViewClick(FILE_VIEW_CLOSE_ID, CLOSED)).toBe(null);
  });

  it("does not close on the backdrop, so a stray click outside cannot throw away a long read", () => {
    // Deliberate scope, not an oversight: click-outside-to-dismiss is the
    // easiest way to lose a file you were halfway through reading, and this
    // panel has no state to recover. Changing this line should be a decision
    // someone takes on purpose, not a drift.
    expect(interpretFileViewClick("file-view-backdrop", OPEN)).toBe(null);
  });

  it("ignores a click in the content body, where the user is selecting text", () => {
    expect(interpretFileViewClick("file-view-body", OPEN)).toBe(null);
  });

  it("ignores a click on the path in the header, which sits beside the button", () => {
    expect(interpretFileViewClick("file-view-path", OPEN)).toBe(null);
  });

  it("ignores a click on the panel itself, so the frame is not a dismiss target", () => {
    expect(interpretFileViewClick("file-view-panel", OPEN)).toBe(null);
  });

  it("ignores a click that resolved to no identified element", () => {
    // Most nodes on this page carry no id at all; the caller passes "" rather
    // than branching, so the empty string must be an ordinary miss.
    expect(interpretFileViewClick("", OPEN)).toBe(null);
  });

  it("ignores the empty id while the panel is closed as well", () => {
    expect(interpretFileViewClick("", CLOSED)).toBe(null);
  });
});
