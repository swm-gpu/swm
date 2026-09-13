"""workspace_pull is unchanged; it must still work with the lock now taken
and released inside _s5cmd_transfer."""

from __future__ import annotations

from pathlib import Path

import swm.sync.pull as pull


def test_workspace_pull_holds_lock_during_copy_and_releases(
    session, tmp_path, pod_paths, s5cmd_shim, monkeypatch,
):
    monkeypatch.setattr(pull, "PUSH_STAMP", pod_paths["PUSH_STAMP"])
    monkeypatch.setattr(pull, "WATCH_LOG", pod_paths["WATCH_LOG"])
    monkeypatch.setattr(pull, "_s3_env", lambda slug: "SWM_TEST_ENV=1")
    monkeypatch.setattr(pull, "start_watcher", lambda session, dest: False)
    s5cmd_shim.observe_lock(pod_paths["TRANSFER_LOCK"])
    dest = tmp_path / "dest"

    pull.workspace_pull(session, "b2", "bucket", "ws", dest=str(dest))

    assert s5cmd_shim.calls == [f"cp --show-progress s3://bucket/ws/* {dest}/"]
    assert s5cmd_shim.lock_seen and s5cmd_shim.lock_seen[0].isdigit()
    assert not Path(pod_paths["TRANSFER_LOCK"]).exists()
    assert Path(pod_paths["PUSH_STAMP"]).exists()
    assert getattr(session, "_swm_transfer_lock_pid", None) is None
