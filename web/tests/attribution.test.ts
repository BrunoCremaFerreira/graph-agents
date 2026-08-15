/**
 * Contract tests (RED) for the attribution monitor.
 *
 * The defect this exists for cost the user hours: the page looked broken. The
 * tree was updating, files kept lighting up -- and no agent figure ever showed
 * up. The cause was outside the browser entirely: the capture hooks were never
 * installed in the observed project, so every event arrived with `agent: ""`,
 * and an empty agent must never create an actor (by design -- see CLAUDE.md).
 *
 * The trap is that "hooks are not installed" and "no agent is working right
 * now" are VISUALLY IDENTICAL: a live tree with nobody on stage. The page can
 * only tell them apart by remembering whether it has EVER seen an attributed
 * event, which is exactly the one bit this module keeps.
 *
 * Three properties carry the weight:
 *
 *  1. **Seed never counts.** The daemon replays the whole project tree on
 *     connect. That is the backdrop, not activity; an agent id riding on a seed
 *     frame would be an artifact of the snapshot, not proof that capture works.
 *  2. **`watch` with an agent counts as much as `hook`.** The daemon credits a
 *     watcher change to the agent whose hook fired inside the attribution
 *     window, so such an event proves the hook chain is alive.
 *  3. **Monotonic.** Once attribution is proven, a flood of unattributed events
 *     (a hand edit outside any agent, say) must not relight the warning. An
 *     indicator that blinks is worse than no indicator.
 *
 * Pure logic, no DOM: the banner element that reads this state is a dumb
 * painter, and the test environment is `node`.
 *
 * Expected to FAIL until src/attribution.ts exists. One failure reason per test.
 */

import { describe, it, expect } from "vitest";
import { createAttributionMonitor } from "../src/attribution";
import type { AgentEvent, EventOrigin, EventType } from "../src/protocol";

