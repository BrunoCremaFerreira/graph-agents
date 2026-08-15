"""Pure normalization of a Claude Code hook event into a broadcastable Event.

This module maps one Claude Code hook payload (as delivered on the hook's stdin)
to a single :class:`Event`, or ``None`` when the payload does not correspond to a
visualizable filesystem operation.

Design notes:
  * The function is **pure** and side-effect free: no I/O, no filesystem access.
    Whether a Write is an add (``A``) or a modification (``M``) is decided from
    the caller-supplied ``known_paths`` set, so the "seen paths" state lives in
    the daemon (single source of truth) rather than being probed per call.
  * It is **defensive by contract**: any malformed input returns ``None`` and
    never raises, because it runs inside a Claude Code hook that must never
    disrupt the user's session.
"""

from __future__ import annotations

import os.path
import shlex
import time
from dataclasses import dataclass

# Operation types and their fixed Gource colors (hex, no leading '#').
_OP_ADDED = "A"
_OP_MODIFIED = "M"
_OP_DELETED = "D"
#: A file an agent read. Not a change to the tree -- see `_read_path` and
#: :meth:`daemon.server.EventHub._broadcast_transient` for what that costs.
_OP_READ = "R"

_COLOR_BY_TYPE = {
    _OP_ADDED: "33FF33",
    _OP_MODIFIED: "FFAA00",
    _OP_DELETED: "FF3333",
    _OP_READ: "AA66FF",
}

# Where an event came from. The frontend reads this to decide how loudly to draw
# it: a seeded file is part of the tree's backdrop and must not flash or spawn an
# actor, while a hook or watcher event is live activity.
ORIGIN_HOOK = "hook"
ORIGIN_SEED = "seed"
ORIGIN_WATCH = "watch"

# Bash commands whose first non-flag argument is the affected path, mapped to
# the operation they represent.
_DELETE_COMMANDS = {"rm", "rmdir"}
_ADD_COMMANDS = {"mkdir", "touch"}


@dataclass
class Event:
    """A single normalized activity event, ready to be serialized to JSON.

    Attributes:
        ts: Unix time in seconds (float).
        agent: Actor **identity** -- the hook's ``agent_id`` when a subagent made
            the call, else its ``session_id``. See :func:`actor_of`.
        type: Operation kind, one of ``"A"`` (added), ``"M"`` (modified),
            ``"D"`` (deleted) or ``"R"`` (read -- the file was opened, nothing
            about the tree changed).
        path: Path relative to the observed project root.
        color: Hex color WITHOUT a leading ``#`` (A->33FF33, M->FFAA00,
            D->FF3333, R->AA66FF).
        origin: What produced the event -- ``"hook"`` (a Claude tool call),
            ``"seed"`` (the tree snapshot taken at boot) or ``"watch"`` (the
            filesystem watcher).
        label: Readable name for the actor (``agent_type``, e.g.
            ``"developer-backend"``), for display only. Last field on
            purpose: every existing positional construction keeps working.
    """

    ts: float
    agent: str
    type: str
    path: str
    color: str
    origin: str = ORIGIN_HOOK
    label: str = ""


def actor_of(hook_json: dict) -> tuple[str, str]:
    """Return ``(agent, label)`` for one hook payload; never raises.

    Two separate notions, because conflating them costs a figure either way:

      * **agent** is the identity. A subagent call carries the session's
        ``session_id`` *plus* its own ``agent_id``, so keying on the session
        alone collapses every specialist of a session into one on-screen actor.
        The opaque ``agent_id`` is what splits them; the session remains the
        truthful fallback for the orchestrator's own calls (which carry no
        ``agent_id`` key at all) and for a junk one.
      * **label** is ``agent_type``, the readable name a viewer reads under the
        figure. It never takes part in the identity: a renamed type would
        otherwise fork one subagent into two actors mid-session, and a malformed
        ``agent_type`` would cost the attribution that did arrive intact.

    Shared with the daemon on purpose: :class:`daemon.server.EventHub` records
    the last actor straight from the raw payload even when normalization yields
    no event (a glob-expanding ``cp``), and two copies of this rule would drift
    into crediting the watcher's changes to a different figure than the hook's.
    """
    if not isinstance(hook_json, dict):
        return "", ""
    agent = _usable_text(hook_json.get("agent_id")) or _usable_text(
        hook_json.get("session_id")
    )
    return agent, _usable_text(hook_json.get("agent_type"))


def _usable_text(value: object) -> str:
    """The value as a stripped string, or ``""`` if it is not usable as one.

    Anything non-string (a number, a dict, ``None``) or blank is refused rather
    than coerced: the project's rule is that garbage must never invent an actor.
    """
    return value.strip() if isinstance(value, str) and value.strip() else ""


