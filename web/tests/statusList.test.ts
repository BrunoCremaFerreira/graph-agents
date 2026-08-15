/**
 * Contract tests (RED) for the model behind the HUD's git status panel.
 *
 * The defect: the graph says a file changed and never whether that change is
 * still uncommitted, so "what is dirty right now?" sends the user out of the
 * page and into a terminal. The daemon answers with a `status` frame; this
 * module decides what is actually shown.
 *
 * Pure -- no DOM, no three.js -- for the same reason as `eventLog.ts` and
 * `labels.ts`: `statusHud.ts` must stay a dumb painter, and the test environment
 * is `node`. Four decisions carry the weight:
 *
 *  - **`visible` derives from the ENTRY COUNT, never from `repo`.** The panel
 *    exists to show uncommitted work; a clean tree in a real repository has
 *    nothing to say, and a panel that appears empty over a clean checkout is a
 *    permanent strip of chrome reporting nothing. Keying visibility on `repo`
 *    would do exactly that.
 *  - **The order is total and computed here.** Grouped by state in
 *    STATE_ORDER, then by path compared as strings. NOT `localeCompare`: its
 *    result depends on the runtime's locale data, so the same dirty tree would
 *    list differently on two machines, and rows would swap under the reader's
 *    eye when the daemon repolls.
 *  - **The cut respects the order.** `rows` is the SORTED list truncated at
 *    `max`, so what survives a 5000-file `git status` is the first rows of the
 *    order, not the first rows of whatever order the daemon happened to walk in.
 *    `hidden` is what the panel says it left out.
 *  - **Glyph and CSS class travel WITH the row**, so the painter chooses
 *    nothing, and a fifth state added later cannot land in the DOM unstyled.
 *
 * Purity is tested explicitly, because sorting the received array in place is
 * the easy mistake here: the caller keeps that array (it is the parsed frame)
 * and a shuffled copy of it would leak into anything else reading the status.
 *
 * Expected to FAIL until src/statusList.ts exists. One failure reason per test.
 */

import { describe, it, expect } from "vitest";
import {
  buildStatusList,
  STATE_ORDER,
  STATE_GLYPH,
  STATE_CLASS,
  DEFAULT_MAX_ROWS,
  type StatusListModel,
} from "../src/statusList";
import type { GitStatus, GitStatusEntry, GitStatusState } from "../src/protocol";

function entry(path: string, state: GitStatusState): GitStatusEntry {
  return { path, state };
}

function status(entries: GitStatusEntry[], overrides: Partial<GitStatus> = {}): GitStatus {
  return { repo: true, truncated: false, entries, ...overrides };
}

describe("status list: when the panel is on screen at all", () => {
  it("stays hidden before any status frame has arrived", () => {
    const model = buildStatusList(null);

    expect(model.visible).toBe(false);
    expect(model.rows).toEqual([]);
    expect(model.total).toBe(0);
    expect(model.hidden).toBe(0);
  });

  it("stays hidden on a clean working tree, instead of showing an empty panel", () => {
    const model = buildStatusList(status([]));

    expect(model.visible).toBe(false);
    expect(model.rows).toEqual([]);
    expect(model.total).toBe(0);
    expect(model.hidden).toBe(0);
  });

  it("shows as soon as one file is dirty", () => {
    const model = buildStatusList(status([entry("a.ts", "modified")]));

    expect(model.visible).toBe(true);
    expect(model.total).toBe(1);
  });

  it("derives visibility from the entries, not from repo: entries outside a repo still show", () => {
    // `repo: false` with entries is a daemon the page must not argue with. The
    // requirement is "appear only when there are uncommitted changes", and there
    // are.
    const model = buildStatusList(status([entry("a.ts", "untracked")], { repo: false }));

    expect(model.visible).toBe(true);
    expect(model.rows).toHaveLength(1);
  });

  it("derives visibility from the entries, not from repo: a clean repo stays hidden", () => {
    const model = buildStatusList(status([], { repo: true }));

    expect(model.visible).toBe(false);
  });
});

describe("status list: order", () => {
  it("groups the states in STATE_ORDER, whatever order they arrived in", () => {
    const model = buildStatusList(
      status([
        entry("d.ts", "untracked"),
        entry("c.ts", "deleted"),
        entry("b.ts", "added"),
        entry("a.ts", "modified"),
      ]),
    );

    expect(model.rows.map((row) => row.state)).toEqual([
      "modified",
      "added",
      "deleted",
      "untracked",
    ]);
  });

  it("orders paths inside a group ascending", () => {
    const model = buildStatusList(
      status([
        entry("web/src/renderer.ts", "modified"),
        entry("daemon/server.py", "modified"),
        entry("rhizome_graph/tree.py", "modified"),
      ]),
    );

    expect(model.rows.map((row) => row.path)).toEqual([
      "daemon/server.py",
      "rhizome_graph/tree.py",
      "web/src/renderer.ts",
    ]);
  });

  it("compares paths as strings, so the order does not depend on the runtime locale", () => {
    // `localeCompare` would put "a.txt" before "README.md" and "Z.txt"; direct
    // `<` orders by code point, which is the same on every machine and stable
    // across repolls.
    const model = buildStatusList(
      status([
        entry("a.txt", "modified"),
        entry("Z.txt", "modified"),
        entry("README.md", "modified"),
      ]),
    );

    expect(model.rows.map((row) => row.path)).toEqual(["README.md", "Z.txt", "a.txt"]);
  });

  it("sorts by group first and path second, never by path alone", () => {
    const model = buildStatusList(
      status([
        entry("a.ts", "untracked"),
        entry("z.ts", "modified"),
      ]),
    );

    expect(model.rows.map((row) => row.path)).toEqual(["z.ts", "a.ts"]);
  });

  it("keeps both rows when the same path shows up under two states", () => {
    // git reports a staged addition and an unstaged deletion of one path; the
    // panel must not silently swallow half of that.
    const model = buildStatusList(
      status([entry("a.ts", "deleted"), entry("a.ts", "added")]),
    );

    expect(model.total).toBe(2);
    expect(model.rows.map((row) => row.state)).toEqual(["added", "deleted"]);
  });
});

