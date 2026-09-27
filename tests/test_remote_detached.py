"""RemoteSession.exec_detached: a remote command that outlives its SSH
connection.

Some hosts' sshd drops a connection within ~25 s once other SSH sessions are
active; a command tied to that connection died at its next write (a truncated
restore, a half-installed PyTorch). These tests run the real launch/follow
protocol against this machine as the "remote", scripting which calls lose
their connection.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from swm.remote import ssh
from swm.remote.ssh import RemoteSession


@pytest.fixture(autouse=True)
def local_remote(tmp_path, monkeypatch):
    monkeypatch.setattr(ssh, "_DETACHED_ROOT", str(tmp_path / "run"))
    # macOS has no setsid(1); this stand-in does what it does.
    shim = tmp_path / "bin" / "setsid"
    shim.parent.mkdir()
    # (#!/usr/bin/env: an interpreter path with a space cannot be a shebang.)
    shim.write_text("#!/usr/bin/env python3\nimport os, sys\nos.setsid()\n"
                    "os.execvp(sys.argv[1], sys.argv[1:])\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim.parent}:{os.environ['PATH']}")


class LocalSession(RemoteSession):
    """``exec`` runs bash on this machine. ``drop(n, command)`` may return
    "before" (the call never reaches the pod) or "after" (it ran, but its
    reply was lost) for the n-th call."""

    def __init__(self, drop=None) -> None:
        super().__init__(host="local")
        self.calls = 0
        self.drop = drop or (lambda n, command: None)

    def exec(self, command, stream=True, line_callback=None):
        self.calls += 1
        fate = self.drop(self.calls, command)
        if fate == "before":
            return 255, "", ""
        done = subprocess.run(["bash", "-c", command], capture_output=True,
                              text=True, timeout=60, check=False)
        if fate == "after":
            return 255, "", ""
        return done.returncode, done.stdout + done.stderr, ""


def _run(session, command, **kwargs):
    lines: list[str] = []
    code, out, _ = session.exec_detached(command, line_callback=lines.append, **kwargs)
    return code, out, lines


def test_exit_code_and_output_come_back_like_exec():
    code, out, lines = _run(LocalSession(), "echo one; echo two >&2; exit 3")
    assert code == 3
    assert lines == ["one\n", "two\n"]
    assert out == "one\ntwo\n"


def test_dropped_polls_cost_nothing_but_a_reconnect():
    def drop(n, command):
        return "before" if 2 <= n <= 6 else None

    code, _, lines = _run(LocalSession(drop),
                          "for i in 1 2 3 4; do echo line $i; sleep 0.4; done")
    assert code == 0
    assert lines == [f"line {i}\n" for i in (1, 2, 3, 4)]


def test_a_lost_launch_reply_never_starts_a_second_copy(tmp_path):
    counter = tmp_path / "count"

    def drop(n, command):
        return "after" if n == 1 else None

    code, _, _ = _run(LocalSession(drop), f"echo ran >> {counter}")
    assert code == 0
    assert counter.read_text() == "ran\n"


def test_an_unreachable_pod_reports_255():
    def drop(n, command):
        return "before" if n > 1 else None

    code, _, _ = _run(LocalSession(drop), "sleep 5", lost_after=0.5)
    assert code == 255


def test_lines_split_across_polls_are_joined():
    code, _, lines = _run(LocalSession(), "printf abc; sleep 0.8; printf 'def\\n'")
    assert code == 0
    assert lines == ["abcdef\n"]


def test_output_that_contains_the_marker_is_still_parsed():
    code, _, lines = _run(LocalSession(), f"echo '{ssh._DETACHED_MARK} 1 2'; exit 4")
    assert code == 4
    assert lines == [f"{ssh._DETACHED_MARK} 1 2\n"]


def test_an_interrupted_caller_stops_the_remote_command(tmp_path):
    finished = tmp_path / "finished"

    def stop(line):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        LocalSession().exec_detached(
            f"echo started; sleep 3; touch {finished}", line_callback=stop)
    time.sleep(4)
    assert not finished.exists()
    assert list(Path(ssh._DETACHED_ROOT).iterdir()) == []


def test_a_launch_that_never_starts_is_reported_not_awaited(monkeypatch):
    monkeypatch.setattr(ssh, "_DETACHED_START_WITHIN", 1.0)

    # A shell that does not exist stands in for any launch that dies at once.
    class NoBash(LocalSession):
        def exec(self, command, stream=True, line_callback=None):
            command = command.replace("nohup bash -c", "nohup no-such-shell -c")
            done = subprocess.run(["/bin/bash", "-c", command], capture_output=True,
                                  text=True, timeout=60, check=False)
            return done.returncode, done.stdout + done.stderr, ""

    code, _, _ = _run(NoBash(), "echo never")
    assert code == 255


def test_the_run_directory_is_removed_after_success():
    code, _, _ = _run(LocalSession(), "true")
    assert code == 0
    assert list(Path(ssh._DETACHED_ROOT).iterdir()) == []
