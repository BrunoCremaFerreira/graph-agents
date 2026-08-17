"""Contract tests (RED) for which window `rhi` opens, decided without a display.

Motivation: `rhi <dir>` has to open a webview window -- not a browser tab -- so
that a program that happens to be written in web technology feels like an
application of the operating system. Whether that window is a pywebview one or a
Chromium in app mode is **not settled**, and cannot be settled here: this host is
a tty with no `DISPLAY` and no WebKit2 namespace, so no agent can run the spike
that decides it. On Linux, pywebview, Tauri and Go-webview all bind the same
WebKitGTK, which is why the choice of shell *language* retires no rendering risk
and an app-mode browser is the only real hedge.

So the code is shaped so that the spike's outcome changes **which backend is
chosen** and **which dependency ships**, and nothing else. That is what these
specify:

  * the decision is a pure function over an injected availability, so no test
    ever needs a display, a browser or pywebview installed;
  * the preference `auto` expresses today is one table in this file, named
    below, and it is the only thing a spike result is allowed to edit;
  * the window module is **forbidden from touching the control token**, and that
    is asserted over its parsed source rather than left as a comment.

The strategy's parameter list is an exhaustive allow-list, and it grew by one --
`close_requested`, the channel that lets a stopping daemon ask an open window to
go away (`tests/test_window_close_request.py` argues the shape). It is still an
allow-list, and the third name is a `threading.Event`, which carries nothing.

**Why the token rule needs teeth.** The daemon injects the control token into
the `index.html` it serves, keyed on nothing but the request path -- so a webview
issuing `GET /` from loopback inherits it exactly as a browser does, and a
window helper needs no token handling whatsoever. A helper that *accepted* one
would be a second place the credential lives, and a chokepoint with a second
entrance is not a chokepoint. Three assertions make it real: the URL is a bare
origin (nothing can be smuggled through it), a strategy takes a URL and a
close-callback and nothing else, and `rhizome_graph/window.py` does not import
`rhizome_graph.token` -- the same structural form as the front end's "no shiki
outside `highlight.ts`" rule.

**`page_url` stays in `rhizome_graph/cli.py`, and is reused rather than moved.**
`_announcement` prints it and `run()` announces it, and neither of those is
allowed to depend on a module that will grow a GUI import; `cli.py` may not name
anything from the daemon side either. So the spelling lives where the two
callers already reach, and the no-query/no-fragment property is pinned there.

**Opening the real window is not specified here, and must not be faked.** No
unit test can assert that a GL surface appeared on somebody's desktop; a test
that mocked WebKitGTK would certify the mock. What stands in for one is the
spike harness, re-run against the shipped code on a real Linux desktop session
and on macOS, and it is the only evidence that the window renders. What *is*
pinned without a display is everything around it: that a machine with no
pywebview can still run `rhi` (the library is never imported unless it is the
backend that was chosen), and that the app-mode argv carries `--app=<url>` and a
private `--user-data-dir`, which is what forces a dedicated browser process that
exits with its own window instead of joining one already running.

The module under specification is imported per test rather than at file level:
while it does not exist, a top-level import fails at *collection* and takes the
whole file's report with it, where this way each test fails on its own line
naming what is missing.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest

from rhizome_graph.cli import build_parser, page_url

REPO_ROOT = Path(__file__).resolve().parent.parent

WINDOW_SOURCE = REPO_ROOT / "rhizome_graph" / "window.py"

#: The three answers there are. `none` is an answer, not a failure: it is what
#: `--no-window` asks for and what a headless machine truthfully reports.
BACKENDS = ("webview", "app_browser", "none")

#: What a caller may ask for. `auto` is plain `rhi`, `none` is `--no-window`,
#: `window` is an explicit `--window` -- and the difference between the last two
#: and `auto` is the same one the port and the ingest socket already draw: a
#: default may be adjusted, an explicit request may not.
REQUESTS = ("auto", "none", "window")

PLATFORMS = ("linux", "darwin")

# ---------------------------------------------------------------------------
# THE ONE TABLE A SPIKE RESULT MAY EDIT.
#
# With both backends available, this is what `auto` picks per platform. Today it
# is `webview` everywhere, because the requirement is a window and not a browser
# being launched. If the spike finds WebKitGTK unusable on Linux -- a blank GL
# surface, a bloom pass that will not composite, a crash on a real desktop
# session -- flip that one entry to `app_browser` and this file is correct
# again. Exactly one test reads this table (`test_auto_prefers_...`); every
# other test below is about fallback, refusal or shape, and none of them encodes
# a preference.
# ---------------------------------------------------------------------------
PREFERRED_WHEN_BOTH_AVAILABLE = {"linux": "webview", "darwin": "webview"}

BOTH = frozenset({"webview", "app_browser"})
NEITHER: frozenset[str] = frozenset()


def window():
    """The module under specification -- see the note in the file docstring."""
    return importlib.import_module("rhizome_graph.window")


def window_tree() -> ast.Module:
    assert WINDOW_SOURCE.exists(), f"there is no {WINDOW_SOURCE}"
    return ast.parse(WINDOW_SOURCE.read_text(encoding="utf-8"))


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


# --- 1. the flags that express the request ----------------------------------


def test_the_parser_accepts_an_explicit_window_request() -> None:
    """`--no-window` alone cannot say "I meant it" -- and D5 needs to know."""
    args = build_parser().parse_args(["--window"])

    assert args.window is True


def test_asking_for_no_window_is_not_asking_for_one() -> None:
    """The default is `auto`: neither flag given means neither was meant."""
    args = build_parser().parse_args([])

    assert args.window is False


def test_the_two_window_flags_cannot_both_be_given(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A command line that asks for a window and for no window is a typo.

    Refused by argparse as a conflict rather than resolved by a precedence rule
    nobody would remember. The refusal *message* is asserted, not just the exit
    status: today `--window` does not exist at all, and "unrecognized argument"
    is also a status 2 -- so the status alone would be a green that means
    nothing.
    """
    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["--window", "--no-window"])

    assert raised.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


