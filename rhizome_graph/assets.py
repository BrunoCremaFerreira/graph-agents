"""Where the built front-end lives, as data rather than as one hard-coded path.

The daemon used to find the page with "two directories above this source file",
which is the checkout's layout and nothing else: a wheel in `site-packages`, a
distribution package under `/usr/lib` or a virtualenv anywhere all miss it, and
missing it is silent -- the daemon serves the WebSocket alone and reports itself
healthy while the browser shows a blank page.

So the search is a list, and it is split in three. :func:`web_dist_candidates`
says where to look and in which order, with no filesystem in it;
:func:`find_web_dist` walks that order, with no policy in it; and the policy
itself is a predicate handed in -- :func:`is_directory` by default,
:func:`holds_page` for the daemon. Fused, none of them could be examined without
faking the others.

The hook command is the same question asked about the other artefact this
installation owns -- "how does somebody else's `.claude/settings.json` run our
adapter?" -- so it gets the same home rather than a second search written
somewhere else. It goes wrong the same way, too: a path that is right in a
checkout and wrong in a wheel, silently.

Pure by contract: this answers a path question before any server exists, so
nothing from the daemon side (`asyncio`, `websockets`, `watchdog`) may be
imported here.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

#: The escape hatch, read first: a packager or a developer whose layout nobody
#: anticipated says so here rather than by patching Python.
WEB_DIST_ENV = "RHIZOME_WEB_DIST"

#: The file the HTTP handler appends to the static root. Its presence is what
#: separates a built front end from a directory that merely has the right name.
PAGE_NAME = "index.html"

#: Where a distribution package installs the built assets. Absolute, and
#: belonging to neither of the two roots the caller passes.
SYSTEM_WEB_DIR = Path("/usr/lib/rhizome-graph/web")

#: This package's own directory -- where the assets ship in an installed wheel,
#: beside the Python code.
PACKAGE_ROOT = Path(__file__).resolve().parent

#: The directory holding this package. In a checkout that is the repository, so
#: `web/dist` below it is the freshest build there is; installed it is
#: `site-packages`, which holds no `web/dist` and simply never matches.
CHECKOUT_ROOT = PACKAGE_ROOT.parent


def web_dist_candidates(
    environ: Mapping[str, str],
    package_root: Path,
    repo_root: Path,
) -> list[Path]:
    """Every place the built page may live, most authoritative first.

    The override wins because an override that loses to anything is not one. An
    unset variable and one set to the empty string must behave alike: passed
    through, `Path("")` is the current working directory, so the daemon would
    serve whatever the user happened to `cd` into.
    """
    candidates: list[Path] = []
    override = environ.get(WEB_DIST_ENV, "")
    if override:
        candidates.append(Path(override))
    candidates.append(Path(package_root) / "web")
    candidates.append(SYSTEM_WEB_DIR)
    candidates.append(Path(repo_root) / "web" / "dist")
    return candidates


def is_directory(candidate: Path) -> bool:
    """The loosest test a candidate can be held to, and the default one.

    `is_dir`, not `exists`: a leftover file at one of these names is not a site
    to serve.
    """
    return Path(candidate).is_dir()


def holds_page(candidate: Path) -> bool:
    """Is there a page in there? The stricter test, and the one the daemon uses.

    A directory that exists and holds no `index.html` is worse than no directory
    at all: `find_web_dist` would elect it, the daemon would report a static
    root, and the browser would get a 404 for the page -- the silent blank page
    this module exists to prevent, reached from a tree that merely was not
    built. Directories of exactly that shape are what packaging placeholders and
    half-finished builds leave behind.
    """
    return Path(candidate).joinpath(PAGE_NAME).is_file()


def find_web_dist(
    candidates: Iterable[Path],
    accept: Callable[[Path], bool] = is_directory,
) -> Path | None:
    """The first candidate `accept` recognises, or `None` if none of them is.

    The predicate is injected rather than fixed, and it is *inside* the loop
    rather than applied to the answer, which is the whole point: with an empty
    `/usr/lib/rhizome-graph/web` ahead of a good `web/dist`, a filter placed
    after the search would answer `None` and never look at the second candidate.
    Skipping a hollow candidate and going on is what a search means.

    `None` is the documented "serve the WebSocket alone" state rather than an
    error -- that is how `--dev` runs, with Vite hosting the front.
    """
    for candidate in candidates:
        if accept(Path(candidate)):
            return Path(candidate)
    return None


def default_web_dist(environ: Mapping[str, str] | None = None) -> Path | None:
    """The installed page, searched over this installation's own layout.

    Two rules, and the second is why this is not one call to
    :func:`find_web_dist`. A candidate this installation merely *guessed* at and
    got wrong is skipped, so an empty `/usr/lib/rhizome-graph/web` left by a
    half-finished install does not hide the good `web/dist` behind it. An
    override, though, is obeyed or refused and never overruled: somebody typed
    that path, so silently serving a different directory instead -- most likely a
    stale checkout build -- answers a question nobody asked, and `None` is a state
    the daemon already reports.
    """
    resolved = os.environ if environ is None else environ
    override = resolved.get(WEB_DIST_ENV, "")
    if override:
        return Path(override) if holds_page(Path(override)) else None
    return find_web_dist(
        web_dist_candidates(resolved, PACKAGE_ROOT, CHECKOUT_ROOT),
        holds_page,
    )


#: The console script an installed package owns, so a settings file elsewhere
#: can name a command instead of a source tree the user may delete.
HOOK_CONSOLE_SCRIPT = "rhi-hook"

#: The adapter as it lives in a checkout, for an installation that has no
#: console script on disk yet -- an editable install predating the entry point,
#: or a clone run straight off `sys.path`.
HOOK_SCRIPT = ("hooks", "emit_event.py")

#: What runs that script. The system interpreter by name, never this
#: environment's: the hook needs no third-party dependency, so it must not
#: depend on one existing, and a virtualenv gets rebuilt.
HOOK_INTERPRETER = "python3"

#: A scratch environment this repository builds and deletes. A command written
#: into *another* project's settings that points here keeps working until the
#: day somebody rebuilds it, and then errors on every tool call over there.
SCRATCH_VIRTUALENV = CHECKOUT_ROOT / ".venv"


def hook_command() -> str:
    """How this installation's hook is run, spelled absolutely.

    Absolute because Claude Code runs a hook from a directory nobody here chose,
    and answered the same way from every working directory because the string is
    written into a file and read back weeks later from anywhere.

    The console script wins when there is one: it survives the checkout being
    moved or deleted, which is the rot `rhi --doctor` exists to find.
    """
    installed = hook_console_script()
    if installed is not None:
        return str(installed)
    return f"{HOOK_INTERPRETER} {CHECKOUT_ROOT.joinpath(*HOOK_SCRIPT)}"


def hook_console_script() -> Path | None:
    """Where `rhi-hook` is on this machine, or ``None`` if it is nowhere usable.

    Beside the running interpreter first, because that is the installation doing
    the asking, then `$PATH`. A shim inside this repository's own scratch
    virtualenv is passed over: it resolves today and is gone after the next
    rebuild.
    """
    candidates: list[Path] = [Path(sys.executable).resolve().parent / HOOK_CONSOLE_SCRIPT]
    on_path = shutil.which(HOOK_CONSOLE_SCRIPT)
    if on_path:
        candidates.append(Path(on_path))
    for candidate in candidates:
        if candidate.is_file() and not _inside(candidate, SCRATCH_VIRTUALENV):
            return candidate
    return None


def _inside(path: Path, directory: Path) -> bool:
    """Is `path` under `directory`? Lexical, and asked of absolute paths only."""
    return str(path).startswith(f"{directory}{os.sep}")
