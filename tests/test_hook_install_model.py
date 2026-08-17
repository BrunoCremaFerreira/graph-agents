"""Contract tests (RED) for the pure model behind hook installation and doctoring.

Motivation: the whole point of this project is **attribution** -- the graph shows
*which agent* did what -- and attribution exists only if the `hooks` block from
`config/settings.json` is inside the OBSERVED project's `.claude/settings.json`.
Today that is a manual copy-paste, and CLAUDE.md records what its absence looks
like: the tree updates while nobody is on camera, which is indistinguishable from
"no agent is working right now". That ambiguity cost real hours.

**`rhi` diagnoses always, offers explicitly, and never writes silently.** Three
reasons, and they are why this is a diagnosis module rather than a "fix it for
me" one:

  * `.claude/settings.json` is a **committed file in many repositories** -- it is
    committed in this one. A tool that edits it as a side effect of "show me a
    graph" writes into the user's git working tree unasked, and the user finds
    out from `git status` afterwards.
  * **A merge can clobber.** The observed project may already hold `PostToolUse`
    hooks with a different matcher -- a formatter, a linter, somebody's audit
    log. Merging JSON hook arrays silently is how one of those is lost.
  * **The real failure mode is rot, not absence.** Live evidence, from this very
    session: all three settings files in this repository named a hook under a
    directory that stopped existing when the project was renamed, and *every
    tool call* came back with a blocking hook error. CLAUDE.md documents a
    missing hook as producing **silence**; a stale absolute path fails
    differently and worse -- loud, attached to every call, degrading the agent
    session rather than the graph. Writing a file is a one-time act; checking
    that what is written still resolves has ongoing value, which is why
    `diagnose` is the centre of this module and the merge is the sideshow.

That last point is what `stale` is for, and the input it is specified with below
is the exact broken command that was in this repository.

**Five states, not four.** `absent`, `stale`, `foreign`, `installed` -- and
`malformed`, for a `.claude/settings.json` that is not JSON at all. It earns its
place because the remedy differs: absent is written into, malformed must never
be, and a reader told only "no hook found" would reach for `--install-hooks` and
have their unparseable-but-precious file replaced. The line between `absent` and
`foreign` is drawn at **contest, not content**: absent means nothing claims
`PostToolUse` at all, so a merge has nothing to lose; foreign means somebody
else's hooks are already there, so a person should be told before we touch it.
Neither is a refusal -- `merge_hook_block` keeps the stranger's entry intact --
they are different things to say.

**One file at a time, and one verdict over several.** `diagnose` answers about a
single `settings.json`, because that is what a state means; Claude Code merges
hooks from the user-level `~/.claude/settings.json` and the project's own, so
`rhi --doctor` reads both and combines the two answers through `overall_state`.
That combination is pure logic over five constants and belongs beside them,
where it can be specified without a subprocess -- see section 7b for the
precedence and the reason behind each step of it.

**Pure, and pinned as pure.** No filesystem, no environment, no daemon imports.
`command_exists` is injected for exactly that reason: `stale` is the state that
matters most and it is a question about the disk, so the disk is a parameter and
the tests need none. The caller owns `$PATH`, `shlex` and `os.path`; this module
owns the JSON shape.

**What is NOT specified here:** where the hook command actually is on this
machine (`rhizome_graph.assets`, specified in `tests/test_hook_dependencies.py`),
and what `rhi` does with any of it (`tests/test_hook_doctor.py`,
`tests/test_hook_install_command.py`). One cross-module round trip lives at the
bottom, because "the command we write is a command we then recognise" is a
property neither module can state alone.

The module under specification is imported per test rather than at file level:
while it does not exist, a top-level import fails at *collection* and takes the
whole file's report with it.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import ast
import copy
import importlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

MODULE_SOURCE = REPO_ROOT / "rhizome_graph" / "hookinstall.py"

#: The template a user copies from, and the file `tests/test_capture_settings.py`
#: already pins the matcher of. Read here to build on that rather than to
#: restate it: what the installer writes must cover what the template covers.
CONFIG_SETTINGS = REPO_ROOT / "config" / "settings.json"

#: The exact command this repository's three settings files carried after the
#: rename, and the reason `stale` exists. `graph-agents` has not been the name of
#: this project for some time; the path resolves to nothing, the interpreter
#: fails before the hook's own "exit 0 and stay silent" rule can run, and Claude
#: Code reports a blocking hook error on every single tool call.
ROTTED_COMMAND = "python3 /home/brn/projects/graph-agents/hooks/emit_event.py"

#: The same command, spelled at a path that does exist. Anything ending in
#: `emit_event.py` is ours however it is spelled, or an install made by hand from
#: `config/settings.json` -- which is how every install has been made until now --
#: would read as somebody else's hook.
WORKING_COMMAND = f"python3 {REPO_ROOT}/hooks/emit_event.py"

#: Somebody else's `PostToolUse` hook. Nothing about it is ours, and in
#: particular nothing here may ever ask the disk about it.
FOREIGN_COMMAND = "prettier --write $CLAUDE_FILE_PATHS"

FOREIGN_ENTRY = {
    "matcher": "Write|Edit",
    "hooks": [{"type": "command", "command": FOREIGN_COMMAND}],
}


def hookinstall():
    """The module under specification -- see the note in the file docstring."""
    return importlib.import_module("rhizome_graph.hookinstall")


def module_tree() -> ast.Module:
    assert MODULE_SOURCE.exists(), f"there is no {MODULE_SOURCE}"
    return ast.parse(MODULE_SOURCE.read_text(encoding="utf-8"))


def imported_modules(tree: ast.Module) -> set[str]:
    """Every module named by an import anywhere in the file, at any depth."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return names