# --- 2. the decision, with availability injected ----------------------------


@pytest.mark.parametrize("platform", PLATFORMS)
def test_auto_prefers_the_backend_this_project_prefers_today(platform: str) -> None:
    """THE preference assertion. See the table above; the spike may edit it."""
    chosen = window().choose_window_backend(
        platform=platform, available=BOTH, requested="auto"
    )

    assert chosen == PREFERRED_WHEN_BOTH_AVAILABLE[platform]


@pytest.mark.parametrize("platform", PLATFORMS)
def test_auto_falls_back_to_an_app_mode_browser_with_no_webview_installed(
    platform: str,
) -> None:
    """No pywebview is the ordinary case: it is an optional dependency."""
    chosen = window().choose_window_backend(
        platform=platform, available=frozenset({"app_browser"}), requested="auto"
    )

    assert chosen == "app_browser"


@pytest.mark.parametrize("platform", PLATFORMS)
def test_auto_uses_a_webview_when_there_is_no_browser_to_borrow(
    platform: str,
) -> None:
    """The mirror of the case above, so neither is the whole rule by accident."""
    chosen = window().choose_window_backend(
        platform=platform, available=frozenset({"webview"}), requested="auto"
    )

    assert chosen == "webview"


@pytest.mark.parametrize("platform", PLATFORMS)
def test_auto_chooses_nothing_when_nothing_can_open_a_window(platform: str) -> None:
    """A headless server, an SSH session, a container -- and this very host."""
    chosen = window().choose_window_backend(
        platform=platform, available=NEITHER, requested="auto"
    )

    assert chosen == "none"


