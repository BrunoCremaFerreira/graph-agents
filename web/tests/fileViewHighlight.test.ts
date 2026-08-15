/**
 * Contract tests (RED) for the second, ASYNCHRONOUS half of the file viewer.
 *
 * The defect this closes is a race that only exists once the panel is coloured.
 * Syntax highlighting cannot happen in the same tick: the first file opened
 * downloads a wasm engine and a grammar, and tokenizing runs while the graph
 * keeps animating behind the modal. So the tokens for one content can land
 * after the user has clicked something else -- or after Escape, or after
 * clicking the SAME file again -- and painting them then puts one text's
 * colours on another text's lines, off by however many lines the two differ.
 *
 * `applyView` already guards this race for CONTENT, by path. The guard here is
 * the CONTENT ITSELF, which is both simpler and strictly stronger:
 *
 *  - it subsumes the path check -- a different file has different content, and
 *    when it does not (two empty files) the tokens are identical anyway, so
 *    adopting them is not a defect;
 *  - it catches what a path check CANNOT: clicking the same file twice while
 *    the first tokenization is in flight. Same path, older text, and the stale
 *    colours would land on the re-read content.
 *
 * On the happy path it is a reference comparison -- `forContent` is the very
 * string that was handed to the tokenizer -- so it costs nothing.
 *
 * Refusal returns the SAME reference, which is the idiom `applyView` already
 * established and what lets `main.ts` write `if (next !== fileView)` as its test
 * for "was it adopted?". And the invariant that makes the whole thing safe:
 * tokens in the state always describe `state.content`, so `applyView` and
 * `requestView` both CLEAR them -- new content invalidates old colour, and
 * colour is a strict enhancement, so dropping it is always safe while keeping
 * it is not.
 *
 * The state gains ONE field, `highlight`. No revision counter: a counter has to
 * be threaded through every transition and can only ever approximate what the
 * content already says exactly.
 *
 * No shiki here, not even as a type: `CodeToken` is ours.
 *
 * Expected to FAIL until src/fileView.ts grows `highlight` and `applyTokens`.
 * One failure reason per test.
 */

import { describe, it, expect } from "vitest";
import {
  createFileView,
  requestView,
  applyView,
  closeView,
  applyTokens,
  type FileViewState,
} from "../src/fileView";

/** The file the user clicked. */
const PATH = "graphagents/normalize.py";

/** A different file, for the "clicked another one while it tokenized" race. */
const OTHER = "web/src/renderer.ts";

/** What the daemon answered for {@link PATH}. */
const CONTENT = "import os\nx = 1\n";

/** One syntax token, in the shape `highlight.ts` converts shiki's into. */
function tok(text: string, color: string, italic = false, bold = false) {
  return { text, color, italic, bold };
}

/** One tokenized fragment per request; here, one fragment of two lines. */
const CHUNKS = [[[tok("import", "#C586C0"), tok(" os", "#D4D4D4")], [tok("x = 1", "#9CDCFE")]]];

/** A panel showing {@link CONTENT}, waiting to be coloured. */
function open(path = PATH, content = CONTENT): FileViewState {
  return applyView(requestView(createFileView(), path), {
    path,
    mode: "text",
    content,
    truncated: false,
    error: "",
  });
}

describe("file view: the initial highlight state", () => {
  it("starts with no tokens, there being no content to describe", () => {
    expect(createFileView().highlight).toBe(null);
  });
});

describe("applyTokens: adopting", () => {
  it("takes the chunks when the panel is open on the very content they describe", () => {
    expect(applyTokens(open(), CONTENT, CHUNKS).highlight).toEqual(CHUNKS);
  });

  it("leaves the content alone, colour being an enhancement over text already on screen", () => {
    expect(applyTokens(open(), CONTENT, CHUNKS).content).toBe(CONTENT);
  });

  it("leaves the panel open, since adopting colour is not a state change the user asked for", () => {
    expect(applyTokens(open(), CONTENT, CHUNKS).open).toBe(true);
  });

  it("leaves the state it was given untouched", () => {
    const before = open();

    applyTokens(before, CONTENT, CHUNKS);

    expect(before.highlight).toBe(null);
  });
});

describe("applyTokens: refusing", () => {
  it("returns the very same state once the panel has been closed", () => {
    // Escape closed it; the tokenization finished afterwards and must not put
    // a modal back over the graph, nor leave state for the next open to adopt.
    const closed = closeView(open());

    expect(applyTokens(closed, CONTENT, CHUNKS)).toBe(closed);
  });

  it("returns the very same state for content that is no longer the one shown", () => {
    // THE race: tokens for normalize.py landing after a click on renderer.ts
    // would colour one file's text with another file's grammar run.
    const second = open(OTHER, "import * as THREE from 'three';\n");

    expect(applyTokens(second, CONTENT, CHUNKS)).toBe(second);
  });

  it("returns the very same state for a stale read of the SAME file, which a path check would miss", () => {
    // Clicked twice while the first tokenization was in flight, and an agent
    // rewrote the file in between: same path, different text.
    const reread = open(PATH, "import os\nx = 2\n");

    expect(applyTokens(reread, CONTENT, CHUNKS)).toBe(reread);
  });

  it("returns the very same state while the panel is still loading, having no content yet", () => {
    const loading = requestView(createFileView(), PATH);

    expect(applyTokens(loading, CONTENT, CHUNKS)).toBe(loading);
  });
});

describe("the invariant: tokens always describe state.content", () => {
  it("drops the tokens when new content arrives for the same file", () => {
    const coloured = applyTokens(open(), CONTENT, CHUNKS);
    const rewritten = applyView(coloured, {
      path: PATH,
      mode: "text",
      content: "import sys\ny = 2\n",
      truncated: false,
      error: "",
    });

    expect(rewritten.highlight).toBe(null);
  });

  it("drops the tokens when another file is clicked, before its answer even arrives", () => {
    const coloured = applyTokens(open(), CONTENT, CHUNKS);

    expect(requestView(coloured, OTHER).highlight).toBe(null);
  });

  it("drops the tokens when the panel is closed", () => {
    const coloured = applyTokens(open(), CONTENT, CHUNKS);

    expect(closeView(coloured).highlight).toBe(null);
  });
});
