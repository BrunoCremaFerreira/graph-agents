/**
 * Contract tests (RED) for the path -> grammar mapping behind syntax highlighting.
 *
 * The defect: the file viewer opens a modal over the graph and paints the file
 * as ONE monochrome string. The panel exists to read what the agents wrote, and
 * reading Python or TypeScript with no colour at all is the opposite of that.
 * Colour means a grammar, and the only thing the browser knows about the file it
 * is showing is its PATH -- the daemon's `fileView` frame carries `mode` and
 * `content`, never a language. So the language is decided here, from the path,
 * on the client.
 *
 * Pure -- no DOM, no Shiki -- for the same reason as `labels.ts` and
 * `statusList.ts`: the highlighter itself is an async, wasm-loading boundary
 * (`highlight.ts`, untested by doctrine, like `wsClient.ts`), so every decision
 * it must not make lives out here where a test can reach it.
 *
 * Three properties carry the weight:
 *
 *  - **An unknown extension answers `null`, and null means plain text.** There
 *    is deliberately NO generic fallback grammar: guessing at `.psd` would
 *    colour a hex dump's neighbours as if they were code, and a wrong grammar is
 *    worse than no grammar. The first batch of languages is a closed list, and
 *    what is outside it stays uncoloured.
 *  - **Only the LAST extension counts.** `web/src/fileView.test.ts` is
 *    TypeScript, not a file of type `test.ts`; `archive.tar.gz` is not `.tar`.
 *  - **A leading dot is not an extension separator.** `.gitignore` is a file
 *    NAMED `.gitignore`, not a file with the extension `gitignore` -- the case
 *    that escapes every naive `path.split(".").pop()`, because that expression
 *    answers `"gitignore"` and any map lookup for it happens to miss today. It
 *    would stop missing the day someone adds a language whose extension matches
 *    a dotfile's name.
 *
 * The exported `LanguageId` union is the other half of the point: `highlight.ts`
 * keys its loader table as `Record<LanguageId, Loader>`, so a language mapped
 * here without a grammar loader there does not compile.
 *
 * Expected to FAIL until src/language.ts exists. One failure reason per test.
 */

import { describe, it, expect } from "vitest";
import { languageForPath } from "../src/language";

/** The first batch, as agreed: 22 grammars, no generic fallback. */
const BATCH = [
  "python",
  "javascript",
  "typescript",
  "jsx",
  "tsx",
  "json",
  "html",
  "css",
  "markdown",
  "shellscript",
  "yaml",
  "toml",
  "sql",
  "c",
  "cpp",
  "rust",
  "go",
  "java",
  "kotlin",
  "csharp",
  "php",
  "ruby",
];

/** Extension -> grammar, the pairs the panel is expected to get right. */
const CANONICAL: ReadonlyArray<readonly [string, string]> = [
  ["main.py", "python"],
  ["main.js", "javascript"],
  ["main.ts", "typescript"],
  ["App.jsx", "jsx"],
  ["App.tsx", "tsx"],
  ["package.json", "json"],
  ["index.html", "html"],
  ["style.css", "css"],
  ["README.md", "markdown"],
  ["start.sh", "shellscript"],
  ["config.yaml", "yaml"],
  ["pyproject.toml", "toml"],
  ["schema.sql", "sql"],
  ["main.c", "c"],
  ["main.cpp", "cpp"],
  ["main.rs", "rust"],
  ["main.go", "go"],
  ["Main.java", "java"],
  ["Main.kt", "kotlin"],
  ["Main.cs", "csharp"],
  ["index.php", "php"],
  ["main.rb", "ruby"],
];

