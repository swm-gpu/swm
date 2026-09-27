"""Control-plane tests: stop_autosync draining and watcher restart carry-over.

These exercise the shell swm sends over SSH, using a recording fake session
and, for the kill escalation, by running the generated command locally
against a process that ignores SIGTERM.
"""

from __future__ import annotations

import base64
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import pytest

from swm.sync import autosync, watcher
from swm.sync.paths import (
    AUTO_ENV,
    AUTO_PID,
    TRANSFER_LOCK,
    WATCH_EXCLUDES,
    WATCH_LOG,
    WATCHER_EXCLUDES_FILE,
    WATCHER_SPEC_FILE,
)


class FakeSession:
    def __init__(self, responses: dict[str, str] | None = None):
        self.commands: list[str] = []
        self.responses = responses or {}

    def exec(self, command: str, stream: bool = True, line_callback=None):
        self.commands.append(command)
        for needle, out in self.responses.items():
            if needle in command:
                return 0, out, ""
        return 0, "", ""


def _index(commands: list[str], needle: str) -> int:
    for i, c in enumerate(commands):
        if needle in c:
            return i
    raise AssertionError(f"no command containing {needle!r} in {commands}")


def test_stop_autosync_is_one_command_that_drains_then_escalates():
    sess = FakeSession()
    autosync.stop_autosync(sess, drain_seconds=120)

    assert len(sess.commands) == 1
    cmd = sess.commands[0]
    assert f"cat {AUTO_PID}" in cmd
    assert "kill -TERM" in cmd
    assert "-lt 60 " in cmd and "sleep 2" in cmd
    assert 'kill -9 -- "-$pid"' in cmd
    assert 'pkill -9 -P "$pid"' in cmd
    assert f"rm -f {TRANSFER_LOCK}" in cmd
    assert f"rm -f {AUTO_PID} {AUTO_ENV}" in cmd


def test_stop_autosync_zero_drain_skips_the_wait_loop():
    sess = FakeSession()
    autosync.stop_autosync(sess, drain_seconds=0)

    cmd = sess.commands[0]
    assert "-lt 0 " in cmd
    assert 'kill -9 -- "-$pid"' in cmd


def test_stop_autosync_command_escalates_to_sigkill_locally(tmp_path: Path):
    pid_file = tmp_path / "autosync.pid"
    env_file = tmp_path / "autosync.env"
    lock = tmp_path / "transfer.lock"
    env_file.write_text("export X=1\n")

    stubborn = subprocess.Popen(
        ["bash", "-c", "trap '' TERM; while :; do sleep 1; done"],
        start_new_session=True,
    )
    try:
        pid_file.write_text(f"{stubborn.pid}\n")
        lock.write_text(f"{stubborn.pid}\n")
        sess = FakeSession()
        autosync.stop_autosync(sess, drain_seconds=2)
        cmd = (
            sess.commands[0]
            .replace(AUTO_PID, str(pid_file))
            .replace(AUTO_ENV, str(env_file))
            .replace(TRANSFER_LOCK, str(lock))
        )
        t0 = time.monotonic()
        subprocess.run(["bash", "-c", cmd], check=False, timeout=30)
        elapsed = time.monotonic() - t0

        deadline = time.monotonic() + 5
        while stubborn.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert stubborn.poll() is not None, "process ignoring TERM was not SIGKILLed"
        assert elapsed >= 2
        assert not lock.exists()
        assert not pid_file.exists()
        assert not env_file.exists()
    finally:
        if stubborn.poll() is None:
            os.killpg(stubborn.pid, signal.SIGKILL)
            stubborn.wait()


def test_stop_autosync_leaves_a_live_manual_holder_lock_alone(tmp_path: Path):
    pid_file = tmp_path / "autosync.pid"
    env_file = tmp_path / "autosync.env"
    lock = tmp_path / "transfer.lock"
    env_file.write_text("export X=1\n")

    holder = subprocess.Popen(["sleep", "30"])
    try:
        pid_file.write_text("999999\n")  # daemon already gone
        lock.write_text(f"{holder.pid}\n")
        sess = FakeSession()
        autosync.stop_autosync(sess, drain_seconds=0)
        cmd = (
            sess.commands[0]
            .replace(AUTO_PID, str(pid_file))
            .replace(AUTO_ENV, str(env_file))
            .replace(TRANSFER_LOCK, str(lock))
        )
        subprocess.run(["bash", "-c", cmd], check=False, timeout=30)

        assert lock.exists()
        assert not pid_file.exists()
    finally:
        holder.kill()
        holder.wait()


