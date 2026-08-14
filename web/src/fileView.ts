/**
 * The state machine behind the file viewer panel.
 *
 * The graph says a file changed and nothing else; seeing WHAT changed meant
 * leaving the page for a terminal. Clicking a file opens a modal showing its
 * `git diff`, else its text, else a hex dump when it is binary.
 *
 * The browser cannot read the disk, so the content is a ROUND TRIP: the click
 * asks the daemon and the answer lands milliseconds later. That makes this a
 * small state machine, and it lives here rather than in the DOM handler for the
 * same reason as {@link ./rootPrompt} and {@link ./search}: `renderer.ts` needs a
 * GL context and cannot be unit-tested, and logic wired straight into a `<div>`
 * is logic no test reaches. Every transition returns a NEW state; nothing is
 * mutated in place.
 */

import type { FileView, FileViewMode } from "./protocol";

export interface FileViewState {
  /** True while the panel covers the graph. */
  readonly open: boolean;
  /** The file being shown, or `""` while closed. */
  readonly path: string;
  /** True between the click and the daemon's answer. */
  readonly loading: boolean;
  /** How to render {@link content}. */
  readonly mode: FileViewMode;
  /** The diff, the text or the hex dump. */
  readonly content: string;
  /** Whether the daemon cut the output short. */
  readonly truncated: boolean;
  /** Why there is nothing to show, or `""`. */
  readonly error: string;
}

/** A closed panel: no file, nothing in flight, nothing to show. */
export function createFileView(): FileViewState {
  return {
    open: false,
    path: "",
    loading: false,
    // Text is the neutral fallback the wire degrades to as well.
    mode: "text",
    content: "",
    truncated: false,
    error: "",
  };
}

/**
 * Open the panel on the CLICK, naming the file whose answer it waits for.
 *
 * Opening only once the daemon replies reads as a click that missed, and the user
 * clicks again — so the panel appears immediately, in `loading`. The previous
 * file's content, truncation notice and error go with it: one file's diff under
 * another file's name is exactly what this feature must never show.
 */
export function requestView(_state: FileViewState, path: string): FileViewState {
  return { ...createFileView(), open: true, loading: true, path };
}

/**
 * Show the daemon's answer.
 *
 * Two guards, both for the race where the user clicked a second file while the
 * first answer travelled the network — the same one `applyCompletion` guards in
 * {@link ./rootPrompt}:
 *
 *  - a frame for a file that is no longer open is IGNORED, which leaves `loading`
 *    true on purpose: the current file's own answer is still coming;
 *  - a frame that arrives after Escape must not throw a modal back over the graph.
 *
 * A frame carrying a `reason` instead of content is still the answer, so it is
 * adopted as-is rather than leaving the panel open, done, and blank.
 */
export function applyView(state: FileViewState, frame: FileView): FileViewState {
  if (!state.open) return state;
  if (frame.path !== state.path) return state;

  return {
    ...state,
    loading: false,
    mode: frame.mode,
    content: frame.content,
    truncated: frame.truncated,
    error: frame.error,
  };
}

/**
 * Report why there is nothing to show, keeping the panel open.
 *
 * "not a text file", "no such path": the reason is all the user gets, and closing
 * the panel throws it away before it can be read. As in {@link applyView}, a
 * failure arriving after Escape must not reopen the panel.
 */
export function failView(state: FileViewState, reason: string): FileViewState {
  if (!state.open) return state;
  return { ...state, loading: false, error: reason };
}

/**
 * Dismiss the panel, leaving no trace.
 *
 * Everything is dropped, including a reply still in flight, so the next click
 * neither flashes the previous file nor inherits its failure.
 */
export function closeView(_state: FileViewState): FileViewState {
  return createFileView();
}
