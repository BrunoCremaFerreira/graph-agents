"""Turning a directory typed in the page into a root the daemon can observe.

The observed root used to be settled once, at boot, by
``RHIZOME_PROJECT_ROOT``: watching a second project meant killing the daemon
and starting over. The page now lets the viewer retype it (``ctrl+L`` opens the
field, ``Tab`` completes, ``Enter`` applies), and this module is the piece
underneath: it decides whether what was typed is a directory, and it answers the
``Tab`` -- the browser cannot read the daemon's disk, so completion has to happen
here.

Two rules shape everything below.

  * **Nothing raises.** This is called from the daemon's event loop, on data
    typed by a human: a NUL byte, a directory whose permissions were just
    revoked, ``//``. An exception here would kill the task serving every
    connected browser, so every failure collapses to ``None`` (refused) or to an
    empty candidate list (nothing to complete) -- both of which the page can
    show. The same house rule as :mod:`rhizome_graph.repo`.
  * **``home`` is a parameter**, never ``os.path.expanduser``. The daemon may
    want to expand ``~`` against something other than its own process
    environment, and passing it in is what makes this module testable without a
    fixed ``$HOME``.

Stdlib only, and pure apart from the directory reads it exists to perform.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Completion:
    """The answer to one ``Tab``.

    ``completed`` is what the field should now contain -- always absolute and
    ``~``-expanded, so the next ``Tab`` has an unambiguous starting point -- and
    ``matches`` are the candidate names for the page to list under it. When
    nothing matches, ``completed`` is the text as it stood: truncating it back to
    the parent directory would delete what the viewer just typed.
    """

    completed: str
    matches: list[str] = field(default_factory=list)


def resolve_root(text: str, home: str) -> str | None:
    """The absolute directory `text` names, or ``None`` if it names none.

    ``None`` is the ordinary answer for a typo, a file, or an empty field -- the
    page turns it into "no such directory". An empty field in particular must not
    fall back to the daemon's cwd, which is not a place anyone asked to look at.

    The path is normalized (``..`` segments, trailing slash) so that
    ``~/x/../x/`` and ``~/x`` cannot become two roots that look different and
    watch the same tree.
    """
    try:
        expanded = _expand_user(text.strip(), home)
        if not expanded:
            return None
        resolved = os.path.normpath(os.path.abspath(expanded))
        return resolved if os.path.isdir(resolved) else None
    except Exception:
        return None


def complete_dir(text: str, home: str) -> Completion:
    """Complete `text` to a directory the way a shell completes ``Tab``.

    Only directories are candidates: a root that is a file cannot be observed, so
    offering one would only produce a refusal one keystroke later. Hidden entries
    stay out until the viewer types the dot that asks for them, which is what
    keeps a listing from being three quarters ``.git``/``.venv``/``.cache``.

    With a single candidate the completion ends in ``/`` -- that trailing slash
    is what lets the next ``Tab`` descend instead of re-offering the same name
    forever. With several, the text only advances to their longest common prefix,
    so repeated tabs converge instead of picking a winner arbitrarily.
    """
    try:
        expanded = _expand_user(text, home)
        parent, prefix = os.path.split(expanded)
        matches = _matching_dirs(parent, prefix)
        if not matches:
            return Completion(expanded, [])
        if len(matches) == 1:
            return Completion(os.path.join(parent, matches[0]) + "/", matches)
        return Completion(os.path.join(parent, os.path.commonprefix(matches)), matches)
    except Exception:
        # Belt and braces: the reads below are already guarded, but this function
        # must survive inputs nobody thought of.
        return Completion(text, [])


def _expand_user(text: str, home: str) -> str:
    """``~`` and ``~/...`` against the *given* home, and nothing else.

    ``~someuser`` is deliberately left untouched rather than looked up in the
    password database: it is not a form anyone types into this field, and
    resolving it would reach outside the home the caller handed us.
    """
    if not home or not text.startswith("~"):
        return text
    if text == "~":
        return home
    if text.startswith("~/"):
        return os.path.join(home.rstrip("/") or "/", text[2:])
    return text


def _matching_dirs(parent: str, prefix: str) -> list[str]:
    """Sorted names of directories in `parent` starting with `prefix`.

    A missing or unreadable parent yields no candidates instead of an error: the
    viewer is typing, and half a path naming nowhere is the normal intermediate
    state, not a fault.

    ``parent`` is empty when the text has no slash yet; the listing then falls
    back to the daemon's cwd, but the completion is rebuilt on the empty parent
    so the field keeps the relative shape that was typed.
    """
    try:
        with os.scandir(parent or ".") as entries:
            names = [
                entry.name
                for entry in entries
                if entry.name.startswith(prefix) and _is_dir(entry)
            ]
    except (OSError, ValueError):
        return []
    if not prefix.startswith("."):
        names = [name for name in names if not name.startswith(".")]
    # The filesystem lists in whatever order it pleases; the page shows this
    # list, and a stable one is the only kind a human can scan.
    return sorted(names)


def _is_dir(entry: os.DirEntry) -> bool:
    """Symlinks are followed on purpose -- a link to a project is a project."""
    try:
        return entry.is_dir()
    except OSError:
        return False
