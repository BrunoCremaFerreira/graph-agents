/**
 * Contract tests (RED) for the observed-root prompt's key bindings.
 *
 * The defect: the root is frozen at daemon boot. Watching a second project
 * means killing the daemon, exporting GRAPHAGENTS_PROJECT_ROOT again and
 * reloading the page -- so the visualiser is unusable for anyone who works in
 * more than one checkout. The page is getting a prompt (ctrl+L) that asks the
 * daemon to switch roots, and the mapping from a key press to a command is the
 * first decision it makes.
 *
 * That mapping lives in a pure module for the same reason `searchKeys.ts` does:
 * decisions taken inside `renderer.ts` need a GL context and cannot be
 * unit-tested. It reads nothing but `key` / `ctrlKey` / `metaKey`, so a real
 * KeyboardEvent and a plain object both fit.
 *
 * The trap is the same one the search box has, one key worse. Once the bar is
 * open the user is TYPING A PATH: `l` occurs in "web/src/labels.ts", and Tab,
 * Enter and Escape mean completion / apply / discard only while the bar is
 * showing. Closed, Tab still has to move focus through the page and Escape must
 * reach whatever else listens for it -- a handler that swallowed them would
 * break the rest of the UI to serve a bar nobody opened.
 *
 * Expected to FAIL until src/rootKeys.ts exists. One failure reason per test.
 */

import { describe, it, expect } from "vitest";
import { interpretRootKey } from "../src/rootKeys";

/** A key event reduced to what the binding actually looks at. */
function key(k: string, mods: { ctrlKey?: boolean; metaKey?: boolean } = {}) {
  return { key: k, ctrlKey: mods.ctrlKey ?? false, metaKey: mods.metaKey ?? false };
}

const OPEN = true;
const CLOSED = false;

describe("interpretRootKey", () => {
  it("opens the root prompt on ctrl+l", () => {
    expect(interpretRootKey(key("l", { ctrlKey: true }), CLOSED)).toBe("open");
  });

  it("opens the root prompt on cmd+l, which is the shortcut a mac user reaches for", () => {
    expect(interpretRootKey(key("l", { metaKey: true }), CLOSED)).toBe("open");
  });

  it("opens on ctrl+L, because a held shift capitalises the reported key", () => {
    // The browser reports `key` after the modifiers are applied, so caps lock or
    // a stray shift would otherwise silently disable the shortcut.
    expect(interpretRootKey(key("L", { ctrlKey: true }), CLOSED)).toBe("open");
  });

  it("still answers open while the bar is showing, so the field can be refocused", () => {
    // Clicking on the graph blurs the input; ctrl+L has to bring it back rather
    // than becoming inert once the bar exists.
    expect(interpretRootKey(key("l", { ctrlKey: true }), OPEN)).toBe("open");
  });

  it("completes the path on Tab while the bar is open", () => {
    expect(interpretRootKey(key("Tab"), OPEN)).toBe("complete");
  });

  it("leaves Tab to the page when the bar is closed, so focus still moves", () => {
    expect(interpretRootKey(key("Tab"), CLOSED)).toBe(null);
  });

  it("applies the typed root on Enter while the bar is open", () => {
    expect(interpretRootKey(key("Enter"), OPEN)).toBe("submit");
  });

  it("ignores Enter when the bar is closed, since there is nothing to apply", () => {
    expect(interpretRootKey(key("Enter"), CLOSED)).toBe(null);
  });

  it("discards the typed root on Escape while the bar is open", () => {
    expect(interpretRootKey(key("Escape"), OPEN)).toBe("cancel");
  });

  it("ignores Escape when the bar is closed, leaving the key to the rest of the page", () => {
    // The search box also answers Escape; a root handler that claimed it while
    // closed would keep the search open forever.
    expect(interpretRootKey(key("Escape"), CLOSED)).toBe(null);
  });

  it("treats a bare l as a character to type, not as a command", () => {
    // The defect this guards: with the bar open, typing the l of
    // "web/src/labels.ts" would otherwise re-fire the open command mid-path.
    expect(interpretRootKey(key("l"), OPEN)).toBe(null);
  });

  it("ignores an ordinary letter whether or not the bar is open", () => {
    expect(interpretRootKey(key("a"), OPEN)).toBe(null);
    expect(interpretRootKey(key("a"), CLOSED)).toBe(null);
  });

  it("ignores a modified key that is not the root shortcut", () => {
    // ctrl+a, ctrl+c and friends belong to the browser and to the input.
    expect(interpretRootKey(key("a", { ctrlKey: true }), OPEN)).toBe(null);
  });

  it("does not read ctrl+f as a root command, because that shortcut is the search box", () => {
    expect(interpretRootKey(key("f", { ctrlKey: true }), CLOSED)).toBe(null);
  });
});