def exists_only(*commands: str) -> Callable[[str], bool]:
    """A `command_exists` that recognises exactly the commands it was given."""
    known = set(commands)
    return lambda command: command in known


def settings_with(*entries: dict) -> str:
    """A `.claude/settings.json` text holding `entries` under `PostToolUse`."""
    return json.dumps({"hooks": {"PostToolUse": list(entries)}})


def ours(command: str) -> dict:
    """A `PostToolUse` entry running `command`, spelled the way ours is."""
    return {
        "matcher": "Write|Edit|MultiEdit|Bash|Read",
        "hooks": [{"type": "command", "command": command}],
    }


def our_entries(settings: dict, command_marker: str = "emit_event.py") -> list[dict]:
    """Every `PostToolUse` entry in `settings` that runs this project's hook."""
    return [
        entry
        for entry in settings.get("hooks", {}).get("PostToolUse", [])
        if any(
            command_marker in str(hook.get("command", ""))
            or "rhi-hook" in str(hook.get("command", ""))
            for hook in entry.get("hooks", [])
        )
    ]


# ===========================================================================
# 1. the module is pure, and stays pure
# ===========================================================================


#: Vocabulary belonging to the disk and to the process's surroundings. A module
#: whose whole contract is "given this text, what state is it in" needs none of
#: it, and `command_exists` is injected precisely so the one disk question this
#: subject has is somebody else's.
IMPURE_VOCABULARY = {
    "open",
    "environ",
    "getenv",
    "read_text",
    "write_text",
    "exists",
    "is_file",
    "mkdir",
    "unlink",
    "listdir",
    "which",
    "run",
    "Popen",
}


def test_the_hook_install_model_never_touches_the_disk_or_the_environment() -> None:
    """Purity, asserted structurally rather than left as a docstring promise.

    The behavioural tests below prove it is pure *today* -- they hand it text and
    a callable and nothing else -- but nothing in them stops the next reader from
    adding one `Path(...).exists()` to a helper they do not call.
    """
    found: set[str] = set()
    for node in ast.walk(module_tree()):
        if isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.Name):
            found.add(node.id)

    offenders = sorted(found & IMPURE_VOCABULARY)

    assert offenders == [], (
        f"rhizome_graph/hookinstall.py reaches for {offenders}. Whether a "
        "command resolves is injected as `command_exists` so that `stale` -- the "
        "state that matters most -- can be specified with no disk at all."
    )


