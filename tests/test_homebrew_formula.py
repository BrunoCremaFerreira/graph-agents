"""The Homebrew formula, checked as text because nothing here can run `brew`.

Motivation. The macOS half of the installation story goes wrong in a way that
has nothing to do with macOS: `web/dist/` is gitignored, and the default thing a
formula does is fetch a GitHub tag archive -- which is generated from the git
tree and therefore carries no built front end at all. `brew install` then
succeeds, `rhi` starts, the daemon binds, and the page is blank, because
`default_web_dist()` matched no candidate and `None` is a documented,
non-erroring state. That is the same defect as
`tests/test_distribution_front_end.py`, arriving through a different door.

**This host cannot verify a formula.** There is no `brew`, no `ruby`, and no mac
anywhere in reach. What that rules out is most of the truth:

  * `brew audit --strict --online` -- the style, metadata, mirror, licence and
    dependency rules an official tap enforces. Nothing here approximates it; the
    two style assertions below cover a handful of its rules and nothing more.
  * `brew install --build-from-source rhizome-graph` -- that the resources
    resolve, that the sdist builds against the Homebrew Python, that
    `virtualenv_install_with_resources` produces working shims.
  * `brew test rhizome-graph` -- the only check that the installed `rhi` runs and
    that the page it serves is really there.
  * That the formula is even syntactically valid Ruby. It is read here as text.
  * `brew uninstall`, upgrade behaviour, and the bottling that a tap normally
    relies on.

So this file pins the things that are decisions rather than syntax: where the
formula lives, what it fetches, that it does not need Node, and that it names the
dependency whose version the daemon actually cares about. Everything else waits
for a mac, and that must be said out loud rather than approximated with a fake
check -- a green suite that implied `brew audit` had passed would be worse than
no coverage.

**Why `Formula/rhizome-graph.rb` and not `packaging/homebrew/`.** Homebrew finds
formulae in a tap at `Formula/`, `HomebrewFormula/` or the repository root. At
that path this repository *is* a tap: `brew tap <user>/rhizome-graph <url>` then
`brew install <user>/rhizome-graph/rhizome-graph` works with no second
repository and no copying by hand.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from rhizome_graph.assets import WEB_DIST_ENV

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The tap layout: `Formula/<name>.rb`, matching the binary package name.
FORMULA = REPO_ROOT / "Formula" / "rhizome-graph.rb"

#: Homebrew derives the class from the file name: hyphens become word breaks and
#: each word is capitalised. `rhizome-graph.rb` therefore has to declare
#: `class RhizomeGraph`, and a mismatch is a load error, not a warning.
FORMULA_CLASS = "RhizomeGraph"

#: The dependency whose version is not cosmetic: `daemon/server.py` imports
#: `websockets.asyncio.server`, which first ships in the 13 series. On macOS
#: there is no distribution package to lean on, so it must be a formula resource.
WEBSOCKETS_MIN = (13,)

#: The other runtime dependency. `pyproject.toml` floors it at 3 so that Debian's
#: `python3-watchdog` stays usable; a Homebrew resource has no such constraint,
#: but it does have to exist, since nothing else installs it on a mac.
REQUIRED_RESOURCES = ("websockets", "watchdog")

#: `resource "websockets" do` ... `url ".../websockets-13.1.tar.gz"`.
RESOURCE = re.compile(r'resource\s+"([^"]+)"\s+do(.*?)\n\s*end', re.DOTALL)
URL = re.compile(r'^\s*url\s+"([^"]+)"', re.MULTILINE)
FIELD = re.compile(r'^\s*(desc|homepage|license|sha256)\s+"([^"]*)"', re.MULTILINE)

#: The version in a PyPI sdist file name: `websockets-13.1.tar.gz`.
SDIST_VERSION = re.compile(r"-(\d+(?:\.\d+)*)\.(?:tar\.gz|zip|tar\.bz2)$")

#: A GitHub archive generated from a git tag or branch. It contains exactly what
#: `git archive` would, so everything in `.gitignore` -- `web/dist` included --
#: is absent from it.
GIT_ARCHIVE = re.compile(r"/archive/(refs/(tags|heads)/)?")

#: A built distribution of this project, under either spelling of the name that
#: setuptools may produce.
PROJECT_SDIST = re.compile(r"rhizome[-_]graph-[^/]*\.(tar\.gz|whl)$")

ARTICLES = ("a ", "an ", "the ")


def _formula_text() -> str:
    assert FORMULA.is_file(), (
        f"{FORMULA.relative_to(REPO_ROOT)} does not exist; there is no formula "
        "for anyone to tap"
    )
    return FORMULA.read_text(encoding="utf-8")


def _fields(text: str) -> dict[str, str]:
    """The formula's top-level string fields. Resource bodies are excluded.

    A resource has its own `url` and `sha256`, and confusing one for the
    formula's own is how a test ends up asserting against a dependency's
    metadata. `FIELD` covers only the fields no resource declares.
    """
    return {name: value for name, value in FIELD.findall(text)}


def _resources(text: str) -> dict[str, str]:
    return {name: body for name, body in RESOURCE.findall(text)}


def _main_url(text: str) -> str:
    """The first `url` in the file, which is the formula's own.

    Homebrew puts the formula's `url` above every `resource` block by
    convention and by readability; taking the first is therefore right, and if a
    formula ever inverted that order the resource tests below would notice.
    """
    found = URL.search(text)
    assert found, "the formula declares no url at all"
    return found.group(1)


def _version(url: str) -> tuple[int, ...]:
    found = SDIST_VERSION.search(url)
    assert found, f"no version in {url!r}"
    return tuple(int(part) for part in found.group(1).split("."))


def test_the_formula_sits_where_a_tap_looks_for_it() -> None:
    """`Formula/rhizome-graph.rb`, so this repository can be tapped directly."""
    assert FORMULA.is_file(), f"{FORMULA.relative_to(REPO_ROOT)} does not exist"


def test_the_formula_class_matches_its_file_name() -> None:
    """Homebrew derives one from the other; a mismatch fails to load.

    This is the cheapest possible stand-in for the Ruby parse nobody here can
    run, and it catches the specific error a formula copied from another project
    carries: the right file name around the wrong class.
    """
    text = _formula_text()

    assert re.search(rf"^class\s+{FORMULA_CLASS}\s*<\s*Formula\b", text, re.MULTILINE), (
        f"the formula does not declare `class {FORMULA_CLASS} < Formula`"
    )


def test_the_formula_carries_the_metadata_a_tap_requires() -> None:
    """`desc`, `homepage`, `license`, and a `url` with a checksum beside it.

    Not style: a formula without `sha256` fetches whatever the server sends
    today, which is the one dependency-supply-chain check Homebrew does for free.
    """
    text = _formula_text()
    fields = _fields(text)

    for name in ("desc", "homepage", "license"):
        assert fields.get(name), f"the formula declares no {name}"
    assert _main_url(text), "the formula declares no url"
    assert fields.get("sha256"), (
        "the formula declares no sha256, so the download is unverified"
    )


def test_the_description_reads_the_way_an_audit_wants() -> None:
    """Two of `brew audit --strict`'s documented `desc` rules, applied by hand.

    Only two, and they are the ones a formula written by someone who has never
    run the audit always trips: no leading article, and a length that fits the
    one-line listing. The audit itself is unrun here -- it checks far more, and a
    green result on this test says nothing about the rest of it.
    """
    desc = _fields(_formula_text()).get("desc", "")

    assert desc, "the formula declares no desc"
    assert not desc.lower().startswith(ARTICLES), (
        f"a desc may not open with an article: {desc!r}"
    )
    assert len(desc) <= 80, f"desc is {len(desc)} characters, over the 80 limit: {desc!r}"


def test_the_formula_declares_the_runtime_dependencies_as_resources() -> None:
    """On a mac nothing else installs them; there is no distribution package.

    The `.deb` can lean on `python3-watchdog` and only vendors websockets. A
    formula has no such option: both arrive as resources or the daemon starts
    and dies on its first import.
    """
    resources = _resources(_formula_text())

    missing = [name for name in REQUIRED_RESOURCES if name not in resources]
    assert missing == [], (
        f"the formula declares no resource for {missing}; it declares {sorted(resources)}"
    )


def test_the_websockets_resource_ships_the_asyncio_server() -> None:
    """The floor that is not cosmetic, restated where a mac would read it.

    `pyproject.toml` says `websockets>=13` and `tests/test_packaging.py` explains
    why: `websockets/asyncio/` holds 0 files in 12.0 and 8 in 13.0, measured by
    unpacking the wheels, and `daemon/server.py` imports from it. A formula pins
    one exact version, so it is the one place where that floor can be silently
    undercut by a copy-paste from an older revision.
    """
    resources = _resources(_formula_text())
    assert "websockets" in resources, "the formula declares no websockets resource"
    url = URL.search(resources["websockets"])
    assert url, "the websockets resource declares no url"

    version = _version(url.group(1))

    assert version >= WEBSOCKETS_MIN, (
        f"the formula pins websockets {version}, which has no "
        f"websockets.asyncio.server; daemon/server.py imports it"
    )


def test_the_formula_does_not_build_the_front_end_at_install_time() -> None:
    """No Node, no npm, no Vite: 9.4 MB of built page must arrive prebuilt.

    A `depends_on "node"` would put a toolchain on every user's machine to
    reproduce an artefact that CI has already produced, make the install minutes
    long, and make it fail on a network that will not reach the npm registry --
    for a program that never runs Node at runtime. It also silently changes what
    is served: a rebuild here is a different bundle from the one the release was
    tested with.
    """
    text = _formula_text()

    assert 'depends_on "node"' not in text, "the formula depends on node"
    assert not re.search(r"\bnpm\b", text), "the formula runs npm during install"
    assert not re.search(r"\bvite\b", text, re.IGNORECASE), (
        "the formula runs a front-end build during install"
    )


def test_the_formula_does_not_fetch_a_git_archive() -> None:
    """A tag tarball is `git archive`, and `web/dist` is gitignored.

    This is the whole macOS half of the blank-page defect. `url
    ".../archive/refs/tags/v1.0.tar.gz"` is the line every formula starts with,
    it downloads and builds and installs perfectly, and what it installs cannot
    serve a page. The formula has to fetch something that was *built* -- a PyPI
    sdist or a release asset produced by `python -m build`, both of which carry
    `rhizome_graph/web/` (pinned by tests/test_distribution_front_end.py).
    """
    url = _main_url(_formula_text())

    assert not GIT_ARCHIVE.search(url), (
        f"the formula fetches a git archive, which contains no built front end "
        f"because web/dist is gitignored: {url}"
    )


def test_the_formula_provides_the_page_by_one_of_the_two_supported_routes() -> None:
    """Either the distribution carries the page, or the formula says where it is.

    `rhizome_graph/assets.py` offers a macOS install exactly two candidates: the
    `RHIZOME_WEB_DIST` override, and `package_root / "web"` -- the latter being
    satisfied for free when the formula installs a distribution that already
    carries `rhizome_graph/web/`. `/usr/lib/rhizome-graph/web` is Debian's and
    `repo_root/web/dist` needs a checkout, so neither applies here.

    Anything else -- installing the page into `libexec/web`, into `pkgshare`,
    into `prefix/"web"` -- puts bytes on the disk that the daemon never opens,
    and looks from the outside exactly like not shipping them.
    """
    text = _formula_text()
    url = _main_url(text)

    carried = bool(PROJECT_SDIST.search(url))
    pointed = WEB_DIST_ENV in text

    assert carried or pointed, (
        "the formula fetches "
        f"{url!r}, which is not a built distribution of this project, and never "
        f"names {WEB_DIST_ENV}; so nothing tells the daemon where the page is"
    )


@pytest.mark.parametrize("command", ("rhi", "rhi-hook"))
def test_the_formula_test_block_exercises_both_installed_commands(command: str) -> None:
    """`brew test` is the gate nobody here can run, so it has to be worth running.

    Both commands, because `rhi-hook` is the one that gets forgotten and it is as
    load-bearing as the launcher: `assets.hook_console_script()` searches `$PATH`
    for it and writes what it finds into another project's
    `.claude/settings.json`, where it fires on every tool call. A formula whose
    `test do` only starts `rhi` passes `brew test` on a machine where the hook
    was never linked.

    Spelling is left free -- `bin/"rhi"` and `#{bin}/rhi` are both idiomatic --
    so the search is for the command as a whole word, which is also what keeps
    `rhi` from matching inside `rhi-hook` or `rhizome`.
    """
    text = _formula_text()

    assert re.search(r"^\s*test do\b", text, re.MULTILINE), (
        "the formula has no `test do` block"
    )
    block = text.split("test do", 1)[1]
    assert re.search(rf"(?<![\w-]){re.escape(command)}(?![\w-])", block), (
        f"the formula's test block never runs {command!r}"
    )
