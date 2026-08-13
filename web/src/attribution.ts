/**
 * One bit of memory: has this page ever seen an event credited to an agent?
 *
 * It exists because two very different situations look identical on screen —
 * a live tree with nobody on stage. Either no agent happens to be working right
 * now, or the capture hooks were never installed in the observed project, so
 * every event arrives with `agent: ""` and (by design) never creates an actor.
 * The only way to tell them apart is to remember whether attribution has EVER
 * worked, which is all this keeps.
 *
 * Pure — no DOM — because the decision is worth testing and the banner that
 * reads it is a dumb painter.
 *
 * Three rules carry the weight:
 *
 *  - **Seed never counts.** The connect-time snapshot is backdrop, not
 *    activity; an agent id riding on a seed frame proves nothing about capture.
 *  - **`watch` with an agent counts as much as `hook`.** The daemon credits a
 *    watcher change to the agent whose hook fired inside the attribution
 *    window, so such an event proves the hook chain is alive.
 *  - **Monotonic.** Once proven, later unattributed events (a hand edit, a
 *    reconnect's seed replay) must not turn it back off: an indicator that
 *    blinks is worse than no indicator.
 *
 * State is per instance, never module-level, so two monitors cannot leak into
 * each other. Every field is checked at runtime: this sits on the socket path,
 * where a malformed frame must be ignored rather than thrown.
 */

import type { AgentEvent } from "./protocol";

export interface AttributionMonitor {
  /** Offer an event. Never throws; nothing is reported back. */
  observe(event: AgentEvent): void;
  /** Whether an attributed (non-seed, non-empty agent) event was ever seen. */
  attributed(): boolean;
}

/** Whether an agent field is real proof of authorship (whitespace is not). */
function isAgentId(agent: unknown): boolean {
  return typeof agent === "string" && agent.trim() !== "";
}

/** Create a monitor with its own latch, initially unattributed. */
export function createAttributionMonitor(): AttributionMonitor {
  let proven = false;

  return {
    observe(event: AgentEvent): void {
      // Latched: once proven, no later event can matter.
      if (proven) return;
      if (typeof event !== "object" || event === null) return;

      const { origin, agent } = event as Partial<AgentEvent>;
      if (origin === "seed") return;
      if (!isAgentId(agent)) return;

      proven = true;
    },

    attributed(): boolean {
      return proven;
    },
  };
}
