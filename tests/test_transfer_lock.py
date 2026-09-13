"""The transfer lock names a LIVE holder: the autosync daemon or a tagged
background process started by a manual transfer. Anything else is stale.

Linux-only (setsid, /proc); run in Docker on macOS.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from swm import bootstrap
from swm.sync.paths import TRANSFER_LOCK_HOLDER_TAG

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="needs setsid and /proc",
)


def _alive(pid: int) -> bool:
    """Live and not a zombie: the container's PID 1 does not reap orphans."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            state = f.read().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return state != "Z"


def _manual_holder_cmd() -> str:
    # Two commands: a lone `sleep` would be exec'd in place by bash and the
    # tag would vanish from the cmdline.
    return f"bash -c 'sleep 30; true # {TRANSFER_LOCK_HOLDER_TAG}'"


@pytest.fixture
def procs():
    """Start background processes; kill whatever is left at teardown."""
    started: list[int] = []

    def start(cmd: str) -> int:
        out = subprocess.run(
            ["bash", "-c", f"setsid {cmd} </dev/null >/dev/null 2>&1 & echo $!"],
            capture_output=True, text=True, check=True,
        ).stdout
        pid = int(out.strip())
        started.append(pid)
        time.sleep(0.2)
        return pid

    yield start
    for pid in started:
        for sig in (signal.SIGKILL,):
            try:
                os.killpg(pid, sig)
            except ProcessLookupError:
                pass
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass


@pytest.fixture
def lock(pod_paths) -> Path:
    return Path(pod_paths["TRANSFER_LOCK"])


@pytest.fixture
def daemon_script(tmp_path) -> Path:
    d = tmp_path / "x"
    d.mkdir()
    return d / ".swm_autosync.sh"


def _cleanup_holder(session) -> None:
    if getattr(session, "_swm_transfer_lock_pid", None):
        bootstrap._release_transfer_lock(session)


# ── lock_owner ──────────────────────────────────────────────────────


def test_lock_owner_free(session, lock):
    assert bootstrap.lock_owner(session) == (None, "")


def test_lock_owner_stale_dead_pid_is_removed(session, lock):
    dead = subprocess.run(
        ["bash", "-c", "bash -c 'exit 0' & echo $!; wait"],
        capture_output=True, text=True,
    ).stdout.strip()
    lock.write_text(f"{dead}\n")

    assert bootstrap.lock_owner(session) == (None, dead)
    assert not lock.exists()


def test_lock_owner_stale_untagged_live_pid_is_removed_not_killed(session, lock, procs):
    pid = procs("sleep 30")
    lock.write_text(f"{pid}\n")

    assert bootstrap.lock_owner(session) == (None, str(pid))
    assert not lock.exists()
    assert _alive(pid)


def test_lock_owner_manual(session, lock, procs):
    pid = procs(_manual_holder_cmd())
    lock.write_text(f"{pid}\n")

    assert bootstrap.lock_owner(session) == ("manual", str(pid))
    assert lock.exists()


def test_lock_owner_autosync(session, lock, procs, daemon_script):
    daemon_script.write_text("#!/bin/bash\nsleep 30\n")
    pid = procs(f"bash {daemon_script}")
    lock.write_text(f"{pid}\n")

    assert bootstrap.lock_owner(session) == ("autosync", str(pid))
    assert lock.exists()


# ── _acquire_transfer_lock ─────────────────────────────────────────


def test_acquire_waits_for_autosync_then_holds(
    session, lock, procs, daemon_script, capsys, wide_console,
):
    daemon_script.write_text(f"#!/bin/bash\nsleep 2\nrm -f {lock}\nsleep 30\n")
    daemon = procs(f"bash {daemon_script}")
    lock.write_text(f"{daemon}\n")

    t0 = time.monotonic()
    try:
        assert bootstrap._acquire_transfer_lock(session, wait_seconds=10) is True
        elapsed = time.monotonic() - t0
        assert 2 <= elapsed < 10
        holder = int(lock.read_text().strip())
        assert holder == session._swm_transfer_lock_pid
        assert _alive(holder)
        assert bootstrap.lock_owner(session) == ("manual", str(holder))
        assert _alive(daemon)
    finally:
        _cleanup_holder(session)
    out = capsys.readouterr().out
    assert out.count("Waiting for the auto-sync cycle to finish") == 1


def test_acquire_times_out_on_autosync_without_killing_it(
    session, lock, procs, daemon_script,
):
    daemon_script.write_text("#!/bin/bash\nsleep 30\n")
    daemon = procs(f"bash {daemon_script}")
    lock.write_text(f"{daemon}\n")

    with pytest.raises(RuntimeError, match="Auto-sync has held the transfer lock"):
        bootstrap._acquire_transfer_lock(session, wait_seconds=1)
    assert _alive(daemon)
    assert lock.read_text().strip() == str(daemon)
    assert getattr(session, "_swm_transfer_lock_pid", None) is None


def test_acquire_raises_immediately_on_manual_holder(session, lock, procs):
    pid = procs(_manual_holder_cmd())
    lock.write_text(f"{pid}\n")

    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match=rf"already running \(PID {pid}\).*--force"):
        bootstrap._acquire_transfer_lock(session)
    assert time.monotonic() - t0 < 3
    assert _alive(pid)


