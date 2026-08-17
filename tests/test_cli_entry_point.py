"""Contract tests (RED) for there being a command called `rhi` at all.

Motivation: everything this program does is reachable only as
`RHIZOME_PROJECT_ROOT=... ./start.sh` from inside the checkout. `pyproject.toml`
declares no `[project.scripts]`, so `pip install .` installs a library and no
command, and `rhizome_graph/cli.py` -- which already builds the whole
configuration from argv, environment and cwd -- has no `main()` for a console
script to point at. Stage A gave the configuration a shape, stage B gave the
daemon a `run(settings)`; what is missing is the front door itself.

Three properties, and the middle one is the reason this file exists rather than
one assertion in another:

  * **The declaration.** `project.scripts.rhi` names `rhizome_graph.cli:main`,
    and `main` is really there. A console script that names a function nobody
    wrote installs cleanly and fails on first use, after `pip` reported success
    -- the same class of defect as a dependency floor below what the code imports
    (`tests/test_packaging.py`).

  * **`--version` prints the *installed* version.** A version literal in the
    source and one in `pyproject.toml` drift, and the moment they do, the number
    quoted in a bug report is not the number that shipped. This is awkward to
    test honestly, because the project's version is currently `0.0.0` -- a
    hard-coded `"0.0.0"` in `cli.py` would satisfy any assertion that merely
    compares the printed text to `importlib.metadata.version(...)`. So the
    behavioural test is paired with a structural one: **no string literal
    anywhere in `cli.py` may equal the installed version**, and the module must
    really import `importlib.metadata`. Together those cannot be passed by a
    literal, whatever it says. The cost is one real constraint on the
    implementation: a `PackageNotFoundError` fallback (running from a checkout
    with nothing installed) must be spelled as something that is *not* the
    current version -- `"unknown"`, say -- which is the honest answer anyway,
    since "I could not find out" and "0.0.0" are different facts.

  * **Neither `--version` nor `--help` starts anything.** They are the two flags
    a person types when they are not sure what they have installed, and both run
    through the same `main()` that otherwise seeds a tree, binds a port and opens
    an ingest socket. A `--help` that leaves a daemon running -- or that takes
    over `/tmp/rhizome-graph.sock` from one already running -- is worse than no
    `--help`. The test gives them a throwaway socket path and a free port and
    checks that the process returns on its own, promptly, having created neither.

The subprocess machinery, and what it deliberately does not cover, is documented
in `tests/rhi_process.py`.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import ast
import importlib.metadata
import subprocess
import tomllib
from pathlib import Path

import pytest

from rhi_process import DISTRIBUTION, ENTRY_POINT, clean_environment, entry_argv, free_port

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: The command a user types. Short on purpose: it is typed once per session, and
#: `rhizome-graph` as a command name is four syllables of ceremony.
COMMAND = "rhi"

#: How long a flag that only prints is given to print and return. Generous by an
#: order of magnitude; a `--help` that has not returned by now has started a
#: daemon, which is exactly what is being ruled out.
PRINT_TIMEOUT_SECONDS = 30.0


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _installed_version() -> str:
    return importlib.metadata.version(DISTRIBUTION)


def _cli_source() -> str:
    import rhizome_graph.cli as cli

    return Path(cli.__file__).read_text(encoding="utf-8")


def _run(argv: tuple[str, ...], environ: dict[str, str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            entry_argv(argv),
            cwd=str(REPO_ROOT),
            env=environ,
            capture_output=True,
            text=True,
            timeout=PRINT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"`rhi {' '.join(argv)}` had not returned after "
            f"{PRINT_TIMEOUT_SECONDS:.0f}s: a flag that only prints started a daemon"
        )


# --- 1. the declaration -----------------------------------------------------


def test_the_package_declares_a_console_script() -> None:
    """`pip install` has to leave a command behind, not only a library."""
    scripts = _pyproject().get("project", {}).get("scripts", {})

    assert COMMAND in scripts, (
        "pyproject.toml declares no [project.scripts] entry for "
        f"{COMMAND!r}, so installing the package installs no command"
    )


def test_the_console_script_points_at_the_cli_entry_function() -> None:
    """One string, named here and in `tests/rhi_process.py`, so they agree."""
    scripts = _pyproject().get("project", {}).get("scripts", {})

    assert scripts.get(COMMAND) == ENTRY_POINT


def test_the_entry_function_the_console_script_names_exists() -> None:
    """A shim pointing at a function nobody wrote fails after `pip` said ok."""
    module_name, _, function_name = ENTRY_POINT.partition(":")
    module = __import__(module_name, fromlist=[function_name])

    assert callable(getattr(module, function_name, None)), (
        f"{ENTRY_POINT} does not name a callable"
    )


# --- 2. --version says what is actually installed ---------------------------


def test_the_version_flag_prints_the_installed_distributions_version() -> None:
    """The number a bug report quotes must be the number that shipped."""
    completed = _run(("--version",), clean_environment())

    assert _installed_version() in completed.stdout, (
        f"`rhi --version` printed {completed.stdout.strip()!r}, which does not "
        f"carry the installed version {_installed_version()!r}"
    )


def test_no_string_in_the_cli_source_spells_the_version(tmp_path: Path) -> None:
    """The guard that stops the test above passing on a coincidence.

    The project's version is `0.0.0` today, so a literal `"0.0.0"` in `cli.py`
    would print exactly what the assertion above looks for while being the very
    bug it exists to prevent. A version may be *read*; it may not be *written*.
    """
    version = _installed_version()
    literals = [
        node.value
        for node in ast.walk(ast.parse(_cli_source()))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]

    assert version not in literals, (
        f"rhizome_graph/cli.py spells the version {version!r} as a literal. It "
        "has to come from importlib.metadata, or the source and pyproject.toml "
        "will drift and the one a user reports will be the wrong one."
    )


def test_the_version_comes_through_importlib_metadata() -> None:
    """The other half of the same guard: it is read, and read from the install."""
    imported: set[str] = set()
    for node in ast.walk(ast.parse(_cli_source())):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module)

    assert "importlib.metadata" in imported, (
        "rhizome_graph/cli.py does not import importlib.metadata, so whatever "
        "`--version` prints did not come from the installed distribution"
    )


def test_the_version_flag_exits_zero() -> None:
    """Asking a question successfully is not an error."""
    completed = _run(("--version",), clean_environment())

    assert completed.returncode == 0, completed.stderr


# --- 3. a flag that only prints starts nothing ------------------------------


@pytest.mark.parametrize("flag", ["--version", "--help"])
def test_a_printing_flag_returns_on_its_own(flag: str, tmp_path: Path) -> None:
    """`_run` fails the test on a timeout; this names what a timeout means."""
    environ = clean_environment(
        RHIZOME_SOCKET=str(tmp_path / "ingest.sock"),
        RHIZOME_HTTP_PORT=str(free_port()),
        RHIZOME_PROJECT_ROOT=str(tmp_path),
    )

    completed = _run((flag,), environ)

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("flag", ["--version", "--help"])
def test_a_printing_flag_opens_no_ingest_socket(flag: str, tmp_path: Path) -> None:
    """The socket is the shared name: taking it over derails a live daemon."""
    ingest = tmp_path / "ingest.sock"
    environ = clean_environment(
        RHIZOME_SOCKET=str(ingest),
        RHIZOME_HTTP_PORT=str(free_port()),
        RHIZOME_PROJECT_ROOT=str(tmp_path),
    )

    _run((flag,), environ)

    assert not ingest.exists(), f"`rhi {flag}` created an ingest socket at {ingest}"


@pytest.mark.parametrize("flag", ["--version", "--help"])
def test_a_printing_flag_seeds_no_project_tree(flag: str, tmp_path: Path) -> None:
    """A cheap proxy for "no daemon ran": seeding logs, on the way up, always.

    Deliberately not a wording assertion about the log line -- only that the
    logging the daemon emits at boot is absent entirely, which it is if nothing
    booted.
    """
    environ = clean_environment(
        RHIZOME_SOCKET=str(tmp_path / "ingest.sock"),
        RHIZOME_HTTP_PORT=str(free_port()),
        RHIZOME_PROJECT_ROOT=str(tmp_path),
    )

    completed = _run((flag,), environ)

    assert "rhizome_graph.daemon" not in completed.stderr, (
        f"`rhi {flag}` produced daemon log output:\n{completed.stderr}"
    )