def normalize_event(
    hook_json: dict,
    known_paths: set[str] | None = None,
    project_root: str | None = None,
) -> Event | None:
    """Turn one Claude Code hook payload into an :class:`Event` (or ``None``).

    See the module docstring and ``tests/test_normalize.py`` for the full
    contract. Malformed input NEVER raises: it returns ``None``.
    """
    try:
        return _normalize(hook_json, known_paths or set(), project_root)
    except Exception:
        # A hook must never crash the user's session: swallow everything.
        return None


def _normalize(
    hook_json: dict,
    known_paths: set[str],
    project_root: str | None,
) -> Event | None:
    if not isinstance(hook_json, dict):
        return None

    tool_name = hook_json.get("tool_name")
    tool_input = hook_json.get("tool_input")
    if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
        return None

    agent, label = actor_of(hook_json)

    resolved = _resolve_operation(tool_name, tool_input, known_paths, project_root)
    if resolved is None:
        return None
    op_type, path = resolved

    return Event(
        ts=_timestamp(hook_json),
        agent=agent,
        type=op_type,
        path=path,
        color=_COLOR_BY_TYPE[op_type],
        label=label,
    )


def seed_event(path: str, ts: float | None = None) -> Event:
    """Build the event that puts an already-existing file on screen.

    Seeded files belong to no agent (``agent=""``): they were there before the
    session started, so attributing them to whoever connects first would draw a
    beam for work nobody did.
    """
    return Event(
        ts=ts if ts is not None else time.time(),
        agent="",
        type=_OP_ADDED,
        path=path,
        color=_COLOR_BY_TYPE[_OP_ADDED],
        origin=ORIGIN_SEED,
    )


def fs_event(
    path: str,
    op_type: str,
    agent: str = "",
    ts: float | None = None,
    label: str = "",
) -> Event | None:
    """Build an event for a change the watcher observed, or ``None`` if invalid.

    `agent` and `label` are filled in by the daemon from the hook that fired
    around the same time; the watcher itself knows neither. An empty `agent`
    means the change could not be attributed (a manual edit, a build step) and
    the frontend draws it without an actor. `label` travels with `agent` so the
    specialist's figure keeps its name for the changes only the watcher sees --
    a glob or a compound command -- which is most of what a busy agent does.
    """
    if op_type not in _COLOR_BY_TYPE or not path:
        return None
    return Event(
        ts=ts if ts is not None else time.time(),
        agent=agent,
        type=op_type,
        path=path,
        color=_COLOR_BY_TYPE[op_type],
        origin=ORIGIN_WATCH,
        label=label,
    )


def _resolve_operation(
    tool_name: str,
    tool_input: dict,
    known_paths: set[str],
    project_root: str | None,
) -> tuple[str, str] | None:
    """Return ``(op_type, relative_path)`` for a relevant tool, else ``None``."""
    if tool_name == "Write":
        rel = _relative_file_path(tool_input, project_root)
        if rel is None:
            return None
        op_type = _OP_MODIFIED if rel in known_paths else _OP_ADDED
        return op_type, rel

    if tool_name in ("Edit", "MultiEdit"):
        rel = _relative_file_path(tool_input, project_root)
        if rel is None:
            return None
        return _OP_MODIFIED, rel

    if tool_name == "Read":
        rel = _read_path(tool_input, project_root)
        if rel is None:
            return None
        return _OP_READ, rel

    if tool_name == "Bash":
        return _parse_bash(tool_input, project_root)

    # Grep, Glob, WebFetch, ... -> nothing to visualize.
    return None


def _relative_file_path(
    tool_input: dict,
    project_root: str | None,
) -> str | None:
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None
    return _make_relative(file_path, project_root)


