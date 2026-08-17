"""Contract tests for the rule the hot path has always had and never had a test.

Motivation: CLAUDE.md's most repeated rule about the adapter is that it runs on
*every* tool call and blocks the agent loop, so it must be **standard library
only and fast**. Nothing anywhere asserted it. The rule survived on care alone,
and care is exactly what a refactor spends: an `import requests` for a nicer
error, a `from rhizome_graph.normalize import ...` that drags a package `__init__`
behind it, a helper moved into a module that happens to import `watchdog`. None
of those fails a test today, and the cost is not an error message -- the hook is
wrapped to exit 0 and stay silent, so a heavier import shows up as a slower agent
loop and, if the dependency is missing, as nothing at all.

Adding a `rhi-hook` console script (stage E) is precisely the change that could
break it invisibly, because an entry point in an installed package sits next to
the daemon's code and imports whatever its neighbours import. So the rule is
written down twice, in the two forms that catch different mistakes:

  * **Structurally**, by parsing the source and checking every imported
    top-level name against `sys.stdlib_module_names`. Cheap, exact, and it names
    the offending import.
  * **By measurement**, importing the entry module in a fresh interpreter and
    asking `sys.modules` what arrived with it. That is what catches a transitive
    import the AST cannot see -- the one a module three files away performs.

Most of this file is **green on arrival**, and that is the right shape for a rule
this load-bearing: it is a guard, not a request. What is red is the `rhi-hook`
console script itself.

**Why `rhi-hook` at all.** The command written into another project's settings
must be an absolute path to something *the package owns*. Today it is `python3
/some/checkout/hooks/emit_event.py` -- and `hooks/` is not in
`[tool.setuptools] packages`, so `pip install rhizome-graph` does not install it
at all. Every hook block that exists points into somebody's checkout, which is
the rot `rhi --doctor` was written to find: rename the directory, move the
clone, delete it after installing, and every tool call in that project returns a
blocking hook error.

**And what that costs.** An installed package means `rhizome_graph/__init__.py`
executes before the hook does, on every tool call. Today that file is one
docstring, 82 bytes, importing nothing -- so the price is a stat and a comment,
and there is a guard below to keep it there. It is the one real objection to
moving the adapter into the package, and it is answered by measurement rather
than by assurance.

**The shebang names the system interpreter.** `hooks/emit_event.py` starts
`#!/usr/bin/env python3`, and it must keep doing so: the hook needs no
third-party dependency, so pointing it at a virtualenv couples the hot path to
an installation detail that an upgrade, a rebuild or a `rm -rf .venv` removes --
and when it goes, it goes loudly, on every tool call, in a project that may have
nothing to do with this one. The same rule is asserted on the command
`hook_command()` produces: it may not point into *this checkout's* `.venv`.

**Deliberately not pinned: the shebang of the shim `pip` generates.** A console
script's first line is written by the installer and names whatever environment
it installed into -- a pipx venv, a user install, a distribution's `/usr/bin`.
That is pip's business, it is rewritten on every install, and it cannot rot the
way a path typed once into a settings file can. Pinning it would be pinning
somebody else's implementation.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import ast
import json
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: The adapter itself: the file every `PostToolUse` hook block installed
#: anywhere currently runs.
HOOK_SCRIPT = REPO_ROOT / "hooks" / "emit_event.py"

#: This repository's own installed settings. Read only, never written -- the
#: file is committed, and a test that edited it would put a change in the
#: working tree nobody asked for.
INSTALLED_SETTINGS = REPO_ROOT / ".claude" / "settings.json"

#: The console script a settings file in another project can name without
#: knowing where this source tree is, or whether it still exists.
HOOK_COMMAND_NAME = "rhi-hook"

#: A virtualenv, however it is spelled. An interpreter under one of these is an
#: installation detail; the hook must survive its removal.
VIRTUALENV_MARKERS = ("/.venv/", "/venv/", "/site-packages/")


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _scripts() -> dict:
    return _pyproject().get("project", {}).get("scripts", {})


def top_level_imports(source: str) -> set[str]:
    """Every top-level module name imported anywhere in `source`, at any depth.

    Relative imports are skipped: `from . import x` names no distribution. What
    is wanted is the set of *packages* an installation would have to provide.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def non_stdlib(names: set[str]) -> list[str]:
    """The names that an installation would have to provide from outside."""
    return sorted(
        name
        for name in names
        if name not in sys.stdlib_module_names and name != "rhizome_graph"
    )


def entry_point() -> tuple[str, str]:
    """The `rhi-hook` console script's target, as (module, function)."""
    target = _scripts().get(HOOK_COMMAND_NAME, "")
    assert target, (
        f"pyproject.toml declares no [project.scripts] entry for "
        f"{HOOK_COMMAND_NAME!r}, so an installed package owns no hook command "
        "and every settings file has to name somebody's checkout"
    )
    module, _, function = target.partition(":")
    return module, function


