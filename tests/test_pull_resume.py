"""workspace_pull resumes an unfinished transfer instead of reporting a
truncated workspace as restored (the production failure: a 59 GB restore cut
off after 51 s came back as "finished with warnings")."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from swm.sync import pull


class _Session:
    def __init__(self, *, fresh: bool) -> None:
        self.fresh = fresh

    def exec(self, command, stream=True, line_callback=None):
        if command.startswith("ls -1A"):
            return 0, "" if self.fresh else "ComfyUI\n", ""
        return 0, "", ""


@pytest.fixture
def events() -> list[str]:
    return []


@pytest.fixture
def transfers(monkeypatch, events):
    calls: list[str] = []
    codes: list[int] = []

    def fake_transfer(session, label, command, force=False):
        events.append("transfer")
        calls.append(command)
        return codes.pop(0) if codes else 0

    @contextmanager
    def fake_lock(session, force=False):
        events.append(f"lock force={force}")
        yield
        events.append("unlock")

    monkeypatch.setattr(pull, "_s5cmd_transfer", fake_transfer)
    monkeypatch.setattr(pull, "transfer_lock", fake_lock)
    monkeypatch.setattr(pull, "_stop_pulls",
                        lambda session, same_pull: events.append(f"stop {same_pull}"))
    monkeypatch.setattr(pull, "_s3_env", lambda slug: "ENV=1")
    monkeypatch.setattr(pull, "restore_permissions", lambda *a, **k: None)
    monkeypatch.setattr(pull, "_repair_framework_links", lambda *a, **k: None)
    monkeypatch.setattr(pull, "start_watcher", lambda *a, **k: False)
    return calls, codes


def test_a_clean_pull_runs_once(transfers):
    calls, _ = transfers
    pull.workspace_pull(_Session(fresh=True), "b2", "bucket", "u1/ws")
    assert len(calls) == 1


def test_a_cut_off_fresh_restore_resumes_and_refetches_truncated_files(transfers):
    calls, codes = transfers
    codes.extend([1, 0])
    pull.workspace_pull(_Session(fresh=True), "b2", "bucket", "u1/ws")
    assert len(calls) == 2
    assert "--if-size-differ" in calls[1] and "--no-clobber" not in calls[1]


def test_a_pull_into_existing_data_resumes_without_overwriting(transfers):
    calls, codes = transfers
    codes.extend([1, 0])
    pull.workspace_pull(_Session(fresh=False), "b2", "bucket", "u1/ws")
    assert "--no-clobber" in calls[0] and "--no-clobber" in calls[1]
    assert "--if-size-differ" not in calls[1]


def test_a_restore_that_never_completes_fails_loudly(transfers):
    calls, codes = transfers
    codes.extend([1, 1, 1])
    with pytest.raises(RuntimeError, match="did not complete"):
        pull.workspace_pull(_Session(fresh=True), "b2", "bucket", "u1/ws")
    assert len(calls) == 1 + pull._PULL_RESUMES


def test_one_lock_covers_the_pull_and_its_resumes(transfers, events):
    """Released between attempts, a holder that could not be torn down over
    a dropped connection blocked the pull's own resume. The attempt cut off
    by the drop may still be running, so it is stopped first."""
    _, codes = transfers
    codes.extend([255, 0])
    pull.workspace_pull(_Session(fresh=True), "b2", "bucket", "u1/ws")
    assert events == [
        "lock force=False",
        "transfer",
        "stop s3://bucket/u1/ws/* /workspace/",
        "transfer",
        "unlock",
    ]


def test_a_forced_pull_stops_the_dead_sessions_pull_before_its_own(transfers, events):
    pull.workspace_pull(_Session(fresh=False), "b2", "bucket", "u1/ws", force=True)
    assert events == [
        "lock force=True",
        "stop s3://bucket/u1/ws/* /workspace/",
        "transfer",
        "unlock",
    ]


def test_bootstrap_hands_force_to_the_pull(monkeypatch):
    """A retried provisioning restores onto the pod its dead first attempt
    created, so it must be able to take that attempt's lock over."""
    import swm.sync.autosync as autosync
    from swm import bootstrap

    seen: dict = {}
    monkeypatch.setattr(bootstrap, "configure_storage", lambda *a, **k: None)
    monkeypatch.setattr(pull, "workspace_pull", lambda *a, **k: seen.update(k))
    monkeypatch.setattr(autosync, "start_autosync", lambda *a, **k: True)

    failed = bootstrap.bootstrap_workspace_on_pod(
        _Session(fresh=False), "b2", "bucket", "u1/ws",
        qualified_id="vastai:1", is_new=False, force=True)

    assert failed == []
    assert seen["force"] is True
