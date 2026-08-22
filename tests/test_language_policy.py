"""The project speaks English, everywhere a human reads it.

Motivation: the HUD once counted uncommitted changes in Portuguese under an
English keys legend, and `start.sh` explained itself in Portuguese to whoever
ran it. Mixed languages are worse than either one alone -- a reader has to
switch mid-sentence, and a grep for a message they saw on screen finds nothing.

What this guards is the *authored* surface: identifiers, comments, docstrings,
and every string a user can end up reading (HUD text, shell log lines, help
output). It scans production sources, shell scripts and the agent definitions.

Two deliberate exclusions:

  * **`tests/` and `web/tests/`.** Encoding behaviour has to be specified with
    real non-ASCII bytes -- a `looks_binary` fixture, a `git status` path that
    forces `core.quotePath` -- so accented fixtures there are the point, not a
    slip. Test *prose* is still English by rule; it is just not machine-checked.
  * **Generated and vendored trees** (`web/dist`, `node_modules`, lockfiles).

The check is two-pronged: an accented Latin letter (which no English word in
this codebase uses), and a small list of unambiguous Portuguese words that
carry no accent. Neither is a language detector; both are enough to catch the
way this actually goes wrong, which is a whole comment or message written in
the other language. A corollary for the files it scans, `CLAUDE.md` included:
describe the forbidden text, never quote it, or the document fails its own rule.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Directories whose every source file is checked, recursively. The last three
#: are the packaging trees: a `Description:` read in `apt show`, a `desc` read in
#: `brew info` and a build script that prints to a terminal are all authored text
#: a human ends up reading.
SCANNED_DIRS = (
    "rhizome_graph",
    "daemon",
    "hooks",
    "web/src",
    "config",
    ".claude/agents",
    "debian",
    "Formula",
    "packaging",
)

#: Individually scanned files that sit at the repository root.
SCANNED_FILES = ("start.sh", "run.sh", "CLAUDE.md", "README.md")

#: `""` is the suffix of `debian/control`, `debian/changelog` and every other
#: maintainer file dpkg names without an extension, and naming a directory above
#: without accepting it here scans zero files -- a green test over a tree nobody
#: reads, which is worse than no coverage because the directory name reads as
#: coverage. `.rb` is Homebrew's.
SCANNED_SUFFIXES = {
    "",
    ".py",
    ".ts",
    ".js",
    ".css",
    ".html",
    ".sh",
    ".json",
    ".md",
    ".rb",
}

#: Directories that hold somebody else's bytes rather than authored text.
#: `tmp`, `files` and `.debhelper` are what a Debian build leaves inside
#: `debian/`; the policy has no business failing on a vendored dependency.
GENERATED_DIRS = {"node_modules", "dist", "__pycache__", "tmp", "files", ".debhelper"}

#: Any Latin letter carrying a diacritic. English here uses none. The two gaps
#: are U+00D7 `×` and U+00F7 `÷`, which sit inside the Latin-1 letter block
#: without being letters -- `(hunk × side)` and `×3` are legitimate prose.
ACCENTED = re.compile(r"[À-ÖØ-öø-ÿĀ-ſ]")

#: Portuguese words that survive without an accent, so ACCENTED misses them.
#: Each must be a word no English sentence in this repository would contain.
PORTUGUESE_WORDS = (
    "arquivo",
    "arquivos",
    "diretorio",
    "usuario",
    "desenvolvedor",
    "alteracao",
    "alteracoes",
    "mudanca",
    "nao",
    "sobe",
    "roda",
    "para o",
    "com o",
    "que o",
    "de que",
    "do projeto",
    "da porta",
)

WORD_RE = tuple(
    (word, re.compile(rf"(?<![\w-]){re.escape(word)}(?![\w-])", re.IGNORECASE))
    for word in PORTUGUESE_WORDS
)


def _scanned_files(root: Path = REPO_ROOT) -> list[Path]:
    """Every authored source file the policy covers, under `root`."""
    found: list[Path] = []
    for name in SCANNED_FILES:
        path = root / name
        if path.is_file():
            found.append(path)
    for name in SCANNED_DIRS:
        base = root / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
                continue
            # Judged against the path RELATIVE to the root, never against
            # `path.parts`. Those are the components of an ABSOLUTE path, and
            # `tmp`, `dist`, `files` and `node_modules` are ordinary names for a
            # directory *above* a checkout -- so a repository cloned into
            # `/tmp/build/...`, which is what CI and `git worktree` do, matched
            # on every file and discarded the whole tree. What was left was the
            # root-level SCANNED_FILES, appended above with no filter at all:
            # this policy silently became a green test over four files.
            if any(part in GENERATED_DIRS for part in path.relative_to(root).parts):
                continue
            found.append(path)
    return found


def _offences(path: Path) -> list[str]:
    """Lines of `path` that read as Portuguese, formatted for a failure message."""
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []

    rel = path.relative_to(REPO_ROOT)
    hits: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        reason = ""
        match = ACCENTED.search(line)
        if match:
            reason = f"accented letter {match.group(0)!r}"
        else:
            for word, pattern in WORD_RE:
                if pattern.search(line):
                    reason = f"Portuguese word {word!r}"
                    break
        if reason:
            hits.append(f"{rel}:{number}: {reason}: {line.strip()[:90]}")
    return hits


def test_the_policy_actually_reads_something() -> None:
    """A scan that silently covers nothing would pass forever."""
    files = _scanned_files()

    assert len(files) > 30, f"expected the whole source tree, got {files}"
    assert any(f.name == "start.sh" for f in files)
    assert any(f.suffix == ".ts" for f in files)
    # A name in SCANNED_DIRS that no longer exists on disk is skipped in
    # silence -- which is exactly how a rename of the Python package turns this
    # whole policy into a green test that reads none of it.
    assert any(
        "rhizome_graph" in path.parts for path in files
    ), "the Python package is not being scanned; SCANNED_DIRS names a directory that is gone"


def test_no_portuguese_in_authored_sources() -> None:
    """Identifiers, comments and user-visible text are English."""
    offences = [hit for path in _scanned_files() for hit in _offences(path)]

    assert offences == [], "non-English text in authored sources:\n" + "\n".join(offences)


@pytest.mark.parametrize(
    "line",
    [
        "  const base = total === 1 ? '1 alteração' : `${total} alterações`;",
        "# start.sh — bootstrap + run do projeto rhizome-graph",
        "  err 'Não foi possível obter um npm'",
        "# roda com cwd em web/",
    ],
)
def test_the_detector_catches_the_text_this_repository_actually_had(line: str) -> None:
    """Guard against a checker that passes because it never matches anything."""
    assert ACCENTED.search(line) or any(p.search(line) for _, p in WORD_RE), line
