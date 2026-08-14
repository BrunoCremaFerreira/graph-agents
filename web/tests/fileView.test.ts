/**
 * Contract tests (RED) for the state machine behind the file viewer panel.
 *
 * The defect: the graph says a file changed and nothing else. Seeing WHAT
 * changed means alt-tabbing to a terminal and running `git diff` on a path read
 * off a label -- at which point the visualiser has stopped being the thing you
 * watch. Clicking a file will open a modal showing, in this order, the file's
 * `git diff`, else its text, else a hex dump when it is binary. Directories open
 * nothing.
 *
 * The browser cannot read the disk, so the content is a ROUND TRIP: the click
 * sends `{kind:"file",path}` and the daemon answers a `fileView` frame some
 * milliseconds later. That makes this module a small state machine, and it lives
 * here rather than in the DOM handler for the same reason as `rootPrompt.ts` and
 * `search.ts`: `renderer.ts` needs a GL context and cannot be unit-tested, and
 * logic wired straight into a <div> is logic no test reaches. Every transition
 * returns a NEW state; nothing is mutated in place.
 *
 * Four properties carry the weight, and two of them are the network:
 *
 *  - **The panel opens on the CLICK, not on the reply.** A window that only
 *    appears once the daemon answers reads as a click that missed, and the user
 *    clicks again -- so `requestView` opens immediately, in `loading`, naming the
 *    path it is waiting for.
 *  - **A reply for another file is ignored.** The user can click a second file
 *    while the first answer is in flight; painting it would show one file's diff
 *    under another file's name. Exactly the race `applyCompletion` guards in
 *    `rootPrompt.ts`.
 *  - **A late reply must not reopen a closed panel.** Escape closes; the answer
 *    arrives afterwards and must not throw a modal back over the graph.
 *  - **A failure keeps the panel open.** "not a text file", "no such path": the
 *    reason is the only thing the user gets, and closing the panel throws it away
 *    before it can be read.
 *
 * Expected to FAIL until src/fileView.ts exists. One failure reason per test.
 */

import { describe, it, expect } from "vitest";
import {
  createFileView,
  requestView,
  applyView,
  failView,
  closeView,
  type FileViewState,
} from "../src/fileView";

/** The file the user clicked in every scenario below. */
const PATH = "web/src/renderer.ts";

/** A different file, for the "clicked again while it loaded" race. */
const OTHER = "daemon/server.py";

/** A panel already waiting on the daemon's answer for {@link PATH}. */
function pending(): FileViewState {
  return requestView(createFileView(), PATH);
}

/** The daemon's answer for a path, defaulting to a plain diff. */
function reply(overrides: Partial<Omit<FileViewState, "open" | "loading">> = {}) {
  return {
    path: PATH,
    mode: "diff" as const,
    content: "@@ -1,3 +1,4 @@\n+const x = 1;\n",
    truncated: false,
    error: "",
    ...overrides,
  };
}

describe("file view: the initial state", () => {
  it("starts closed, because no file has been clicked yet", () => {
    expect(createFileView().open).toBe(false);
  });

  it("starts naming no file", () => {
    expect(createFileView().path).toBe("");
  });

  it("starts with nothing in flight", () => {
    expect(createFileView().loading).toBe(false);
  });

  it("starts empty, with no content to show", () => {
    expect(createFileView().content).toBe("");
  });

  it("starts with no error, since nothing has failed", () => {
    expect(createFileView().error).toBe("");
  });

  it("starts untruncated, so no \"output cut\" notice hangs over an empty panel", () => {
    expect(createFileView().truncated).toBe(false);
  });

  it("starts in text mode, the neutral fallback the wire also degrades to", () => {
    expect(createFileView().mode).toBe("text");
  });
});

describe("requestView", () => {
  it("opens the panel on the click itself, before the daemon has answered", () => {
    // A panel that appears only when the reply lands reads as a click that
    // missed, and the user clicks again -- twice, then on something else.
    expect(pending().open).toBe(true);
  });

  it("marks the panel as loading while the answer is in flight", () => {
    expect(pending().loading).toBe(true);
  });

  it("names the file that was clicked, which is what the reply gets matched against", () => {
    expect(pending().path).toBe(PATH);
  });

  it("clears the previous file's content, so one file's diff is never shown under another's name", () => {
    const shown = applyView(pending(), reply());

    expect(requestView(shown, OTHER).content).toBe("");
  });

  it("clears the previous file's error, which accused a path that is no longer open", () => {
    const failed = failView(pending(), "not a text file");

    expect(requestView(failed, OTHER).error).toBe("");
  });

  it("clears the previous file's truncation notice", () => {
    const shown = applyView(pending(), reply({ truncated: true }));

    expect(requestView(shown, OTHER).truncated).toBe(false);
  });

  it("leaves the state it was given untouched", () => {
    const before = createFileView();

    requestView(before, PATH);

    expect(before.open).toBe(false);
  });
});

