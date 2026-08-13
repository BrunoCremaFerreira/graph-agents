/**
 * Contract tests (RED) for the HUD's recent-activity list.
 *
 * Three defects motivate this module, all of which make the list useless as a
 * read of "what just happened":
 *
 *  1. On connect the daemon replays the ENTIRE project tree as `origin: "seed"`
 *     `A` events -- tens to thousands of them. A list that accepts those opens
 *     already full of backdrop, and the first real edit scrolls off the top
 *     before anyone reads it. Seed is dropped here, and only seed: a `watch`
 *     event with `agent: ""` is a real change nobody could be credited for and
 *     must still be listed.
 *  2. A single save fires the hook AND the watcher, and agents re-edit the same
 *     file repeatedly; without collapsing, one file's name fills the panel.
 *     Collapsing must apply ONLY against the entry currently on top -- folding a
 *     new occurrence into an older entry further down would silently reorder the
 *     list under the reader's eye.
 *  3. A long session cannot grow an unbounded array behind the HUD.
 *
 * `splitPath` lives here rather than in the DOM/HUD element because deciding
 * where a path breaks into directory and file name is arithmetic on a string,
 * and the test environment is `node` with no DOM.
 *
 * Expected to FAIL until src/eventLog.ts exists. One failure reason per test.
 */

import { describe, it, expect } from "vitest";
import {
  createEventLog,
  splitPath,
  DEFAULT_MAX_ENTRIES,
  type LogEntry,
} from "../src/eventLog";
import type { AgentEvent, EventType } from "../src/protocol";

function event(
  type: EventType,
  path: string,
  overrides: Partial<AgentEvent> = {},
): AgentEvent {
  const color = type === "A" ? "33FF33" : type === "M" ? "FFAA00" : "FF3333";
  return {
    ts: 1000,
    agent: "sess-1",
    type,
    path,
    color,
    origin: "hook",
    label: "",
    ...overrides,
  };
}

describe("event log: what gets in", () => {
  it("drops a seeded event so the list opens on activity, not on the tree snapshot", () => {
    const log = createEventLog();

    const accepted = log.push(event("A", "src/app.py", { agent: "", origin: "seed" }));

    expect(accepted).toBe(false);
    expect(log.entries()).toEqual([]);
  });

  it("keeps a hook event, which is an agent acting right now", () => {
    const log = createEventLog();

    const accepted = log.push(event("M", "src/app.py"));

    expect(accepted).toBe(true);
    expect(log.entries()).toHaveLength(1);
  });

  it("keeps an unattributed watcher change, because the change really happened", () => {
    const log = createEventLog();

    const accepted = log.push(event("M", "src/app.py", { agent: "", origin: "watch" }));

    expect(accepted).toBe(true);
    expect(log.entries()).toHaveLength(1);
  });

  it("survives the connect-time seed burst with only the live event listed", () => {
    const log = createEventLog();
    for (let i = 0; i < 500; i += 1) {
      log.push(event("A", `src/file-${i}.py`, { agent: "", origin: "seed" }));
    }

    log.push(event("M", "src/app.py"));

    expect(log.entries().map((e) => e.path)).toEqual(["src/app.py"]);
  });

  it("records the event's own fields on the entry it creates", () => {
    const log = createEventLog();

    log.push(event("D", "docs/old.md", { agent: "worker-7", ts: 1712.5 }));

    expect(log.entries()[0]).toMatchObject({
      path: "docs/old.md",
      type: "D",
      agent: "worker-7",
      ts: 1712.5,
      count: 1,
    });
  });
});

describe("event log: ordering", () => {
  it("lists the most recent event first", () => {
    const log = createEventLog();

    log.push(event("A", "first.ts"));
    log.push(event("M", "second.ts"));
    log.push(event("D", "third.ts"));

    expect(log.entries().map((e) => e.path)).toEqual(["third.ts", "second.ts", "first.ts"]);
  });

  it("orders by arrival, not by the timestamp the daemon stamped", () => {
    const log = createEventLog();

    log.push(event("M", "later-clock.ts", { ts: 5000 }));
    log.push(event("M", "earlier-clock.ts", { ts: 10 }));

    expect(log.entries().map((e) => e.path)).toEqual(["earlier-clock.ts", "later-clock.ts"]);
  });
});

describe("event log: collapsing repeats", () => {
  it("folds an immediate repeat of the same path and type into one entry", () => {
    const log = createEventLog();

    log.push(event("M", "src/app.py"));
    log.push(event("M", "src/app.py"));

    expect(log.entries()).toHaveLength(1);
  });

  it("counts the repetitions it folded away", () => {
    const log = createEventLog();

    log.push(event("M", "src/app.py"));
    log.push(event("M", "src/app.py"));
    log.push(event("M", "src/app.py"));

    expect(log.entries()[0].count).toBe(3);
  });

  it("reports a folded repeat as accepted, not as ignored", () => {
    const log = createEventLog();
    log.push(event("M", "src/app.py"));

    const accepted = log.push(event("M", "src/app.py"));

    expect(accepted).toBe(true);
  });

  it("carries the newest timestamp and agent onto the folded entry", () => {
    const log = createEventLog();
    log.push(event("M", "src/app.py", { agent: "sess-1", ts: 100 }));

    log.push(event("M", "src/app.py", { agent: "worker-9", ts: 250 }));

    expect(log.entries()[0]).toMatchObject({ agent: "worker-9", ts: 250 });
  });

  it("does not fold a different operation on the same path", () => {
    const log = createEventLog();

    log.push(event("A", "src/app.py"));
    log.push(event("M", "src/app.py"));

    expect(log.entries().map((e) => e.type)).toEqual(["M", "A"]);
  });

  it("folds only against the top entry, so a returning path opens a new line", () => {
    const log = createEventLog();

    log.push(event("M", "x.ts"));
    log.push(event("M", "y.ts"));
    log.push(event("M", "x.ts"));

    expect(log.entries()).toHaveLength(3);
  });

  it("leaves the older occurrence in place instead of reordering the list", () => {
    const log = createEventLog();

    log.push(event("M", "x.ts"));
    log.push(event("M", "y.ts"));
    log.push(event("M", "x.ts"));

    expect(log.entries().map((e) => e.path)).toEqual(["x.ts", "y.ts", "x.ts"]);
    expect(log.entries().map((e) => e.count)).toEqual([1, 1, 1]);
  });
});

