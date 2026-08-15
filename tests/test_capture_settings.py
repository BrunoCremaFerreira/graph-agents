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

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SETTINGS = REPO_ROOT / "config" / "settings.json"

HOOK_SCRIPT = "emit_event.py"

#: Every tool whose calls must reach the hook. The first four carry authorship
#: for changes; `Read` is what puts a violet flash on the file an agent opened.
REQUIRED_TOOLS = {"Write", "Edit", "MultiEdit", "Bash", "Read"}


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
