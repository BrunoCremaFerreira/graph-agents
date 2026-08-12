/**
 * Contract tests (RED) for the pure simulation model.
 *
 * They specify BEHAVIOR only -- the directory tree, actor registry, and fade.
 * No WebGL/three.js drawing is exercised. Expected to FAIL until
 * `desenvolvedor-frontend` implements createSimulation (currently a
 * NotImplementedError stub). One failure reason per test.
 */

import { describe, it, expect } from "vitest";
import { createSimulation } from "../src/simulation";
import { AgentEvent, EventType } from "../src/protocol";

function event(
  type: EventType,
  path: string,
  agent = "sess-1",
  ts = 1000,
): AgentEvent {
  const color = type === "A" ? "33FF33" : type === "M" ? "FFAA00" : "FF3333";
  // `origin` distinguishes live agent activity from the seeded project tree;
  // these tests are all about live activity, hence "hook".
  return { ts, agent, type, path, color, origin: "hook" };
}

describe("simulation model", () => {
  it("materializes ancestor directories when a file is added", () => {
    const sim = createSimulation();

    sim.applyEvent(event("A", "src/api/users.ts"));

    expect(sim.getNode("src")?.kind).toBe("dir");
    expect(sim.getNode("src/api")?.kind).toBe("dir");
    expect(sim.getNode("src/api/users.ts")?.kind).toBe("file");
  });

  it("removes the node on a delete event", () => {
    const sim = createSimulation();
    sim.applyEvent(event("A", "src/gone.ts"));

    sim.applyEvent(event("D", "src/gone.ts"));

    expect(sim.hasNode("src/gone.ts")).toBe(false);
  });

  it("registers a new actor for a never-seen agent", () => {
    const sim = createSimulation();

    sim.applyEvent(event("A", "a.ts", "worker-7"));

    expect(sim.getActor("worker-7")).toBeDefined();
    expect(sim.getActor("worker-7")?.agent).toBe("worker-7");
  });

  it("brings an actor to full intensity right after its event", () => {
    const sim = createSimulation();

    sim.applyEvent(event("M", "a.ts", "worker-7"));

    expect(sim.getActor("worker-7")?.intensity).toBeCloseTo(1, 5);
  });

  it("fades an idle actor's intensity as time advances", () => {
    const sim = createSimulation();
    sim.applyEvent(event("M", "a.ts", "worker-7"));
    const before = sim.getActor("worker-7")!.intensity;

    sim.tick(5);

    const after = sim.getActor("worker-7")!.intensity;
    expect(after).toBeLessThan(before);
  });
});
