/**
 * Contract tests (RED) for the observed-root prompt's state machine.
 *
 * The defect: the observed root is fixed at daemon boot, so switching projects
 * means restarting everything. The page is getting a bar (ctrl+L) that types a
 * directory and asks the daemon to switch. Everything the bar decides -- what is
 * in the field, which completion candidates are showing, whether an error is up
 * -- is pure data, and it lives here rather than in the DOM handler for the same
 * reason as `search.ts`: `renderer.ts` needs a GL context and cannot be
 * unit-tested, and logic wired straight into an <input> is logic no test reaches.
 * Every transition returns a NEW state; nothing is mutated in place.
 *
 * Four properties carry the weight, and three of them are network races:
 *
 *  - **The browser cannot read the disk.** Tab is answered by the DAEMON, over
 *    the same socket the events arrive on, so the reply lands some milliseconds
 *    later -- during which the user keeps typing. `applyCompletion` therefore
 *    IGNORES a reply whose `path` is not the text currently in the field:
 *    adopting a stale one overwrites the characters just typed, which reads as
 *    the bar fighting the keyboard.
 *  - **The candidate list is always the last Tab's.** Typing again invalidates
 *    it, so `setText` clears the matches; a list left over from a previous
 *    prefix advertises directories that no longer match what is on screen.
 *  - **Escape leaves no trace.** A half-typed path must not reappear at the next
 *    ctrl+L, or the user applies a root they abandoned.
 *  - **A rejected path keeps the bar open.** The whole point of an error ("no
 *    such directory") is to let the user fix the typo; closing the bar throws
 *    away the path they were correcting.
 *
 * Expected to FAIL until src/rootPrompt.ts exists. One failure reason per test.
 */

import { describe, it, expect } from "vitest";
import {
  createRootPrompt,
  openPrompt,
  setText,
  applyCompletion,
  cancelPrompt,
  failPrompt,
  type RootPromptState,
} from "../src/rootPrompt";

/** The root the daemon is watching when the user hits ctrl+L. */
const CURRENT = "/home/brn/projects/graph-agents";

/** A prompt opened on the current root, which is where every scenario starts. */
function opened(): RootPromptState {
  return openPrompt(createRootPrompt(), CURRENT);
}

describe("root prompt: the initial state", () => {
  it("starts closed, because the bar is not on screen until ctrl+L", () => {
    expect(createRootPrompt().open).toBe(false);
  });

  it("starts with nothing typed", () => {
    expect(createRootPrompt().text).toBe("");
  });

  it("starts with no error, since nothing has been rejected yet", () => {
    expect(createRootPrompt().error).toBe("");
  });

  it("starts with no completion candidates", () => {
    expect(createRootPrompt().matches).toEqual([]);
  });
});

describe("openPrompt", () => {
  it("shows the bar", () => {
    expect(opened().open).toBe(true);
  });

  it("prefills the field with the directory being observed, so the user edits rather than retypes", () => {
    expect(opened().text).toBe(CURRENT);
  });

  it("remembers the current root as the original, so the bar knows what it would replace", () => {
    expect(opened().original).toBe(CURRENT);
  });

  it("opens with no completion candidates, since nothing has been asked of the daemon yet", () => {
    expect(opened().matches).toEqual([]);
  });

  it("clears an error left over from a previous rejected path", () => {
    // Reopening on a stale "no such directory" would accuse the prefilled root,
    // which is the one directory known to be good.
    const failed = failPrompt(opened(), "no such directory");

    expect(openPrompt(failed, CURRENT).error).toBe("");
  });

  it("leaves the state it was given untouched", () => {
    const before = createRootPrompt();

    openPrompt(before, CURRENT);

    expect(before).toEqual({ open: false, text: "", original: "", matches: [], error: "" });
  });
});

describe("setText", () => {
  it("records what was typed", () => {
    expect(setText(opened(), "/home/brn/projects/other").text).toBe("/home/brn/projects/other");
  });

  it("keeps the bar open while the user types", () => {
    expect(setText(opened(), "/home/brn/pro").open).toBe(true);
  });

  it("keeps the original root, which is what the bar falls back to", () => {
    expect(setText(opened(), "/home/brn/pro").original).toBe(CURRENT);
  });

  it("clears the error, because the path being complained about is no longer the one in the field", () => {
    const failed = failPrompt(opened(), "no such directory");

    expect(setText(failed, "/home/brn/projects/oth").error).toBe("");
  });

  it("drops the previous candidates, because the shown list is always the last Tab's", () => {
    // Candidates computed for "/home/brn/pro" name directories that may not even
    // start with what is now in the field.
    const completed = applyCompletion(setText(opened(), "/home/brn/pro"), {
      path: "/home/brn/pro",
      completed: "/home/brn/projects/",
      matches: ["/home/brn/projects/", "/home/brn/proto/"],
    });

    expect(setText(completed, "/home/brn/projects/g").matches).toEqual([]);
  });

  it("accepts an empty field without closing the bar, since deleting is how a path gets retyped", () => {
    expect(setText(opened(), "").open).toBe(true);
  });

  it("leaves the state it was given untouched", () => {
    const before = opened();

    setText(before, "/somewhere/else");

    expect(before.text).toBe(CURRENT);
  });
});