def _read_path(tool_input: dict, project_root: str | None) -> str | None:
    """The file a Read touched, relative to the root, or ``None`` if it is outside it.

    Deliberately NOT :func:`_make_relative`, which hands an absolute path that is
    not under the root straight back, unchanged. That is survivable for a write:
    it names a real change, and the watcher corrects the picture moments later.
    A read has no such correction, because nothing happened on disk -- and agents
    read ``/etc``, ``~/.claude``, ``node_modules`` and other checkouts all day, so
    the same leniency would hang permanent junk nodes off the top of the tree.

    Hence: a path is accepted only when it lies strictly *under* the root, on a
    path boundary -- comparing the raw prefix would file
    ``/home/x/project-other/a.py`` inside ``/home/x/project``. The root itself is
    refused too: the tree has no node for its own root.

    A prefix test alone is not enough, because ``..`` walks back out through it:
    ``<root>/../other/a.py`` starts with the root and still leaves it, and a
    relative ``../other/a.py`` or ``src/../../other/a.py`` never had a prefix to
    test. So the path is collapsed with :func:`os.path.normpath` FIRST and the
    boundary is checked on the result -- for a relative path that check *is* the
    leading ``..`` the collapse leaves behind. It is purely lexical on purpose:
    this function is pure and hot, and it must not touch the disk. Symlinks therefore still get through; resolving them is
    ``resolve_inside``'s job in the daemon, where a ``stat`` is affordable and
    the path arrives from the network.

    The collapsed form is what gets returned, so ``src/../a.py`` and ``a.py``
    light up ONE node instead of two spellings of the same file. Note ``..`` is
    only an escape as a whole segment: ``..hidden.py`` and ``a..b.py`` are
    ordinary names and pass through untouched.
    """
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path.strip():
        return None

    raw = file_path.strip()
    root = os.path.normpath(project_root) if project_root else ""

    if raw.startswith("/"):
        if not root.startswith("/"):
            return None
        absolute = os.path.normpath(raw)
        if not absolute.startswith(root.rstrip("/") + "/"):
            return None
        return absolute[len(root.rstrip("/")) + 1:] or None

    # Relative: no root to join it to (there may not even be one), but none is
    # needed -- a relative path escapes exactly when collapsing it leaves a
    # leading `..`, so `src/../../other/a.py` is caught as `../other/a.py`.
    relative = os.path.normpath(raw)
    if relative == os.pardir or relative.startswith(os.pardir + "/"):
        return None
    return None if relative == os.curdir else relative


def _parse_bash(
    tool_input: dict,
    project_root: str | None,
) -> tuple[str, str] | None:
    """Parse a shell command into a single filesystem operation.

    Only the primary, *unambiguous* change to the tree is reported:
      * ``rm`` / ``rmdir``     -> ``D`` of the first target
      * ``mkdir`` / ``touch``  -> ``A`` of the first target
      * ``cp``                 -> ``A`` of the destination (last argument)
      * ``mv``                 -> ``D`` of the origin (first argument)

    Anything the command does not pin to one concrete path yields ``None``: a
    glob (``cp *.md docs/``) names files this function cannot enumerate, and a
    directory destination is not a file at all. Guessing there used to put a
    phantom node on screen that never went away. The filesystem watcher reports
    what those commands actually did, so silence here costs nothing.
    """
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return None

    try:
        tokens = shlex.split(command)
    except ValueError:
        # Unbalanced quotes: an unparseable command, not a filesystem change.
        return None
    if not tokens:
        return None

    program = tokens[0]
    operands = [tok for tok in tokens[1:] if not tok.startswith("-")]
    if not operands or any(_has_glob(operand) for operand in operands):
        return None

    if program in _DELETE_COMMANDS:
        return _OP_DELETED, _clean(operands[0], project_root)
    if program in _ADD_COMMANDS:
        return _OP_ADDED, _clean(operands[0], project_root)
    if program == "cp":
        if len(operands) != 2 or _is_directory_target(operands[-1]):
            return None
        return _OP_ADDED, _clean(operands[-1], project_root)
    if program == "mv":
        if len(operands) != 2 or _is_directory_target(operands[-1]):
            # `mv a.md docs/` keeps the file under a new name we cannot build
            # here; the watcher reports both ends of the move instead.
            return None
        return _OP_DELETED, _clean(operands[0], project_root)

    return None


def _has_glob(operand: str) -> bool:
    """Whether the shell would expand this operand into an unknown set."""
    return any(char in operand for char in "*?[")


def _is_directory_target(operand: str) -> bool:
    """A trailing slash is the one unambiguous 'this is a directory' marker."""
    return operand.endswith("/")


def _clean(path: str, project_root: str | None) -> str:
    """Relativize `path` and drop a trailing slash.

    ``rm -rf build/`` and ``rm -rf build`` must name the same node, or the graph
    grows two entries for one directory.
    """
    relative = _make_relative(path, project_root)
    return relative.rstrip("/") or relative


def _make_relative(path: str, project_root: str | None) -> str:
    """Return ``path`` relative to ``project_root`` when it is absolute and under it.

    Relative inputs are returned unchanged (minus a leading ``./``). Absolute
    paths outside the root are also returned unchanged, so nothing is silently
    misfiled under the tree.
    """
    normalized = path.strip()
    if project_root and normalized.startswith(project_root + "/"):
        return normalized[len(project_root) + 1:]
    if normalized.startswith("./"):
        return normalized[2:]
    return normalized


def _timestamp(hook_json: dict) -> float:
    """Prefer a timestamp carried by the payload, else fall back to now."""
    raw = hook_json.get("timestamp")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    return time.time()
