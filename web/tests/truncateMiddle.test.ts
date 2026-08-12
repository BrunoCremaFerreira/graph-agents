/**
 * Contract tests (RED) for truncateMiddle.
 *
 * The defect it prevents: the HUD shows the observed root, and real roots are
 * long (`/home/brn/projects/clients/acme/backend-services`). Clipping the tail
 * -- the obvious thing, and what CSS ellipsis does by default -- throws away
 * exactly the part that identifies the project, leaving every checkout under
 * ~/projects looking identical. So the elision has to happen in the MIDDLE:
 * the head gives context, the tail gives identity, and both must survive.
 *
 * This is arithmetic on a string, with no DOM involved, so it is specified here
 * rather than left to a stylesheet where it cannot be tested.
 *
 * Expected to FAIL until `truncateMiddle` exists in src/protocol.ts.
 */

import { describe, it, expect } from "vitest";
import { truncateMiddle } from "../src/protocol";

const LONG = "/home/brn/projects/very/deep/nested/graph-agents"; // 47 chars

/** Either elision marker is acceptable; the position is what is specified. */
const ELISION = /(…|\.\.\.)/;

describe("truncateMiddle", () => {
  it("returns text shorter than the limit untouched", () => {
    expect(truncateMiddle("~/projects/x", 40)).toBe("~/projects/x");
  });

  it("returns text exactly at the limit untouched", () => {
    const text = "0123456789";

    expect(truncateMiddle(text, 10)).toBe(text);
  });

  it("cuts longer text down to exactly the limit", () => {
    expect(truncateMiddle(LONG, 30)).toHaveLength(30);
  });

  it("keeps the tail, which is the part that names the project", () => {
    expect(truncateMiddle(LONG, 30).endsWith("graph-agents")).toBe(true);
  });

  it("keeps the head, which is the part that gives context", () => {
    expect(truncateMiddle(LONG, 30).startsWith("/home/")).toBe(true);
  });

  it("marks the elision in the middle rather than at either end", () => {
    const result = truncateMiddle(LONG, 30);
    const marker = ELISION.exec(result);

    expect(marker).not.toBeNull();
    const index = (marker as RegExpExecArray).index;
    expect(index).toBeGreaterThan(0);
    expect(index + (marker as RegExpExecArray)[0].length).toBeLessThan(result.length);
  });

  it.each([1, 3, 5, 12, 46])(
    "never returns more characters than the limit %i allows",
    (max) => {
      expect(truncateMiddle(LONG, max).length).toBeLessThanOrEqual(max);
    },
  );

  it.each([1, 3, 5, 12, 46])("fills the whole limit %i when it has to cut", (max) => {
    expect(truncateMiddle(LONG, max)).toHaveLength(max);
  });

  it("returns an empty string for a zero limit", () => {
    expect(truncateMiddle(LONG, 0)).toBe("");
  });

  it("returns an empty string for a negative limit instead of throwing", () => {
    expect(truncateMiddle(LONG, -5)).toBe("");
  });

  it("returns an empty string for empty text, whatever the limit", () => {
    expect(truncateMiddle("", 20)).toBe("");
    expect(truncateMiddle("", 0)).toBe("");
    expect(truncateMiddle("", -1)).toBe("");
  });

  it("never throws on degenerate input", () => {
    expect(() => truncateMiddle(LONG, 2)).not.toThrow();
    expect(() => truncateMiddle("ab", 1)).not.toThrow();
    expect(() => truncateMiddle(LONG, Number.NaN)).not.toThrow();
  });
});
