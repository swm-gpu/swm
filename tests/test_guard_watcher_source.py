"""Unit tests for the on-pod guard daemon's activity helpers.

``guard._WATCHER_SOURCE`` is the Python program swm writes to the pod. Its
helper functions are exec'd here (everything above the signal wiring / main
loop) with the lock and failing-marker paths redirected to a tmpdir via the
``SWM_GUARD_*`` environment overrides.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from swm.guard import _WATCHER_SOURCE
from swm.sync.paths import TRANSFER_LOCK_HOLDER_TAG

linux_only = pytest.mark.skipif(
    sys.platform != "linux", reason="needs /proc/<pid>/cmdline",
)


@pytest.fixture
def helpers(tmp_path: Path, monkeypatch):
    lock = tmp_path / "transfer.lock"
    marker = tmp_path / "autosync.failing"
    monkeypatch.setenv("SWM_GUARD_LOCK", str(lock))
    monkeypatch.setenv("SWM_GUARD_AUTOSYNC_FAILING", str(marker))
    monkeypatch.setattr(sys, "argv", ["watcher.py"])
    head = _WATCHER_SOURCE.split("signal.signal(signal.SIGTERM", 1)[0]
    ns: dict = {"__name__": "swm_guard_head"}
    exec(compile(head, "swm_guard_head", "exec"), ns)
    ns["_lock"] = lock
    ns["_marker"] = marker
    return ns


def _holder(argv0: str) -> subprocess.Popen:
    proc = subprocess.Popen(["bash", "-c", f"exec -a {argv0} sleep 30"])
    time.sleep(0.2)
    return proc


def test_no_lock_is_not_activity(helpers):
    assert helpers["transfer_locked"]() is False


def test_dead_pid_lock_is_stale(helpers):
    dead = subprocess.Popen(["true"])
    dead.wait()
    helpers["_lock"].write_text(f"{dead.pid}\n")
    assert helpers["transfer_locked"]() is False


def test_garbage_lock_is_stale(helpers):
    helpers["_lock"].write_text("not-a-pid\n")
    assert helpers["transfer_locked"]() is False


@linux_only
def test_live_unrecognised_holder_is_stale(helpers):
    proc = subprocess.Popen(["sleep", "30"])
    try:
        helpers["_lock"].write_text(f"{proc.pid}\n")
        assert helpers["transfer_locked"]() is False
    finally:
        proc.kill()
        proc.wait()


@linux_only
def test_daemon_holder_without_marker_is_activity(helpers):
    proc = _holder("/tmp/.swm_autosync.sh")
    try:
        helpers["_lock"].write_text(f"{proc.pid}\n")
        assert helpers["transfer_locked"]() is True
    finally:
        proc.kill()
        proc.wait()


@linux_only
def test_daemon_holder_with_marker_is_not_activity(helpers):
    proc = _holder("/tmp/.swm_autosync.sh")
    try:
        helpers["_lock"].write_text(f"{proc.pid}\n")
        helpers["_marker"].write_text("count=5 since=2026-09-13T00:00:00Z\n")
        assert helpers["transfer_locked"]() is False
    finally:
        proc.kill()
        proc.wait()


@linux_only
def test_manual_holder_is_activity_even_when_autosync_failing(helpers):
    proc = _holder(TRANSFER_LOCK_HOLDER_TAG)
    try:
        helpers["_lock"].write_text(f"{proc.pid}\n")
        helpers["_marker"].write_text("count=5 since=2026-09-13T00:00:00Z\n")
        assert helpers["transfer_locked"]() is True
    finally:
        proc.kill()
        proc.wait()


def test_busy_processes_drops_s5cmd_only_while_failing(helpers):
    lines = (
        "111 s5cmd --log error cp --no-follow-symlinks /workspace/.swm_staging/autosync/* s3://b/ws/\n"
        "222 pip install torch\n"
    )
    helpers["sh"] = lambda _cmd: lines.strip()

    assert helpers["busy_processes"]() == [
        "111 s5cmd --log error cp --no-follow-symlinks /workspace/.swm_staging/autosync/* s3://b/ws/",
        "222 pip install torch",
    ]

    helpers["_marker"].write_text("count=5 since=2026-09-13T00:00:00Z\n")
    assert helpers["busy_processes"]() == ["222 pip install torch"]


def test_autosync_error_is_last_marker_line_truncated(helpers):
    assert helpers["autosync_failing"]() is False
    assert helpers["autosync_error"]() == ""
    helpers["_marker"].write_text(
        "count=5 since=2026-09-13T00:00:00Z\n" + "ERROR " + "x" * 400 + "\n",
    )
    assert helpers["autosync_failing"]() is True
    err = helpers["autosync_error"]()
    assert err.startswith("ERROR xxx")
    assert len(err) == 200


def test_status_payload_reports_autosync_health():
    assert '"autosync_failing": ' in _WATCHER_SOURCE
    assert '"autosync_error": ' in _WATCHER_SOURCE
    for key in ("transfer_locked", "busy_processes", "idle_seconds", "recent_fs_write"):
        assert f'"{key}": ' in _WATCHER_SOURCE
