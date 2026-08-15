/**
 * Which grammar a file's path asks for.
 *
 * The daemon's `fileView` frame carries `mode` and `content` and never a
 * language — nothing on the wire says "this is Python". The only thing the
 * browser knows about the file it is showing is its PATH, so the decision is
 * made here, on the client, from the name alone.
 *
 * Pure, for the same reason as {@link ./labels} and {@link ./statusList}: the
 * highlighter itself ({@link ./highlight}) is an async, wasm-loading boundary
 * that no unit test can reach, so every decision it must not make lives out
 * here.
 *
 * Three rules carry the weight:
 *
 *  - **An unknown extension answers `null`, and null means plain text.** There
 *    is deliberately no generic fallback grammar: a wrong grammar is worse than
 *    no grammar, and the first batch of languages is a closed list.
 *  - **Only the LAST extension counts.** `fileView.test.ts` is TypeScript, not
 *    a file of type `test.ts`.
 *  - **A leading dot is not a separator.** `.gitignore` is a file NAMED
 *    `.gitignore`; `"…".split(".").pop()` answers `"gitignore"` and would start
 *    matching the day a language claimed that extension.
 *
 * The exported {@link LanguageId} union is the other half of the point: the
 * loader table in {@link ./highlight} is a `Record<LanguageId, Loader>`, so a
 * language named here without a grammar to load there does not compile.
 */

/** The first batch of grammars: 22 languages, no generic fallback. */
export type LanguageId =
  | "python"
  | "javascript"
  | "typescript"
  | "jsx"
  | "tsx"
  | "json"
  | "html"
  | "css"
  | "markdown"
  | "shellscript"
  | "yaml"
  | "toml"
  | "sql"
  | "c"
  | "cpp"
  | "rust"
  | "go"
  | "java"
  | "kotlin"
  | "csharp"
  | "php"
  | "ruby";

/**
 * Lower-cased extension → grammar.
 *
 * The aliases are the second spellings that are as common as the first: `.yml`,
 * `.mjs`, the four C++ header suffixes. A bare `.h` reads as C, which is what
 * VS Code does with an ambiguous header — matching the user's own editor beats
 * being cleverer than it in one of two windows on the same screen.
 */
const BY_EXTENSION: Readonly<Record<string, LanguageId>> = {
  py: "python",
  js: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  ts: "typescript",
  mts: "typescript",
  cts: "typescript",
  jsx: "jsx",
  tsx: "tsx",
  json: "json",
  html: "html",
  htm: "html",
  css: "css",
  md: "markdown",
  markdown: "markdown",
  sh: "shellscript",
  bash: "shellscript",
  zsh: "shellscript",
  yaml: "yaml",
  yml: "yaml",
  toml: "toml",
  sql: "sql",
  c: "c",
  h: "c",
  cpp: "cpp",
  cc: "cpp",
  cxx: "cpp",
  hpp: "cpp",
  hh: "cpp",
  hxx: "cpp",
  rs: "rust",
  go: "go",
  java: "java",
  kt: "kotlin",
  kts: "kotlin",
  cs: "csharp",
  php: "php",
  rb: "ruby",
};

/**
 * The grammar for a path, or `null` when nothing in the name says which.
 *
 * The extension is read from the last path segment only: a dot in a directory
 * name (`a.b/c`, `src.ts/Makefile`) says nothing about the file.
 */
export function languageForPath(path: string): LanguageId | null {
  const name = path.slice(path.lastIndexOf("/") + 1);
  const dot = name.lastIndexOf(".");
  // `dot <= 0` covers both "no extension" and the dotfile whose whole name
  // looks like one; a trailing dot leaves an empty extension, which misses.
  if (dot <= 0) return null;
  return BY_EXTENSION[name.slice(dot + 1).toLowerCase()] ?? null;
}
