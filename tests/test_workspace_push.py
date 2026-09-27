"""workspace_push: lock ordering, snapshot re-queue, and tier-3 stamping.

Runs the real push code against a tmp workspace with a local bash session and
a fake s5cmd. The transfer lock itself is covered in test_transfer_lock.py;
here it only has to be held at the right moments.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

import swm.sync.push as push
from swm.sync.paths import TRANSFER_LOCK_HOLDER_TAG

linux_only = pytest.mark.skipif(
    sys.platform != "linux", reason="in-place uploads stat files with GNU stat -c",
)


@pytest.fixture
def workspace(tmp_path, pod_paths) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def no_watcher(monkeypatch):
    monkeypatch.setattr(push, "start_watcher", lambda session, src: False)
    monkeypatch.setattr(push, "is_watcher_alive", lambda session: False)


def _write_stamp(pod_paths, age_seconds: float = 60.0) -> None:
    stamp = Path(pod_paths["PUSH_STAMP"])
    stamp.write_text("")
    past = time.time() - age_seconds
    os.utime(stamp, (past, past))


def _index_of(commands: list[str], *needles: str) -> int:
    for i, c in enumerate(commands):
        if all(n in c for n in needles):
            return i
    raise AssertionError(f"no command containing {needles!r} in {commands!r}")


# ── ordering / re-queue ────────────────────────────────────────────


def test_lock_is_held_before_snapshot_and_snapshot_requeued_on_error(
    session, workspace, pod_paths, monkeypatch, s5cmd_shim,
):
    _write_stamp(pod_paths)
    (workspace / "a.txt").write_text("a")
    watch_log = Path(pod_paths["WATCH_LOG"])
    watch_log.write_text(f"{workspace}/a.txt\n{workspace}/gone.txt\n")
    monkeypatch.setattr(push, "start_watcher", lambda session, src: True)
    monkeypatch.setattr(push, "is_watcher_alive", lambda session: True)

    def exploding_transfer(session, label, cmd, force=False):
        raise RuntimeError("ssh dropped")

    monkeypatch.setattr(push, "_s5cmd_transfer", exploding_transfer)

    with pytest.raises(RuntimeError, match="ssh dropped"):
        push.workspace_push(
            session, "b2", "bucket", "ws", src=str(workspace), delete=True,
        )

    cmds = session.commands
    holder_started = _index_of(cmds, TRANSFER_LOCK_HOLDER_TAG, "sleep")
    snapshot = _index_of(
        cmds, f"cp {pod_paths['WATCH_LOG']}", f": > {pod_paths['WATCH_LOG']}",
    )
    requeue = _index_of(
        cmds, f"cat {pod_paths['_WATCH_SNAP']} >> {pod_paths['WATCH_LOG']}",
    )
    assert holder_started < snapshot < requeue
    assert watch_log.read_text() == f"{workspace}/a.txt\n{workspace}/gone.txt\n"
    assert not Path(pod_paths["TRANSFER_LOCK"]).exists()
    assert not Path(pod_paths["PUSH_STAMP"]).stat().st_mtime > time.time() - 30


def test_staging_root_clear_happens_under_the_lock(
    session, workspace, pod_paths, no_watcher, s5cmd_shim,
):
    (workspace / "a.txt").write_text("a")
    push.workspace_push(session, "b2", "bucket", "ws", src=str(workspace))

    cmds = session.commands
    holder_started = _index_of(cmds, TRANSFER_LOCK_HOLDER_TAG, "sleep")
    root_clear = _index_of(cmds, f"{workspace}/.swm_staging", "-delete")
    assert holder_started < root_clear
    assert not Path(pod_paths["TRANSFER_LOCK"]).exists()


# ── tier 2: find -newer sees symlinks and materialises them ────────


def test_find_tier_stages_symlinks_and_uploads_without_following(
    session, workspace, pod_paths, no_watcher, s5cmd_shim, capsys, wide_console,
):
    old = workspace / "old.txt"
    old.write_text("old")
    os.utime(old, (time.time() - 200, time.time() - 200))
    _write_stamp(pod_paths, age_seconds=60)
    (workspace / "new.txt").write_text("new")
    (workspace / "new_link").symlink_to("old.txt")
    (workspace / "dangling").symlink_to("missing")

    rc = push.workspace_push(session, "b2", "bucket", "ws", src=str(workspace))

    assert rc == 0
    assert len(s5cmd_shim.calls) == 1
    argv = s5cmd_shim.calls[0]
    assert "--no-follow-symlinks" in argv
    assert f"{workspace}/.swm_staging/push/*" in argv
    staged = Path(pod_paths["_FILELIST"])
    assert not staged.exists()
    out = capsys.readouterr().out
    assert "3 file(s) changed" in out
    assert "1 symlink(s) materialised" in out
    assert "skipped 1 symlink(s)" in out
    assert Path(pod_paths["PUSH_STAMP"]).stat().st_mtime > time.time() - 30


# ── quota-refused staging links upload in place ────────────────────


def _watcher_alive(monkeypatch) -> None:
    monkeypatch.setattr(push, "start_watcher", lambda session, src: True)
    monkeypatch.setattr(push, "is_watcher_alive", lambda session: True)


@linux_only
def test_watcher_push_uploads_a_quota_refused_file_in_place(
    session, workspace, pod_paths, monkeypatch, s5cmd_shim, fake_ln,
):
    _write_stamp(pod_paths)
    _watcher_alive(monkeypatch)
    (workspace / "a.txt").write_text("a")
    (workspace / "models").mkdir()
    (workspace / "models" / "big.bin").write_text("big")
    Path(pod_paths["WATCH_LOG"]).write_text(
        f"{workspace}/a.txt\n{workspace}/models/big.bin\n")
    fake_ln("big.bin")

    rc = push.workspace_push(session, "b2", "bucket", "ws", src=str(workspace))

    assert rc == 0
    staged_copy, in_place = s5cmd_shim.calls
    assert f"{workspace}/.swm_staging/push/*" in staged_copy
    assert "--no-follow-symlinks" in in_place
    assert in_place.endswith(
        f"{workspace}/models/big.bin s3://bucket/ws/models/big.bin")
    assert Path(pod_paths["PUSH_STAMP"]).stat().st_mtime > time.time() - 30


@linux_only
def test_push_with_every_file_quota_refused_skips_the_empty_staged_copy(
    session, workspace, pod_paths, monkeypatch, s5cmd_shim, fake_ln,
):
    _write_stamp(pod_paths)
    _watcher_alive(monkeypatch)
    (workspace / "big.bin").write_text("big")
    Path(pod_paths["WATCH_LOG"]).write_text(f"{workspace}/big.bin\n")
    fake_ln("big.bin")

    rc = push.workspace_push(session, "b2", "bucket", "ws", src=str(workspace))

    assert rc == 0
    assert len(s5cmd_shim.calls) == 1
    assert s5cmd_shim.calls[0].endswith(f"{workspace}/big.bin s3://bucket/ws/big.bin")


@linux_only
def test_in_place_upload_of_a_file_that_changed_meanwhile_fails_the_push(
    session, workspace, pod_paths, no_watcher, monkeypatch, s5cmd_shim, fake_ln,
):
    _write_stamp(pod_paths)
    big = workspace / "big.bin"
    big.write_text("big")
    fake_ln("big.bin")
    real_transfer = push._s5cmd_transfer

    def transfer_while_writing(session, label, cmd, force=False):
        rc = real_transfer(session, label, cmd, force=force)
        if "big.bin s3://" in cmd:
            with big.open("a") as f:
                f.write(" and more")
        return rc

    monkeypatch.setattr(push, "_s5cmd_transfer", transfer_while_writing)

    rc = push.workspace_push(session, "b2", "bucket", "ws", src=str(workspace))

    assert rc != 0
    assert not Path(pod_paths["PUSH_STAMP"]).stat().st_mtime > time.time() - 30


# ── tier 3 ─────────────────────────────────────────────────────────


def test_first_push_retries_missing_objects_then_stamps(
    session, workspace, pod_paths, no_watcher, s5cmd_shim,
):
    (workspace / "a.txt").write_text("a")
    s5cmd_shim.set_rcs(1, 0)

    rc = push.workspace_push(session, "b2", "bucket", "ws", src=str(workspace))

    assert rc == 0
    calls = s5cmd_shim.calls
    assert len(calls) == 2
    assert "--no-follow-symlinks" in calls[0]
    assert " -n " not in f" {calls[0]} "
    assert " -n " in f" {calls[1]} "
    assert "--no-follow-symlinks" in calls[1]
    assert Path(pod_paths["PUSH_STAMP"]).exists()


def test_first_push_never_stamps_after_failed_retry_even_with_force(
    session, workspace, pod_paths, no_watcher, s5cmd_shim, capsys, wide_console,
):
    (workspace / "a.txt").write_text("a")
    s5cmd_shim.set_rcs(1, 1)

    rc = push.workspace_push(
        session, "b2", "bucket", "ws", src=str(workspace), force=True,
    )

    assert rc == 1
    assert len(s5cmd_shim.calls) == 2
    assert not Path(pod_paths["PUSH_STAMP"]).exists()
    out = capsys.readouterr().out
    assert "Stamp NOT written" in out
    assert "Advancing stamp anyway" not in out


def test_first_push_materialises_symlinks_in_a_second_pass(
    session, workspace, pod_paths, no_watcher, s5cmd_shim, capsys, wide_console,
):
    (workspace / "venv" / "bin").mkdir(parents=True)
    (workspace / "venv" / "bin" / "python3.11").write_text("#!/bin/sh\n")
    (workspace / "venv" / "bin" / "python3").symlink_to("python3.11")
    (workspace / "venv" / "bin" / "python").symlink_to("python3")
    (workspace / "broken").symlink_to("nowhere")
    (workspace / "dirlink").symlink_to("venv")
    (workspace / ".swm_staging" / "push").mkdir(parents=True)
    (workspace / ".swm_staging" / "push" / "stray").symlink_to("../../broken")

    rc = push.workspace_push(session, "b2", "bucket", "ws", src=str(workspace))

    assert rc == 0
    calls = s5cmd_shim.calls
    assert len(calls) == 2
    assert f" {workspace}/ s3://bucket/ws/" in calls[0]
    assert "--no-follow-symlinks" in calls[1]
    assert f" {workspace}/.swm_staging/push/* s3://bucket/ws/" in calls[1]
    staging = workspace / ".swm_staging" / "push"
    assert [p for p in staging.rglob("*") if p.is_symlink() or not p.is_dir()] == []
    out = capsys.readouterr().out
    assert "2 symlink(s) materialised" in out
    assert "skipped 2 symlink(s)" in out
    assert "Pushing 2 materialised symlink(s)" in out
    assert Path(pod_paths["PUSH_STAMP"]).exists()


def test_first_push_without_symlinks_runs_a_single_copy(
    session, workspace, pod_paths, no_watcher, s5cmd_shim,
):
    (workspace / "a.txt").write_text("a")
    rc = push.workspace_push(session, "b2", "bucket", "ws", src=str(workspace))
    assert rc == 0
    assert len(s5cmd_shim.calls) == 1
    assert Path(pod_paths["PUSH_STAMP"]).exists()
