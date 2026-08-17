"""What a `.claude/settings.json` says about attribution, and how to fix it.

Attribution is the point of this program -- the graph shows *which agent* did
what -- and it exists only if the `PostToolUse` block reaches the observed
project. Both ways of not having it are invisible from the page:

  * **Absent.** The tree updates while nobody is on camera, which is
    indistinguishable from "no agent is working right now".
  * **Stale.** Worse, and measured rather than imagined: a command naming a
    directory that stopped existing at a rename fails before the hook's own
    "exit 0 and stay silent" rule can run, so Claude Code reports a blocking
    hook error on *every* tool call. That is what :data:`STALE` is for, and it
    is why :func:`diagnose` is the centre of this module while the merge is the
    sideshow.

**`rhi` diagnoses always, offers explicitly, and never writes silently.**
`.claude/settings.json` is a committed file in many repositories -- it is
committed in this one -- so editing it as a side effect of "show me a graph"
would put a change in somebody's working tree unasked; and a project may already
carry a formatter's or a linter's `PostToolUse` hook, which a silent merge is
how one loses. Hence five states rather than four: :data:`FOREIGN` is *contest,
not content* (somebody else's hooks are there, so a person is told before we
touch the file), and :data:`MALFORMED` earns its place because its remedy
differs -- reported as absent, an unparseable file would be handed to
`--install-hooks` and replaced.

**Pure, and pinned as pure.** No filesystem, no environment, no daemon imports.
Whether a command resolves is injected as `command_exists`, which takes the
whole command string: the caller owns `shlex`, `$PATH` and the disk, and this
module owns the JSON shape.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

#: Nothing claims `PostToolUse` at all, so a merge has nothing to lose.
ABSENT = "absent"

#: Our hook is there and the command it names cannot be executed. The loud
#: failure: it errors on every tool call, however healthy anything beside it is.
STALE = "stale"

#: Somebody else's `PostToolUse` hooks are there and ours is not.
FOREIGN = "foreign"

#: Our hook is there and it resolves. The healthy answer.
INSTALLED = "installed"

#: The file is not JSON, or is JSON that is not a settings object. Never write
#: into one of these: nobody here can reconstruct what was in it.
MALFORMED = "malformed"

#: Worst-first, which is the order a verdict over several files is decided in.
#: `stale` above `installed` because hooks from every settings file run, so one
#: command that cannot be executed errors whatever the others say; `installed`
#: above `malformed` because a working hook is a working hook; `absent` last,
#: because it is the absence of information.
STATE_PRECEDENCE = (STALE, INSTALLED, MALFORMED, FOREIGN, ABSENT)

#: The event whose array carries the capture hooks.
POST_TOOL_USE = "PostToolUse"

#: The tools whose calls must reach the hook. The first four carry authorship
#: for changes; `Read` is what puts a violet ring on the file an agent opened,
#: and it is hook-only by nature -- the watcher cannot see a file being read.
CAPTURED_TOOLS = ("Write", "Edit", "MultiEdit", "Bash", "Read")

#: The matcher those tools spell, which is what a `PostToolUse` entry carries.
HOOK_MATCHER = "|".join(CAPTURED_TOOLS)

#: How this project's hook is spelled, however it was installed: the adapter
#: script named by hand from `config/settings.json`, or the console script an
#: installed package owns. Recognition is by the hook's own name and not by the
#: directory it sits in -- an install made from a second checkout is still ours,
#: and calling it a stranger's would duplicate it on the next install.
HOOK_NAMES = ("emit_event.py", "rhi-hook")


@dataclass(frozen=True)
class Diagnosis:
    """What one settings file says, and the commands it says it with.

    The commands travel with the state because the rot this module exists for is
    a *path* problem: "there is a hook" is half an answer, and which one is the
    other half. They are ours alone -- what a stranger's formatter runs is none
    of this program's business, and is never asked about on disk either.
    """

    state: str
    commands: tuple[str, ...] = ()


def diagnose(
    settings_text: str,
    expected_command: str,
    command_exists: Callable[[str], bool],
) -> Diagnosis:
    """What `settings_text` says about our hook, given a way to resolve commands.

    Total: a `.claude/settings.json` is hand-edited by people and by other
    tools, and this is the function pointed at the broken one, so nothing here
    raises whatever shape arrives.
    """
    if not settings_text.strip():
        # A settings file that is not there is read as the empty string by the
        # caller: nothing was written, so nothing is contested. Inventing a
        # sixth state for the commonest case of all would help nobody.
        return Diagnosis(ABSENT)
    settings = parse_settings(settings_text)
    if settings is None:
        return Diagnosis(MALFORMED)

    ours: list[str] = []
    strangers = 0
    for command in _post_tool_use_commands(settings):
        if _is_ours(command, expected_command):
            ours.append(command)
        else:
            strangers += 1

    if ours:
        # One broken command beside a working one is still broken: Claude Code
        # runs every matching hook, so the loud one decides.
        broken = any(not command_exists(command) for command in ours)
        return Diagnosis(STALE if broken else INSTALLED, tuple(ours))
    if strangers:
        return Diagnosis(FOREIGN)
    return Diagnosis(ABSENT)


def parse_settings(settings_text: str) -> dict | None:
    """The settings object `settings_text` spells, or ``None`` if it spells none.

    Parseable is not the same as usable: an array, a string or a number is JSON
    that no merge can be performed on, so it is refused here rather than
    somewhere further in.
    """
    try:
        parsed = json.loads(settings_text)
    except Exception:  # noqa: BLE001 - an unreadable file is an answer, not a crash
        return None
    return parsed if isinstance(parsed, dict) else None


def overall_state(states: Iterable[str]) -> str:
    """One verdict over the several settings files Claude Code merges.

    A hook in the user-level `~/.claude/settings.json` really does fire for a
    session in this project, so a doctor that looked at the project alone would
    report broken attribution to somebody whose setup works -- a false alarm,
    which is worse than no diagnostic because it teaches the reader to ignore
    the next one. The precedence is :data:`STATE_PRECEDENCE`, and it is a
    precedence rather than a fold so that which file was read first cannot be
    observed from the exit status.
    """
    seen = set(states)
    for state in STATE_PRECEDENCE:
        if state in seen:
            return state
    return ABSENT


def hook_block(command: str) -> dict:
    """The `hooks` block that runs `command` for every tool worth capturing.

    The command is a parameter and never a literal: where the hook lives is
    resolved on the machine doing the installing.
    """
    return {
        POST_TOOL_USE: [
            {
                "matcher": HOOK_MATCHER,
                "hooks": [{"type": "command", "command": command}],
            }
        ]
    }


def merge_hook_block(settings: Mapping, block: Mapping) -> dict:
    """`settings` with `block`'s hooks in it, and everything else untouched.

    Pure and idempotent. Idempotent because somebody unsure whether they already
    installed will install again -- that is what unsure means -- and two
    identical blocks fire the hook twice per tool call, so every change flashes
    twice on the graph with no error anywhere. Our own entries are therefore
    replaced rather than added to, which is also the fix for :data:`STALE`.

    A stranger's entry survives byte for byte: it is the whole reason this is a
    merge and not a write.
    """
    merged = copy.deepcopy(dict(settings))
    hooks = merged.get("hooks")
    hooks = copy.deepcopy(hooks) if isinstance(hooks, dict) else {}
    for event, entries in block.items():
        present = hooks.get(event)
        kept = (
            [entry for entry in present if not _entry_is_ours(entry)]
            if isinstance(present, list)
            else []
        )
        hooks[event] = kept + copy.deepcopy(list(entries))
    merged["hooks"] = hooks
    return merged


def _post_tool_use_commands(settings: Mapping) -> list[str]:
    """Every command string under `PostToolUse`, in file order.

    Defensive at every level: each of these keys is hand-edited, so a value of
    the wrong shape is skipped rather than trusted.
    """
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return []
    entries = hooks.get(POST_TOOL_USE)
    if not isinstance(entries, list):
        return []
    return [command for entry in entries for command in _entry_commands(entry)]


def _entry_commands(entry: object) -> list[str]:
    """The command strings one `PostToolUse` entry runs."""
    if not isinstance(entry, dict):
        return []
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return []
    found: list[str] = []
    for hook in hooks:
        if not isinstance(hook, dict):
            continue
        command = hook.get("command")
        if isinstance(command, str) and command.strip():
            found.append(command)
    return found


def _entry_is_ours(entry: object) -> bool:
    """Does this entry run our hook? The question a merge replaces on."""
    return any(_is_ours(command, "") for command in _entry_commands(entry))


def _is_ours(command: str, expected_command: str) -> bool:
    """Is `command` this project's hook, however it happens to be spelled?

    By the hook's own name, so a `RHIZOME_TRACE_LOG=...` prefix (the documented
    way to debug it) and an install made from another checkout both read as
    ours. The command we would write ourselves counts too, so the writer and the
    reader cannot drift into installing a second copy beside the first.
    """
    if expected_command and command.strip() == expected_command.strip():
        return True
    return any(_program_name(token) in HOOK_NAMES for token in command.split())


def _program_name(token: str) -> str:
    """The last segment of a path-like token, with no `os.path` in sight."""
    return token.rsplit("/", 1)[-1].strip("'\"")
