/**
 * Contract tests (RED) for the search key bindings.
 *
 * A seeded project puts hundreds of nodes on screen and there is no way to find
 * one: the user has to recognise a dot. Search fixes that, but the shortcut that
 * opens it is a decision, and decisions taken inside `renderer.ts` need a GL
 * context and cannot be tested -- so the mapping from a key event to a command
 * lives in a pure module, the same way `view.ts` and `labels.ts` do.
 *
 * The trap this module exists to avoid: once the field is open the user is
 * TYPING. A plain "f" is a character, not a command, and a handler that reacts
 * to bare letters would make the field unusable -- every match letter would
 * reopen or re-trigger the search. Only modified keys and the navigation keys
 * mean anything, and the navigation keys only while the field is open.
 *
 * Expected to FAIL until src/searchKeys.ts exists.
 *
 * One failure reason per test.
 */

import { describe, it, expect } from "vitest";
import { interpretSearchKey } from "../src/searchKeys";

/** A key event reduced to what the binding actually looks at. */
function key(k: string, mods: { ctrlKey?: boolean; metaKey?: boolean } = {}) {
  return { key: k, ctrlKey: mods.ctrlKey ?? false, metaKey: mods.metaKey ?? false };
}

const OPEN = true;
const CLOSED = false;

describe("interpretSearchKey", () => {
  it("opens the search on ctrl+f", () => {
    expect(interpretSearchKey(key("f", { ctrlKey: true }), CLOSED)).toBe("open");
  });

  it("opens the search on cmd+f, which is the shortcut a mac user reaches for", () => {
    expect(interpretSearchKey(key("f", { metaKey: true }), CLOSED)).toBe("open");
  });

  it("opens on ctrl+F, because a held shift capitalises the reported key", () => {
    // The browser reports `key` after the modifiers are applied, so caps lock or
    // a stray shift would otherwise silently disable the shortcut.
    expect(interpretSearchKey(key("F", { ctrlKey: true }), CLOSED)).toBe("open");
  });

  it("still answers open when the search is already showing, so the field can be refocused", () => {
    expect(interpretSearchKey(key("f", { ctrlKey: true }), OPEN)).toBe("open");
  });

  it("steps to the next match on F3 while the search is open", () => {
    expect(interpretSearchKey(key("F3"), OPEN)).toBe("next");
  });

  it("ignores F3 when no search is running, since there is nothing to step through", () => {
    expect(interpretSearchKey(key("F3"), CLOSED)).toBe(null);
  });

  it("steps to the next match on Enter while the search is open", () => {
    expect(interpretSearchKey(key("Enter"), OPEN)).toBe("next");
  });

  it("ignores Enter when the search is closed", () => {
    expect(interpretSearchKey(key("Enter"), CLOSED)).toBe(null);
  });

  it("closes the search on Escape", () => {
    expect(interpretSearchKey(key("Escape"), OPEN)).toBe("close");
  });

  it("ignores Escape when the search is closed, leaving the key to the rest of the page", () => {
    expect(interpretSearchKey(key("Escape"), CLOSED)).toBe(null);
  });

  it("treats a bare f as a character to type, not as a command", () => {
    // The defect this guards: with the field open, typing the letter f in
    // "footer.ts" would otherwise re-fire the open command on every keystroke.
    expect(interpretSearchKey(key("f"), OPEN)).toBe(null);
  });

  it("ignores an ordinary letter whether or not the search is open", () => {
    expect(interpretSearchKey(key("a"), OPEN)).toBe(null);
    expect(interpretSearchKey(key("a"), CLOSED)).toBe(null);
  });

  it("ignores a modified key that is not the search shortcut", () => {
    // ctrl+a, ctrl+c and friends belong to the browser and to the input.
    expect(interpretSearchKey(key("a", { ctrlKey: true }), OPEN)).toBe(null);
  });
});
