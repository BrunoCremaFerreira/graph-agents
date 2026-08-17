"""Contract tests (RED) for the dependency floors declared in pyproject.toml.

Motivation: `daemon/server.py` imports `websockets.asyncio.server` -- the new
asyncio implementation, not the legacy one -- while `pyproject.toml` declares
`websockets>=12` in both extras. That subpackage does not exist in 12.x. A
resolver honouring the declared floor is therefore free to install a version
that satisfies every constraint and dies on the first import, and it will do
exactly that on the machine where the resolution is done fresh: a distribution
build, a container, a `pip install` on a host that already holds an older
`websockets` for something else. Here it has never been seen, because the
virtualenv predates the question and holds a version far above the floor.

That is the whole content of packaging: a lower bound is not documentation, it
is the answer a resolver is entitled to give. A bound below what the code
imports is not conservative, it is wrong -- and the failure it buys is an
`ImportError` at daemon start, after the install reported success.

The `watchdog` floor is the same subject read in the opposite direction, and the
two live together here on purpose. A floor that is too *low* is an install that
cannot start; a floor that is too *high* is a package that cannot be built from
what a distribution ships -- Debian noble carries `python3-watchdog` 3.0.0, and
`watchdog>=4` rejects it, forcing a wheel into the vendored virtualenv the `.deb`
would otherwise not need to carry. Neither test is the general rule: each names
the measurement that fixes its own bound, and each says which way it points, so
that "at least 13" sitting beside "at most 3" cannot be read as a contradiction.

Both extras are checked in both cases. `daemon` is what a user installs; `dev` is
what CI and this suite install, and a `dev` floor below the `daemon` floor means
the tests can be run against a combination the daemon itself cannot use.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: The first `websockets` release that ships `websockets/asyncio/`, which is
#: what `daemon/server.py` imports (`from websockets.asyncio.server import ...`).
#:
#: How this was measured, so the next person can re-measure rather than trust
#: it: the wheels were downloaded and unpacked, and the files under
#: `websockets/asyncio/` counted. 12.0 has **0** -- the directory is not in the
#: distribution at all -- and 13.0 has **8**. It is deliberately a major-version
#: string and not a specific patch: the subpackage appeared with the 13 series,
#: and pinning tighter would claim a precision the measurement does not have.
WEBSOCKETS_ASYNCIO_MIN = "13"

#: The highest `watchdog` floor this codebase can justify declaring. Read as a
#: CEILING ON THE LOWER BOUND, not as a lower bound: `watchdog>=3` satisfies it
#: and `watchdog>=4` does not, which is the reverse of the constant above.
#:
#: How this was measured. A scratch virtualenv was built with `watchdog==3.0.0`
#: pinned (websockets untouched, 17.0.1), and the whole suite run against it:
#: **678 passed**, no skips, no errors -- the state of the suite at the time of
#: the measurement. Re-checked here independently: `watchdog 3.0.0` imports the
#: three names `daemon/watcher.py` actually uses -- `FileSystemEvent` and
#: `FileSystemEventHandler` from `watchdog.events`, `Observer` from
#: `watchdog.observers` -- and `tests/test_watcher.py` plus
#: `tests/test_root_switch.py` are green on it (35 passed).
#:
#: Nothing in 4.x is therefore load-bearing, and the cost of pretending
#: otherwise is concrete: Debian noble ships `python3-watchdog` 3.0.0, so a
#: floor of 4 rules the system package out and puts a wheel into the vendored
#: virtualenv the `.deb` has to carry.
#:
#: What may legitimately break this test is a *raise* of the floor -- and then
#: the person raising it names, here, the watchdog API that forced it. Raising
#: the floor to silence a red test without that name is the failure this
#: constant exists to prevent.
WATCHDOG_HIGHEST_JUSTIFIED_FLOOR = "3"

#: The extras that install the daemon's runtime dependencies. Both, because the
#: suite runs under `dev` and the user runs under `daemon`.
EXTRAS_DECLARING_WEBSOCKETS = ("daemon", "dev")

#: The same two extras, named for the other distribution so each test reads on
#: its own.
EXTRAS_DECLARING_WATCHDOG = ("daemon", "dev")

_LOWER_BOUND = re.compile(r">=\s*([0-9][0-9A-Za-z.\-_]*)")


def _requirements(extra: str) -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return list(data["project"]["optional-dependencies"][extra])


def _declared_lower_bound(extra: str, distribution: str) -> str | None:
    """The `>=` version declared for `distribution` in `extra`, if any.

    Hand-rolled rather than `packaging.requirements`: this suite runs on the
    stdlib plus pytest, and the requirement strings in this file are a handful
    of `name>=N` literals. `None` means the distribution is absent from the
    extra, or is pinned some other way -- both of which the caller must treat as
    a failure and not as "no opinion".
    """
    for requirement in _requirements(extra):
        name = re.split(r"[<>=!~\[; ]", requirement, maxsplit=1)[0].strip()
        if name.lower().replace("_", "-") != distribution:
            continue
        found = _LOWER_BOUND.search(requirement)
        return found.group(1) if found else None
    return None


def _version_key(version: str) -> tuple[int, ...]:
    """`"13.0.1"` -> `(13, 0, 1)`; anything non-numeric ends the comparison."""
    parts: list[int] = []
    for piece in version.split("."):
        digits = re.match(r"\d+", piece)
        if digits is None:
            break
        parts.append(int(digits.group()))
    return tuple(parts)


# --- 1. websockets: the floor may not be too LOW ---------------------------


@pytest.mark.parametrize("extra", EXTRAS_DECLARING_WEBSOCKETS)
def test_the_declared_websockets_floor_ships_the_asyncio_server(extra: str) -> None:
    """A floor below the import is a clean install that cannot start.

    Direction: `declared >= WEBSOCKETS_ASYNCIO_MIN`. Raising the floor always
    satisfies this test; lowering it below 13 is what breaks it.
    """
    declared = _declared_lower_bound(extra, "websockets")

    assert declared is not None, (
        f"the {extra!r} extra declares no `websockets>=` lower bound, so a "
        "resolver may install any version, including one without "
        "`websockets/asyncio/`"
    )
    assert _version_key(declared) >= _version_key(WEBSOCKETS_ASYNCIO_MIN), (
        f"the {extra!r} extra declares websockets>={declared}, but "
        f"daemon/server.py imports websockets.asyncio.server, which first "
        f"appears in {WEBSOCKETS_ASYNCIO_MIN}. An install honouring this floor "
        "succeeds and then fails at import."
    )


# --- 2. watchdog: the floor may not be too HIGH ----------------------------


@pytest.mark.parametrize("extra", EXTRAS_DECLARING_WATCHDOG)
def test_the_declared_watchdog_floor_demands_no_more_than_the_code_uses(
    extra: str,
) -> None:
    """A floor above the measurement excludes a distribution for nothing.

    Direction: `declared <= WATCHDOG_HIGHEST_JUSTIFIED_FLOOR`, the opposite of
    the websockets test above. Here it is *raising* the floor that breaks the
    test, and it should: the whole suite is green on watchdog 3.0.0 and
    `daemon/watcher.py` imports three names that have existed since long before
    4.0, so a floor of 4 asserts a requirement no code in this repository has.
    What it costs is not theoretical -- it rules out Debian's `python3-watchdog`
    and drags a wheel into the vendored virtualenv the package would ship.

    A genuine dependency on a 4.x API is a legitimate reason to fail this test.
    Naming that API in WATCHDOG_HIGHEST_JUSTIFIED_FLOOR and raising the constant
    is the fix; raising the floor alone is the mistake it guards against.
    """
    declared = _declared_lower_bound(extra, "watchdog")

    assert declared is not None, (
        f"the {extra!r} extra declares no `watchdog>=` lower bound; this test "
        "reads that bound and cannot check what is not there"
    )
    assert _version_key(declared) <= _version_key(WATCHDOG_HIGHEST_JUSTIFIED_FLOOR), (
        f"the {extra!r} extra declares watchdog>={declared}, but the suite is "
        f"green on watchdog {WATCHDOG_HIGHEST_JUSTIFIED_FLOOR}.0.0 and "
        "daemon/watcher.py imports only FileSystemEvent, FileSystemEventHandler "
        "and Observer, all of which it has. The floor excludes the version "
        "Debian ships for no measured reason; lower it, or record here the 4.x "
        "API that requires it."
    )


def test_both_extras_declare_the_same_websockets_floor() -> None:
    """What the suite is tested against must be what the daemon may be run on."""
    floors = {extra: _declared_lower_bound(extra, "websockets") for extra in EXTRAS_DECLARING_WEBSOCKETS}

    assert len(set(floors.values())) == 1, (
        f"the extras disagree on the websockets floor: {floors}; the suite would "
        "then be green against a version the daemon is not allowed to use, or "
        "the reverse"
    )
