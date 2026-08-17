"""Contract test (RED) for where daemon/server.py is allowed to read the air.

Motivation: moving the configuration onto a `Settings` is only worth doing if it
stays moved. The behavioural tests in `tests/test_run_settings.py` prove the
ambient read is gone *today* -- they call `run()` with `RHIZOME_*` stripped from
the environment -- but they cannot stop the next reader from creeping back in,
because a new `os.environ.get` added to a helper nobody calls in those tests
fails nothing. This is the boundary written down, in the same spirit as the "no
shiki outside `highlight.ts`" rule on the front end: a structural assertion over
the source, cheap to run and impossible to satisfy by accident.

**`main()` is the one permitted reader.** It is the command line's front door:
`start.sh` configures the daemon by exporting variables and that must keep
working exactly as it does (see `tests/test_start_script.py`), so the reads have
to happen somewhere -- they simply have to happen *at the entry*, once, where a
second front door (`rhi`) can supply the same values from a different source.
Every read below `main()` is a configuration input that no caller can see,
override or test without `monkeypatch.setenv`.

What counts as a read is deliberately broad: `os.environ[...]`, `os.environ.get`,
`os.getenv`, `"X" in os.environ`, and a bare mention of `os.environ` passed to
somebody else. The last one matters most, because it is the form an offender
most naturally mutates into -- `default_web_dist(os.environ)` reads the
environment just as surely as `os.environ.get("RHIZOME_WEB_DIST")` does, and
calling it "handing a mapping to a pure function" would let the whole seam leak
back in one helper at a time.

**There are no exemptions besides `main()`, and `Session.__init__` was
considered and refused.** It has the most sympathetic case there is -- it
resolves its token through `token_from_env(os.environ)` only when the caller
supplies none -- and exempting it would punch the hole in precisely the class
this seam exists to close. "Reads no environment variable, except in one
constructor" is not the property; a `Session` whose token depends on which shell
started the process is the ambient configuration this stage is removing, whoever
built it. The coverage that fallback carried has moved rather than gone: the
environment pinning `RHIZOME_TOKEN` is specified on `settings_from`
(`tests/test_cli_settings.py`), and the token reaching the gate is specified on
the `Session` that is handed one (`tests/test_settings_control_gate.py`).

Style: one property, asserted over the parsed source.
"""

from __future__ import annotations

import ast
from pathlib import Path

import daemon.server as server

#: The only scope allowed to read the process environment. One entry, and it
#: stays one entry: `main()` is the command line's front door, where variables
#: exported by `start.sh` become a `Settings` a second front door could have
#: built from a flag instead. Every other reader is a configuration input no
#: caller can see, override or test without `monkeypatch.setenv` -- see the
#: module docstring for why `Session.__init__` was considered and refused.
ENVIRONMENT_READERS_ALLOWED = {"main"}


def _is_environment_read(node: ast.AST) -> bool:
    """Any mention of the process environment, however it is spelled.

    `os.environ` covers the subscript, the `.get`, the `in` test and the bare
    argument alike, because all four are the same attribute access underneath.
    `from os import environ, getenv` is covered by the bare-name arm.
    """
    if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
        return isinstance(node.value, ast.Name) and node.value.id == "os"
    return isinstance(node, ast.Name) and node.id in ("environ", "getenv")


def _scopes(module: ast.Module) -> list[tuple[str, list[ast.AST]]]:
    """Every scope of the module, as (qualified name, nodes to search)."""
    scopes: list[tuple[str, list[ast.AST]]] = []
    loose: list[ast.AST] = []
    for item in module.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scopes.append((item.name, [item]))
        elif isinstance(item, ast.ClassDef):
            for member in item.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    scopes.append((f"{item.name}.{member.name}", [member]))
                else:
                    loose.append(member)
        else:
            loose.append(item)
    scopes.append(("<module level>", loose))
    return scopes


def _environment_readers(source: str) -> list[str]:
    offenders: list[str] = []
    for name, nodes in _scopes(ast.parse(source)):
        for root in nodes:
            if any(_is_environment_read(node) for node in ast.walk(root)):
                offenders.append(name)
                break
    return sorted(set(offenders) - ENVIRONMENT_READERS_ALLOWED)


def test_the_daemon_reads_the_environment_only_at_its_entry_point() -> None:
    """Configuration arrives as an argument; only `main()` turns air into one."""
    source = Path(server.__file__).read_text(encoding="utf-8")

    offenders = _environment_readers(source)

    assert offenders == [], (
        "daemon/server.py reads the process environment outside main(): "
        f"{offenders}. Configuration belongs on the Settings that run() is "
        "given, so a second front door can supply it from somewhere else."
    )