/** The aliases inside the batch: a second spelling of a grammar already there. */
const ALIASES: ReadonlyArray<readonly [string, string]> = [
  ["bundle.mjs", "javascript"],
  ["bundle.cjs", "javascript"],
  ["config.mts", "typescript"],
  ["config.cts", "typescript"],
  ["config.yml", "yaml"],
  ["index.htm", "html"],
  ["CHANGELOG.markdown", "markdown"],
  ["install.bash", "shellscript"],
  ["prompt.zsh", "shellscript"],
  ["node.cc", "cpp"],
  ["node.cxx", "cpp"],
  ["node.hpp", "cpp"],
  ["node.hh", "cpp"],
  ["node.hxx", "cpp"],
  ["build.kts", "kotlin"],
];

describe("languageForPath: the first batch of grammars", () => {
  it("resolves every language in the batch from its canonical extension", () => {
    expect(CANONICAL.map(([path]) => languageForPath(path))).toEqual(
      CANONICAL.map(([, lang]) => lang),
    );
  });

  it("covers all 22 grammars of the batch, so none was mapped and then forgotten", () => {
    expect([...new Set(CANONICAL.map(([, lang]) => lang))].sort()).toEqual([...BATCH].sort());
  });

  it("resolves the second spelling of a grammar it already knows, since .yml is as common as .yaml", () => {
    expect(ALIASES.map(([path]) => languageForPath(path))).toEqual(ALIASES.map(([, lang]) => lang));
  });

  it("reads a bare .h as C, which is what VS Code does with an ambiguous header", () => {
    // C++ projects use .h too, but nothing in the path says so; VS Code's choice
    // is the one the user's editor already made, and matching it avoids a file
    // that highlights differently in two windows on the same screen.
    expect(languageForPath("include/graph.h")).toBe("c");
  });
});

describe("languageForPath: how the extension is found", () => {
  it("ignores the directories in front of the name", () => {
    expect(languageForPath("rhizome_graph/normalize.py")).toBe("python");
  });

  it("uses only the LAST extension, so a test file is TypeScript and not a `test.ts` language", () => {
    expect(languageForPath("web/src/fileView.test.ts")).toBe("typescript");
  });

  it("matches an upper-case extension, because a file is not a different language for being shouted", () => {
    expect(languageForPath("README.MD")).toBe("markdown");
  });

  it("matches a mixed-case extension too", () => {
    expect(languageForPath("scripts/A.Py")).toBe("python");
  });

  it("answers null for a file with no extension at all, which has nothing to go on", () => {
    expect(languageForPath("Makefile")).toBe(null);
  });

  it("answers null for a name that ends in a dot, since the extension is empty", () => {
    expect(languageForPath("weird.")).toBe(null);
  });

  it("answers null for a dotfile whose whole name looks like an extension", () => {
    // THE case that escapes: `".gitignore".split(".").pop()` is `"gitignore"`,
    // and the leading dot is not a separator -- this is a file NAMED
    // `.gitignore`, not a file of type `gitignore`.
    expect(languageForPath(".gitignore")).toBe(null);
  });

  it("answers null for a dotfile inside a directory, where the leading dot is just as unremarkable", () => {
    expect(languageForPath("src/.bashrc")).toBe(null);
  });

  it("answers null for a name with no extension under a directory whose name has a dot", () => {
    // `a.b/c` is a file called `c`; the dot belongs to the directory, and a
    // right-most `indexOf(".")` over the whole path finds it.
    expect(languageForPath("a.b/c")).toBe(null);
  });

  it("answers null for an extension outside the batch, rather than guessing a grammar", () => {
    expect(languageForPath("assets/logo.psd")).toBe(null);
  });

  it("answers null for an invented extension, since there is no generic fallback by design", () => {
    expect(languageForPath("notes.xyz")).toBe(null);
  });

  it("answers null for the empty path, which the panel can be handed while it is closed", () => {
    expect(languageForPath("")).toBe(null);
  });

  it("answers null for a directory-looking path ending in a slash", () => {
    expect(languageForPath("web/src/")).toBe(null);
  });

  it("does not read an extension out of a parent directory when the name has none", () => {
    // `src.ts/Makefile` is a Makefile; the dot belongs to the directory.
    expect(languageForPath("src.ts/Makefile")).toBe(null);
  });
});
