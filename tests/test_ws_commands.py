"""Contract tests (RED) for the WebSocket *inbound* channel.

Motivation: `_handle_ws_client` reads the frames a browser sends and throws them
away on purpose -- the channel is documented as broadcast-only. That is exactly
why the observed root is frozen at boot: the page has no way to say anything back
to the daemon, so retyping the root (``ctrl+L``, ``Tab``, ``Enter``) has nowhere
to arrive. Two commands open that direction:

  * ``{"kind": "complete", "path": ...}`` -- answer a ``Tab``. The browser cannot
    read the daemon's disk, so completion has to be resolved here.
  * ``{"kind": "setRoot", "path": ...}`` -- observe another project.

Both are answered **to that client alone**, never broadcast: one viewer pressing
``Tab`` must not repaint the field of everybody else looking at the same daemon.

Only the pure pieces are specified here, because they are what can be pinned down
without a socket:

  * ``parse_command`` -- this is data straight off the network, typed by a human
    into a field. It must **never raise**: an exception here kills the task
    serving that browser. Everything unrecognized collapses to ``None``.
  * ``control_allowed`` -- ``setRoot`` makes the daemon walk an arbitrary
    directory, so it is not something an open port should hand to anyone. The
    policy is loopback-only by default (an SSH tunnel and VS Code port forwarding
    both arrive as loopback, so the ordinary remote setup keeps working), with
    ``GRAPHAGENTS_ALLOW_REMOTE_CONTROL=1`` deliberately opening it up.
  * ``completion_response`` -- the frame sent back. It echoes the path that was
    asked about, **intact**: the viewer keeps typing while the answer travels, so
    the page compares the echo against the field to drop answers that arrived too
    late. Without the echo, a slow completion overwrites newer keystrokes.

Style: Arrange-Act-Assert, one failure reason per test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from daemon.server import completion_response, control_allowed, parse_command


def _dirs(root: Path, *names: str) -> None:
    for name in names:
        (root / name).mkdir(parents=True, exist_ok=True)


# --- 1. parse_command: a frame off the wire -> a command, or nothing --------

def test_a_complete_command_is_understood():
    assert parse_command('{"kind":"complete","path":"~/proj"}') == {
        "kind": "complete",
        "path": "~/proj",
    }


def test_a_set_root_command_is_understood():
    assert parse_command('{"kind":"setRoot","path":"/srv/other"}') == {
        "kind": "setRoot",
        "path": "/srv/other",
    }


def test_the_path_is_handed_on_exactly_as_typed():
    # No trimming, no `~` expansion here: `resolve_root`/`complete_dir` do that
    # against the daemon's home, and the echo in the answer has to match what the
    # field still contains for the page to recognize its own request.
    command = parse_command('{"kind":"complete","path":"  ~/pro  "}')

    assert command is not None and command["path"] == "  ~/pro  "


def test_malformed_json_is_not_a_command():
    assert parse_command("{not json") is None


def test_a_frame_that_is_not_an_object_is_not_a_command():
    # A bare array or string parses fine as JSON and would blow up on `.get`.
    assert parse_command('["setRoot","/etc"]') is None
    assert parse_command('"setRoot"') is None
    assert parse_command("42") is None


def test_a_frame_without_a_kind_is_not_a_command():
    assert parse_command('{"path":"/srv/other"}') is None


def test_an_unknown_kind_is_not_a_command():
    # Only the two commands the daemon actually implements get through; anything
    # else is a client from another version, not an instruction.
    assert parse_command('{"kind":"shutdown","path":"/"}') is None


def test_a_command_without_a_path_is_not_a_command():
    assert parse_command('{"kind":"setRoot"}') is None


def test_a_path_that_is_not_a_string_is_not_a_command():
    # `resolve_root` calls `.strip()`; a number or a list reaching it would raise
    # inside the loop serving that browser.
    assert parse_command('{"kind":"setRoot","path":42}') is None
    assert parse_command('{"kind":"complete","path":null}') is None
    assert parse_command('{"kind":"complete","path":["/a","/b"]}') is None


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "\x00",
        "{}",
        "null",
        '{"kind":null}',
        '{"kind":{"nested":"object"},"path":"/x"}',
    ],
)
def test_garbage_off_the_network_is_refused_and_never_raises(raw: str):
    assert parse_command(raw) is None


# --- 2. control_allowed: who may repoint the daemon ------------------------

def test_loopback_ipv4_may_control_the_daemon():
    assert control_allowed("127.0.0.1", allow_remote=False) is True


def test_loopback_ipv6_may_control_the_daemon():
    assert control_allowed("::1", allow_remote=False) is True


def test_the_ipv4_mapped_loopback_form_may_control_the_daemon():
    # A dual-stack listener reports a local connection in this shape; refusing it
    # would break control on the very setup it is meant to allow.
    assert control_allowed("::ffff:127.0.0.1", allow_remote=False) is True


def test_a_remote_host_may_not_control_the_daemon_by_default():
    assert control_allowed("192.168.1.50", allow_remote=False) is False


def test_a_remote_host_may_control_the_daemon_when_it_is_opted_in():
    assert control_allowed("192.168.1.50", allow_remote=True) is True


def test_an_unknown_peer_is_refused():
    # `getpeername` can yield nothing usable; "no idea who this is" must not be
    # read as "local".
    assert control_allowed("", allow_remote=False) is False
    assert control_allowed("not-an-address", allow_remote=False) is False


# --- 3. completion_response: the answer to one Tab -------------------------

def test_the_answer_is_a_completion_frame(tmp_path: Path):
    _dirs(tmp_path, "alpha")

    assert completion_response("~/al", str(tmp_path))["kind"] == "completion"


def test_the_answer_carries_back_the_path_it_was_asked_about(tmp_path: Path):
    # The viewer is still typing while this travels; the page drops the answer
    # unless this field still matches what is in the input.
    _dirs(tmp_path, "alpha")

    assert completion_response("~/al", str(tmp_path))["path"] == "~/al"


def test_a_single_candidate_completes_and_ends_in_a_slash(tmp_path: Path):
    # The trailing slash is what lets the next Tab descend instead of re-offering
    # the same directory forever.
    _dirs(tmp_path, "alpha")

    answer = completion_response("~/al", str(tmp_path))

    assert answer["completed"] == str(tmp_path / "alpha") + "/"
    assert answer["matches"] == ["alpha"]


def test_several_candidates_advance_only_to_their_common_prefix(tmp_path: Path):
    _dirs(tmp_path, "album", "alpha")

    answer = completion_response("~/al", str(tmp_path))

    assert answer["completed"] == str(tmp_path / "al")
    assert answer["matches"] == ["album", "alpha"]


def test_nothing_matching_keeps_the_text_and_lists_no_candidates(tmp_path: Path):
    _dirs(tmp_path, "alpha")

    answer = completion_response("~/zz", str(tmp_path))

    assert answer["matches"] == []
    assert answer["path"] == "~/zz"


def test_the_answer_is_serializable_as_it_stands(tmp_path: Path):
    # It goes on the wire as JSON; a `Completion` dataclass smuggled in whole
    # would raise inside the send, on the daemon's loop.
    _dirs(tmp_path, "album", "alpha")

    assert json.loads(json.dumps(completion_response("~/al", str(tmp_path))))["matches"] == [
        "album",
        "alpha",
    ]


def test_a_path_that_cannot_be_read_still_answers(tmp_path: Path):
    # A NUL byte, a revoked permission, `//`: half a path naming nowhere is the
    # normal intermediate state of someone typing, not a fault.
    answer = completion_response("/proc/1/root/\x00nope", str(tmp_path))

    assert answer["kind"] == "completion"
    assert answer["matches"] == []
