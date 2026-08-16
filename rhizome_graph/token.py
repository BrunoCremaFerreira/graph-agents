"""The boot token: the secret that says a command came from *this* page.

Pure, like every other module here: minting, comparing and embedding are
decisions, and the socket loop only calls them.

Why a token exists at all. Authorization used to be the peer's IP address alone
(:func:`daemon.server.control_allowed`), and that address lies in two ways, both
reproduced against a running daemon rather than reasoned about:

  * A WebSocket handshake is **not** subject to the same-origin policy. Any page
    open in a browser on this host can ``new WebSocket("ws://127.0.0.1:8080/ws")``
    and start sending ``setRoot`` and ``file``; every frame arrives from
    loopback, so the address gate waves it through and a page from anywhere on
    the web reads any file the daemon can reach.
  * Any proxy on loopback erases the real peer. The Vite dev server binds every
    interface and proxies ``/ws``, so a connection from across the LAN reaches
    the daemon as 127.0.0.1 and passes the same gate.

A secret minted at boot and injected into the page the daemon serves closes
both: a cross-site page cannot read it (fetching the page to scrape it is
precisely what same-origin does stop), and a proxy has none to forward. It beat
an Origin allow-list because it is indifferent to the port, which is what SSH
and VS Code forwarding change.

The token is an **additional** condition on the address gate, never a
replacement -- see :func:`daemon.server._handle_ws_client`.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
from collections.abc import Mapping

#: Where a human may pin the token, so a wrapper script or a second tool can be
#: told what this daemon expects.
TOKEN_ENV_VAR = "RHIZOME_TOKEN"

#: Bytes of entropy per token. `secrets.token_urlsafe(32)` yields 43 urlsafe
#: characters, which travel through a JSON frame and an HTML page untouched.
TOKEN_ENTROPY_BYTES = 32

#: The global the served page reads its own token from.
TOKEN_GLOBAL = "window.__RHIZOME_TOKEN__"

#: A closing `</head>`, in any case a hand-written page or a minifier may emit.
_HEAD_CLOSE = re.compile(r"</head\s*>", re.IGNORECASE)

#: Characters that must never appear raw inside the script element. The HTML
#: parser ends a script at the first `</script`, whatever the JavaScript around
#: it means, so `<` alone would be enough; `>` and `&` go too, so no reading of
#: the surrounding markup can reconstruct a tag.
_HTML_UNSAFE = {"<": "\\u003c", ">": "\\u003e", "&": "\\u0026"}


def mint_token() -> str:
    """A fresh secret for one daemon's lifetime.

    Never a constant compiled into the source: that would be a password
    published on the internet, which is no better than the IP check it adds to.
    """
    return secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)


def token_from_env(environ: Mapping[str, str]) -> str:
    """The token this daemon will expect, pinned by the environment or minted.

    An empty ``RHIZOME_TOKEN`` mints one rather than yielding the empty string:
    a wrapper script exporting the variable blank must not disable the whole
    defence, and an empty expected token is refused by :func:`token_matches`
    anyway, which would lock the page out of its own daemon.
    """
    pinned = environ.get(TOKEN_ENV_VAR, "")
    return pinned if isinstance(pinned, str) and pinned else mint_token()


def token_matches(expected: str, given: object) -> bool:
    """Does ``given``, straight off the network, carry ``expected``?

    Constant time, because a comparison that stops at the first differing byte
    leaks the token one character at a time to whoever can measure the answer.

    Two refusals matter more than the comparison itself:

      * An empty ``expected`` accepts nothing. A daemon that somehow booted
        without a token must refuse every command rather than accept every
        tokenless one -- fail closed, not open.
      * Anything that is not an ASCII ``str`` is refused instead of raising.
        ``given`` is attacker-controlled, and ``hmac.compare_digest`` raises
        ``TypeError`` on a number, a list or a single accented character; an
        exception here would kill the task serving that browser.
    """
    if not isinstance(expected, str) or not expected:
        return False
    if not isinstance(given, str) or isinstance(given, bool):
        return False
    try:
        return bool(hmac.compare_digest(expected, given))
    except TypeError:  # a non-ASCII `str` reaching `compare_digest`
        return False


def inject_token(html: str, token: str) -> str:
    """The served page, taught the token it must send back.

    Placed before ``</head>`` so the global is defined ahead of every script the
    bundle loads and the client can read it at module scope instead of racing
    the first frame it wants to send. A document with no head (a fragment, a
    stripped ``index.html``) gets the script prepended: better an oddly placed
    token than a page that can never send a command.

    The value is written as a JSON string literal with ``<``, ``>`` and ``&``
    escaped to ``\\uXXXX``. ``RHIZOME_TOKEN`` is set by a human and reaches this
    function verbatim, so nothing in it may close the string literal (a quote or
    a backslash), break the line (a newline is a syntax error inside a
    JavaScript literal), or close the element -- a token spelled
    ``</script><img src=x onerror=...>`` would turn this security fix into an
    XSS. Escaped, never stripped: mangling the value leaves the page unable to
    talk to the daemon that chose it.
    """
    script = f"<script>{TOKEN_GLOBAL} = {_javascript_literal(token)};</script>"
    match = _HEAD_CLOSE.search(html)
    if match is None:
        return script + html
    return html[: match.start()] + script + html[match.start() :]


def _javascript_literal(token: str) -> str:
    """``token`` as a double-quoted literal safe inside a script element."""
    # `ensure_ascii` (the default) also folds newlines and every non-ASCII
    # character -- U+2028 and U+2029 included, which JavaScript treats as line
    # terminators -- into escapes.
    literal = json.dumps(token)
    for character, escape in _HTML_UNSAFE.items():
        literal = literal.replace(character, escape)
    return literal