def test_start_autosync_stale_script_uses_draining_stop(monkeypatch):
    sess = FakeSession({"kill -0": "alive", "sha256sum": "deadbeef"})
    calls: list[tuple] = []
    monkeypatch.setattr(autosync, "stop_autosync", lambda s, **kw: calls.append(kw))
    monkeypatch.setattr(autosync, "_pull_stamp_exists", lambda s: True)
    monkeypatch.setattr(autosync, "start_watcher", lambda s, src: True)
    monkeypatch.setattr(autosync, "_write_env_file", lambda s, slug: None)
    monkeypatch.setattr(autosync.time, "sleep", lambda s: None)

    autosync.start_autosync(sess, "b2", "bucket", "ws")

    assert calls == [{}]


def test_start_autosync_refreshes_a_watcher_an_older_swm_started(monkeypatch):
    """Redeploying the daemon must also replace a running watcher whose event
    list is stale; start_watcher is a no-op when the watcher is current."""
    sess = FakeSession()
    started: list[str] = []
    monkeypatch.setattr(autosync, "_pull_stamp_exists", lambda s: True)
    monkeypatch.setattr(autosync, "start_watcher", lambda s, src: started.append(src) or True)
    monkeypatch.setattr(autosync, "_write_env_file", lambda s, slug: None)
    monkeypatch.setattr(autosync.time, "sleep", lambda s: None)

    autosync.start_autosync(sess, "b2", "bucket", "ws")

    assert started == ["/workspace"]


def _watcher_script(sess: FakeSession) -> str:
    cmd = next(c for c in sess.commands if "base64 -d > /tmp/.swm_start_watcher.sh" in c)
    return base64.b64decode(re.search(r"echo '([A-Za-z0-9+/=]+)'", cmd).group(1)).decode()


def test_watcher_logs_event_names_including_a_renames_old_name(monkeypatch):
    sess = FakeSession({"command -v inotifywait": "yes"})
    monkeypatch.setattr(watcher.time, "sleep", lambda s: None)

    watcher.start_watcher(sess, "/workspace")

    script = _watcher_script(sess)
    assert "-e modify,create,delete,moved_to,moved_from " in script
    assert "--format '%e %w%f' " in script
    assert WATCHER_SPEC_FILE in script


def test_start_watcher_replaces_a_watcher_with_an_older_event_list(monkeypatch):
    """Same excludes, but a watcher started by swm <= 0.3.4 logs bare paths and
    no moved_from; it is restarted with its pending entries carried over."""
    sess = FakeSession({
        "kill -0": "alive",
        WATCHER_EXCLUDES_FILE: "|".join(WATCH_EXCLUDES),
        "command -v inotifywait": "yes",
    })
    monkeypatch.setattr(watcher.time, "sleep", lambda s: None)

    assert watcher.start_watcher(sess, "/workspace") is True

    cmds = sess.commands
    assert _index(cmds, f"cp -f {WATCH_LOG} ") < _index(cmds, "bash /tmp/.swm_start_watcher.sh")


def test_start_watcher_leaves_a_current_watcher_running(monkeypatch):
    sess = FakeSession({
        "kill -0": "alive",
        WATCHER_EXCLUDES_FILE: "|".join(WATCH_EXCLUDES),
        WATCHER_SPEC_FILE: watcher.WATCH_SPEC,
    })
    monkeypatch.setattr(watcher.time, "sleep", lambda s: None)

    assert watcher.start_watcher(sess, "/workspace") is True

    assert not any("start_watcher.sh" in c for c in sess.commands)


def test_start_watcher_restart_carries_pending_entries(monkeypatch):
    sess = FakeSession({
        "kill -0": "alive",
        WATCHER_EXCLUDES_FILE: "stale-regex",
        "command -v inotifywait": "yes",
    })
    monkeypatch.setattr(watcher.time, "sleep", lambda s: None)

    assert watcher.start_watcher(sess, "/workspace") is True

    cmds = sess.commands
    saved = _index(cmds, f"cp -f {WATCH_LOG} ")
    stopped = _index(cmds, "pkill -f 'inotifywait -m -r --exclude'")
    started = _index(cmds, "bash /tmp/.swm_start_watcher.sh")
    restored = _index(cmds, f">> {WATCH_LOG}")
    assert saved < stopped < started < restored


def test_start_watcher_fresh_start_does_not_touch_carry(monkeypatch):
    sess = FakeSession({"command -v inotifywait": "yes"})
    monkeypatch.setattr(watcher.time, "sleep", lambda s: None)

    watcher.start_watcher(sess, "/workspace")

    assert not any("cp -f" in c for c in sess.commands)
    assert not any(f">> {WATCH_LOG}" in c and "cat " in c for c in sess.commands)


@pytest.mark.parametrize("placeholder", [
    "__SWM_FAIL_STREAK__", "__SWM_FAILING_MARKER__", "__SWM_LOCK_TAG__",
])
def test_render_substitutes_new_placeholders(placeholder: str):
    body = autosync._render_daemon_script("b2", "bucket", "ws", "/workspace", 60)
    assert placeholder not in body
    assert not re.search(r"__SWM_[A-Z_]+__", body)
