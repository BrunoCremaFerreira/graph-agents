"""Contract tests (RED) for the boot token: `rhizome_graph.token`.

Motivation, measured against a scratch daemon rather than reasoned about:
`control_allowed` decides who may drive this daemon from the peer's IP address
alone, and that address lies in two ways.

  * A WebSocket handshake is **not** subject to the same-origin policy. Any page
    open in a browser on this host -- an advert frame, a documentation site --
    can `new WebSocket("ws://127.0.0.1:8080/ws")` and start sending `setRoot`
    and `file`. Every one of those frames arrives from 127.0.0.1, so the gate
    waves them through and the page reads any file the daemon can reach.
  * Any proxy sitting on loopback erases the real peer. `web/vite.config.ts`
    binds `host: true` and proxies `/ws`, so a connection from anywhere on the
    LAN reaches the daemon as 127.0.0.1 and passes the same gate.

The fix is a secret the daemon mints at boot and injects into the page it
serves. A cross-site page cannot read it (fetching the page to scrape it is what
same-origin does stop), and a proxy has none to forward. It is indifferent to
the port and to SSH / VS Code forwarding, which is why it beat an Origin
allow-list.

This file specifies only the pure module. The token is an ADDITIONAL condition
on top of `control_allowed`, never a replacement -- see
`tests/test_ws_control_token.py` for the wiring.

Two properties here are the ones that would turn a security fix into a security
hole if they were got wrong:

  * `token_matches` must answer False for an EMPTY expected token. A daemon that
    somehow booted without one must refuse every command, not accept every
    tokenless command.
  * `inject_token` writes attacker-influenceable text (the value of
    `RHIZOME_TOKEN`) into a `<script>` element. It must be impossible for that
    value to close the element or escape its string literal, or the page has an
    XSS where it used to have a fix.

The embedded value is read back with `json.loads` of a double-quoted literal,
which pins the shape the browser has to parse: `window.__RHIZOME_TOKEN__ =
"<json string>";`.

Style: Arrange-Act-Assert, one failure reason per test.
"""

from __future__ import annotations

import json
import re
import string

import pytest

from rhizome_graph.token import (
    inject_token,
    mint_token,
    token_from_env,
    token_matches,
)

PAGE = (
    "<!doctype html>\n<html lang=\"en\">\n  <head>\n"
    "    <meta charset=\"UTF-8\" />\n    <title>rhizome-graph</title>\n"
    "  </head>\n  <body>\n    <canvas id=\"stage\"></canvas>\n"
    "  </body>\n</html>\n"
)

URL_SAFE = set(string.ascii_letters + string.digits + "-_")

#: `window.__RHIZOME_TOKEN__ = "..."` with the double-quoted literal captured.
_ASSIGNMENT = re.compile(r'window\.__RHIZOME_TOKEN__\s*=\s*("(?:[^"\\]|\\.)*")')


def _embedded_token(html: str) -> str:
    """What a browser would end up with in `window.__RHIZOME_TOKEN__`."""
    match = _ASSIGNMENT.search(html)
    assert match is not None, f"no assignment to the global in: {html!r}"
    return json.loads(match.group(1))


# --- 1. mint_token: a secret worth having ----------------------------------

def test_a_minted_token_is_not_empty():
    # An empty token is refused by `token_matches`, so minting one would lock
    # the page out of its own daemon.
    assert mint_token() != ""


def test_a_minted_token_survives_a_url_and_an_html_attribute_untouched():
    # It travels inside a JSON frame and inside the served page; the urlsafe
    # alphabet is what makes both of those trips uneventful.
    assert set(mint_token()) <= URL_SAFE


def test_a_minted_token_carries_real_entropy():
    # `secrets.token_urlsafe(32)` is 43 characters. The bound is loose on
    # purpose -- what must never happen is a short, guessable token.
    assert len(mint_token()) >= 32


def test_two_boots_do_not_share_a_token():
    # A constant compiled into the source would be a password published on the
    # internet, which is no better than the IP check it replaces.
    assert mint_token() != mint_token()


# --- 2. token_from_env: the environment may pin it -------------------------

def test_the_environment_variable_is_used_when_it_is_set():
    assert token_from_env({"RHIZOME_TOKEN": "chosen-by-hand"}) == "chosen-by-hand"


def test_an_absent_variable_yields_a_freshly_minted_token():
    assert token_from_env({}) != ""


def test_an_empty_variable_yields_a_freshly_minted_token():
    # `RHIZOME_TOKEN=` in a wrapper script must not disable the whole defence.
    assert token_from_env({"RHIZOME_TOKEN": ""}) != ""


def test_two_daemons_with_no_variable_do_not_share_a_token():
    assert token_from_env({}) != token_from_env({})


def test_unrelated_variables_are_ignored():
    assert token_from_env({"RHIZOME_PROJECT_ROOT": "/srv/proj"}) != "/srv/proj"


# --- 3. token_matches: the comparison itself -------------------------------

