/**
 * The bottom-centre caption: which directory is on screen, and on what branch.
 *
 * Presentation only — no domain logic. It takes an already-parsed
 * {@link DaemonMeta} and writes text into two spans. The only decision it makes
 * is how many characters fit, and even that is delegated to `truncateMiddle`
 * (pure, and tested), because clipping the tail with CSS would hide exactly the
 * segment that identifies the project.
 *
 * DOM-bound, so it is not unit-tested; keep it that thin.
 */

import { truncateMiddle, type DaemonMeta } from "./protocol";

/** Rough width of one 12px system-ui character, in px. Sizing only. */
const CHAR_PX = 6.6;

/** The caption is capped at 50vw by CSS; mirror that when budgeting chars. */
const WIDTH_FRACTION = 0.5;

/** Branch names get their own slice so a long path cannot eat them. */
const MAX_BRANCH_CHARS = 24;

/** Never shrink the path below this, even on an absurdly narrow viewport. */
const MIN_ROOT_CHARS = 12;

export interface ContextHud {
  /** Show a meta frame the daemon just sent. */
  setMeta(meta: DaemonMeta): void;
  /** Re-fit the text after a viewport change. */
  refresh(): void;
}

/**
 * Bind the caption to `#context` (root span + branch span).
 *
 * Before the first meta frame arrives both spans stay empty and the branch span
 * stays hidden, so a page talking to an older daemon simply shows nothing
 * rather than a placeholder.
 */
export function createContextHud(container: HTMLElement): ContextHud {
  const rootEl = container.querySelector<HTMLElement>("#context-root");
  const branchEl = container.querySelector<HTMLElement>("#context-branch");
  let meta: DaemonMeta | null = null;

  function render(): void {
    if (!meta || !rootEl || !branchEl) return;

    const branch = meta.branch ? truncateMiddle(meta.branch, MAX_BRANCH_CHARS) : "";
    const viewport = typeof window !== "undefined" ? window.innerWidth : 0;
    const total = Math.floor((viewport * WIDTH_FRACTION) / CHAR_PX);
    const budget = Math.max(MIN_ROOT_CHARS, total - branch.length - 3);

    rootEl.textContent = truncateMiddle(meta.root, budget);
    branchEl.textContent = branch;
    // Hiding the span hides its `::before` separator too: no orphan " · ".
    branchEl.hidden = branch.length === 0;
  }

  return {
    setMeta(next: DaemonMeta): void {
      meta = next;
      render();
    },
    refresh: render,
  };
}
