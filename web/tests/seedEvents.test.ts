/**
 * Contract tests (RED) for how the model treats seeded and unattributed events.
 *
 * The daemon now sends the project's existing tree at connect time (`origin:
 * "seed"`) and reports filesystem changes it could not attribute to any agent
 * (`agent: ""`). Both must land on screen without lying about activity: a file
 * that was already there is not "being edited", and a change nobody can be
 * blamed for must not invent an actor.
 *
 * One failure reason per test.
 */

import { describe, it, expect } from "vitest";
import { createSimulation } from "../src/simulation";
import { parseEvent, type AgentEvent, type EventType } from "../src/protocol";

function event(
  type: EventType,
  path: string,
  overrides: Partial<AgentEvent> = {},
): AgentEvent {
  const color = type === "A" ? "33FF33" : type === "M" ? "FFAA00" : "FF3333";
  return { ts: 1000, agent: "sess-1", type, path, color, origin: "hook", ...overrides };
}

describe("protocol: origin field", () => {
  it("keeps a valid origin from the wire", () => {
    const parsed = parseEvent({
      ts: 1,
      agent: "",
      type: "A",
      path: "a.ts",
      color: "33FF33",
      origin: "seed",
    });

    expect(parsed?.origin).toBe("seed");
  });

  it("defaults to hook when the field is absent, for older daemons", () => {
    const parsed = parseEvent({ ts: 1, agent: "x", type: "A", path: "a.ts", color: "33FF33" });

    expect(parsed?.origin).toBe("hook");
  });

  it("falls back to hook on an unknown origin instead of rejecting the event", () => {
    const parsed = parseEvent({
      ts: 1,
      agent: "x",
      type: "A",
      path: "a.ts",
      color: "33FF33",
      origin: "telepathy",
    });

    expect(parsed?.origin).toBe("hook");
  });

  it("accepts an empty agent, which marks an unattributed change", () => {
    const parsed = parseEvent({ ts: 1, agent: "", type: "M", path: "a.ts", color: "FFAA00" });

    expect(parsed).not.toBeNull();
    expect(parsed?.agent).toBe("");
  });
});

describe("simulation: seeded tree", () => {
  it("adds a seeded file to the tree", () => {
    const sim = createSimulation();

    sim.applyEvent(event("A", "src/app.py", { agent: "", origin: "seed" }));

    expect(sim.getNode("src/app.py")?.kind).toBe("file");
  });

  it("does not highlight a seeded file: it is backdrop, not activity", () => {
    const sim = createSimulation();

    sim.applyEvent(event("A", "src/app.py", { agent: "", origin: "seed" }));

    expect(sim.getNode("src/app.py")?.highlight).toBe(0);
  });

  it("registers no actor for a seeded file", () => {
    const sim = createSimulation();

    sim.applyEvent(event("A", "src/app.py", { agent: "", origin: "seed" }));

    expect(sim.listActors()).toEqual([]);
  });

  it("highlights a seeded file once an agent actually touches it", () => {
    const sim = createSimulation();
    sim.applyEvent(event("A", "src/app.py", { agent: "", origin: "seed" }));

    sim.applyEvent(event("M", "src/app.py"));

    expect(sim.getNode("src/app.py")?.highlight).toBe(1);
  });
});

describe("simulation: unattributed changes", () => {
  it("registers no actor when the agent is empty", () => {
    const sim = createSimulation();

    sim.applyEvent(event("M", "src/app.py", { agent: "", origin: "watch" }));

    expect(sim.listActors()).toEqual([]);
  });

  it("still highlights the file, because the change really happened", () => {
    const sim = createSimulation();

    sim.applyEvent(event("M", "src/app.py", { agent: "", origin: "watch" }));

    expect(sim.getNode("src/app.py")?.highlight).toBe(1);
  });

  it("registers an actor for an attributed watcher change", () => {
    const sim = createSimulation();

    sim.applyEvent(event("A", "docs/copied.md", { origin: "watch" }));

    expect(sim.listActors().map((a) => a.agent)).toEqual(["sess-1"]);
  });
});