def test_the_hook_install_model_imports_nothing_from_the_daemon_side() -> None:
    """It answers a question about a file long before any server exists."""
    forbidden = {"daemon", "asyncio", "websockets", "watchdog"}

    offenders = sorted(
        name for name in imported_modules(module_tree()) if name.split(".")[0] in forbidden
    )

    assert offenders == [], (
        f"rhizome_graph/hookinstall.py imports {offenders}; `rhi --doctor` "
        "starts nothing and must not pay for an event loop to say so"
    )


# ===========================================================================
# 2. diagnose -- absent
# ===========================================================================


def test_a_project_with_no_settings_at_all_is_absent() -> None:
    """The commonest case by far: no `.claude/settings.json` was ever written.

    The caller reads a missing file as the empty string rather than inventing a
    sixth state for it; nothing was there, and nothing is contested.
    """
    answer = hookinstall().diagnose("", WORKING_COMMAND, exists_only(WORKING_COMMAND))

    assert answer.state == hookinstall().ABSENT


def test_an_empty_settings_object_is_absent() -> None:
    """A file somebody created and never filled in is still an empty field."""
    answer = hookinstall().diagnose("{}", WORKING_COMMAND, exists_only(WORKING_COMMAND))

    assert answer.state == hookinstall().ABSENT


def test_settings_that_configure_other_things_are_absent() -> None:
    """No `PostToolUse` means nothing is contested, whatever else is in there."""
    text = json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}, "model": "opus"})

    answer = hookinstall().diagnose(text, WORKING_COMMAND, exists_only(WORKING_COMMAND))

    assert answer.state == hookinstall().ABSENT


def test_hooks_for_other_events_alone_are_absent() -> None:
    """`PreToolUse` and `Stop` are not the array a merge would touch."""
    text = json.dumps(
        {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}}
    )

    answer = hookinstall().diagnose(text, WORKING_COMMAND, exists_only(WORKING_COMMAND))

    assert answer.state == hookinstall().ABSENT


# ===========================================================================
# 3. diagnose -- malformed
# ===========================================================================


def test_a_settings_file_that_is_not_json_is_malformed() -> None:
    """Its own answer, because its own remedy: never write into this.

    Reported as "absent" it would be handed to `--install-hooks`, which would
    replace a file whose contents nobody can reconstruct.
    """
    answer = hookinstall().diagnose(
        '{"hooks": {,,,', WORKING_COMMAND, exists_only(WORKING_COMMAND)
    )

    assert answer.state == hookinstall().MALFORMED


@pytest.mark.parametrize("text", ['["a", "b"]', '"hooks"', "42", "null"], ids=[
    "array", "string", "number", "null"
])
def test_json_that_is_not_a_settings_object_is_malformed(text: str) -> None:
    """Parseable is not the same as usable, and a merge needs a mapping."""
    answer = hookinstall().diagnose(text, WORKING_COMMAND, exists_only(WORKING_COMMAND))

    assert answer.state == hookinstall().MALFORMED


# ===========================================================================
# 4. diagnose -- foreign
# ===========================================================================


def test_somebody_elses_post_tool_use_hook_is_foreign() -> None:
    """The state that exists so a merge is announced rather than performed.

    Contest, not content: there is nothing wrong with this file, but there is
    something in it that a careless write could lose.
    """
    answer = hookinstall().diagnose(
        settings_with(FOREIGN_ENTRY), WORKING_COMMAND, exists_only(WORKING_COMMAND)
    )

    assert answer.state == hookinstall().FOREIGN


def test_a_foreign_hook_is_never_asked_about_on_disk() -> None:
    """Whether `prettier` is installed is none of this program's business.

    A diagnosis that stats every command it finds turns a report about our own
    hook into an audit of somebody else's toolchain, and would report their
    broken formatter as our problem.
    """
    asked: list[str] = []

    def command_exists(command: str) -> bool:
        asked.append(command)
        return True

    hookinstall().diagnose(settings_with(FOREIGN_ENTRY), WORKING_COMMAND, command_exists)

    assert asked == [], f"asked the disk about a hook that is not ours: {asked}"


