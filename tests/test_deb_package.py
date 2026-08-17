"""The Debian package: what is inside it, and what it says it needs.

Motivation. A `.deb` is the one distribution channel where every mistake is
silent at build time and loud a week later on somebody else's machine. Four of
them are specific to this project and each has already been paid for once, in
another form:

  * **The front end is gitignored** (`web/dist/`, 9.4 MB). A package built from a
    clean checkout carries a daemon that starts, binds, serves the WebSocket and
    shows a blank page -- `default_web_dist()` matches no candidate and `None` is
    a documented, non-erroring state. `tests/test_distribution_front_end.py` says
    the same thing about the wheel; this file says it about `/usr/lib`.
  * **The hook runs on every tool call and must not depend on the venv.** A
    console script generated inside `/usr/lib/rhizome-graph/venv/` gets a shebang
    pointing at the vendored interpreter, which couples the hottest path in the
    system to an installation detail an upgrade removes. When that path breaks,
    it breaks as a blocking error on every single tool call in the user's Claude
    Code session -- the one thing the adapter is forbidden to do.
  * **A vendored virtualenv is bound to a Python minor version.** A venv built
    against 3.12 does not run on 3.11 and does not run on 3.13. `Depends:
    python3` with no bounds installs cleanly and then fails after the next
    release upgrade moves the system interpreter -- which is precisely when
    nobody is looking at this package.
  * **Shipped sources with no bytecode make the hot path pay for a compile it
    can never cache.** `/usr/lib` is not writable by the user whose agent fires
    the hook, so the interpreter recompiles `rhizome_graph/hook.py` and
    everything it imports on *every single tool call* and then fails to write
    the `__pycache__` that would end it. Measured on the build host: 42.5 ms per
    invocation with no cache against 38.8 ms with one, over an 18 ms bare
    interpreter start -- roughly 3.7 ms of pure waste, permanently, on the one
    path CLAUDE.md says must be "dependency-free and fast" because it "blocks
    the agent loop". A package is the only shape of this project where the fix
    is not automatic, and it is one `compileall` line.

**Why the venv is vendored at all**, since the alternative would be smaller:
`daemon/server.py` imports `websockets.asyncio.server`, which first ships in the
13 series; Debian noble carries `python3-websockets` 10.4. Measured by unpacking
the wheels: `websockets/asyncio/` holds 0 files in 12.0 and 8 in 13.0.
`python3-watchdog` needs no such treatment -- noble's 3.0.0 runs the whole suite
green -- so watchdog stays a distribution dependency and only websockets is
carried. `tests/test_packaging.py` pins the same two facts as version floors in
`pyproject.toml`; here they are pinned as facts about the produced package.

**Built, then read back -- not asserted over the staging tree.** The staging
directory is the build's *input*; everything interesting is a property of its
*output*. `dpkg-deb --build` is what applies `DEBIAN/control`, what an
`--exclude` rule or a copy that silently skipped 9.4 MB would show up in, and
what a user inspects with `dpkg -c`. So the tests here run the repository's own
build script and then interrogate the archive it produced:

  * `dpkg-deb -x` for the tree, because the extracted files answer questions
    about content (a shebang) and structure with `pathlib`, whereas
    `dpkg-deb --contents` has to be re-parsed out of a column format with
    symlink arrows and paths that may contain spaces;
  * `dpkg-deb --contents` where the question really is about the manifest as
    dpkg records it -- ownership, which is the check that the builder ran under
    `fakeroot`;
  * `dpkg-deb --field` for the control metadata, since what apt reads is the
    binary control, not the authored source one.

**What the build script must be**, stated because these tests drive it:
`packaging/build-deb.sh OUTPUT_DIRECTORY` builds exactly one `.deb` into that
directory, writes nothing into the checkout, and exits non-zero on failure. The
tests do not care how it stages, whether it shells out to `dpkg-deb` directly or
through `dpkg-buildpackage`; only that the archive appears where it was asked for.

**Slow and network-touching, so opt-in**, by the same convention as
`tests/test_distribution_front_end.py`: `RHIZOME_PACKAGE_TESTS=1`. Vendoring the
virtualenv means installing websockets, which needs an index or a wheel cache.
The dependency assertions over the authored `debian/control` are *not* gated --
they are text, they are the decisions, and they should fail fast for everyone.

**What this file does not and cannot verify on this host.**

  * `debhelper` is not installed, so a `dpkg-buildpackage`/`dh` build is never
    exercised here; nor is `debian/rules` as a build entry point.
  * `lintian` is not installed. Debian policy conformance -- `copyright`,
    `changelog.Debian.gz`, section, priority, the many `E:` classes -- is
    entirely unchecked. That is the real review gate for a package meant to be
    uploaded anywhere, and it has not been run.
  * Nothing is ever *installed*: `dpkg -i` needs root. So the maintainer scripts
    are not executed, no dependency is actually resolved by apt, `/usr/bin/rhi`
    is never run from the path it was built for, and the `python3-gi` /
    WebKitGTK stack is not proven to satisfy `rhizome_graph/window.py`.
  * Nothing is verified on any Debian release other than the one this host runs;
    the version bound test asserts the shape of the bound, not that the bound is
    right for a release nobody here can boot.
  * Upgrade and removal behaviour (`postinst`, `prerm`, leftover files, a venv
    surviving a purge) is untested.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from rhizome_graph.assets import SYSTEM_WEB_DIR
from rhizome_graph.hookinstall import hook_block

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The same opt-in switch `tests/test_distribution_front_end.py` documents.
PACKAGE_TESTS_ENV = "RHIZOME_PACKAGE_TESTS"

#: The Debian source-package directory; the name is fixed by dpkg.
DEBIAN_CONTROL = REPO_ROOT / "debian" / "control"

#: The one entry point these tests need. Argument: an output directory.
BUILD_SCRIPT = REPO_ROOT / "packaging" / "build-deb.sh"

#: The binary package name, which is also the doc directory and the install
#: prefix under `/usr/lib`. `SYSTEM_WEB_DIR` from `assets.py` is the resolver's
#: half of the same agreement and is imported rather than respelled.
PACKAGE_NAME = "rhizome-graph"
INSTALL_PREFIX = Path("/usr/lib") / PACKAGE_NAME
VENDORED_VENV = INSTALL_PREFIX / "venv"
DOC_DIR = Path("/usr/share/doc") / PACKAGE_NAME

#: The launcher and the adapter, as a user's `$PATH` sees them.
LAUNCHER = Path("/usr/bin/rhi")
HOOK_COMMAND = Path("/usr/bin/rhi-hook")

#: The interpreter the hook must be run by: the system one, named absolutely.
#: Not `/usr/bin/env python3` -- a hook inherits the agent's environment, and a
#: `$PATH` pointing at a project virtualenv would silently choose that.
SYSTEM_INTERPRETER_SHEBANG = "#!/usr/bin/python3"

#: What `/usr/bin/rhi-hook` imports once it has put the install prefix on
#: `sys.path`. Named rather than the shim's own path because the shim is a
#: `/usr/bin` file with no `.py` suffix: it is never imported, never cached, and
#: is not the thing whose compilation matters.
HOOK_ENTRY_MODULE = "rhizome_graph.hook"

#: PEP 552's flag word, bit for bit. Bit 0: the `.pyc` records a hash of its
#: source rather than an mtime and a size. Bit 1: the interpreter must re-read
#: and re-hash the source before trusting it.
PYC_HASH_BASED = 0b01
PYC_CHECK_SOURCE = 0b10

#: magic (4) + flags (4) + either mtime and size or the source hash (8).
PYC_HEADER_BYTES = 16

#: The GTK/WebKit stack `rhizome_graph/window.py` needs for its pywebview
#: backend. All three, because `python3-gi` alone imports and then fails to find
#: a WebKit typelib at the moment the window is created.
WEBVIEW_DEPENDS = ("python3-gi", "gir1.2-webkit2-4.1", "libwebkit2gtk-4.1-0")

#: The front end, minus source maps -- see the same reasoning in
#: `tests/test_distribution_front_end.py`.
CHECKOUT_WEB_DIST = REPO_ROOT / "web" / "dist"

_RELATION = re.compile(r"^(?P<name>[^\s(|]+)(?:\s*\((?P<op>[<>=]+)\s*(?P<version>[^)]+)\))?")


class BuiltPackage(NamedTuple):
    """One built `.deb`, plus what the checkout looked like on either side of it."""

    deb: Path
    tree: Path
    checkout_before: set[str]
    checkout_after: set[str]


# --- reading the authored control file -------------------------------------


def _stanzas(text: str) -> list[dict[str, str]]:
    """A deb822 file as a list of field maps, continuation lines folded in.

    Hand-rolled rather than `python3-debian`: this suite runs on the stdlib plus
    pytest, and the grammar needed here is "field: value, continued by indented
    lines, stanzas separated by a blank line".
    """
    stanzas: list[dict[str, str]] = []
    current: dict[str, str] = {}
    last_field = ""
    for line in text.splitlines():
        if not line.strip():
            if current:
                stanzas.append(current)
                current, last_field = {}, ""
            continue
        if line[0] in " \t" and last_field:
            current[last_field] += " " + line.strip()
            continue
        if ":" not in line:
            continue
        field, _, value = line.partition(":")
        last_field = field.strip()
        current[last_field] = value.strip()
    if current:
        stanzas.append(current)
    return stanzas


def _binary_stanza() -> dict[str, str]:
    """The `Package: rhizome-graph` stanza of the authored `debian/control`."""
    assert DEBIAN_CONTROL.is_file(), (
        f"{DEBIAN_CONTROL.relative_to(REPO_ROOT)} does not exist; the Debian "
        "package declares its dependencies nowhere"
    )
    stanzas = _stanzas(DEBIAN_CONTROL.read_text(encoding="utf-8"))
    binary = [s for s in stanzas if s.get("Package", "") == PACKAGE_NAME]
    assert len(binary) == 1, (
        f"expected exactly one `Package: {PACKAGE_NAME}` stanza in "
        f"{DEBIAN_CONTROL.relative_to(REPO_ROOT)}, got {len(binary)}"
    )
    return binary[0]


def _entries(field_value: str) -> list[str]:
    """The comma-separated entries of a relationship field, alternatives split.

    `python3-gi | python3-gobject` is two names for this purpose: an alternative
    is a dependency that may be satisfied, so both are reported and a test asking
    "is this name required" gets a truthful yes.
    """
    entries: list[str] = []
    for chunk in field_value.split(","):
        for alternative in chunk.split("|"):
            text = alternative.strip()
            # `${misc:Depends}` and friends are substitution variables that a
            # debhelper build expands at build time. They are placeholders, not
            # package names, and comparing them against a built control would
            # report a difference that is the tooling working correctly.
            if text and not text.startswith("${"):
                entries.append(text)
    return entries


def _names(field_value: str) -> set[str]:
    """Just the package names of a relationship field, versions dropped."""
    found: set[str] = set()
    for entry in _entries(field_value):
        match = _RELATION.match(entry)
        if match:
            found.add(match.group("name"))
    return found


def _relations(field_value: str, package: str) -> list[tuple[str, tuple[int, ...]]]:
    """Every `(operator, version)` constraint declared on `package`."""
    found: list[tuple[str, tuple[int, ...]]] = []
    for entry in _entries(field_value):
        match = _RELATION.match(entry)
        if not match or match.group("name") != package:
            continue
        if match.group("op") and match.group("version"):
            found.append((match.group("op"), _version(match.group("version"))))
    return found


def _version(text: str) -> tuple[int, ...]:
    """`"3.12.1-2"` -> `(3, 12, 1)`; the comparison stops at the first non-digit."""
    parts: list[int] = []
    for piece in text.strip().split("."):
        digits = re.match(r"\d+", piece)
        if digits is None:
            break
        parts.append(int(digits.group()))
    return tuple(parts)


def _satisfies(candidate: tuple[int, ...], operator: str, bound: tuple[int, ...]) -> bool:
    """Does `candidate` satisfy `operator bound`, in dpkg's relation spelling?"""
    if operator in (">=", ">"):
        return candidate >= bound
    if operator == ">>":
        return candidate > bound
    if operator in ("<=", "<"):
        return candidate <= bound
    if operator == "<<":
        return candidate < bound
    if operator == "=":
        return candidate == bound
    raise AssertionError(f"unknown dpkg relation operator {operator!r}")