@pytest.mark.parametrize("platform", PLATFORMS)
def test_no_window_chooses_nothing_however_much_is_available(platform: str) -> None:
    """`--no-window` is an instruction, not a hint: availability is irrelevant."""
    chosen = window().choose_window_backend(
        platform=platform, available=BOTH, requested="none"
    )

    assert chosen == "none"


@pytest.mark.parametrize("platform", PLATFORMS)
def test_an_explicit_request_still_answers_none_when_nothing_is_available(
    platform: str,
) -> None:
    """This function answers what *can* be opened, never what should be done.

    An explicit `--window` that cannot be honoured has to end the run loudly
    (`tests/test_window_lifecycle.py`), and that is the caller's decision --
    it holds the request. Folding the two together here would leave a pure
    function with an exit code in it.
    """
    chosen = window().choose_window_backend(
        platform=platform, available=NEITHER, requested="window"
    )

    assert chosen == "none"


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("requested", REQUESTS)
@pytest.mark.parametrize(
    "available", [NEITHER, frozenset({"webview"}), frozenset({"app_browser"}), BOTH]
)
def test_the_answer_is_always_one_of_the_three_backend_names(
    platform: str, requested: str, available: frozenset
) -> None:
    """Total over the whole table: no `None`, no exception, no fourth answer."""
    chosen = window().choose_window_backend(
        platform=platform, available=available, requested=requested
    )

    assert chosen in BACKENDS


def test_a_backend_is_never_chosen_that_was_not_available() -> None:
    """The guard the fallback rules would otherwise only imply."""
    for platform in PLATFORMS:
        for requested in REQUESTS:
            for available in (NEITHER, frozenset({"webview"}), frozenset({"app_browser"})):
                chosen = window().choose_window_backend(
                    platform=platform, available=available, requested=requested
                )

                assert chosen == "none" or chosen in available, (
                    f"{platform}/{requested} with {sorted(available)} chose {chosen}"
                )


# --- 3. the URL the window is given -----------------------------------------


def test_the_page_url_is_a_bare_origin() -> None:
    """No query, no fragment: nothing can be smuggled to a window through it."""
    url = page_url("127.0.0.1", 8080)
    parsed = urlparse(url)

    assert parsed.query == "" and parsed.fragment == "", url
    assert "?" not in url and "#" not in url, url


def test_the_page_url_of_a_wildcard_bind_is_still_a_bare_origin() -> None:
    """The branch that rewrites the host must not grow anything onto the URL."""
    url = page_url("0.0.0.0", 9000)

    assert "?" not in url and "#" not in url, url


# --- 4. the window module holds no credential -------------------------------


def test_the_window_module_never_imports_the_control_token() -> None:
    """One place a token lives, and the window is not it.

    Structural, like the "no shiki outside `highlight.ts`" rule: a behavioural
    test can prove no token is passed today, and only this stops the next reader
    from reaching for one.
    """
    imported = imported_modules(window_tree())

    offenders = sorted(
        name
        for name in imported
        if name == "rhizome_graph.token" or name.endswith(".token") or name == "token"
    )

    assert offenders == [], (
        f"rhizome_graph/window.py imports {offenders}. The window inherits the "
        "control token by fetching the page the daemon injects it into; a "
        "window helper that can name a token is a second place the credential "
        "lives."
    )


def test_no_function_in_the_window_module_takes_a_token() -> None:
    """The same rule stated over the surface rather than over the imports.

    A parameter is how a credential would actually arrive: `open_window(url,
    on_closed, token=...)` imports nothing and defeats the whole property.
    """
    forbidden = {"token", "secret", "credential"}
    offenders = []
    for node in ast.walk(window_tree()):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        arguments = node.args
        parameters = [
            argument.arg
            for argument in (
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
            )
        ]
        offenders.extend(
            f"{node.name}({name})" for name in parameters if name in forbidden
        )

    assert offenders == [], offenders