def test_force_kills_manual_holder_and_takes_over(session, lock, procs):
    pid = procs(_manual_holder_cmd())
    lock.write_text(f"{pid}\n")

    try:
        assert bootstrap._acquire_transfer_lock(session, force=True) is True
        assert not _alive(pid)
        assert int(lock.read_text().strip()) == session._swm_transfer_lock_pid
    finally:
        _cleanup_holder(session)


def test_force_on_autosync_interrupts_children_never_the_daemon(
    session, lock, procs, daemon_script,
):
    # A daemon mid-cycle: its child transfer is killed, it "re-queues" and
    # releases the lock, and it must still be alive afterwards.
    daemon_script.write_text(f"#!/bin/bash\nsleep 30\nrm -f {lock}\nsleep 30\n")
    daemon = procs(f"bash {daemon_script}")
    lock.write_text(f"{daemon}\n")

    t0 = time.monotonic()
    try:
        assert bootstrap._acquire_transfer_lock(session, force=True) is True
        assert time.monotonic() - t0 < 30
        assert _alive(daemon)
        assert int(lock.read_text().strip()) == session._swm_transfer_lock_pid
    finally:
        _cleanup_holder(session)


def test_acquire_never_deletes_files(session, lock, tmp_path):
    lock.write_text("999999\n")
    try:
        assert bootstrap._acquire_transfer_lock(session) is True
    finally:
        _cleanup_holder(session)
    assert not any(
        "find" in c and "-delete" in c for c in session.commands
    ), "stale-lock handling must never run a deleting find"


def test_reentrant_acquire_and_release(session, lock):
    assert bootstrap._acquire_transfer_lock(session) is True
    holder = session._swm_transfer_lock_pid
    assert bootstrap._acquire_transfer_lock(session) is False
    assert session._swm_transfer_lock_pid == holder
    assert int(lock.read_text().strip()) == holder

    bootstrap._release_transfer_lock(session)
    assert not lock.exists()
    assert getattr(session, "_swm_transfer_lock_pid", None) is None
    time.sleep(0.3)
    assert not _alive(holder)


def test_start_lock_holder_loses_to_a_lock_created_meanwhile(session, lock):
    """The daemon may take the lock between lock_owner() and the holder's
    install (one SSH round trip apart). The holder installs with an
    exclusive create, so it must back off and clean up rather than clobber."""
    lock.write_text("424242\n")
    assert bootstrap._start_lock_holder(session) == 0
    assert lock.read_text().strip() == "424242"
    time.sleep(0.3)
    holders = subprocess.run(
        ["pgrep", "-f", TRANSFER_LOCK_HOLDER_TAG], capture_output=True, text=True,
    ).stdout.split()
    assert not any(_alive(int(pid)) for pid in holders)


def test_release_leaves_lock_that_is_no_longer_ours(session, lock):
    assert bootstrap._acquire_transfer_lock(session) is True
    lock.write_text("424242\n")
    bootstrap._release_transfer_lock(session)
    assert lock.read_text().strip() == "424242"


# ── _s5cmd_transfer / transfer_lock ────────────────────────────────


def test_s5cmd_transfer_holds_lock_only_for_its_duration(
    session, lock, s5cmd_shim, pod_paths,
):
    s5cmd_shim.observe_lock(str(lock))
    rc = bootstrap._s5cmd_transfer(session, "copy", "s5cmd cp a b")

    assert rc == 0
    assert s5cmd_shim.calls == ["cp a b"]
    seen = s5cmd_shim.lock_seen
    assert seen and seen[0].isdigit(), "lock must name the holder while s5cmd runs"
    assert not lock.exists()
    assert not _alive(int(seen[0]))
    assert getattr(session, "_swm_transfer_lock_pid", None) is None


def test_s5cmd_transfer_does_not_wrap_command_with_old_lock_trap(
    session, lock, s5cmd_shim, pod_paths, monkeypatch,
):
    calls: list[list[str]] = []

    def fake_call(cmd):
        calls.append(cmd)
        return 0

    monkeypatch.setattr(bootstrap.subprocess, "call", fake_call)
    bootstrap._s5cmd_transfer(session, "copy", "s5cmd cp a b")
    assert calls == [["bash", "-c", "s5cmd cp a b"]]


def test_s5cmd_transfer_inside_transfer_lock_does_not_release(
    session, lock, s5cmd_shim, pod_paths,
):
    with bootstrap.transfer_lock(session):
        holder = session._swm_transfer_lock_pid
        rc = bootstrap._s5cmd_transfer(session, "copy", "s5cmd cp a b")
        assert rc == 0
        assert int(lock.read_text().strip()) == holder
        assert _alive(holder)
    assert not lock.exists()
    time.sleep(0.3)
    assert not _alive(holder)


def test_transfer_lock_releases_on_exception(session, lock):
    with pytest.raises(ValueError):
        with bootstrap.transfer_lock(session):
            holder = session._swm_transfer_lock_pid
            raise ValueError("boom")
    assert not lock.exists()
    time.sleep(0.3)
    assert not _alive(holder)