# ===========================================================================
# 5. diagnose -- installed
# ===========================================================================


def test_our_hook_that_resolves_is_installed() -> None:
    """The healthy answer, and the one `rhi --doctor` exits zero on."""
    answer = hookinstall().diagnose(
        settings_with(ours(WORKING_COMMAND)),
        WORKING_COMMAND,
        exists_only(WORKING_COMMAND),
    )

    assert answer.state == hookinstall().INSTALLED


def test_an_installed_diagnosis_carries_the_command_it_found() -> None:
    """So a report can print the hook that is actually wired up.

    The rot this module exists for is a *path* problem, so "there is a hook" is
    half an answer; which one is the other half.
    """
    answer = hookinstall().diagnose(
        settings_with(ours(WORKING_COMMAND)),
        WORKING_COMMAND,
        exists_only(WORKING_COMMAND),
    )

    assert answer.commands == (WORKING_COMMAND,)


def test_our_hook_beside_somebody_elses_is_still_installed() -> None:
    """A project with a formatter hook and our hook has our hook."""
    answer = hookinstall().diagnose(
        settings_with(FOREIGN_ENTRY, ours(WORKING_COMMAND)),
        WORKING_COMMAND,
        exists_only(WORKING_COMMAND),
    )

    assert answer.state == hookinstall().INSTALLED


def test_a_debug_prefixed_command_is_still_ours() -> None:
    """The documented way to debug the hook must not read as a stranger's.

    CLAUDE.md tells the user to prefix `RHIZOME_TRACE_LOG=...` on the command;
    a recogniser that only matched the bare spelling would call that foreign and
    then offer to install a second copy beside it.
    """
    command = f"RHIZOME_TRACE_LOG=/tmp/trace.log {WORKING_COMMAND}"

    answer = hookinstall().diagnose(
        settings_with(ours(command)), WORKING_COMMAND, exists_only(command)
    )

    assert answer.state == hookinstall().INSTALLED


def test_a_hook_installed_from_another_checkout_is_still_ours() -> None:
    """Recognition is by the hook's own name, not by the path it sits at.

    Somebody who installed from a second clone, or from a different working
    copy, has our hook -- and if it resolves, it works. Calling it foreign would
    duplicate it on the next install.
    """
    command = "python3 /srv/checkouts/rhizome-graph/hooks/emit_event.py"

    answer = hookinstall().diagnose(
        settings_with(ours(command)), WORKING_COMMAND, exists_only(command)
    )

    assert answer.state == hookinstall().INSTALLED


def test_the_console_script_form_is_ours_too() -> None:
    """What an installed `rhi-hook` looks like in somebody else's settings."""
    command = "/home/someone/.local/bin/rhi-hook"

    answer = hookinstall().diagnose(
        settings_with(ours(command)), command, exists_only(command)
    )

    assert answer.state == hookinstall().INSTALLED


# ===========================================================================
# 6. diagnose -- stale, which is what this module is really for
# ===========================================================================


def test_a_hook_whose_script_no_longer_exists_is_stale() -> None:
    """The measured defect, with the measured input.

    This exact string was in all three settings files in this repository after
    the rename, and it produced a blocking hook error on every tool call --
    louder and worse than the silence a missing hook produces.
    """
    answer = hookinstall().diagnose(
        settings_with(ours(ROTTED_COMMAND)), WORKING_COMMAND, exists_only(WORKING_COMMAND)
    )

    assert answer.state == hookinstall().STALE


def test_a_stale_diagnosis_carries_the_command_that_no_longer_resolves() -> None:
    """A report that says "stale" without the path is a report of a feeling."""
    answer = hookinstall().diagnose(
        settings_with(ours(ROTTED_COMMAND)), WORKING_COMMAND, exists_only(WORKING_COMMAND)
    )

    assert answer.commands == (ROTTED_COMMAND,)