describe("applyView", () => {
  it("shows the content the daemon sent", () => {
    expect(applyView(pending(), reply()).content).toBe("@@ -1,3 +1,4 @@\n+const x = 1;\n");
  });

  it("adopts the mode, since a diff and a hex dump are not rendered the same way", () => {
    expect(applyView(pending(), reply({ mode: "hex" })).mode).toBe("hex");
  });

  it("records that the content was cut, so the panel can say so instead of lying by omission", () => {
    expect(applyView(pending(), reply({ truncated: true })).truncated).toBe(true);
  });

  it("stops loading once the content is in", () => {
    expect(applyView(pending(), reply()).loading).toBe(false);
  });

  it("keeps the panel open, since arriving content is the whole point of the panel", () => {
    expect(applyView(pending(), reply()).open).toBe(true);
  });

  it("keeps the path that was clicked", () => {
    expect(applyView(pending(), reply()).path).toBe(PATH);
  });

  it("shows the reason a frame carries instead of an empty pane", () => {
    // The daemon reports "no such path" in the frame itself; swallowing it
    // leaves a panel that is open, done loading, and blank.
    expect(applyView(pending(), reply({ content: "", error: "no such path" })).error).toBe(
      "no such path",
    );
  });

  it("ignores a reply for a file that is no longer the one open", () => {
    // THE race: the user clicked a second file while the first answer travelled
    // the network. Painting it puts renderer.ts's diff under server.py's name.
    const second = requestView(pending(), OTHER);

    expect(applyView(second, reply({ content: "renderer diff" })).content).toBe("");
  });

  it("stays loading after ignoring a reply for another file, because its own answer is still coming", () => {
    const second = requestView(pending(), OTHER);

    expect(applyView(second, reply({ content: "renderer diff" })).loading).toBe(true);
  });

  it("does not reopen a panel the user already closed", () => {
    // Escape closed it; the answer to the click arrives afterwards and must not
    // throw a modal back over the graph.
    const closed = closeView(pending());

    expect(applyView(closed, reply()).open).toBe(false);
  });

  it("leaves the state it was given untouched", () => {
    const before = pending();

    applyView(before, reply());

    expect(before.content).toBe("");
  });
});

describe("failView", () => {
  it("stores the reason, which is the only thing the user gets when there is no content", () => {
    expect(failView(pending(), "not a text file").error).toBe("not a text file");
  });

  it("stops loading, so the panel does not spin forever on a request that already failed", () => {
    expect(failView(pending(), "not a text file").loading).toBe(false);
  });

  it("keeps the panel open, because a failure the user never sees is a panel that flickered", () => {
    expect(failView(pending(), "not a text file").open).toBe(true);
  });

  it("keeps the path, so the message can name the file it is about", () => {
    expect(failView(pending(), "not a text file").path).toBe(PATH);
  });

  it("does not reopen a panel the user already closed", () => {
    const closed = closeView(pending());

    expect(failView(closed, "not a text file").open).toBe(false);
  });

  it("leaves the state it was given untouched", () => {
    const before = pending();

    failView(before, "not a text file");

    expect(before.error).toBe("");
  });
});

describe("closeView", () => {
  it("hides the panel", () => {
    expect(closeView(applyView(pending(), reply())).open).toBe(false);
  });

  it("drops the content, so the next click does not flash the previous file", () => {
    expect(closeView(applyView(pending(), reply())).content).toBe("");
  });

  it("forgets the path it was showing", () => {
    expect(closeView(applyView(pending(), reply())).path).toBe("");
  });

  it("clears an error, so a past failure does not haunt the next file", () => {
    expect(closeView(failView(pending(), "not a text file")).error).toBe("");
  });

  it("stops loading, so a reply in flight cannot leave a closed panel spinning", () => {
    expect(closeView(pending()).loading).toBe(false);
  });

  it("returns to exactly the initial state", () => {
    expect(closeView(applyView(pending(), reply({ mode: "hex", truncated: true })))).toEqual(
      createFileView(),
    );
  });
});