describe("applyCompletion", () => {
  it("adopts the completed path as the new text", () => {
    const typed = setText(opened(), "/home/brn/pro");

    const state = applyCompletion(typed, {
      path: "/home/brn/pro",
      completed: "/home/brn/projects/",
      matches: ["/home/brn/projects/"],
    });

    expect(state.text).toBe("/home/brn/projects/");
  });

  it("keeps the candidates so the bar can list what the prefix still allows", () => {
    const typed = setText(opened(), "/home/brn/pro");

    const state = applyCompletion(typed, {
      path: "/home/brn/pro",
      completed: "/home/brn/pro",
      matches: ["/home/brn/projects/", "/home/brn/proto/"],
    });

    expect(state.matches).toEqual(["/home/brn/projects/", "/home/brn/proto/"]);
  });

  it("leaves the bar open, because completing is a step towards submitting, not a submit", () => {
    const typed = setText(opened(), "/home/brn/pro");

    const state = applyCompletion(typed, {
      path: "/home/brn/pro",
      completed: "/home/brn/projects/",
      matches: [],
    });

    expect(state.open).toBe(true);
  });

  it("ignores a reply for a path that is no longer what the user typed", () => {
    // THE race this module exists for: Tab goes to the daemon, the user keeps
    // typing while it answers, and adopting the late reply would overwrite the
    // characters typed in between. The reply is for "/home/brn/pro"; the field
    // has moved on.
    const typedOn = setText(opened(), "/home/brn/projects/gra");

    const state = applyCompletion(typedOn, {
      path: "/home/brn/pro",
      completed: "/home/brn/projects/",
      matches: ["/home/brn/projects/"],
    });

    expect(state.text).toBe("/home/brn/projects/gra");
  });

  it("does not show the candidates of a reply it ignored", () => {
    const typedOn = setText(opened(), "/home/brn/projects/gra");

    const state = applyCompletion(typedOn, {
      path: "/home/brn/pro",
      completed: "/home/brn/projects/",
      matches: ["/home/brn/projects/", "/home/brn/proto/"],
    });

    expect(state.matches).toEqual([]);
  });

  it("does not reopen a bar the user already dismissed", () => {
    // Escape closes; the daemon's answer to the last Tab arrives afterwards and
    // must not put the bar back on screen.
    const cancelled = cancelPrompt(setText(opened(), "/home/brn/pro"));

    const state = applyCompletion(cancelled, {
      path: "/home/brn/pro",
      completed: "/home/brn/projects/",
      matches: ["/home/brn/projects/"],
    });

    expect(state.open).toBe(false);
  });

  it("leaves the state it was given untouched", () => {
    const typed = setText(opened(), "/home/brn/pro");

    applyCompletion(typed, {
      path: "/home/brn/pro",
      completed: "/home/brn/projects/",
      matches: ["/home/brn/projects/"],
    });

    expect(typed.text).toBe("/home/brn/pro");
  });
});

describe("cancelPrompt", () => {
  it("hides the bar", () => {
    expect(cancelPrompt(setText(opened(), "/half/typed")).open).toBe(false);
  });

  it("discards what was typed, so Escape leaves no trace at the next ctrl+L", () => {
    // Reopening on an abandoned path is how a root nobody meant to apply gets
    // applied by a reflex Enter.
    expect(cancelPrompt(setText(opened(), "/half/typed")).text).toBe("");
  });

  it("forgets the candidates of the last completion", () => {
    const completed = applyCompletion(setText(opened(), "/home/brn/pro"), {
      path: "/home/brn/pro",
      completed: "/home/brn/pro",
      matches: ["/home/brn/projects/", "/home/brn/proto/"],
    });

    expect(cancelPrompt(completed).matches).toEqual([]);
  });

  it("clears an error, so a rejected path does not haunt the next attempt", () => {
    expect(cancelPrompt(failPrompt(opened(), "no such directory")).error).toBe("");
  });
});

describe("failPrompt", () => {
  it("stores the reason the daemon gave, so the user is told what is wrong", () => {
    expect(failPrompt(setText(opened(), "/nope"), "no such directory").error).toBe(
      "no such directory",
    );
  });

  it("keeps the bar open, because an error is an invitation to fix the path", () => {
    expect(failPrompt(setText(opened(), "/nope"), "no such directory").open).toBe(true);
  });

  it("keeps the rejected text in the field, since that is what has to be corrected", () => {
    expect(failPrompt(setText(opened(), "/nope"), "no such directory").text).toBe("/nope");
  });

  it("does not reopen a bar the user already dismissed", () => {
    // Escape closed it while the daemon was still validating; a late refusal
    // must not pop the bar back up over the graph.
    const cancelled = cancelPrompt(setText(opened(), "/nope"));

    expect(failPrompt(cancelled, "no such directory").open).toBe(false);
  });

  it("leaves the state it was given untouched", () => {
    const typed = setText(opened(), "/nope");

    failPrompt(typed, "no such directory");

    expect(typed.error).toBe("");
  });
});