def test_one_broken_hook_beside_a_working_one_is_stale() -> None:
    """Stale is the worse answer and it wins, because it is the loud one.

    Claude Code runs every matching hook, so one command that cannot be executed
    errors on every tool call no matter how healthy the one beside it is.
    """
    answer = hookinstall().diagnose(
        settings_with(ours(WORKING_COMMAND), ours(ROTTED_COMMAND)),
        WORKING_COMMAND,
        exists_only(WORKING_COMMAND),
    )

    assert answer.state == hookinstall().STALE


def test_a_console_script_that_was_uninstalled_is_stale() -> None:
    """`pip uninstall` while another project's settings still name `rhi-hook`."""
    command = "/home/someone/.local/bin/rhi-hook"

    answer = hookinstall().diagnose(
        settings_with(ours(command)), WORKING_COMMAND, exists_only(WORKING_COMMAND)
    )

    assert answer.state == hookinstall().STALE


# ===========================================================================
# 7. diagnose -- total over garbage, like everything else on this path
# ===========================================================================


@pytest.mark.parametrize(
    "settings",
    [
        {"hooks": "PostToolUse"},
        {"hooks": {"PostToolUse": "Write"}},
        {"hooks": {"PostToolUse": [None, 3, "x"]}},
        {"hooks": {"PostToolUse": [{"hooks": "no"}]}},
        {"hooks": {"PostToolUse": [{"hooks": [None]}]}},
        {"hooks": {"PostToolUse": [{"hooks": [{"command": None}]}]}},
        {"hooks": None},
    ],
    ids=["hooks-str", "array-str", "junk-entries", "hooks-str-2", "null-hook",
         "null-command", "null-hooks"],
)
def test_a_settings_file_of_the_wrong_shape_is_answered_not_raised(
    settings: dict,
) -> None:
    """Nothing on this path may raise -- the same rule the hook itself obeys.

    A `.claude/settings.json` is hand-edited by people and by other tools, and
    `rhi --doctor` exists to be run on the broken one. Crashing on the file it
    was pointed at is the one thing it may not do.
    """
    answer = hookinstall().diagnose(
        json.dumps(settings), WORKING_COMMAND, exists_only(WORKING_COMMAND)
    )

    assert answer.state in {
        hookinstall().ABSENT,
        hookinstall().FOREIGN,
        hookinstall().MALFORMED,
    }, answer


# ===========================================================================
# 7b. two files, one verdict
# ===========================================================================
#
# Claude Code merges hooks from the user-level `~/.claude/settings.json` and the
# project's own `.claude/settings.json`, and a hook in either one fires for a
# session in that project. So `rhi --doctor` reads both and has to combine two
# diagnoses into one answer -- and that combination is pure logic about five
# constants, which belongs here rather than inside a command whose only test is
# a subprocess.
#
# The precedence, and why each step of it:
#
#   STALE > INSTALLED > MALFORMED > FOREIGN > ABSENT
#
#   * `stale` first, and above `installed`, because both files' hooks run: a
#     command that cannot be executed errors on every tool call however healthy
#     the other file is. It is the same reason one broken hook beside a working
#     one is stale *within* a file, above.
#   * `installed` above `malformed`, because a working hook is a working hook.
#     An unparseable user-level file is a real thing to mention and not a reason
#     to tell somebody their attribution is broken when it is not.
#   * `absent` last, because it is the absence of information: anything else
#     either file has to say is more useful than "nothing here".


def test_a_working_hook_in_either_file_is_the_verdict() -> None:
    """The overruled specification's actual defect, as a unit.

    A doctor that looked only at the project would report a broken setup to
    somebody whose hook is installed globally and works -- a false alarm, which
    is worse than no diagnostic because it teaches the user to ignore the next
    one.
    """
    module = hookinstall()

    assert module.overall_state([module.ABSENT, module.INSTALLED]) == module.INSTALLED


@pytest.mark.parametrize("other", ["ABSENT", "FOREIGN", "MALFORMED", "INSTALLED"])
def test_a_stale_hook_anywhere_is_the_verdict(other: str) -> None:
    """Stale wins from either file, because hooks from both files run.

    The user with a working project hook and a rotted global one gets a blocking
    error on every tool call, and a verdict of "installed" would send them
    looking anywhere but here.
    """
    module = hookinstall()

    assert module.overall_state([module.STALE, getattr(module, other)]) == module.STALE


