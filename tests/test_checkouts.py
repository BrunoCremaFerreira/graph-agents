"""Contract tests (RED) for rhizome_graph.checkouts -- the downward question.

Motivation: every repository question in this codebase walks *upward*.
`repo.find_checkout_root` climbs from the observed root until it meets a `.git`,
and `status.git_status` returns `None` -- without forking anything -- the moment
that climb comes back empty. So a workspace root like `~/projects`, holding five
checkouts side by side, shows no status panel at all: not "everything is clean",
but silence, which reads exactly like a healthy repository with nothing to
report. The one downward walk that does exist, `tree.scan_tree`, prunes every
dotted directory in place, so it can never *see* a `.git` even when it steps
right over one.

This module is that missing question: "which checkouts sit below this
directory". It is filesystem-reading and pure of policy -- no `git`, no network,
no state -- which is what lets the status poll call it on every round without a
cache (measured at 0.2-0.4 ms against ~20 ms of forks it precedes).

Three properties are worth naming up front, because they are the ones a later
change is most likely to break:

  * **Upward wins, and discovery stops at what it finds.** A root that is itself
    a checkout answers `[""]` and does not go looking for more; a checkout found
    below the root is not descended into either. Otherwise a repository with a
    vendored checkout inside it would be counted twice and forked twice, and
    `git status` already reports the nested one as a single entry.
  * **Three budgets, because the walk precedes forks.** A fork is ~4 ms warm and
    its timeout is 5 s, so what has to be bounded is the worst case and not the
    measured one. `MAX_DEPTH`, `MAX_CHECKOUTS` and `MAX_SCANNED_DIRS` are pinned
    here by name and by value: they are the only thing standing between a poll
    every 3 s and a home directory with a network mount in it.
  * **Never raises, ever.** The caller is a background poll on the daemon's
    loop. An unreadable directory must cost results, never an exception.

On what "depth" counts. The table in `docs/features/todo/multi-repo-git-status.md`
justifies `MAX_DEPTH = 3` as "covers `~/projects/a` and `~/src/github.com/org/repo`",
so depth is counted here in **segments of the returned prefix**: `github.com/org/repo`
is three and must be found, `a/b/c/d` is four and must not. That reading is the
one the constant's stated purpose requires; the table's shorthand "`org/repo/.git`"
is satisfied by it too.

The module boundary is asserted structurally at the end of this file, in the same
spirit as `tests/test_daemon_environment_boundary.py` and the "no shiki outside
`highlight.ts`" rule on the front end: `checkouts.py` may reach for `repo` and
`tree` and nothing else of ours, and it forks nothing. A source-level assertion
is what stops the next change routing a `git` call through here, where a caller
that believes discovery is cheap would be paying 5 s timeouts without knowing it.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import ast
import inspect
import os
import threading
from pathlib import Path

import pytest

# The module does not exist yet: this import failing IS the first RED, and it is
# the reason every test below reports at once instead of one at a time.
from rhizome_graph import checkouts


def _make_checkout(path: Path) -> Path:
    """Create a plain repository at `path` and return it."""
    git_dir = path / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return path


def _wide_tree(root: Path, branches: int = 50, leaves: int = 100) -> int:
    """Build `branches * leaves` empty directories and report how many exist.

    Two levels rather than one long chain, so every directory in it is within
    reach of `MAX_DEPTH` and would be visited by an unbudgeted walk.
    """
    for i in range(branches):
        parent = root / f"d{i:03d}"
        parent.mkdir()
        for j in range(leaves):
            (parent / f"e{j:03d}").mkdir()
    return branches + branches * leaves


# --- 1.1 A checkout that is a direct child of the observed root -------------

def test_lists_every_child_that_holds_a_dot_git_and_no_plain_directory(tmp_path: Path):
    """Discovery finds the checkouts under a workspace root, and only those."""
    _make_checkout(tmp_path / "a")
    _make_checkout(tmp_path / "b")
    (tmp_path / "c").mkdir()

    assert checkouts.find_checkouts(str(tmp_path)) == ["a", "b"]


def test_the_prefixes_come_back_sorted_whatever_order_the_disk_reports(tmp_path: Path):
    """Sorted, so the panel's contents do not depend on inode order."""
    for name in ("zulu", "alpha", "mike"):
        _make_checkout(tmp_path / name)

    assert checkouts.find_checkouts(str(tmp_path)) == ["alpha", "mike", "zulu"]