# --- building and reading back the package ---------------------------------


def _require_opt_in() -> None:
    if os.environ.get(PACKAGE_TESTS_ENV, "") != "1":
        pytest.skip(
            f"packaging tests are opt-in and slow; run with {PACKAGE_TESTS_ENV}=1"
        )


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> BuiltPackage:
    """One real `.deb`, built by the repository's own script and unpacked."""
    _require_opt_in()
    for tool in ("dpkg-deb", "fakeroot"):
        if shutil.which(tool) is None:  # pragma: no cover - depends on the machine
            pytest.skip(f"{tool} is not installed; a .deb cannot be built or read here")
    assert BUILD_SCRIPT.is_file(), (
        f"{BUILD_SCRIPT.relative_to(REPO_ROOT)} does not exist; nothing in this "
        "repository can produce a .deb"
    )

    base = tmp_path_factory.mktemp("deb")
    outdir = base / "out"
    outdir.mkdir()
    before = {entry.name for entry in REPO_ROOT.iterdir()}

    completed = subprocess.run(
        [shutil.which("bash") or "/bin/bash", str(BUILD_SCRIPT), str(outdir)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=1800,
    )
    after = {entry.name for entry in REPO_ROOT.iterdir()}
    assert completed.returncode == 0, (
        f"{BUILD_SCRIPT.name} failed:\n"
        + completed.stdout[-4000:]
        + completed.stderr[-4000:]
    )

    debs = sorted(outdir.glob("*.deb"))
    assert len(debs) == 1, f"expected exactly one .deb in {outdir}, got {debs}"

    tree = base / "tree"
    tree.mkdir()
    subprocess.run(
        ["dpkg-deb", "-x", str(debs[0]), str(tree)], check=True, timeout=600
    )
    return BuiltPackage(debs[0], tree, before, after)


def _installed(built: BuiltPackage, path: Path) -> Path:
    """An absolute installed path, as it sits inside the extracted archive."""
    return built.tree / path.relative_to("/")


def _field(built: BuiltPackage, name: str) -> str:
    """One field of the binary control, as apt would read it. `""` when absent.

    Asked for a single field, `dpkg-deb --field` prints the bare value; asked for
    several it prints `Field: value` lines. Both spellings are accepted here so
    that the helper does not silently return the empty string -- which every
    caller would read as "the package declares nothing", the exact false green
    this file is meant to avoid.
    """
    completed = subprocess.run(
        ["dpkg-deb", "--field", str(built.deb), name],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    text = completed.stdout.strip()
    if text.lower().startswith(f"{name.lower()}:"):
        text = text[len(name) + 1 :]
    return " ".join(text.split())


def _contents(built: BuiltPackage) -> list[str]:
    """`dpkg-deb --contents`, raw, one line per entry."""
    completed = subprocess.run(
        ["dpkg-deb", "--contents", str(built.deb)],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return completed.stdout.splitlines()


def _front_end_files() -> list[str]:
    return sorted(
        str(path.relative_to(CHECKOUT_WEB_DIST))
        for path in CHECKOUT_WEB_DIST.rglob("*")
        if path.is_file() and path.suffix != ".map"
    )


# --- the interpreter the package is for, and what the hook loads on it ------


def _venv_python_minor(built: BuiltPackage) -> int:
    """The N of the `python3.N` the vendored virtualenv was built against.

    The venv's own `lib/python3.N` directory is the artefact's statement of which
    interpreter this package is for, and
    `test_the_python3_bound_brackets_the_interpreter_the_venv_was_built_for`
    pins that the declared `Depends` range admits exactly that N and neither
    neighbour. So a test written against this number is written against the
    declared range, without parsing the range twice.
    """
    venv = _installed(built, VENDORED_VENV)
    assert venv.is_dir(), f"{VENDORED_VENV} is not in the package"
    minors = sorted(
        int(match.group(1))
        for path in venv.glob("lib/python3.*")
        if (match := re.fullmatch(r"python3\.(\d+)", path.name))
    )
    assert len(minors) == 1, f"expected one python3.N tree in the venv, got {minors}"
    return minors[0]


def _packaged_sources_the_hook_imports(built: BuiltPackage) -> list[str]:
    """The package's own `.py` files that importing the hook entry module runs.

    Measured in a fresh interpreter against the *extracted* tree rather than
    listed here by hand: the set is two files today, and the day
    `rhizome_graph/hook.py` imports a sibling for one helper, the test that
    consumes this has to grow with it without anybody remembering a literal in
    a test file. Only files under the install prefix are reported -- the
    standard library is compiled by whoever built the interpreter, and is not
    this package's business.

    `-B` plus `PYTHONDONTWRITEBYTECODE`, and `PYTHONPATH` dropped, because the
    probe must not create the very `__pycache__` its caller is about to look
    for, nor find the checkout's copy of the package instead of the packaged
    one.
    """
    prefix = _installed(built, INSTALL_PREFIX)
    probe = (
        "import json, os, sys\n"
        f"prefix = {str(prefix)!r}\n"
        "sys.path.insert(0, prefix)\n"
        f"import {HOOK_ENTRY_MODULE}\n"
        "print(json.dumps(sorted(\n"
        "    os.path.relpath(module.__file__, prefix)\n"
        "    for module in list(sys.modules.values())\n"
        "    if getattr(module, '__file__', None)\n"
        "    and os.path.abspath(module.__file__).startswith(prefix + os.sep)\n"
        ")))\n"
    )
    environment = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-B", "-c", probe],
        cwd=str(built.tree),
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, (
        f"importing {HOOK_ENTRY_MODULE} out of the packaged tree failed:\n"
        + completed.stderr[-4000:]
    )
    return json.loads(completed.stdout)


def _cache_path(source: Path, minor: int) -> Path:
    """Where CPython 3.`minor` looks for `source`'s bytecode, and nowhere else.

    The tag in the name is the compatibility key: 3.13 does not read
    `hook.cpython-312.pyc` and never will, so a `.pyc` under the wrong tag is
    not a slow package, it is a package carrying dead weight that looks done.
    """
    return source.parent / "__pycache__" / f"{source.stem}.cpython-3{minor}.pyc"


def _settings_fragments(directory: Path) -> list[tuple[Path, dict]]:
    """Every file under `directory` that *is* a Claude Code settings object.

    Identified by shape -- JSON, an object, with a `hooks` mapping in it -- and
    never by the words its bytes happen to contain. What this replaces, and why,
    is in `test_prose_about_the_hook_is_not_mistaken_for_the_settings_fragment`.

    Undecodable and unparseable files are skipped rather than reported: a doc
    directory holds a gzipped changelog and a licence, and neither is a
    candidate for anything. Skipping is safe here only because the caller
    asserts that something was found.
    """
    found: list[tuple[Path, dict]] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("hooks"), Mapping):
            found.append((path, parsed))
    return found


def _hook_commands(fragment: Mapping[str, Any]) -> list[str]:
    """The commands a settings fragment's hook blocks actually run.

    Walked structurally, for the same reason the file is selected structurally:
    a fragment that names `rhi-hook` in a `matcher`, in a stray key or in a
    comment somebody's editor left behind is not a fragment that runs it.
    """
    commands: list[str] = []
    hooks = fragment.get("hooks", {})
    if not isinstance(hooks, Mapping):
        return commands
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            inner = entry.get("hooks", [])
            if not isinstance(inner, list):
                continue
            for hook in inner:
                if isinstance(hook, Mapping) and isinstance(hook.get("command"), str):
                    commands.append(hook["command"])
    return commands


def _pyc_header(path: Path) -> tuple[bytes, int]:
    """`(magic, flags)` of a `.pyc`, straight out of the first bytes."""
    header = path.read_bytes()[:PYC_HEADER_BYTES]
    assert len(header) == PYC_HEADER_BYTES, (
        f"{path.name} is {len(header)} bytes; that is not a .pyc header"
    )
    return header[:4], int.from_bytes(header[4:8], "little")


# --- 1. the authored dependencies: fast, always run ------------------------


def test_the_repository_can_build_a_debian_package() -> None:
    """There is a build entry point, and it is executable.

    Ungated on purpose. Every assertion over a built package skips when the
    tooling is missing, and a skip that could also mean "no build script exists"
    would let this whole file report a coverage it does not have.
    """
    assert BUILD_SCRIPT.is_file(), (
        f"{BUILD_SCRIPT.relative_to(REPO_ROOT)} does not exist"
    )
    assert os.access(BUILD_SCRIPT, os.X_OK), (
        f"{BUILD_SCRIPT.relative_to(REPO_ROOT)} is not executable"
    )


@pytest.mark.parametrize("dependency", WEBVIEW_DEPENDS)
def test_the_package_requires_the_webview_runtime(dependency: str) -> None:
    """The window backend is not optional; without it `rhi` has nowhere to draw.

    `rhizome_graph/window.py` falls back to an app-mode browser, but that is a
    fallback for a machine that has no WebKitGTK, not a reason to ship a desktop
    package that never opens a desktop window.
    """
    depends = _binary_stanza().get("Depends", "")

    assert dependency in _names(depends), (
        f"{dependency} is missing from Depends: {depends!r}"
    )


def test_git_is_recommended_rather_than_required() -> None:
    """A missing `git` quiets two panels; it does not break the graph.

    `rhizome_graph/gitcmd.py` answers `None` on a missing binary, a non-zero exit
    or a timeout, and both callers read that as "nothing to show": the diff view
    falls through to text or hex, and the uncommitted-changes panel is simply not
    on screen. Everything else -- the seed, the watcher, attribution, the
    renderer -- is untouched. `Depends` would make apt refuse to install the
    package on a machine without git for a feature that degrades gracefully;
    `Recommends` installs it by default and lets it be declined.
    """
    stanza = _binary_stanza()

    assert "git" in _names(stanza.get("Recommends", "")), (
        f"git is not in Recommends: {stanza.get('Recommends', '')!r}"
    )
    assert "git" not in _names(stanza.get("Depends", "")), (
        "git is in Depends, which refuses installation over a feature that only "
        f"degrades: {stanza.get('Depends', '')!r}"
    )


def test_a_browser_is_only_suggested() -> None:
    """The fallback path, and a fallback may not drag a browser onto a server.

    The window opens through pywebview when the WebKit stack is there; the
    app-mode browser is what `window.py` reaches for when it is not. Naming one
    in `Suggests` tells a user which packages make that path work without
    installing a browser on a machine that deliberately has none.
    """
    stanza = _binary_stanza()
    suggests = _names(stanza.get("Suggests", ""))

    assert suggests, f"Suggests is empty or absent: {stanza.get('Suggests', '')!r}"
    assert any("chrom" in name or "firefox" in name for name in sorted(suggests)), (
        f"no browser named in Suggests: {sorted(suggests)}"
    )


def test_the_distribution_websockets_is_not_depended_on() -> None:
    """noble ships 10.4 and the daemon imports a subpackage that starts at 13.

    This is the negative half of the vendored virtualenv: carrying a venv *and*
    declaring `python3-websockets` would install a second, older copy and invite
    exactly the import that fails. Measured by unpacking the wheels --
    `websockets/asyncio/` has 0 files in 12.0 and 8 in 13.0.
    """
    stanza = _binary_stanza()
    declared = _names(stanza.get("Depends", "")) | _names(stanza.get("Recommends", ""))

    assert "python3-websockets" not in declared, (
        "python3-websockets is declared, but the version Debian ships has no "
        "websockets.asyncio.server; the venv is vendored precisely to avoid it"
    )


def test_watchdog_comes_from_the_distribution() -> None:
    """The other half: what does not need vendoring is not vendored.

    noble's `python3-watchdog` is 3.0.0, the suite is green on it, and
    `daemon/watcher.py` imports three names that have existed far longer. So it
    is a dependency, not a wheel inside the venv -- which keeps the package
    smaller and lets the distribution's security updates apply to it.
    """
    depends = _binary_stanza().get("Depends", "")

    assert "python3-watchdog" in _names(depends), (
        f"python3-watchdog is missing from Depends: {depends!r}"
    )


def test_the_python3_dependency_is_bounded_at_both_ends() -> None:
    """A vendored venv runs on one Python minor version and no other.

    An unbounded `Depends: python3` is the common way this class of package
    breaks: it installs cleanly today, the next release upgrade moves the system
    interpreter, and the venv's `pyvenv.cfg`, its `lib/python3.N/` tree and every
    console script inside it now point at an interpreter that is gone. Bounding
    it turns a silent breakage into an apt message.
    """
    depends = _binary_stanza().get("Depends", "")
    relations = _relations(depends, "python3")

    assert any(op in (">=", ">>", ">") for op, _ in relations), (
        f"Depends declares no lower bound on python3: {depends!r}"
    )
    assert any(op in ("<<", "<=", "<") for op, _ in relations), (
        f"Depends declares no upper bound on python3, so a release upgrade may "
        f"move the interpreter out from under the vendored venv: {depends!r}"
    )


# --- 2. the built package: opt-in ------------------------------------------


def test_building_the_package_leaves_nothing_in_the_checkout(
    package: BuiltPackage,
) -> None:
    """The build writes to the directory it was given, and nowhere else.

    A staging tree, a `dist/`, a stray `.deb` or a `.buildinfo` at the repository
    root is build output in a source tree: it lands in the next `tar`, it
    confuses the very `web/dist` search this package exists to satisfy, and it is
    9.4 MB plus a virtualenv the next `rm` has to find.
    """
    unexpected = package.checkout_after - package.checkout_before

    assert unexpected == set(), (
        f"the build left {sorted(unexpected)} in the checkout"
    )


@pytest.mark.parametrize("command", (LAUNCHER, HOOK_COMMAND), ids=lambda p: p.name)
def test_the_package_installs_the_command_executably(
    package: BuiltPackage, command: Path
) -> None:
    """Both commands land on `$PATH` with the execute bit set.

    `rhi-hook` matters more than it looks: `assets.hook_console_script()` searches
    `$PATH` for it and writes what it finds into another project's
    `.claude/settings.json`, where it is invoked on every tool call.
    """
    installed = _installed(package, command)

    assert installed.is_file(), f"{command} is not in the package"
    assert os.access(installed, os.X_OK), f"{command} is installed without +x"


def test_the_hook_command_runs_under_the_system_interpreter(
    package: BuiltPackage,
) -> None:
    """`#!/usr/bin/python3`, never the vendored venv's interpreter.

    The deferred half of the shebang rule, and the reason it is not taste. The
    hook fires on every tool call, needs no third-party dependency, and must
    survive the venv being rebuilt, upgraded or removed -- `assets.py` already
    states the same rule for the checkout path (`HOOK_INTERPRETER = "python3"`).
    A console script generated by `pip` inside `/usr/lib/rhizome-graph/venv`
    carries that venv's interpreter in its shebang, so installing the generated
    shim is exactly the mistake this pins against. When it breaks it breaks as a
    blocking error on every tool call in someone's session.
    """
    installed = _installed(package, HOOK_COMMAND)
    assert installed.is_file(), f"{HOOK_COMMAND} is not in the package"
    first_line = installed.read_text(encoding="utf-8", errors="replace").splitlines()[0]

    assert first_line.strip() == SYSTEM_INTERPRETER_SHEBANG, (
        f"{HOOK_COMMAND} is run by {first_line!r}, not by the system interpreter"
    )
    assert str(VENDORED_VENV) not in first_line, (
        f"{HOOK_COMMAND} points into the vendored virtualenv, which an upgrade "
        f"replaces: {first_line!r}"
    )


def test_the_launcher_runs_under_the_vendored_interpreter(
    package: BuiltPackage,
) -> None:
    """`rhi` is the opposite case, and the contrast is the whole design.

    The daemon needs `websockets` >= 13, which the distribution does not carry,
    so the launcher must reach the vendored virtualenv. Reading it as text rather
    than running it: executing an installed shim from an extraction directory
    would only prove it is relocatable, which it is not required to be.
    """
    installed = _installed(package, LAUNCHER)
    assert installed.is_file(), f"{LAUNCHER} is not in the package"
    text = installed.read_text(encoding="utf-8", errors="replace")

    assert str(VENDORED_VENV) in text, (
        f"{LAUNCHER} never names {VENDORED_VENV}, so the daemon it starts runs "
        "on whatever websockets the system happens to have"
    )


def test_the_package_carries_the_built_front_end(package: BuiltPackage) -> None:
    """All of `web/dist`, at `SYSTEM_WEB_DIR`, or the page is blank.

    The path is imported from `rhizome_graph/assets.py` rather than respelled:
    that constant is the second candidate `web_dist_candidates()` offers, and a
    package that installs the page one directory to the side is indistinguishable
    at runtime from a package that omitted it. Every non-map file is required --
    the 22 grammar chunks are fetched lazily on the first file a user opens, so a
    partial copy serves a page that works until it does not.
    """
    if not CHECKOUT_WEB_DIST.is_dir():
        pytest.skip("web/dist is not built here; there is no front end to compare")
    root = _installed(package, SYSTEM_WEB_DIR)
    expected = _front_end_files()

    assert root.is_dir(), f"{SYSTEM_WEB_DIR} is not in the package"
    missing = [name for name in expected if not (root / name).is_file()]
    assert missing == [], (
        f"{len(missing)} of {len(expected)} built files are absent from "
        f"{SYSTEM_WEB_DIR}, e.g. {missing[:10]}"
    )


def test_the_vendored_virtualenv_carries_the_asyncio_websockets_server(
    package: BuiltPackage,
) -> None:
    """The venv exists to carry one thing; this is that thing, on disk.

    `daemon/server.py` imports `websockets.asyncio.server`. A venv that was built
    but resolved an old websockets -- from a cache, from a system site-packages
    leaking in through `--system-site-packages` -- produces a package that
    installs and dies at daemon start, after apt reported success.
    """
    venv = _installed(package, VENDORED_VENV)
    assert venv.is_dir(), f"{VENDORED_VENV} is not in the package"

    modules = sorted(venv.glob("lib/python3.*/site-packages/websockets/asyncio/*.py"))

    assert modules, (
        "the vendored virtualenv holds no websockets/asyncio/, which is what "
        f"daemon/server.py imports; under {VENDORED_VENV}"
    )


def test_the_python3_bound_brackets_the_interpreter_the_venv_was_built_for(
    package: BuiltPackage,
) -> None:
    """The declared range admits that interpreter and neither of its neighbours.

    This is the version bound checked against the artefact instead of against the
    control file: the venv's `lib/python3.N` says which minor version it was
    built for, and the bound must admit N while excluding N-1 and N+1. A bound
    that is merely present but wrong -- copied from an older release, or written
    before the build host was upgraded -- reads as correct in review and installs
    a venv the system interpreter cannot run.
    """
    minor = _venv_python_minor(package)
    built_for = (3, minor)
    relations = _relations(_field(package, "Depends"), "python3")
    assert relations, "the built package declares no version relation on python3"

    def admits(candidate: tuple[int, ...]) -> bool:
        return all(_satisfies(candidate, op, bound) for op, bound in relations)

    assert admits(built_for), (
        f"the venv was built for python{built_for[0]}.{built_for[1]}, which the "
        f"declared range rejects: {relations}"
    )
    assert not admits((3, minor + 1)), (
        f"the declared range admits python3.{minor + 1}, which cannot run a "
        f"venv built for python3.{minor}: {relations}"
    )
    assert not admits((3, minor - 1)), (
        f"the declared range admits python3.{minor - 1}, which cannot run a "
        f"venv built for python3.{minor}: {relations}"
    )


def test_every_module_the_hook_imports_ships_byte_compiled(
    package: BuiltPackage,
) -> None:
    """`/usr/lib` is not writable, so an uncached import is an uncacheable one.

    This is the only place in the project where the interpreter cannot fix this
    for itself. In a checkout the first run writes `__pycache__` beside the
    source and every run after it is fast; installed, the sources live under
    `/usr/lib/rhizome-graph` owned by root, the hook runs as the user whose agent
    fired it, and the write that would end the recompiling fails silently. So
    the cost is not paid once, it is paid on every `Write`, `Edit`, `MultiEdit`,
    `Bash` and `Read` in every session for the life of the installation: 42.5 ms
    against 38.8 ms, measured on the build host over an 18 ms bare interpreter
    start.

    **Why the set is measured and not listed.** "There are `.pyc` files
    somewhere under the prefix" is the assertion that passes while the hook's
    own two modules are the ones left out -- `daemon/` compiles, the hot path
    does not, and the package looks done. So the set is whatever importing
    :data:`HOOK_ENTRY_MODULE` out of the *packaged* tree actually executes, and
    it grows by itself the day `hook.py` imports a sibling.

    **Why the tag, and not just the presence.** A `.pyc` is looked up by
    `<name>.cpython-3N.pyc` and CPython 3.13 will not read 3.12's file. The
    package declares `Depends: python3 (>= 3.N), python3 (<< 3.N+1)` and the
    build script derives both bounds from the interpreter that builds it, so N
    is knowable from the artefact -- and bytecode under any other tag is worse
    than none, because nothing will ever load it and the build looks finished.
    """
    minor = _venv_python_minor(package)
    prefix = _installed(package, INSTALL_PREFIX)
    sources = _packaged_sources_the_hook_imports(package)
    assert f"{HOOK_ENTRY_MODULE.replace('.', '/')}.py" in sources, (
        f"the probe did not even find {HOOK_ENTRY_MODULE} in the package: {sources}"
    )

    missing = [
        relative
        for relative in sources
        if not _cache_path(prefix / relative, minor).is_file()
    ]

    present = sorted(str(p.relative_to(prefix)) for p in prefix.rglob("*.pyc"))
    assert missing == [], (
        f"{missing} ship as plain sources with no cpython-3{minor} bytecode, so "
        "the hook recompiles them on every tool call and cannot cache the result "
        f"under {INSTALL_PREFIX}. .pyc files that are in the package: "
        f"{present[:10] or 'none at all'}"
    )


def test_the_shipped_bytecode_is_hash_based_and_unchecked(
    package: BuiltPackage,
) -> None:
    """Timestamp invalidation would give the cost straight back, silently.

    The interesting decision here, argued rather than defaulted, over PEP 552's
    three modes:

      * **timestamp** (the default) records the source's mtime and size at
        compile time, and the interpreter discards the `.pyc` when the installed
        `.py` disagrees. `dpkg` sets file mtimes from the archive, and the `.py`
        and the `.pyc` are stamped by different steps of the build; a rebuild, a
        `touch`, a filesystem with coarser timestamps or a restore from backup
        is enough. When it happens, nothing is reported: the import silently
        falls back to compiling, fails to write the cache into root-owned
        `/usr/lib`, and the package is back to where this test started while
        still containing bytecode. A hazard that is invisible when it fires is
        the one worth designing out.
      * **checked-hash** validates by reading and hashing the whole source on
        every import. That trades the failure above for an unconditional read
        and hash of every hook module on every tool call -- paid on the hot
        path, to detect somebody editing a dpkg-managed file, which is what
        `dpkg -V` is for and not what this project must spend the agent loop on.
      * **unchecked-hash** is loaded as it stands. Source and bytecode are
        replaced together by the one thing that ever writes there -- the package
        manager -- which is exactly the ownership PEP 552 describes it for.

    So: bit 0 set, bit 1 clear. Read out of the flag word rather than inferred
    from the build script's command line, because the flag word is what the
    interpreter reads.
    """
    minor = _venv_python_minor(package)
    prefix = _installed(package, INSTALL_PREFIX)
    compiled = [
        _cache_path(prefix / relative, minor)
        for relative in _packaged_sources_the_hook_imports(package)
    ]
    inspectable = [path for path in compiled if path.is_file()]
    assert inspectable, (
        "no bytecode was shipped for the hook's modules at all, so there is no "
        "invalidation mode to check; see "
        "test_every_module_the_hook_imports_ships_byte_compiled"
    )

    modes = {path.name: _pyc_header(path)[1] for path in inspectable}

    timestamp_based = sorted(n for n, f in modes.items() if not f & PYC_HASH_BASED)
    assert timestamp_based == [], (
        f"{timestamp_based} carry timestamp-based bytecode, which dpkg's own "
        "mtimes can invalidate; compile with --invalidation-mode unchecked-hash"
    )
    rechecked = sorted(n for n, f in modes.items() if f & PYC_CHECK_SOURCE)
    assert rechecked == [], (
        f"{rechecked} are checked-hash, so every tool call re-reads and re-hashes "
        "the source of a file only dpkg ever writes"
    )


def test_the_shipped_bytecode_carries_this_interpreters_magic_number(
    package: BuiltPackage,
) -> None:
    """The belt to the tag's braces, run only where it can mean anything.

    The name `hook.cpython-3N.pyc` is written by whichever interpreter compiled
    it, so tag and magic normally agree by construction -- but the tag is a file
    name and the magic is the thing the interpreter actually compares, and a
    build that copies, renames or reuses a cached artefact can separate the two.
    Skipped rather than approximated when the interpreter running the tests is
    not the minor version the package declares: `importlib.util.MAGIC_NUMBER` is
    this process's, and no other value for it is knowable here.
    """
    minor = _venv_python_minor(package)
    if sys.version_info[:2] != (3, minor):
        pytest.skip(
            f"these tests run on python{sys.version_info[0]}.{sys.version_info[1]} "
            f"and the package is built for python3.{minor}; the expected magic "
            "number is not knowable from here"
        )
    prefix = _installed(package, INSTALL_PREFIX)
    compiled = [
        _cache_path(prefix / relative, minor)
        for relative in _packaged_sources_the_hook_imports(package)
    ]
    inspectable = [path for path in compiled if path.is_file()]
    assert inspectable, (
        "no bytecode was shipped for the hook's modules at all; see "
        "test_every_module_the_hook_imports_ships_byte_compiled"
    )

    expected = importlib.util.MAGIC_NUMBER
    foreign = sorted(
        path.name for path in inspectable if _pyc_header(path)[0] != expected
    )

    assert foreign == [], (
        f"{foreign} were compiled by an interpreter this one does not share a "
        f"magic number with, so python3.{minor} will ignore them and recompile"
    )


def test_the_documentation_carries_the_hook_block_to_install(
    package: BuiltPackage,
) -> None:
    """Attribution is opt-in and manual, so the block has to ship with the package.

    Without the hook block in the observed project's settings, every event
    arrives with `agent: ""`, an empty agent never creates an actor, and the graph
    updates with nobody on camera -- indistinguishable from "no agent is working
    right now", which is the ambiguity this project has already paid hours for.
    The template must name the installed console script, not a path inside
    somebody's checkout of this repository.

    **How the fragment is found matters as much as what it says.** It is
    selected by being a JSON object with a `hooks` mapping, not by containing
    the words `hooks` and `rhi-hook` -- the sibling test
    `test_prose_about_the_hook_is_not_mistaken_for_the_settings_fragment` is
    where that story is written down, and it is the reason a README may
    document the console script without turning this test red for a reason that
    has nothing to do with the package.
    """
    doc = _installed(package, DOC_DIR)
    assert doc.is_dir(), f"{DOC_DIR} is not in the package"

    readmes = [p for p in doc.rglob("*") if p.is_file() and p.name.startswith("README")]
    fragments = _settings_fragments(doc)

    assert readmes, f"no README in {DOC_DIR}: {sorted(p.name for p in doc.iterdir())}"
    assert fragments, (
        f"no settings fragment in {DOC_DIR}: no shipped file is a JSON object "
        f"with a hooks block, only {sorted(p.name for p in doc.iterdir())}"
    )
    commands = {
        command for _path, parsed in fragments for command in _hook_commands(parsed)
    }
    assert any(str(HOOK_COMMAND) in command for command in sorted(commands)), (
        f"no shipped fragment runs {HOOK_COMMAND}, so a user copying one points "
        f"their settings at a source tree they do not have: {sorted(commands)}"
    )


def test_prose_about_the_hook_is_not_mistaken_for_the_settings_fragment(
    tmp_path: Path,
) -> None:
    """The trap the selection above used to be, pinned so it cannot come back.

    Ungated on purpose: it is a property of the selector, needs no build, and is
    the half of the story a rebuilt package would not tell for months.

    The selection this replaces was `"hooks" in text and "rhi-hook" in text`,
    and every file matching it was handed to `json.loads`. It passed only
    because today's `README.md` says `hooks` ten times and never names the
    console script -- so it was skipped, and the assertion ran against
    `claude-settings.json` alone. The day somebody documents `rhi-hook` in the
    README, which is a thing that should happen, the README is selected, the
    first line of Markdown is not JSON, and the test fails with a
    `JSONDecodeError` that reads exactly like the packaging having broken. A
    test that fails for a reason it does not test is worse than no test: it gets
    deleted, and the thing it really guarded goes with it.

    So the fragment is identified by what it *is* -- a JSON object with a
    `hooks` mapping in it -- and prose can then say anything it likes.
    """
    doc = tmp_path / "doc"
    doc.mkdir()
    (doc / "README.md").write_text(
        "# rhizome-graph\n\n"
        "Attribution needs the PostToolUse hooks block installed. Copy the\n"
        "`hooks` fragment shipped beside this file, or point your own hooks at\n"
        "the `rhi-hook` command -- `rhi --install-hooks` writes the same hooks.\n",
        encoding="utf-8",
    )
    (doc / "changelog.Debian.gz").write_bytes(b"\x1f\x8b\x08\x00rhi-hook hooks\xff")
    fragment = doc / "claude-settings.json"
    fragment.write_text(
        json.dumps({"hooks": hook_block(str(HOOK_COMMAND))}, indent=2) + "\n",
        encoding="utf-8",
    )

    found = _settings_fragments(doc)

    assert [path for path, _ in found] == [fragment], (
        "prose that happens to spell the same words as the fragment is being "
        f"read as a settings file: {[str(p) for p, _ in found]}"
    )


def test_the_packaged_files_are_owned_by_root(package: BuiltPackage) -> None:
    """Read through `dpkg-deb --contents`, which is where ownership is recorded.

    The extraction the other tests read cannot answer this -- unpacking as an
    ordinary user rewrites every owner. A package built without `fakeroot`
    carries the builder's uid, and dpkg then installs files owned by a user that
    does not exist on the target machine.
    """
    entries = _contents(package)

    assert entries, "dpkg-deb --contents printed nothing"
    foreign = [line for line in entries if " root/root " not in line]
    assert foreign == [], (
        "entries not owned by root/root (was the build run under fakeroot?):\n"
        + "\n".join(foreign[:10])
    )


def test_the_built_control_declares_what_the_source_control_declares(
    package: BuiltPackage,
) -> None:
    """What apt reads is the binary control, and it is generated, not authored.

    Every dependency test above reads `debian/control`. If the build derives
    `DEBIAN/control` by templating, by hand-editing or through `dh_gencontrol`,
    those decisions can be correct in the file under review and absent from the
    archive that ships. This is the one test that joins the two.
    """
    authored = _binary_stanza()

    for field in ("Depends", "Recommends", "Suggests"):
        declared = _names(authored.get(field, ""))
        built = _names(_field(package, field))
        assert declared <= built, (
            f"{field} names {sorted(declared - built)} in debian/control but not "
            f"in the built package: {sorted(built)}"
        )
