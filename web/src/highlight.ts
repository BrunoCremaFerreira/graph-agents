/**
 * The syntax highlighter: the one module that names shiki.
 *
 * A dirty boundary, thin on purpose and untested by doctrine, like
 * {@link ./wsClient}. Everything it could get wrong on its own — which grammar a
 * path asks for, which fragments are worth tokenizing, where the tokens land —
 * was pulled out into {@link ./language}, {@link ./fileDoc} and
 * {@link ./fileView}, so that no test needs a mock, a DOM, or the wasm engine
 * behind this file.
 *
 * Four rules hold here:
 *
 *  - **Nothing is loaded until a file is opened.** This whole module arrives
 *    through `await import("./highlight")`, the wasm engine and the theme
 *    through imports inside it, and each grammar through a chunk of its own —
 *    so the page's first paint pays nothing for a panel that may never open.
 *  - **The 22 loaders are written out as literal arrows.** A computed
 *    `import(\`@shikijs/langs/${id}\`)` cannot be scanned by Rollup: it either
 *    fails the build or drags all 346 grammars into `dist`. Typing the table as
 *    `Record<LanguageId, …>` makes `tsc` prove it stayed in step with
 *    {@link ./language} for free.
 *  - **The engine is oniguruma (wasm), not the JavaScript one.** Measured side
 *    by side over the batch: the JS engine loses a trailing `// c` comment in
 *    C++ and collapses on embedded `<script>`/`<style>` in HTML, and `forgiving`
 *    swallows the failure silently. Fidelity is the whole reason for choosing
 *    shiki over a hand-rolled tokenizer.
 *  - **Everything is caught, and failure resolves to `null`.** Colour is an
 *    enhancement; a grammar that will not load must leave the viewer showing
 *    the file in plain text, never break it.
 */

import type { CodeChunk, CodeToken } from "./fileView";
import type { HighlightRequest } from "./fileDoc";
import type { LanguageId } from "./language";

/** Dark+'s own foreground, for a token the grammar gave no colour. */
const DEFAULT_COLOR = "#D4D4D4";

/** `fontStyle` is a bitfield, not an enum — test the bit, never import it. */
const ITALIC = 1;
const BOLD = 2;

/** The theme, by the name it registers itself under. */
const THEME = "dark-plus";

/**
 * A grammar per language, each as a literal `import()` so Rollup can see it.
 *
 * `Record<LanguageId, …>` is what keeps this exhaustive: a language added to
 * {@link ./language} without a loader here does not compile.
 */
const LOADERS: Record<LanguageId, () => Promise<unknown>> = {
  python: () => import("@shikijs/langs/python"),
  javascript: () => import("@shikijs/langs/javascript"),
  typescript: () => import("@shikijs/langs/typescript"),
  jsx: () => import("@shikijs/langs/jsx"),
  tsx: () => import("@shikijs/langs/tsx"),
  json: () => import("@shikijs/langs/json"),
  html: () => import("@shikijs/langs/html"),
  css: () => import("@shikijs/langs/css"),
  markdown: () => import("@shikijs/langs/markdown"),
  shellscript: () => import("@shikijs/langs/shellscript"),
  yaml: () => import("@shikijs/langs/yaml"),
  toml: () => import("@shikijs/langs/toml"),
  sql: () => import("@shikijs/langs/sql"),
  c: () => import("@shikijs/langs/c"),
  cpp: () => import("@shikijs/langs/cpp"),
  rust: () => import("@shikijs/langs/rust"),
  go: () => import("@shikijs/langs/go"),
  java: () => import("@shikijs/langs/java"),
  kotlin: () => import("@shikijs/langs/kotlin"),
  csharp: () => import("@shikijs/langs/csharp"),
  php: () => import("@shikijs/langs/php"),
  ruby: () => import("@shikijs/langs/ruby"),
};

/** The one highlighter, built at most once. Its type is left inferred. */
let corePromise: ReturnType<typeof buildCore> | null = null;

/** Grammar loads already in flight, so two quick opens load one grammar. */
const loaded = new Map<LanguageId, Promise<void>>();

async function buildCore() {
  const [{ createHighlighterCore }, { createOnigurumaEngine }, theme] = await Promise.all([
    import("shiki/core"),
    import("shiki/engine/oniguruma"),
    import("@shikijs/themes/dark-plus"),
  ]);
  return createHighlighterCore({
    themes: [theme.default],
    // Grammars are loaded on demand; bundling them here is the 15 MB mistake.
    langs: [],
    engine: await createOnigurumaEngine(import("shiki/wasm")),
  });
}

function core(): ReturnType<typeof buildCore> {
  if (corePromise === null) {
    corePromise = buildCore();
    // A rejected promise cached forever would make one bad load permanent.
    corePromise.catch(() => {
      corePromise = null;
    });
  }
  return corePromise;
}

/** Load a grammar once, even if two files of that language open at once. */
function loadLanguage(
  highlighter: Awaited<ReturnType<typeof buildCore>>,
  lang: LanguageId,
): Promise<void> {
  const inFlight = loaded.get(lang);
  if (inFlight) return inFlight;
  // shiki accepts the raw `import()` promise; unwrapping `.default` is wrong.
  // The table is typed `Promise<unknown>` so no shiki type is named outside the
  // call itself, which is where the cast puts it back.
  const started = highlighter
    .loadLanguage(LOADERS[lang]() as Parameters<typeof highlighter.loadLanguage>[0])
    .then(() => undefined);
  started.catch(() => loaded.delete(lang));
  loaded.set(lang, started);
  return started;
}

/**
 * Tokenize every fragment, or answer `null` if anything at all went wrong.
 *
 * Asynchronous although the tokenizing itself is synchronous, deliberately:
 * moving the work to a Web Worker later changes this file and no test.
 */
export async function highlightChunks(
  lang: LanguageId | null,
  requests: readonly HighlightRequest[],
): Promise<readonly CodeChunk[] | null> {
  if (lang === null || requests.length === 0) return null;
  try {
    const highlighter = await core();
    await loadLanguage(highlighter, lang);
    return requests.map((request) =>
      highlighter
        .codeToTokensBase(request.code, {
          lang,
          theme: THEME,
          includeExplanation: false,
          // A longer line comes back as one unstyled token instead of stalling.
          tokenizeMaxLineLength: 2000,
          tokenizeTimeLimit: 500,
        })
        .map((line) =>
          line.map(
            (token): CodeToken => ({
              text: token.content,
              color: token.color ?? DEFAULT_COLOR,
              italic: ((token.fontStyle ?? 0) & ITALIC) !== 0,
              bold: ((token.fontStyle ?? 0) & BOLD) !== 0,
            }),
          ),
        ),
    );
  } catch {
    // No colour is a degradation; a broken viewer is a defect.
    return null;
  }
}