def test_nothing_anywhere_is_absent() -> None:
    """The case the whole command exists for: attribution is simply not set up."""
    module = hookinstall()

    assert module.overall_state([module.ABSENT, module.ABSENT]) == module.ABSENT


def test_an_unreadable_file_is_the_verdict_when_nothing_works() -> None:
    """Malformed outranks absent: it is the one that needs a human, not a flag."""
    module = hookinstall()

    assert module.overall_state([module.ABSENT, module.MALFORMED]) == module.MALFORMED


def test_somebody_elses_hooks_outrank_an_empty_file() -> None:
    """`--install-hooks` has something to lose in one file and not the other."""
    module = hookinstall()

    assert module.overall_state([module.ABSENT, module.FOREIGN]) == module.FOREIGN


@pytest.mark.parametrize(
    "states",
    [("STALE", "INSTALLED"), ("MALFORMED", "INSTALLED"), ("FOREIGN", "ABSENT")],
    ids=["stale-installed", "malformed-installed", "foreign-absent"],
)
def test_the_verdict_does_not_depend_on_which_file_was_read_first(
    states: tuple[str, str],
) -> None:
    """A precedence with an order dependency in it is a coin toss.

    Which of the two files is read first is an implementation detail nobody
    should be able to observe from the exit status.
    """
    module = hookinstall()
    first, second = (getattr(module, name) for name in states)

    assert module.overall_state([first, second]) == module.overall_state([second, first])


def test_a_verdict_over_nothing_at_all_is_absent() -> None:
    """Total, like everything else on this path: no file readable, no crash."""
    module = hookinstall()

    assert module.overall_state([]) == module.ABSENT


# ===========================================================================
# 8. merge_hook_block -- pure, idempotent, and it never loses a stranger
# ===========================================================================


def test_merging_into_an_empty_settings_installs_the_block() -> None:
    """The base case, stated as behaviour rather than as JSON equality."""
    module = hookinstall()

    merged = module.merge_hook_block({}, module.hook_block(WORKING_COMMAND))

    assert len(our_entries(merged)) == 1


def test_merging_twice_is_the_same_as_merging_once() -> None:
    """Idempotence, the property that makes `--install-hooks` safe to re-run.

    Somebody who is not sure whether they already ran it will run it again --
    that is what "not sure" means -- and two identical hook blocks means every
    tool call fires the hook twice, so every change flashes twice on the graph.
    """
    module = hookinstall()
    block = module.hook_block(WORKING_COMMAND)

    once = module.merge_hook_block({}, block)
    twice = module.merge_hook_block(once, block)

    assert twice == once


def test_merging_over_a_rotted_command_replaces_it_rather_than_adding_to_it() -> None:
    """The upgrade path: the fix for `stale` is a merge, not a second entry.

    Leaving the broken one in place would leave the blocking error on every tool
    call exactly as it was, with a working hook beside it.
    """
    module = hookinstall()
    settings = {"hooks": {"PostToolUse": [ours(ROTTED_COMMAND)]}}

    merged = module.merge_hook_block(settings, module.hook_block(WORKING_COMMAND))

    assert len(our_entries(merged)) == 1
    assert ROTTED_COMMAND not in json.dumps(merged)


def test_an_unrelated_post_tool_use_hook_survives_the_merge() -> None:
    """The clobber this design exists to prevent, asserted on the victim.

    Byte for byte: not merely "prettier is mentioned somewhere", but the
    stranger's entry still present exactly as it was written.
    """
    module = hookinstall()
    settings = {"hooks": {"PostToolUse": [copy.deepcopy(FOREIGN_ENTRY)]}}

    merged = module.merge_hook_block(settings, module.hook_block(WORKING_COMMAND))

    assert FOREIGN_ENTRY in merged["hooks"]["PostToolUse"]


