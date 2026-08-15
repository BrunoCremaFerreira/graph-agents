/**
 * Contract tests (RED) for the product name the front end says out loud.
 *
 * The defect: the name is currently a bare literal inside `renderer.ts`
 * (`console.error("<old name>: frame failed:", error)`), and `renderer.ts`
 * needs a GL context, so nothing about it can be unit-tested. During a rename
 * that literal is exactly the kind of occurrence that survives -- it is not in
 * a manifest, not in the HTML, and no test reads it -- and the result is a
 * console line naming a project that no longer exists, which is what someone
 * greps for when the canvas has gone black.
 *
 * So the name becomes a constant in a pure sibling (`src/branding.ts`), the way
 * `view.ts` and `labels.ts` hold the renderer's other decisions, and
 * `renderer.ts` imports it instead of spelling it. One place to rename, and a
 * test can reach it.
 *
 * Expected to FAIL until `src/branding.ts` exists and exports `APP_NAME`.
 */

import { describe, it, expect } from "vitest";
import { APP_NAME } from "../src/branding";

describe("APP_NAME", () => {
  it("is the project's name, in the hyphenated form a human reads", () => {
    expect(APP_NAME).toBe("rhizome-graph");
  });
});