describe("status list: rows carry their own presentation", () => {
  it.each([
    ["modified", "~", "m"],
    ["added", "+", "a"],
    ["deleted", "−", "d"],
    ["untracked", "?", "u"],
  ] as const)("gives a %s row its glyph and css class", (state, glyph, cssClass) => {
    const model = buildStatusList(status([entry("a.ts", state)]));

    expect(model.rows[0]).toEqual({ path: "a.ts", state, glyph, cssClass });
  });

  it("keeps the path exactly as received, normalizing nothing", () => {
    const model = buildStatusList(status([entry("web//src/a b.ts", "modified")]));

    expect(model.rows[0].path).toBe("web//src/a b.ts");
  });

  it("exposes the four states in display order", () => {
    expect([...STATE_ORDER]).toEqual(["modified", "added", "deleted", "untracked"]);
  });

  it("exposes a glyph for every state it can order", () => {
    expect(STATE_GLYPH).toEqual({
      modified: "~",
      added: "+",
      deleted: "−",
      untracked: "?",
    });
  });

  it("exposes a css class for every state it can order", () => {
    expect(STATE_CLASS).toEqual({
      modified: "m",
      added: "a",
      deleted: "d",
      untracked: "u",
    });
  });
});

describe("status list: the cap", () => {
  function manyModified(count: number): GitStatus {
    const entries: GitStatusEntry[] = [];
    for (let i = count; i > 0; i -= 1) {
      entries.push(entry(`f${String(i).padStart(4, "0")}.ts`, "modified"));
    }
    return status(entries);
  }

  it("defaults to 200 rows", () => {
    expect(DEFAULT_MAX_ROWS).toBe(200);
  });

  it("hides nothing while the list fits", () => {
    const model = buildStatusList(manyModified(3));

    expect(model.total).toBe(3);
    expect(model.rows).toHaveLength(3);
    expect(model.hidden).toBe(0);
  });

  it("counts every entry in total even when the rows are cut", () => {
    const model = buildStatusList(manyModified(10), 4);

    expect(model.total).toBe(10);
    expect(model.rows).toHaveLength(4);
  });

  it("reports the remainder as hidden, so the panel can say how much it left out", () => {
    const model = buildStatusList(manyModified(10), 4);

    expect(model.hidden).toBe(6);
  });

  it("cuts AFTER sorting, keeping the first rows of the order and not of the array", () => {
    // The entries arrive in descending name order; a naive slice-then-sort would
    // keep f0010..f0007 and show them as the top of the list.
    const model = buildStatusList(manyModified(10), 3);

    expect(model.rows.map((row) => row.path)).toEqual(["f0001.ts", "f0002.ts", "f0003.ts"]);
  });

  it("cuts across groups, not within each one", () => {
    const model = buildStatusList(
      status([
        entry("u1.ts", "untracked"),
        entry("m1.ts", "modified"),
        entry("m2.ts", "modified"),
      ]),
      2,
    );

    expect(model.rows.map((row) => row.path)).toEqual(["m1.ts", "m2.ts"]);
    expect(model.hidden).toBe(1);
  });

  it("caps a very long list at the default when no max is given", () => {
    const model = buildStatusList(manyModified(250));

    expect(model.total).toBe(250);
    expect(model.rows).toHaveLength(DEFAULT_MAX_ROWS);
    expect(model.hidden).toBe(50);
  });

  it.each([
    ["zero", 0],
    ["negative", -5],
    ["NaN", Number.NaN],
    ["Infinity", Number.POSITIVE_INFINITY],
  ])("treats a degenerate max (%s) as the default instead of blanking the panel", (_label, bad) => {
    const model = buildStatusList(manyModified(3), bad);

    expect(model.visible).toBe(true);
    expect(model.rows).toHaveLength(3);
    expect(model.hidden).toBe(0);
  });
});

describe("status list: purity", () => {
  it("does not reorder the array it was given", () => {
    const entries = [
      entry("z.ts", "untracked"),
      entry("a.ts", "modified"),
      entry("m.ts", "deleted"),
    ];
    const snapshot = entries.map((item) => ({ ...item }));

    buildStatusList(status(entries));

    expect(entries).toEqual(snapshot);
  });

  it("does not mutate the entries themselves while decorating them", () => {
    const entries = [entry("a.ts", "modified")];

    buildStatusList(status(entries));

    expect(entries[0]).toEqual({ path: "a.ts", state: "modified" });
  });

  it("gives the same answer twice for the same input", () => {
    const frame = status([
      entry("z.ts", "untracked"),
      entry("a.ts", "modified"),
      entry("m.ts", "deleted"),
    ]);

    const first: StatusListModel = buildStatusList(frame);
    const second: StatusListModel = buildStatusList(frame);

    expect(second).toEqual(first);
  });

  it("returns a fresh rows array each call, so a caller holding one cannot corrupt the next", () => {
    const frame = status([entry("a.ts", "modified")]);

    const first = buildStatusList(frame);
    first.rows.length = 0;

    expect(buildStatusList(frame).rows).toHaveLength(1);
  });
});
