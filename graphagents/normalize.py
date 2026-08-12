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

import shlex
import time
from dataclasses import dataclass

# Operation types and their fixed Gource colors (hex, no leading '#').
_OP_ADDED = "A"
_OP_MODIFIED = "M"
_OP_DELETED = "D"

_COLOR_BY_TYPE = {
    _OP_ADDED: "33FF33",
    _OP_MODIFIED: "FFAA00",
    _OP_DELETED: "FF3333",
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
        agent: Actor id, derived from the hook's top-level ``session_id``.
        type: Operation kind, one of ``"A"`` (added), ``"M"`` (modified),
            ``"D"`` (deleted).
        path: Path relative to the observed project root.
        color: Hex color WITHOUT a leading ``#`` (A->33FF33, M->FFAA00,
            D->FF3333).
        origin: What produced the event -- ``"hook"`` (a Claude tool call),
            ``"seed"`` (the tree snapshot taken at boot) or ``"watch"`` (the
            filesystem watcher).
    """

    ts: float
    agent: str
    type: str
    path: str
    color: str
    origin: str = ORIGIN_HOOK


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

    agent = str(hook_json.get("session_id", ""))

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
) -> Event | None:
    """Build an event for a change the watcher observed, or ``None`` if invalid.

    `agent` is filled in by the daemon from the hook that fired around the same
    time; an empty string means the change could not be attributed (a manual
    edit, a build step) and the frontend draws it without an actor.
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

    if tool_name == "Bash":
        return _parse_bash(tool_input, project_root)

    # Read, Grep, Glob, WebFetch, ... -> nothing to visualize.
    return None


def _relative_file_path(
    tool_input: dict,
    project_root: str | None,
) -> str | None:
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None
    return _make_relative(file_path, project_root)


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
