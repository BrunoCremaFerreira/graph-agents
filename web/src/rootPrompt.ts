/**
 * The state machine behind the observed-root bar (ctrl+L).
 *
 * Pure data and pure transforms, free of the DOM and of three.js, for the same
 * reason as {@link ./search}: `renderer.ts` needs a GL context and cannot be
 * unit-tested, and logic wired straight into an `<input>` is logic no test
 * reaches. Every transition returns a NEW state; nothing is mutated in place.
 *
 * Three of the four properties that carry weight here are network races, because
 * the browser cannot read the disk: Tab is answered by the DAEMON, over the same
 * socket the events arrive on, and the answer lands milliseconds later while the
 * user keeps typing.
 */

import type { RootCompletion } from "./protocol";

export interface RootPromptState {
  /** True while the bar is on screen and taking keystrokes. */
  readonly open: boolean;
  /** What is in the field right now. */
  readonly text: string;
  /** The root that was being observed when the bar opened. */
  readonly original: string;
  /** Candidates from the LAST completion; always the last Tab's. */
  readonly matches: readonly string[];
  /** Why the daemon refused the path, or `""` when nothing was refused. */
  readonly error: string;
}

/** A closed, empty bar: nothing typed, nothing matched, nothing refused. */
export function createRootPrompt(): RootPromptState {
  return { open: false, text: "", original: "", matches: [], error: "" };
}

/**
 * Show the bar, prefilled with the directory being observed.
 *
 * Prefilling means the user edits a path instead of retyping one, and any error
 * left over from a previous attempt goes: a stale "no such directory" would
 * accuse the prefilled root, which is the one directory known to be good.
 */
export function openPrompt(_state: RootPromptState, current: string): RootPromptState {
  return { open: true, text: current, original: current, matches: [], error: "" };
}

/**
 * Record what was typed.
 *
 * The candidates go with it: the shown list is always the last Tab's, and a list
 * computed for an older prefix advertises directories that no longer match what
 * is on screen. The error goes too — the path being complained about is no
 * longer the one in the field. An empty field is a legitimate state (deleting is
 * how a path gets retyped), so it does not close the bar.
 */
export function setText(state: RootPromptState, text: string): RootPromptState {
  return { ...state, text, matches: [], error: "" };
}

/**
 * Adopt the daemon's answer to a Tab.
 *
 * Two guards, both for the same race — the reply travelled the network while the
 * user went on typing:
 *
 *  - a reply whose `path` is not the text currently in the field is IGNORED,
 *    candidates included. Adopting it would overwrite the characters typed in
 *    between, which reads as the bar fighting the keyboard.
 *  - a reply that arrives after Escape must not put the bar back on screen.
 *
 * Otherwise the completed path becomes the text and the candidates are kept, so
 * the bar can list what the prefix still allows. Completing is a step towards
 * submitting, not a submit, so the bar stays open.
 */
export function applyCompletion(
  state: RootPromptState,
  reply: RootCompletion,
): RootPromptState {
  if (!state.open) return state;
  if (reply.path !== state.text) return state;

  return { ...state, text: reply.completed, matches: reply.matches.slice(), error: "" };
}

/**
 * Dismiss the bar, leaving no trace.
 *
 * A half-typed path must not reappear at the next ctrl+L, or a reflex Enter
 * applies a root the user abandoned.
 */
export function cancelPrompt(_state: RootPromptState): RootPromptState {
  return createRootPrompt();
}

/**
 * Show the daemon's refusal of the typed path.
 *
 * The bar stays open with the rejected text still in it: the whole point of "no
 * such directory" is to let the user fix the typo, and closing would throw away
 * the path they were correcting. As in {@link applyCompletion}, a refusal that
 * arrives after Escape must not pop the bar back up over the graph — the daemon
 * was still validating when the user gave up.
 */
export function failPrompt(state: RootPromptState, reason: string): RootPromptState {
  if (!state.open) return state;
  return { ...state, error: reason };
}