function event(
  type: EventType,
  path: string,
  overrides: Partial<AgentEvent> = {},
): AgentEvent {
  const color =
    type === "A"
      ? "33FF33"
      : type === "M"
        ? "FFAA00"
        : type === "R"
          ? "AA66FF"
          : "FF3333";
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

describe("attribution monitor: the initial state", () => {
  it("starts unattributed, because nothing has been observed yet", () => {
    const monitor = createAttributionMonitor();

    expect(monitor.attributed()).toBe(false);
  });

  it("reports the same answer when asked twice without any event in between", () => {
    const monitor = createAttributionMonitor();

    monitor.attributed();

    expect(monitor.attributed()).toBe(false);
  });

  it("gives each monitor its own state instead of sharing a module-level flag", () => {
    const proven = createAttributionMonitor();
    proven.observe(event("M", "src/app.py", { agent: "sess-1" }));

    const fresh = createAttributionMonitor();

    expect(fresh.attributed()).toBe(false);
  });
});

describe("attribution monitor: what proves capture is alive", () => {
  it("turns on for a hook event carrying an agent id", () => {
    const monitor = createAttributionMonitor();

    monitor.observe(event("M", "src/app.py", { agent: "sess-1", origin: "hook" }));

    expect(monitor.attributed()).toBe(true);
  });

  it("turns on for an attributed watcher change, credited inside the attribution window", () => {
    const monitor = createAttributionMonitor();

    monitor.observe(event("M", "src/app.py", { agent: "sess-1", origin: "watch" }));

    expect(monitor.attributed()).toBe(true);
  });

  it.each<[string, EventType]>([
    ["a creation", "A"],
    ["a modification", "M"],
    ["a deletion", "D"],
  ])("accepts %s as proof, since the operation kind says nothing about authorship", (_label, type) => {
    const monitor = createAttributionMonitor();

    monitor.observe(event(type, "src/app.py", { agent: "worker-7" }));

    expect(monitor.attributed()).toBe(true);
  });

  it("accepts a read as proof too, since reading a file is a tool call like any other", () => {
    // Guard, not a new rule: the monitor asks "did a hook ever carry an agent
    // id?", and the operation kind has never been part of that answer. It is
    // pinned because `R` is the fourth kind and the one an agent emits most, so
    // a session where the agent only reads before it writes must light the
    // "hooks are installed" state at the FIRST read, not minutes later at the
    // first edit -- otherwise the page accuses a correctly hooked project of
    // having no hooks for as long as the agent is still exploring.
    const monitor = createAttributionMonitor();

    monitor.observe(event("R", "src/app.py", { agent: "worker-7", origin: "hook" }));

    expect(monitor.attributed()).toBe(true);
  });

  it("does not require observe() to report anything back", () => {
    const monitor = createAttributionMonitor();

    expect(monitor.observe(event("M", "src/app.py"))).toBeUndefined();
  });
});

describe("attribution monitor: what does not prove anything", () => {
  it.each<[string, EventOrigin]>([
    ["a hook", "hook"],
    ["the watcher", "watch"],
  ])("stays off for an empty agent from %s: that is the state being detected", (_label, origin) => {
    const monitor = createAttributionMonitor();

    monitor.observe(event("M", "src/app.py", { agent: "", origin }));

    expect(monitor.attributed()).toBe(false);
  });

  it("stays off for a seeded event, which is backdrop rather than activity", () => {
    const monitor = createAttributionMonitor();

    monitor.observe(event("A", "src/app.py", { agent: "", origin: "seed" }));

    expect(monitor.attributed()).toBe(false);
  });

  it("stays off for a seeded event even when it arrives with an agent id", () => {
    const monitor = createAttributionMonitor();

    monitor.observe(event("A", "src/app.py", { agent: "sess-1", origin: "seed" }));

    expect(monitor.attributed()).toBe(false);
  });

  it("stays off through an entire connect-time seed burst", () => {
    const monitor = createAttributionMonitor();

    for (let i = 0; i < 500; i += 1) {
      monitor.observe(event("A", `src/file-${i}.py`, { agent: "sess-1", origin: "seed" }));
    }

    expect(monitor.attributed()).toBe(false);
  });

  it.each([
    ["a single space", " "],
    ["several spaces", "   "],
    ["a tab", "\t"],
    ["a newline", "\n"],
  ])("treats a whitespace-only agent (%s) as no agent at all", (_label, agent) => {
    const monitor = createAttributionMonitor();

    monitor.observe(event("M", "src/app.py", { agent }));

    expect(monitor.attributed()).toBe(false);
  });

  it("stays off after a long run of unattributed watcher changes", () => {
    const monitor = createAttributionMonitor();

    for (let i = 0; i < 50; i += 1) {
      monitor.observe(event("M", `src/file-${i}.py`, { agent: "", origin: "watch" }));
    }

    expect(monitor.attributed()).toBe(false);
  });
});

describe("attribution monitor: monotonicity", () => {
  it("stays on when an unattributed change follows the proof", () => {
    const monitor = createAttributionMonitor();
    monitor.observe(event("M", "src/app.py", { agent: "sess-1" }));

    monitor.observe(event("M", "notes.md", { agent: "", origin: "watch" }));

    expect(monitor.attributed()).toBe(true);
  });

  it("stays on under a flood of unattributed changes, so the warning cannot blink", () => {
    const monitor = createAttributionMonitor();
    monitor.observe(event("M", "src/app.py", { agent: "sess-1" }));

    for (let i = 0; i < 200; i += 1) {
      monitor.observe(event("M", `hand-edit-${i}.md`, { agent: "", origin: "watch" }));
    }

    expect(monitor.attributed()).toBe(true);
  });

  it("stays on when a later seed replay arrives after a reconnect", () => {
    const monitor = createAttributionMonitor();
    monitor.observe(event("M", "src/app.py", { agent: "sess-1" }));

    for (let i = 0; i < 20; i += 1) {
      monitor.observe(event("A", `src/file-${i}.py`, { agent: "", origin: "seed" }));
    }

    expect(monitor.attributed()).toBe(true);
  });

  it("stays on when a malformed event arrives after the proof", () => {
    const monitor = createAttributionMonitor();
    monitor.observe(event("M", "src/app.py", { agent: "sess-1" }));

    monitor.observe(null as unknown as AgentEvent);

    expect(monitor.attributed()).toBe(true);
  });
});

/**
 * Monotonicity holds WITHIN one observed project, and ctrl+L now switches the
 * project under the page's feet. The latch answers "are the capture hooks
 * installed HERE?", and the answer is a property of the checkout: hooks live in
 * the observed project's own `.claude/settings.json`, so proof gathered while
 * watching this repository says nothing about the next one. Carrying the latch
 * across a `reset` would suppress the warning in exactly the project that needs
 * it -- a fresh checkout with no hooks, whose tree updates with nobody on
 * camera, which is the ambiguity this module exists to end.
 *
 * Reset is the ONLY way the latch goes back off; no event may do it.
 */
describe("attribution monitor: reset", () => {
  it("unlatches, because the new project has its own hooks or lack of them", () => {
    const monitor = createAttributionMonitor();
    monitor.observe(event("M", "src/app.py", { agent: "sess-1" }));

    monitor.reset();

    expect(monitor.attributed()).toBe(false);
  });

  it("latches again on the first attributed event of the new project", () => {
    const monitor = createAttributionMonitor();
    monitor.observe(event("M", "src/app.py", { agent: "sess-1" }));
    monitor.reset();

    monitor.observe(event("M", "other/app.py", { agent: "sess-2", origin: "watch" }));

    expect(monitor.attributed()).toBe(true);
  });

  it("stays off after a reset when the new project only sends unattributed changes", () => {
    // The whole point: a tree that updates with `agent: ""` in the new checkout
    // has to relight the "hooks are not installed" warning.
    const monitor = createAttributionMonitor();
    monitor.observe(event("M", "src/app.py", { agent: "sess-1" }));
    monitor.reset();

    monitor.observe(event("A", "other/seeded.py", { agent: "", origin: "seed" }));
    monitor.observe(event("M", "other/hand-edit.md", { agent: "", origin: "watch" }));

    expect(monitor.attributed()).toBe(false);
  });

  it("is harmless on a monitor that has proven nothing yet", () => {
    const monitor = createAttributionMonitor();

    expect(() => monitor.reset()).not.toThrow();
  });
});

describe("attribution monitor: a realistic session", () => {
  it("flips exactly once, on the first attributed event of the stream", () => {
    const monitor = createAttributionMonitor();
    const stream: AgentEvent[] = [
      event("A", "README.md", { agent: "", origin: "seed" }),
      event("A", "src/app.py", { agent: "", origin: "seed" }),
      event("A", "src/util.py", { agent: "", origin: "seed" }),
      event("M", "notes.md", { agent: "", origin: "watch" }),
      event("M", "src/app.py", { agent: "", origin: "watch" }),
      event("M", "src/app.py", { agent: "sess-1", origin: "hook" }),
      event("M", "src/app.py", { agent: "sess-1", origin: "watch" }),
      event("M", "notes.md", { agent: "", origin: "watch" }),
    ];

    const timeline = stream.map((e) => {
      monitor.observe(e);
      return monitor.attributed();
    });

    expect(timeline).toEqual([false, false, false, false, false, true, true, true]);
  });

  it("never flips at all in the hooks-were-never-installed session", () => {
    const monitor = createAttributionMonitor();
    const stream: AgentEvent[] = [
      event("A", "README.md", { agent: "", origin: "seed" }),
      event("A", "src/app.py", { agent: "", origin: "seed" }),
      event("M", "src/app.py", { agent: "", origin: "watch" }),
      event("A", "src/new.py", { agent: "", origin: "watch" }),
      event("D", "src/old.py", { agent: "", origin: "watch" }),
    ];

    const timeline = stream.map((e) => {
      monitor.observe(e);
      return monitor.attributed();
    });

    expect(timeline).toEqual([false, false, false, false, false]);
  });
});

describe("attribution monitor: hostile input", () => {
  it.each([
    ["null", null],
    ["undefined", undefined],
    ["a string", "not-an-event"],
    ["a number", 7],
    ["an array", []],
    ["an empty object", {}],
  ])("ignores %s instead of throwing", (_label, raw) => {
    const monitor = createAttributionMonitor();

    expect(() => monitor.observe(raw as unknown as AgentEvent)).not.toThrow();
    expect(monitor.attributed()).toBe(false);
  });

  it.each([
    ["a number", 42],
    ["null", null],
    ["undefined", undefined],
    ["an object", { id: "sess-1" }],
    ["an array of ids", ["sess-1"]],
  ])("ignores an agent that is not a string (%s)", (_label, agent) => {
    const monitor = createAttributionMonitor();

    monitor.observe({ ...event("M", "src/app.py"), agent } as unknown as AgentEvent);

    expect(monitor.attributed()).toBe(false);
  });

  // Whether a missing origin counts is deliberately NOT pinned here: parseEvent
  // already degrades an absent origin to "hook", so nothing on the wire reaches
  // the monitor without one. Only the no-throw guarantee is specified.
  it("survives an event whose origin field is missing entirely", () => {
    const monitor = createAttributionMonitor();
    const { origin: _dropped, ...noOrigin } = event("M", "src/app.py", { agent: "sess-1" });

    expect(() => monitor.observe(noOrigin as unknown as AgentEvent)).not.toThrow();
  });
});