def test_hooks_for_other_events_survive_the_merge() -> None:
    """A `Stop` hook is not in the array being merged and must not notice one."""
    module = hookinstall()
    stop = {"hooks": [{"type": "command", "command": "notify-send done"}]}
    settings = {"hooks": {"Stop": [stop], "PostToolUse": []}}

    merged = module.merge_hook_block(settings, module.hook_block(WORKING_COMMAND))

    assert merged["hooks"]["Stop"] == [stop]


def test_settings_that_are_not_hooks_survive_the_merge() -> None:
    """`permissions`, `model`, `env` -- the rest of somebody's configuration."""
    module = hookinstall()
    settings = {"permissions": {"allow": ["Bash(ls:*)"]}, "model": "opus"}

    merged = module.merge_hook_block(settings, module.hook_block(WORKING_COMMAND))

    assert merged["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert merged["model"] == "opus"


def test_the_merge_does_not_mutate_what_it_was_given() -> None:
    """Pure by contract, and the caller holds the original to compare against.

    `--install-hooks` prints what it will change before writing it, which means
    holding both versions at once; a merge that edited its argument would leave
    the preview and the outcome the same object.
    """
    module = hookinstall()
    settings = {"hooks": {"PostToolUse": [copy.deepcopy(FOREIGN_ENTRY)]}}
    before = copy.deepcopy(settings)

    module.merge_hook_block(settings, module.hook_block(WORKING_COMMAND))

    assert settings == before


# ===========================================================================
# 9. the block that gets written covers what the template covers
# ===========================================================================


def _template_tools() -> set[str]:
    """Every tool `config/settings.json`'s matcher covers, as a set.

    Read from the file rather than restated: `tests/test_capture_settings.py`
    already pins what that matcher must contain, and duplicating the list here
    would give a future sixth tool two places to be forgotten instead of one.
    """
    settings = json.loads(CONFIG_SETTINGS.read_text(encoding="utf-8"))
    covered: set[str] = set()
    for entry in settings.get("hooks", {}).get("PostToolUse", []):
        commands = " ".join(str(hook.get("command", "")) for hook in entry.get("hooks", []))
        if "emit_event.py" not in commands:
            continue
        covered |= {
            part.strip() for part in str(entry.get("matcher", "")).split("|") if part.strip()
        }
    return covered


def test_the_installed_block_matches_every_tool_the_template_matches() -> None:
    """An installer that covers less than the copy-paste it replaces is a step
    backwards, and a silent one: a tool missing from the matcher reports
    nothing, forever, with no error anywhere."""
    module = hookinstall()

    written = {
        part.strip()
        for entry in module.hook_block(WORKING_COMMAND).get("PostToolUse", [])
        for part in str(entry.get("matcher", "")).split("|")
        if part.strip()
    }

    assert _template_tools() <= written, (
        "the block --install-hooks writes covers fewer tools than "
        f"config/settings.json does. Missing: {sorted(_template_tools() - written)}"
    )


def test_the_block_carries_the_command_it_was_given() -> None:
    """The whole reason `hook_block` takes a parameter: the path is resolved on
    the machine doing the installing, never baked in here."""
    module = hookinstall()

    assert WORKING_COMMAND in json.dumps(module.hook_block(WORKING_COMMAND))


# ===========================================================================
# 10. the round trip -- what we write is what we recognise
# ===========================================================================


def test_a_freshly_installed_block_diagnoses_as_installed() -> None:
    """The property neither module can state alone, and the one that rots first.

    `assets.hook_command()` decides how the command is spelled on this machine
    and `diagnose` decides what counts as ours. If those two ever drift, `rhi
    --install-hooks` writes a hook and `rhi --doctor` immediately reports it
    missing -- and, worse, a second `--install-hooks` adds a duplicate.
    """
    module = hookinstall()
    from rhizome_graph.assets import hook_command

    command = hook_command()
    written = module.merge_hook_block({}, module.hook_block(command))

    answer = module.diagnose(json.dumps(written), command, exists_only(command))

    assert answer.state == module.INSTALLED
