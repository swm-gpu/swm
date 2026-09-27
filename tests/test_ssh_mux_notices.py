"""OpenSSH's connection-sharing fallback notices never reach parsed output.

When the shared connection refuses another session (sshd MaxSessions), ssh
prints two notices and opens a fresh connection; the command still runs. The
notices come ahead of the command's output on stderr, which exec merges, so
they used to corrupt anything that parsed it: stat_path reported a directory
as a file. Reproduced against a real sshd with MaxSessions 1; here a fake ssh
prints the same notices and runs the command locally.
"""

from __future__ import annotations

import os

import pytest

from swm.remote.ssh import RemoteSession

NOTICES = (
    "mux_client_request_session: session request failed: Session open refused by peer\\r\\n"
    "ControlSocket /tmp/swm-ssh-7ffb680e already exists, disabling multiplexing\\r\\n"
)


@pytest.fixture
def sess(tmp_path, monkeypatch) -> RemoteSession:
    shim = tmp_path / "bin" / "ssh"
    shim.parent.mkdir()
    shim.write_text('#!/bin/bash\nprintf "%b" "$FAKE_SSH_NOTICES" >&2\n'
                    'exec bash -c "${@: -1}"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim.parent}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_NOTICES", NOTICES)
    return RemoteSession("pod")


def test_exec_output_is_the_commands_alone(sess):
    lines: list[str] = []
    assert sess.exec("echo hello; echo world", line_callback=lines.append) == (
        0, "hello\nworld\n", "")
    assert lines == ["hello\n", "world\n"]


def test_stat_path_and_is_directory_read_a_directory_as_one(sess, tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    for name in ("a.txt", "b.txt", "c.mp4"):
        (tree / name).write_text("x")
    assert sess.is_directory(str(tree))
    assert sess.stat_path(str(tree)) == (True, 3, True)


def test_exec_pipe_lines_exclude_the_notices(sess):
    lines: list[str] = []
    assert sess.exec_pipe('echo \'{"ok": true}\'', line_callback=lines.append) == 0
    assert lines == ['{"ok": true}\n']


def test_notice_text_from_the_command_itself_is_kept(sess):
    echoed = "ControlSocket x already exists, disabling multiplexing"
    _, out, _ = sess.exec(f"echo first; echo '{echoed}'")
    assert out == f"first\n{echoed}\n"


def test_output_without_notices_is_unchanged(sess, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_NOTICES", "")
    assert sess.exec("printf 'a\\r\\nb\\n'", stream=False) == (0, "a\r\nb\n", "")