def test_the_exact_token_matches():
    assert token_matches("s3cret-token", "s3cret-token") is True


def test_a_different_token_does_not_match():
    assert token_matches("s3cret-token", "another-token") is False


def test_a_prefix_of_the_token_does_not_match():
    assert token_matches("s3cret-token", "s3cret") is False


def test_a_token_with_something_appended_does_not_match():
    assert token_matches("s3cret-token", "s3cret-token-extra") is False


def test_an_empty_given_token_does_not_match():
    # This is the whole class of attack: a frame from a cross-site page carries
    # no token at all.
    assert token_matches("s3cret-token", "") is False


def test_no_expected_token_accepts_nothing():
    # A daemon that failed to mint one must refuse every command rather than
    # accept every tokenless one. Fail closed, not open.
    assert token_matches("", "") is False
    assert token_matches("", "anything") is False


@pytest.mark.parametrize("given", [None, 42, 3.5, True, ["s3cret-token"], {"token": "x"}, b"s3cret-token"])
def test_a_token_that_is_not_a_string_does_not_match(given: object):
    # `given` comes straight off the network. `hmac.compare_digest` raises
    # TypeError on most of these, and an exception here kills the task serving
    # that browser.
    assert token_matches("s3cret-token", given) is False


def test_a_non_ascii_token_is_refused_instead_of_raising():
    # `hmac.compare_digest` raises TypeError when a `str` argument is not
    # ASCII-only. A single accented character in a hostile frame would otherwise
    # take down the connection handler.
    assert token_matches("s3cret-token", "s3cret-tokén") is False


def test_matching_is_case_sensitive():
    assert token_matches("s3cret-token", "S3CRET-TOKEN") is False


def test_a_minted_token_matches_itself():
    token = mint_token()

    assert token_matches(token, token) is True


# --- 4. inject_token: the page learns its own token ------------------------

def test_the_global_is_defined_in_the_served_page():
    assert "window.__RHIZOME_TOKEN__" in inject_token(PAGE, "s3cret-token")


def test_the_page_carries_back_the_token_it_was_given():
    assert _embedded_token(inject_token(PAGE, "s3cret-token")) == "s3cret-token"


def test_the_token_is_defined_before_the_head_closes():
    # Ahead of every script the bundle loads, so the client can read it at
    # module scope rather than racing the first frame it wants to send.
    injected = inject_token(PAGE, "s3cret-token")

    assert injected.index("window.__RHIZOME_TOKEN__") < injected.index("</head>")


def test_the_rest_of_the_document_is_left_alone():
    injected = inject_token(PAGE, "s3cret-token")

    assert "<canvas id=\"stage\"></canvas>" in injected
    assert injected.startswith("<!doctype html>")
    assert injected.rstrip().endswith("</html>")


def test_an_uppercase_head_tag_is_still_recognized():
    # Hand-written HTML and some minifiers emit it this way; missing it would
    # silently drop the token and lock the page out.
    page = "<html><HEAD><title>x</title></HEAD><body></body></html>"

    assert _embedded_token(inject_token(page, "s3cret-token")) == "s3cret-token"


def test_a_document_with_no_head_still_gets_its_token():
    # A fragment, a stripped `dist/index.html`, a build tool that dropped the
    # tag: better an injected page than a browser that can never send a command.
    page = "<html><body><canvas id=\"stage\"></canvas></body></html>"

    assert _embedded_token(inject_token(page, "s3cret-token")) == "s3cret-token"


def test_an_empty_document_still_gets_its_token():
    assert _embedded_token(inject_token("", "s3cret-token")) == "s3cret-token"


def test_a_token_holding_quotes_and_backslashes_arrives_intact():
    # `RHIZOME_TOKEN` is set by a human; a quote in it must not end the string
    # literal it sits in.
    token = "a\"b\\c'd"

    assert _embedded_token(inject_token(PAGE, token)) == token


def test_a_token_that_looks_like_a_closing_script_tag_cannot_close_it():
    # The XSS this fix must not introduce: the HTML parser ends a script element
    # at the first `</script`, whatever the JavaScript around it means. Exactly
    # one terminator may exist in the injected page.
    injected = inject_token(PAGE, "</script><img src=x onerror=alert(1)>")

    assert len(re.findall(r"</script", injected, re.IGNORECASE)) == 1


def test_a_token_that_looks_like_a_closing_script_tag_still_arrives_intact():
    # Escaped, not stripped: mangling the value would leave the page unable to
    # talk to the daemon that chose it.
    token = "</script><img src=x onerror=alert(1)>"

    assert _embedded_token(inject_token(PAGE, token)) == token


def test_a_token_holding_a_newline_stays_on_one_javascript_line():
    # A raw newline inside a JavaScript string literal is a syntax error, and a
    # broken inline script takes the token down with it.
    token = "line-one\nline-two"

    assert _embedded_token(inject_token(PAGE, token)) == token