#: Every parameter a strategy may have, and there are no others. An exhaustive
#: allow-list rather than a count: what it exists to stop is a credential
#: arriving as a fourth argument, and `close_requested` (see
#: `tests/test_window_close_request.py`) is an inert `threading.Event` that
#: carries nothing.
STRATEGY_PARAMETERS = ["url", "on_closed", "close_requested"]


@pytest.mark.parametrize("backend", ["webview", "app_browser"])
def test_a_window_strategy_takes_a_url_a_close_callback_and_a_close_request(
    backend: str,
) -> None:
    """Three parameters is the whole contract, and why it is enforceable.

    `strategy_for` is also the injection seam every lifecycle test replaces --
    resolved through the module at call time, never bound at import -- so a fake
    strategy that closes immediately can specify shutdown without a GUI.
    """
    strategy = window().strategy_for(backend)

    assert strategy is not None, f"no strategy for {backend!r}"
    assert list(inspect.signature(strategy).parameters) == STRATEGY_PARAMETERS


def test_there_is_no_strategy_for_the_none_backend() -> None:
    """`none` means no window, so the lookup answers with nothing to call."""
    assert window().strategy_for("none") is None


# --- 5. the backend that is not chosen is not imported ----------------------


def test_importing_the_window_module_pulls_in_no_webview_library() -> None:
    """`rhi --no-window` on a machine with no pywebview must simply work.

    A fresh interpreter, because this one may have imported anything: the
    question is what `import rhizome_graph.window` costs by itself.
    """
    probe = (
        "import sys; import rhizome_graph.window; "
        "print(sorted(n for n in sys.modules if n.split('.')[0] "
        "in ('webview', 'pywebview')))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "[]", completed.stdout


def test_the_webview_library_is_named_only_inside_a_function() -> None:
    """The structural half: a module-level import is the thing to prevent."""
    tree = window_tree()
    inner = {
        id(node)
        for scope in ast.walk(tree)
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(scope)
    }
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        modules = imported_modules(ast.Module(body=[node], type_ignores=[]))
        if any(name.split(".")[0] in ("webview", "pywebview") for name in modules):
            if id(node) not in inner:
                offenders.append(ast.dump(node))

    assert offenders == [], (
        "rhizome_graph/window.py imports a webview library at module level, so "
        f"`rhi --no-window` fails on a machine without it: {offenders}"
    )


# --- 6. what an app-mode browser is actually asked to do --------------------


def test_the_app_browser_argv_starts_with_the_browser_that_was_named() -> None:
    argv = window().app_browser_argv("/usr/bin/chromium", "http://127.0.0.1:8080/", "/tmp/p")

    assert argv[0] == "/usr/bin/chromium"


def test_the_app_browser_argv_opens_the_url_in_app_mode() -> None:
    """`--app=` is what removes the tab strip, the omnibox and the bookmarks.

    Without it this is a browser tab, which is the one thing the requirement
    rules out.
    """
    url = "http://127.0.0.1:8080/"

    argv = window().app_browser_argv("chromium", url, "/tmp/p")

    assert f"--app={url}" in argv


def test_the_app_browser_argv_uses_a_profile_directory_of_its_own() -> None:
    """This is what makes the process dedicated, and it is load-bearing.

    Without a private `--user-data-dir`, a Chromium already running hands the
    URL to itself and the command returns immediately: `rhi` would then think
    the window is open while owning no process at all, and closing the window
    would end nothing.
    """
    argv = window().app_browser_argv("chromium", "http://127.0.0.1:8080/", "/tmp/rhi-p")

    assert "--user-data-dir=/tmp/rhi-p" in argv


def test_the_app_browser_argv_carries_no_token_anywhere() -> None:
    """The command line of a child process is world-readable on Linux."""
    argv = window().app_browser_argv("chromium", "http://127.0.0.1:8080/", "/tmp/p")

    assert not any("token" in argument.lower() for argument in argv), argv
