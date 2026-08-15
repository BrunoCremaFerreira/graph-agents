"""The project is called rhizome-graph, everywhere the name is load-bearing.

Motivation: a rename that stops halfway is worse than no rename. The name is
not decoration here -- it is the ingest socket path that the hook and the daemon
have to agree on independently (two files, two literals, no shared constant), it
is the distribution/import package every test and the daemon itself import by
name, and it is what the page calls itself in the one place a user reads it.

Each of those can be renamed alone and leave the tree running until the exact
moment it does not: a hook writing to `/tmp/<old>.sock` while the daemon listens
on `/tmp/<new>.sock` loses every attributed event and produces the *specific*
failure this project has already paid for once -- a graph that updates with
nobody on camera, indistinguishable from "no agent is working right now".

The socket assertions are deliberately literal. Comparing the hook's constant to
the daemon's would pass while both were wrong; each is pinned to the string, and
then to each other.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent

SOCKET_PATH = "/tmp/rhizome-graph.sock"


def _load_hook() -> ModuleType:
    """Import `hooks/emit_event.py`, which is a script and not a package."""
    path = REPO_ROOT / "hooks" / "emit_event.py"
    spec = importlib.util.spec_from_file_location("emit_event_under_test", path)
    assert spec and spec.loader, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_hook_defaults_to_the_project_socket() -> None:
    """The sender's default path carries the new name."""
    assert _load_hook().DEFAULT_SOCKET_PATH == SOCKET_PATH


def test_the_daemon_defaults_to_the_project_socket() -> None:
    """The listener's default path carries the new name."""
    from daemon.server import DEFAULT_SOCKET_PATH

    assert DEFAULT_SOCKET_PATH == SOCKET_PATH


def test_hook_and_daemon_agree_on_the_default_socket() -> None:
    """Two literals in two files; a drift between them silences attribution."""
    from daemon.server import DEFAULT_SOCKET_PATH

    assert _load_hook().DEFAULT_SOCKET_PATH == DEFAULT_SOCKET_PATH


def test_the_python_package_is_importable_under_its_new_name() -> None:
    """`rhizome_graph` -- underscored, because an import cannot carry a hyphen."""
    from rhizome_graph import normalize

    assert hasattr(normalize, "normalize_event")


#: Every authored tree where a configuration variable can hide. A name that is
#: absent from disk is skipped, which is why `test_language_policy` pins that
#: the package directory is really being read.
SCANNED_DIRS = ("rhizome_graph", "daemon", "hooks", "web/src", "config", ".claude")
SCANNED_FILES = ("start.sh", "run.sh", "pyproject.toml")
SCANNED_SUFFIXES = {".py", ".ts", ".js", ".css", ".html", ".sh", ".json", ".toml", ".md"}

#: The old environment-variable prefix, spelled in two halves so that this file
#: cannot itself be mistaken for an occurrence by a grep over the rename.
OLD_PREFIX = "GRAPH" + "AGENTS_"


def _authored_files() -> list[Path]:
    found = [REPO_ROOT / name for name in SCANNED_FILES]
    found = [path for path in found if path.is_file()]
    for name in SCANNED_DIRS:
        base = REPO_ROOT / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
                continue
            if any(part in {"node_modules", "dist", "__pycache__"} for part in path.parts):
                continue
            found.append(path)
    return found


def test_no_source_still_reads_the_old_environment_variables() -> None:
    """All ten of them move together, or a documented switch quietly stops working.

    An overlooked one is invisible: the code reads a variable nobody exports any
    more, silently takes its default, and the only symptom is a setting that has
    no effect -- on the port, the log level or the remote-control gate.
    """
    offences = [
        f"{path.relative_to(REPO_ROOT)}:{number}"
        for path in _authored_files()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if OLD_PREFIX in line
    ]

    assert offences == [], "old environment-variable prefix still read at:\n" + "\n".join(
        offences
    )


def test_the_web_package_is_named_for_the_project() -> None:
    """`web/package.json` names the workspace, and npm sees it."""
    manifest = json.loads((REPO_ROOT / "web" / "package.json").read_text(encoding="utf-8"))

    assert manifest["name"] == "rhizome-graph-web"


def test_the_page_calls_itself_by_the_new_name() -> None:
    """The title bar and the header are the only place a user reads the name."""
    html = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert "rhizome-graph" in html
    assert "graph-agents" not in html