# ===========================================================================
# 1. the adapter that exists today -- guards, green on arrival
# ===========================================================================


def test_the_hook_imports_only_the_standard_library() -> None:
    """The rule CLAUDE.md repeats most and nothing has ever checked.

    A third-party import on this path costs startup time on every tool call, and
    when it is missing it costs nothing visible at all: the hook's own rule is to
    exit 0 and stay silent, so the graph simply goes dark.
    """
    offenders = non_stdlib(top_level_imports(HOOK_SCRIPT.read_text(encoding="utf-8")))

    assert offenders == [], (
        f"hooks/emit_event.py imports {offenders}, which the standard library "
        "does not provide. The adapter runs on every tool call and blocks the "
        "agent loop, and a missing dependency here is silent by design."
    )


def test_the_hook_script_names_the_system_interpreter(tmp_path: Path) -> None:
    """A hook pointed at a virtualenv breaks when the virtualenv is rebuilt.

    Loudly, on every tool call, in a project that may have nothing to do with
    this one -- the interpreter fails before the hook's defensive wrapper can
    exit 0.
    """
    shebang = HOOK_SCRIPT.read_text(encoding="utf-8").splitlines()[0]

    assert shebang.startswith("#!"), f"hooks/emit_event.py has no shebang: {shebang!r}"
    assert "python3" in shebang, shebang
    assert not any(marker in shebang for marker in VIRTUALENV_MARKERS), (
        f"hooks/emit_event.py is pointed at a virtualenv: {shebang!r}. The hook "
        "needs no third-party dependency, so it must not depend on one existing."
    )


def test_this_repositorys_own_hook_command_names_no_virtualenv() -> None:
    """The doctrine on the one real installation there is to look at.

    `tests/test_capture_settings.py` already pins that this command names a
    script that exists, after a rename broke exactly that. This is the other way
    the same line goes wrong: still present, still resolving, and resolving
    through an interpreter that a rebuild deletes.
    """
    settings = json.loads(INSTALLED_SETTINGS.read_text(encoding="utf-8"))
    commands = [
        str(hook.get("command", ""))
        for entry in settings.get("hooks", {}).get("PostToolUse", [])
        for hook in entry.get("hooks", [])
    ]

    offenders = [
        command
        for command in commands
        if any(marker in command for marker in VIRTUALENV_MARKERS)
    ]

    assert offenders == [], offenders


# ===========================================================================
# 2. the console script the package owns
# ===========================================================================


def test_the_package_declares_a_hook_console_script() -> None:
    """So a settings file can name a command instead of a checkout."""
    assert HOOK_COMMAND_NAME in _scripts(), (
        f"pyproject.toml declares no {HOOK_COMMAND_NAME!r}; every hook block "
        "then has to name a path inside a source tree the user may delete"
    )


def test_the_hook_entry_point_lives_in_an_installed_package() -> None:
    """`hooks/` is not in `[tool.setuptools] packages` and never gets installed.

    An entry point pointing there declares a command that cannot run from a
    wheel, which is the failure `tests/test_cli_entry_point.py` describes for
    `rhi`: `pip` reports success and the command fails on first use.

    **The cost this constraint imposes, named so nobody pays it by accident.**
    Putting the entry module under `rhizome_graph/` means `rhizome_graph/
    __init__.py` is executed on **every tool call**, ahead of the hook itself.
    Measured at the time of writing: that file is one docstring, 82 bytes, with
    no import in it, so the cost is a stat and a parse of a comment. It has to
    stay that way -- an `from .normalize import ...` there for convenience would
    put the daemon's neighbourhood on the hot path, and a `__init__` that
    imports anything heavy is invisible in the entry module's own source.
    `test_the_package_init_the_hook_pays_for_imports_nothing` below is the
    guard, and the fresh-interpreter probe after it is what catches whatever the
    guard's structural view cannot see.
    """
    module, _function = entry_point()
    packages = _pyproject().get("tool", {}).get("setuptools", {}).get("packages", [])

    assert module.split(".")[0] in packages, (
        f"{HOOK_COMMAND_NAME} points at {module}, which is not in a package "
        f"that gets installed ({packages})"
    )


def test_the_package_init_the_hook_pays_for_imports_nothing() -> None:
    """`rhizome_graph/__init__.py` runs before the hook does, every tool call.

    Green on arrival -- the file is one docstring and nothing else -- and that
    is the point: this is the invoice for keeping the hook inside an installed
    package, and it is a bill that must stay at zero. An import added here is
    paid on every `Write`, `Edit`, `Bash` and `Read` in every session, by the
    one piece of code CLAUDE.md says must be stdlib-only and fast, and it is
    charged to a module the hook's own source never mentions.
    """
    init = REPO_ROOT / "rhizome_graph" / "__init__.py"
    tree = ast.parse(init.read_text(encoding="utf-8"))

    imports = [
        ast.dump(node)
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]

    assert imports == [], (
        "rhizome_graph/__init__.py imports something. The hook runs from inside "
        "this package, so every tool call in every session now pays for it:\n"
        + "\n".join(imports)
    )