describe("event log: entry cap", () => {
  it("never holds more entries than the max it was created with", () => {
    const log = createEventLog(3);

    for (let i = 0; i < 20; i += 1) log.push(event("M", `f-${i}.ts`));

    expect(log.entries()).toHaveLength(3);
  });

  it("discards the oldest entries when it overflows", () => {
    const log = createEventLog(3);

    for (let i = 0; i < 5; i += 1) log.push(event("M", `f-${i}.ts`));

    expect(log.entries().map((e) => e.path)).toEqual(["f-4.ts", "f-3.ts", "f-2.ts"]);
  });

  it("holds exactly one entry when the max is 1", () => {
    const log = createEventLog(1);

    log.push(event("M", "a.ts"));
    log.push(event("M", "b.ts"));

    expect(log.entries().map((e) => e.path)).toEqual(["b.ts"]);
  });

  it("caps a log created with no max at DEFAULT_MAX_ENTRIES", () => {
    const log = createEventLog();

    for (let i = 0; i < DEFAULT_MAX_ENTRIES + 25; i += 1) log.push(event("M", `f-${i}.ts`));

    expect(log.entries()).toHaveLength(DEFAULT_MAX_ENTRIES);
  });

  it("defaults to 200 entries: deep enough to scroll, bounded for a long session", () => {
    expect(DEFAULT_MAX_ENTRIES).toBe(200);
  });

  it.each([
    ["zero", 0],
    ["negative", -5],
    ["NaN", Number.NaN],
    ["Infinity", Number.POSITIVE_INFINITY],
  ])("still records events when the max is degenerate (%s)", (_label, max) => {
    const log = createEventLog(max);

    log.push(event("M", "a.ts"));
    log.push(event("M", "b.ts"));

    expect(log.entries().map((e) => e.path)).toEqual(["b.ts", "a.ts"]);
  });

  it("falls back to the default cap when the max is degenerate", () => {
    const log = createEventLog(Number.NaN);

    for (let i = 0; i < DEFAULT_MAX_ENTRIES + 25; i += 1) log.push(event("M", `f-${i}.ts`));

    expect(log.entries()).toHaveLength(DEFAULT_MAX_ENTRIES);
  });
});

describe("event log: hostile input", () => {
  it("ignores a null event instead of throwing", () => {
    const log = createEventLog();

    expect(() => log.push(null as unknown as AgentEvent)).not.toThrow();
    expect(log.entries()).toEqual([]);
  });

  it("ignores an event whose path is not a string", () => {
    const log = createEventLog();

    const accepted = log.push({ ...event("M", "a.ts"), path: undefined } as unknown as AgentEvent);

    expect(accepted).toBe(false);
    expect(log.entries()).toEqual([]);
  });

  it("keeps its own state when a caller mutates the array it handed out", () => {
    const log = createEventLog();
    log.push(event("M", "a.ts"));

    const snapshot = log.entries() as LogEntry[];
    try {
      snapshot.length = 0;
    } catch {
      // A frozen array is an equally valid way to honour the contract.
    }

    expect(log.entries().map((e) => e.path)).toEqual(["a.ts"]);
  });
});

describe("splitPath", () => {
  it("splits a nested path at the last slash, keeping the slash on the directory", () => {
    expect(splitPath("web/src/renderer.ts")).toEqual({ dir: "web/src/", name: "renderer.ts" });
  });

  it("leaves the directory empty for a top-level file", () => {
    expect(splitPath("README.md")).toEqual({ dir: "", name: "README.md" });
  });

  it("yields an empty name for a path that ends in a slash", () => {
    expect(splitPath("web/src/")).toEqual({ dir: "web/src/", name: "" });
  });

  it("returns both parts empty for an empty path", () => {
    expect(splitPath("")).toEqual({ dir: "", name: "" });
  });

  it("splits at the last slash even when slashes repeat, without rewriting the path", () => {
    expect(splitPath("docs//guide.md")).toEqual({ dir: "docs//", name: "guide.md" });
  });

  it.each([
    "web/src/renderer.ts",
    "README.md",
    "",
    "web/src/",
    "docs//guide.md",
    "/abs/leading.txt",
    "/",
  ])("reassembles the original path from dir + name (%j)", (path) => {
    const { dir, name } = splitPath(path);

    expect(dir + name).toBe(path);
  });

  it.each([
    "web/src/renderer.ts",
    "README.md",
    "",
    "web/src/",
    "docs//guide.md",
    "/abs/leading.txt",
    "/",
  ])("never leaves a slash inside the name (%j)", (path) => {
    expect(splitPath(path).name).not.toContain("/");
  });
});
