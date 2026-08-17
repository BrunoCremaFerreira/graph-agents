"""The two policy scanners must reach the packaging files, and a suffix set stops them.

Motivation, and it is a real defect rather than a hypothetical one. This
repository enforces two rules over its authored sources by walking the tree:
`tests/test_language_policy.py` fails on non-English text a human can read, and
`tests/test_project_naming.py` fails on a stale project name or a stale
environment-variable prefix. Packaging introduces exactly the kind of file both
rules exist for -- `debian/control` carries a `Description:` a user reads in
`apt show`, a Homebrew formula carries a `desc` a user reads in `brew info`, and
both name the project, its socket, its directories and its commands in literal
strings that a rename would leave behind.

Neither scanner can see any of it, and the obvious fix does not help:

  * Both `_scanned_files()` and `_authored_files()` walk `SCANNED_DIRS` and then
    filter on `path.suffix in SCANNED_SUFFIXES`.
  * `debian/control`, `debian/rules`, `debian/changelog`, `debian/compat` and
    `debian/postinst` have **no suffix at all** -- `Path("debian/control").suffix`
    is the empty string, which is in neither suffix set.
  * `.rb` is in neither suffix set either.

So adding `"debian"` to `SCANNED_DIRS` scans **zero files** and leaves both
policy tests green over a directory they never read. That is worse than no
coverage, because the directory name sitting in the tuple reads as coverage to
the next person who checks. The tests below are written to stay red through that
fix: they assert the *files* are in the scan, not that the *directory* is in the
list, so the only way to green them is to make the walk accept a file the suffix
filter currently drops.

**What this file deliberately does not do.** It does not assert the content of
the packaging files -- the existing policy tests do that, and they will do it for
free once the walk reaches them. This file only pins the reach of the walk.

There is a third miss with a different cause, kept here because the symptom is
identical: `packaging/build-deb.sh` has a suffix both walks accept and lives in a
directory neither walks. That one really is fixed by naming the directory -- and
that is why it is worth having beside the other two, since a fix that stops
after the easy case is exactly what this file exists to prevent.

Three paths are pinned as literals, and they are decisions worth stating:

  * `debian/` is where dpkg looks; the name is not ours to choose.
  * `Formula/rhizome-graph.rb` is what makes this repository usable as a tap
    directly (`brew tap <user>/<repo> <url>` finds formulae in `Formula/`,
    `HomebrewFormula/` or the repository root). A formula filed anywhere else --
    `packaging/homebrew/` reads nicer -- has to be copied into a separate tap
    repository by hand before anyone can install it.
  * `packaging/build-deb.sh` is the build entry point `tests/test_deb_package.py`
    drives; the path is a contract between those two tests and the script.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import test_language_policy as english_policy
import test_project_naming as naming_policy

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The Debian source-package directory. Its name is fixed by dpkg.
DEBIAN_DIR = REPO_ROOT / "debian"

#: The one file every Debian package must have, and the one this project makes
#: real decisions in: `Depends`, `Recommends`, `Suggests`, and the `Description`
#: a user reads in `apt show`.
DEBIAN_CONTROL = DEBIAN_DIR / "control"

#: The Homebrew formula, at the path that makes this repository a tap.
FORMULA_DIR = REPO_ROOT / "Formula"
HOMEBREW_FORMULA = FORMULA_DIR / "rhizome-graph.rb"

#: The build entry point `tests/test_deb_package.py` drives. It is the third
#: kind of miss, and the cheapest to overlook: `.sh` IS in both suffix sets, so
#: this file is skipped purely for living in a directory neither walk names --
#: and it is a shell script that prints to a human and spells the project name,
#: the install prefix and the two commands, which is exactly what both policies
#: were written for (`start.sh` is scanned by name for the same reason).
PACKAGING_DIR = REPO_ROOT / "packaging"
DEB_BUILD_SCRIPT = PACKAGING_DIR / "build-deb.sh"

#: Every authored packaging file the policies must cover. Named individually
#: rather than globbed so that a missing one is a failure and not a silence.
PACKAGING_FILES = (DEBIAN_CONTROL, HOMEBREW_FORMULA, DEB_BUILD_SCRIPT)

#: The trees those files live in, all of which must be walked.
PACKAGING_DIRS = (DEBIAN_DIR, FORMULA_DIR, PACKAGING_DIR)

#: Generated or vendored trees a Debian build drops inside `debian/`. They are
#: build output, not authored sources, and scanning them would fail the policy
#: on whatever a dependency happens to contain.
DEBIAN_BUILD_OUTPUT = {"tmp", "files", ".debhelper", "rhizome-graph", "__pycache__"}


def _authored_packaging_files() -> list[Path]:
    """Every file a human wrote in a packaging tree, as it is on disk.

    Build output is dropped by directory name: a `dpkg-buildpackage` run leaves
    `debian/tmp`, `debian/files` and `debian/.debhelper` behind, and a test that
    demanded those be policy-clean would fail on somebody else's bytes.
    """
    found: list[Path] = []
    for base in PACKAGING_DIRS:
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if any(part in DEBIAN_BUILD_OUTPUT for part in path.relative_to(base).parts):
                continue
            found.append(path)
    return found


def _relative(paths: list[Path]) -> set[str]:
    return {str(path.relative_to(REPO_ROOT)) for path in paths}


@pytest.mark.parametrize("packaging_file", PACKAGING_FILES, ids=lambda p: p.name)
def test_the_english_policy_scan_reaches_the_packaging_file(packaging_file: Path) -> None:
    """A description a user reads in `apt show` or `brew info` is authored text.

    This fails today because the files do not exist, and for `debian/control`
    and the formula it goes on failing after `"debian"` and `"Formula"` are
    appended to `SCANNED_DIRS`, because the walk keeps only the suffixes in
    `SCANNED_SUFFIXES` and those two have `""` and `".rb"`. That second failure
    is the point of the test; `packaging/build-deb.sh` is the control case that
    the directory list alone does fix.
    """
    scanned = _relative(english_policy._scanned_files())

    assert str(packaging_file.relative_to(REPO_ROOT)) in scanned, (
        f"{packaging_file.relative_to(REPO_ROOT)} is outside the English policy "
        f"scan; it holds text a user reads. Scanned: {sorted(scanned)}"
    )


@pytest.mark.parametrize("packaging_file", PACKAGING_FILES, ids=lambda p: p.name)
def test_the_naming_policy_scan_reaches_the_packaging_file(packaging_file: Path) -> None:
    """A packaging file spells the project name more often than any source file.

    The package name, the install prefix, the vendored virtualenv path, the two
    console scripts and the ingest socket all appear here as literals. A rename
    that stops at the suffix filter leaves a package that installs under the old
    name and a hook that writes to the old socket -- the precise failure
    `test_project_naming.py` was written to catch.
    """
    scanned = _relative(naming_policy._authored_files())

    assert str(packaging_file.relative_to(REPO_ROOT)) in scanned, (
        f"{packaging_file.relative_to(REPO_ROOT)} is outside the naming scan; "
        f"a stale project name there survives every existing test"
    )


def test_no_authored_packaging_file_escapes_the_english_policy_scan() -> None:
    """Whatever the packaging ends up containing, all of it is read.

    The parametrized tests above name three files. This one is the guard against
    the fix that satisfies exactly those and nothing else: it walks what is
    actually on disk, so `debian/changelog`, `debian/rules` and `debian/postinst`
    are covered the moment they are written, without this file being edited.

    The existence assertion is deliberate. Without it a missing `debian/` makes
    the walk empty and the test vacuously green -- the same "reads as coverage"
    failure the whole file is about.
    """
    for directory in PACKAGING_DIRS:
        assert directory.is_dir(), f"{directory.relative_to(REPO_ROOT)} does not exist"

    authored = _relative(_authored_packaging_files())
    scanned = _relative(english_policy._scanned_files())

    assert authored, "no authored packaging files found to check"
    assert authored <= scanned, (
        "authored packaging files outside the English policy scan: "
        f"{sorted(authored - scanned)}"
    )


def test_no_authored_packaging_file_escapes_the_naming_scan() -> None:
    """The same guard for the rename policy, which walks a second suffix set.

    The two scanners keep their own `SCANNED_DIRS` and `SCANNED_SUFFIXES` on
    purpose -- they cover different trees for different reasons -- so fixing one
    walk does not fix the other, and a single test over one of them would report
    a coverage the other does not have.
    """
    for directory in PACKAGING_DIRS:
        assert directory.is_dir(), f"{directory.relative_to(REPO_ROOT)} does not exist"

    authored = _relative(_authored_packaging_files())
    scanned = _relative(naming_policy._authored_files())

    assert authored, "no authored packaging files found to check"
    assert authored <= scanned, (
        f"authored packaging files outside the naming scan: {sorted(authored - scanned)}"
    )


def test_the_suffix_filter_is_what_stops_the_scan_today() -> None:
    """The defect named directly, so the diagnosis outlives this file's failures.

    A reader who sees the tests above go red will reach for `SCANNED_DIRS`, since
    that is the tuple that says which trees are covered. This test says why that
    is not enough: the suffix sets hold no entry that matches a file called
    `control`, and Homebrew's `.rb` is in neither. Both must accept these files,
    however the walk is taught to do it.

    It is written against the module constants rather than against the walk
    because it is a statement about the filter, not about the tree: it is meant
    to still be readable on a day when `debian/` is full and green.
    """
    suffixes = {
        "language policy": set(english_policy.SCANNED_SUFFIXES),
        "naming policy": set(naming_policy.SCANNED_SUFFIXES),
    }

    for policy, accepted in suffixes.items():
        assert Path("debian/control").suffix in accepted, (
            f"the {policy} suffix set rejects a file with no extension, so no "
            f"file under debian/ is ever read: {sorted(accepted)}"
        )
        assert Path("Formula/rhizome-graph.rb").suffix in accepted, (
            f"the {policy} suffix set rejects .rb, so the Homebrew formula is "
            f"never read: {sorted(accepted)}"
        )