def test_the_hook_entry_function_exists() -> None:
    """A shim naming a function nobody wrote fails after `pip` said ok."""
    module_name, function_name = entry_point()
    module = __import__(module_name, fromlist=[function_name])

    assert callable(getattr(module, function_name, None)), (
        f"{module_name}:{function_name} does not name a callable"
    )


def test_the_hook_entry_module_imports_only_the_standard_library() -> None:
    """The same rule as the script, on the file that replaces it.

    This is the whole risk of moving the hook into the package: the entry module
    sits beside `daemon/` and `launch.py`, and importing one of its neighbours
    for one helper drags `websockets` and `watchdog` onto the hot path.
    """
    module_name, _function = entry_point()
    module = __import__(module_name, fromlist=["__file__"])
    source = Path(module.__file__).read_text(encoding="utf-8")

    offenders = non_stdlib(top_level_imports(source))

    assert offenders == [], (
        f"{module_name} imports {offenders}. The hook runs on every tool call "
        "and must not need anything installed beyond Python itself."
    )


def test_importing_the_hook_entry_module_pulls_in_nothing_third_party() -> None:
    """The measured half, which is what catches a transitive import.

    A fresh interpreter, because this one has imported half the project already;
    `sys.modules` is snapshotted around the import so the answer is exactly what
    arrived with it, on any machine.
    """
    module_name, _function = entry_point()
    probe = (
        "import sys;"
        "before=set(sys.modules);"
        f"import {module_name};"
        "added={n.split('.')[0] for n in set(sys.modules)-before};"
        "print(sorted(n for n in added "
        "if n not in sys.stdlib_module_names and not n.startswith('_')))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() in ("[]", "['rhizome_graph']"), (
        f"importing {module_name} pulled in {completed.stdout.strip()}"
    )


# ===========================================================================
# 3. what actually gets written into somebody else's settings
# ===========================================================================


def hook_command() -> str:
    from rhizome_graph.assets import hook_command as resolve

    return resolve()


def test_there_is_one_place_that_says_how_to_run_the_hook() -> None:
    """`assets.py` already owns "where does this installation keep things".

    The same question the built page raised -- a path that was right in a
    checkout and wrong in a wheel, silently -- so it gets the same answer and
    the same home rather than a second search written somewhere else.
    """
    answer = hook_command()

    assert isinstance(answer, str) and answer, "assets.hook_command() answered nothing"


def test_the_hook_command_is_absolute() -> None:
    """Claude Code runs a hook from a directory nobody here chose."""
    program = shlex.split(hook_command())[-1]

    assert Path(program).is_absolute(), hook_command()


def test_the_hook_command_names_something_that_is_really_there() -> None:
    """A command written today may not start out stale."""
    program = shlex.split(hook_command())[-1]

    assert Path(program).is_file(), f"{hook_command()!r} names {program}"


def test_the_hook_command_names_this_projects_hook() -> None:
    """The recogniser in `hookinstall.diagnose` has to accept what we write.

    Stated here as well as in `tests/test_hook_install_model.py` because this is
    the end that moves: a rename of the entry point that forgot the recogniser
    would make every fresh install diagnose as somebody else's hook.
    """
    program = Path(shlex.split(hook_command())[-1]).name

    assert program in {HOOK_COMMAND_NAME, "emit_event.py"}, hook_command()


def test_the_hook_command_does_not_point_into_this_checkouts_virtualenv() -> None:
    """`.venv/` here is a scratch directory that gets deleted and rebuilt.

    A settings file in *another* project that names it keeps working until the
    day somebody rebuilds this one, and then errors on every tool call over
    there. A pipx or user install is a different matter -- that environment is
    managed and stable -- so what is forbidden is this repository's own.
    """
    assert str(REPO_ROOT / ".venv") not in hook_command(), hook_command()


def test_the_hook_command_does_not_depend_on_the_current_directory() -> None:
    """It is written into a file and read back weeks later, from anywhere.

    Measured from two working directories rather than argued from `isabs`: a
    command assembled with a relative path can still look absolute once the
    process happens to be in the right place.
    """
    probe = "from rhizome_graph.assets import hook_command; print(hook_command())"
    runs = [
        subprocess.run(
            [sys.executable, "-c", probe],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        for cwd in (str(REPO_ROOT), str(REPO_ROOT / "hooks"))
    ]

    for completed in runs:
        assert completed.returncode == 0, completed.stderr
    answers = {completed.stdout.strip() for completed in runs}

    assert len(answers) == 1, f"hook_command() answered differently per cwd: {answers}"
