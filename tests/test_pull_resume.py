"""workspace_pull resumes an unfinished transfer instead of reporting a
truncated workspace as restored (the production failure: a 59 GB restore cut
off after 51 s came back as "finished with warnings")."""

from __future__ import annotations

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
def transfers(monkeypatch):
    calls: list[str] = []
    codes: list[int] = []

    def fake_transfer(session, label, command, force=False):
        calls.append(command)
        return codes.pop(0) if codes else 0

    monkeypatch.setattr(pull, "_s5cmd_transfer", fake_transfer)
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
