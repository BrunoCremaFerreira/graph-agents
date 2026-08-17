"""Contract test (RED) for the capture hooks shipped in config/settings.json.

Motivation: this file is the one place where forgetting a single word makes a
whole feature silently do nothing. The `PostToolUse` matcher decides which tool
calls Claude Code even runs the hook for; a tool missing from it produces no
error, no log line and no event -- the daemon keeps running, the watcher keeps
reporting filesystem changes, and the graph looks exactly like a healthy setup
in which nobody happens to be doing that thing. For `R` (read) it is worse than
for the others: a read changes nothing on disk, so the watcher cannot fill the
gap the way it does for a glob-expanding `cp`. No matcher entry means no read
events, ever, and nothing on screen says so.

What is asserted is coverage, not the literal string: the matcher is a regular
alternation, so reordering it or adding a sixth tool is not a regression and
must not fail this. The union is taken over every `PostToolUse` entry that runs
`emit_event.py`, so splitting the block in two stays legal too.

Second defect, second half of this file: the command in `.claude/settings.json`
named a script under a directory that stopped existing when the project was
renamed. Claude Code does not fail a hook the way this codebase does -- the
adapter's own rule is to exit 0 and stay silent, but a command whose *file* is
missing never reaches that rule, so the interpreter's error surfaced as a
blocking hook error on every single tool call. The matcher assertion above
could not see it: the command still contained `emit_event.py`, which was the
whole of what was checked. Only `.claude/` is checked for existence;
`config/settings.json` is a template a user copies and edits, and its path is
deliberately a placeholder.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SETTINGS = REPO_ROOT / "config" / "settings.json"

#: This repository's own installed copy: the hooks that attribute the work done
#: in this checkout, and the file the rename broke.
INSTALLED_SETTINGS = REPO_ROOT / ".claude" / "settings.json"

HOOK_SCRIPT = "emit_event.py"

#: Every tool whose calls must reach the hook. The first four carry authorship
#: for changes; `Read` is what puts a violet flash on the file an agent opened.
REQUIRED_TOOLS = {"Write", "Edit", "MultiEdit", "Bash", "Read"}


def _capture_commands(settings_file: Path) -> list[str]:
    """Every PostToolUse command that runs our hook, in file order."""
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    return [
        command
        for entry in settings.get("hooks", {}).get("PostToolUse", [])
        for command in (str(hook.get("command", "")) for hook in entry.get("hooks", []))
        if HOOK_SCRIPT in command
    ]


def _script_paths(command: str) -> list[str]:
    """The script arguments of `python3 <path>`, as the shell would split them.

    `shlex` rather than `split()`, because the documented way to debug the hook
    is to prefix the command with `RHIZOME_TRACE_LOG=...`, and a log path may be
    quoted. Anything ending in `.py` is a script this command will try to run.
    """
    return [token for token in shlex.split(command) if token.endswith(".py")]


def _capture_matchers() -> set[str]:
    """Every tool name matched by a PostToolUse entry that runs our hook."""
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    covered: set[str] = set()
    for entry in settings.get("hooks", {}).get("PostToolUse", []):
        commands = " ".join(
            str(hook.get("command", "")) for hook in entry.get("hooks", [])
        )
        if HOOK_SCRIPT not in commands:
            continue
        matcher = str(entry.get("matcher", ""))
        covered |= {part.strip() for part in matcher.split("|") if part.strip()}
    return covered


def test_the_capture_hook_fires_for_reads_as_well_as_for_changes():
    covered = _capture_matchers()

    assert REQUIRED_TOOLS <= covered, (
        "config/settings.json is what a user copies into the observed project's "
        f".claude/settings.json; a tool missing from the matcher is captured "
        f"nowhere and reports nothing. Missing: {sorted(REQUIRED_TOOLS - covered)}"
    )


def test_the_installed_hook_command_names_a_script_that_exists() -> None:
    """A command pointing at a deleted directory errors on every tool call.

    This is the rename's actual casualty: the path still ended in
    `emit_event.py`, so it satisfied every check there was, while naming a
    directory that no longer existed. The interpreter fails before the hook's
    own defensive wrapper can exit 0, so the silence this codebase promises for
    a broken adapter is replaced by a blocking error on every tool call.
    """
    commands = _capture_commands(INSTALLED_SETTINGS)
    assert commands, f"{INSTALLED_SETTINGS} runs no capture hook at all"

    missing = [
        f"{command!r} -> {script}"
        for command in commands
        for script in _script_paths(command)
        if not Path(script).is_file()
    ]

    assert missing == [], (
        "the installed hook command names a script that is not on disk; "
        "Claude Code reports this as a blocking hook error on every tool "
        "call:\n" + "\n".join(missing)
    )