def test_a_directory_holding_no_checkout_at_all_answers_an_empty_list(tmp_path: Path):
    """`[]` and not `None`: "nothing below here" is an answer, not a failure."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.md").write_text("hello\n", encoding="utf-8")

    assert checkouts.find_checkouts(str(tmp_path)) == []


# --- 1.2 The root is itself a checkout -------------------------------------

def test_a_root_that_is_itself_a_checkout_answers_the_empty_prefix(tmp_path: Path):
    """The empty prefix means "the observed root", which joins to nothing."""
    _make_checkout(tmp_path)

    assert checkouts.find_checkouts(str(tmp_path)) == [""]


def test_a_root_that_is_a_checkout_does_not_list_the_checkouts_inside_it(tmp_path: Path):
    """One repository, one panel: a vendored checkout is its parent's business.

    `git status` already reports a nested checkout as a single entry, so listing
    it separately would double-count it and double the forks.
    """
    _make_checkout(tmp_path)
    _make_checkout(tmp_path / "third_party" / "lib")

    assert checkouts.find_checkouts(str(tmp_path)) == [""]


def test_discovery_does_not_descend_into_a_checkout_it_just_found(tmp_path: Path):
    """The same rule one level down: a found checkout is a leaf of the walk."""
    _make_checkout(tmp_path / "a")
    _make_checkout(tmp_path / "a" / "third_party" / "lib")

    assert checkouts.find_checkouts(str(tmp_path)) == ["a"]


# --- 1.3 The depth bound ---------------------------------------------------

def test_a_checkout_three_prefix_segments_below_the_root_is_found(tmp_path: Path):
    """`~/src/github.com/org/repo` is the layout `MAX_DEPTH = 3` exists for."""
    _make_checkout(tmp_path / "github.com" / "org" / "repo")

    assert checkouts.find_checkouts(str(tmp_path)) == ["github.com/org/repo"]


def test_a_checkout_four_prefix_segments_below_the_root_is_out_of_reach(tmp_path: Path):
    """The bound is a bound: past it the walk stops rather than costing a fork."""
    _make_checkout(tmp_path / "a" / "b" / "c" / "d")

    assert checkouts.find_checkouts(str(tmp_path)) == []


def test_the_depth_bound_is_three_levels():
    """Pinned by name: the value is the reason `~/src/github.com/org/repo` works."""
    assert checkouts.MAX_DEPTH == 3


# --- 1.4 The two budgets ---------------------------------------------------

def test_twenty_sibling_checkouts_are_cut_down_to_the_checkout_budget(tmp_path: Path):
    """Each checkout costs a fork whose timeout is 5 s; the count is bounded."""
    for i in range(20):
        _make_checkout(tmp_path / f"repo{i:02d}")

    found = checkouts.find_checkouts(str(tmp_path))

    assert len(found) == checkouts.MAX_CHECKOUTS
    assert found == sorted(set(found)), "the cut must keep the list sorted and unique"


def test_the_checkout_budget_is_sixteen():
    assert checkouts.MAX_CHECKOUTS == 16


def test_the_scanned_directory_budget_is_four_thousand():
    assert checkouts.MAX_SCANNED_DIRS == 4000


def test_the_signature_defaults_are_the_module_constants():
    """The keyword defaults and the named constants cannot drift apart.

    A caller reads `max_depth=3` from the signature and a maintainer retunes
    `MAX_DEPTH`; if they are two literals, only one of them moves.
    """
    defaults = {
        name: parameter.default
        for name, parameter in inspect.signature(checkouts.find_checkouts).parameters.items()
        if parameter.default is not inspect.Parameter.empty
    }

    assert defaults == {
        "max_depth": checkouts.MAX_DEPTH,
        "max_checkouts": checkouts.MAX_CHECKOUTS,
        "max_dirs": checkouts.MAX_SCANNED_DIRS,
    }


def test_a_tree_of_five_thousand_empty_directories_neither_raises_nor_hangs(tmp_path: Path):
    """The pathological tree the directory budget exists for.

    Run on a thread and joined with a timeout, because "does not hang" cannot be
    asserted from inside the call that would be hanging.
    """
    _wide_tree(tmp_path)
    result: list[list[str]] = []
    worker = threading.Thread(
        target=lambda: result.append(checkouts.find_checkouts(str(tmp_path))),
        daemon=True,
    )

    worker.start()
    worker.join(timeout=20)

    assert not worker.is_alive(), "find_checkouts did not finish over 5000 empty directories"
    assert result == [[]], "no checkout exists in that tree, so nothing may be reported"


def test_the_walk_opens_no_more_directories_than_its_budget_allows(tmp_path: Path, monkeypatch):
    """The budget is spent on directories opened, and it actually binds.

    Counting the reads is the only way this bound is observable at all: over a
    tree with no checkout in it, a walk that respects the budget and a walk that
    reads all 5050 directories return the same empty list. The count is taken at
    `os.scandir` and `os.listdir` together so the assertion survives the walk
    being written either way (`os.walk` resolves `scandir` through this same
    module global, so it is counted too).
    """
    created = _wide_tree(tmp_path)
    visits: list[str] = []
    real_scandir, real_listdir = os.scandir, os.listdir

    def counting_scandir(path="."):
        visits.append(str(path))
        return real_scandir(path)

    def counting_listdir(path="."):
        visits.append(str(path))
        return real_listdir(path)

    monkeypatch.setattr(os, "scandir", counting_scandir)
    monkeypatch.setattr(os, "listdir", counting_listdir)

    checkouts.find_checkouts(str(tmp_path))

    assert len(visits) <= checkouts.MAX_SCANNED_DIRS + 1, (
        f"the walk opened {len(visits)} directories of the {created} present; "
        f"MAX_SCANNED_DIRS is {checkouts.MAX_SCANNED_DIRS} (+1 for the root)"
    )


# --- 1.5 The shapes a real disk has ----------------------------------------

def test_a_dot_git_file_marks_a_checkout_just_as_a_directory_does(tmp_path: Path):
    """A worktree and a submodule carry a `.git` *file* pointing elsewhere.

    `repo._find_dot_git` accepts both shapes, and so must this: a worktree is a
    working tree with a status like any other.
    """
    worktree = tmp_path / "a"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /elsewhere/.git/worktrees/a\n", encoding="utf-8")

    assert checkouts.find_checkouts(str(tmp_path)) == ["a"]


def test_a_symlinked_directory_is_not_followed_into(tmp_path: Path):
    """Following links duplicates checkouts and invites a loop that never ends."""
    _make_checkout(tmp_path / "real")
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)

    found = checkouts.find_checkouts(str(tmp_path))

    assert found == ["real"], "a symlinked directory must not be walked as a second checkout"


def test_an_unreadable_directory_costs_results_but_never_an_exception(tmp_path: Path):
    """The caller is a background poll: one bad permission bit must not kill it."""
    if os.geteuid() == 0:
        pytest.skip("root reads through any permission bits")
    _make_checkout(tmp_path / "visible")
    locked = tmp_path / "locked"
    _make_checkout(locked / "hidden")
    locked.chmod(0o000)

    try:
        found = checkouts.find_checkouts(str(tmp_path))
    finally:
        locked.chmod(0o700)

    assert found == ["visible"]


# --- 1.6 owning_checkout: which checkout owns this path --------------------

def test_names_the_sub_checkout_that_owns_a_file_below_the_root(tmp_path: Path):
    """The question a click asks: which working tree should `git diff` run in.

    Compared through `realpath` on both sides because the answer is derived from
    an absolute path, and a temporary directory reached through a symlinked
    component (`/tmp` is one on more than one platform) would otherwise never
    match a path a test spelled out by hand.
    """
    checkout = _make_checkout(tmp_path / "a")
    target = checkout / "src" / "x.py"
    target.parent.mkdir(parents=True)
    target.write_text("print(1)\n", encoding="utf-8")

    owner = checkouts.owning_checkout(str(tmp_path), str(target))

    assert owner is not None
    assert os.path.realpath(owner) == os.path.realpath(str(checkout))


def test_a_checkout_above_the_observed_root_owns_nothing_inside_it(tmp_path: Path):
    """Above the root is out of scope: that case is today's single-repo path.

    Answering with the checkout here would hand `git diff` a working directory
    the viewer never agreed to observe.
    """
    checkout = _make_checkout(tmp_path / "a")
    observed = checkout / "src"
    observed.mkdir()
    target = observed / "x.py"
    target.write_text("print(1)\n", encoding="utf-8")

    assert checkouts.owning_checkout(str(observed), str(target)) is None


def test_a_root_that_is_itself_the_checkout_owns_its_own_files(tmp_path: Path):
    """At-or-under includes "at": the root is a legitimate owner."""
    _make_checkout(tmp_path)
    target = tmp_path / "src" / "x.py"
    target.parent.mkdir(parents=True)
    target.write_text("print(1)\n", encoding="utf-8")

    owner = checkouts.owning_checkout(str(tmp_path), str(target))

    assert owner is not None
    assert os.path.realpath(owner) == os.path.realpath(str(tmp_path))


# --- The module boundary, asserted over the source -------------------------

#: The only modules of ours `checkouts.py` may reach for. `repo` owns the
#: upward walk and the two `.git` shapes; `tree` owns which directory names are
#: noise. Anything else is a layer this module has no business knowing about --
#: `status` would drag the porcelain parser in, and `gitcmd` would turn a walk
#: measured in tenths of a millisecond into something that can block for 5 s.
FIRST_PARTY_IMPORTS_ALLOWED = {"repo", "tree"}

#: Every spelling of "start a process". The plan's claim is that discovery
#: "forks nothing"; that claim is what allows the status poll to call it on the
#: event loop's thread before deciding whether any fork is warranted at all.
FORKING_NAMES = (
    "subprocess",
    "multiprocessing",
    "popen",
    "system",
    "fork",
    "execv",
    "execvp",
    "spawnv",
    "spawnl",
    "gitcmd",
)


def _source() -> str:
    return Path(checkouts.__file__).read_text(encoding="utf-8")


#: The packages that are ours. An import whose head is one of these is a layer
#: decision; everything else is the standard library, which this module is free
#: to use (it reads directories, so it needs `os`).
OUR_PACKAGES = frozenset({"rhizome_graph", "daemon", "hooks"})


def _first_party_imports(module: ast.Module) -> set[str]:
    """The names `checkouts.py` pulls out of this project, however spelled.

    All four forms collapse to the same answer, because all four are how this
    boundary would most naturally be crossed: `from rhizome_graph import status`,
    `from rhizome_graph.status import parse_status`, `import rhizome_graph.status`
    and the relative `from .status import parse_status`.
    """
    imported: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level == 0 and base.split(".")[0] not in OUR_PACKAGES:
                continue
            tail = base.split(".")[-1] if base else ""
            if tail and tail not in OUR_PACKAGES:
                imported.add(tail)
            else:
                imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] in OUR_PACKAGES:
                    imported.add(parts[-1])
    return imported


def _identifiers(module: ast.Module) -> set[str]:
    """Every name the code *uses*: bare names, attributes and imported modules.

    Identifiers rather than raw text on purpose. The module's own docstring is
    expected to say that it forks nothing -- the plan asks for exactly that
    sentence -- and a substring search over the source would then fail on the
    promise instead of on a breach of it.
    """
    names: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.update(alias.name.split("."))
                if alias.asname:
                    names.add(alias.asname)
        elif isinstance(node, ast.ImportFrom):
            names.update((node.module or "").split("."))
            names.update(alias.name for alias in node.names)
    return names


def test_checkouts_reaches_for_repo_and_tree_and_nothing_else_of_ours():
    """The boundary written down, not merely intended."""
    imported = _first_party_imports(ast.parse(_source()))

    assert imported <= FIRST_PARTY_IMPORTS_ALLOWED, (
        "rhizome_graph/checkouts.py imports "
        f"{sorted(imported - FIRST_PARTY_IMPORTS_ALLOWED)} from this project. "
        "Discovery answers a path question over the filesystem; it may know the "
        "upward walk (repo) and which directories are noise (tree), and nothing else."
    )


def test_checkouts_never_starts_a_process():
    """No `git`, by construction rather than by convention.

    Asserted over every identifier in the parsed source, so a late
    `import subprocess` inside a function -- the form this leaks back in,
    because it changes no import block a reviewer skims -- is caught the same
    as a lone `os.popen`.
    """
    used = _identifiers(ast.parse(_source()))

    offenders = sorted(used & set(FORKING_NAMES))

    assert offenders == [], (
        f"rhizome_graph/checkouts.py names {offenders}. Discovery forks "
        "nothing: the status poll calls it on every round precisely because it "
        "is 50-100x cheaper than the forks it decides on, and gitcmd stays the "
        "one place in this project where a process is started."
    )
