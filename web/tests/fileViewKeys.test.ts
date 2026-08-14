/**
 * Contract tests (RED) for the file viewer's key binding.
 *
 * The defect this guards is PRECEDENCE, not the key itself. Escape is already
 * spoken for twice on this page: `searchKeys.ts` closes the search box with it
 * and `rootKeys.ts` discards the root bar. Once a modal covers the graph, the
 * key is the modal's -- a viewer that cannot be dismissed without also wiping
 * the search the user was in the middle of is a viewer nobody opens twice.
 *
 * So the binding is its own pure module, like `searchKeys.ts` and `rootKeys.ts`,
 * and for the same reason: decisions taken inside `renderer.ts` need a GL
 * context and cannot be unit-tested. It reads nothing but `key`, so a real
 * KeyboardEvent and a plain object both fit -- and it answers `null` for every
 * key while the panel is CLOSED, which is what lets the caller consult it first
 * and fall through to the search box and the root bar when it declines.
 *
 * Expected to FAIL until src/fileViewKeys.ts exists. One failure reason per test.
 */

import { describe, it, expect } from "vitest";
import { interpretFileViewKey } from "../src/fileViewKeys";

const OPEN = true;
const CLOSED = false;

describe("interpretFileViewKey", () => {
  it("closes the panel on Escape while it is showing", () => {
    expect(interpretFileViewKey({ key: "Escape" }, OPEN)).toBe("close");
  });

  it("leaves Escape alone when the panel is closed, so it still reaches the search box", () => {
    // A handler claiming Escape while no panel exists keeps the search box and
    // the root bar open forever.
    expect(interpretFileViewKey({ key: "Escape" }, CLOSED)).toBe(null);
  });

  it("ignores Enter, which belongs to the root bar and not to a read-only panel", () => {
    expect(interpretFileViewKey({ key: "Enter" }, OPEN)).toBe(null);
  });

  it("ignores an ordinary letter while the panel is open, so nothing is typed at a viewer", () => {
    expect(interpretFileViewKey({ key: "f" }, OPEN)).toBe(null);
  });

  it("ignores the arrow keys, which scroll the content rather than dismissing it", () => {
    expect(interpretFileViewKey({ key: "ArrowDown" }, OPEN)).toBe(null);
  });

  it("does not read a lowercase \"escape\" as the key, since the browser reports \"Escape\"", () => {
    expect(interpretFileViewKey({ key: "escape" }, OPEN)).toBe(null);
  });

  it("ignores Tab, which the root bar completes with", () => {
    expect(interpretFileViewKey({ key: "Tab" }, OPEN)).toBe(null);
  });
});
