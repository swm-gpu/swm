"""Shared fixtures: a local stand-in for RemoteSession and a fake s5cmd.

The sync code drives a pod through shell one-liners over SSH. Running those
same one-liners in a local bash exercises them faithfully without a pod, and
a fake ``s5cmd`` on PATH lets the transfer paths run without network or
credentials.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


class LocalSession:
    """Runs ``exec`` commands in a local bash and records them in order."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def exec(
        self,
        command: str,
        stream: bool = True,
        line_callback=None,
    ) -> tuple[int, str, str]:
        self.commands.append(command)
        proc = subprocess.run(
            ["bash", "-c", command], capture_output=True, text=True,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def _ssh_cmd(self) -> list[str]:
        return ["bash", "-c"]


_SHIM = """#!/bin/bash
# Fake s5cmd: append argv to $SWM_SHIM_LOG and exit with the n-th code of
# the comma-separated $SWM_SHIM_RCS (last code repeats).
printf '%s\\n' "$*" >> "$SWM_SHIM_LOG"
if [ -n "${SWM_SHIM_LOCK:-}" ]; then
  printf '%s\\n' "$(cat "$SWM_SHIM_LOCK" 2>/dev/null || echo none)" >> "$SWM_SHIM_LOG.lock"
fi
n=$(wc -l < "$SWM_SHIM_LOG" | tr -d ' ')
IFS=, read -ra rcs <<< "${SWM_SHIM_RCS:-0}"
idx=$((n - 1))
[ "$idx" -ge "${#rcs[@]}" ] && idx=$((${#rcs[@]} - 1))
exit "${rcs[$idx]}"
"""


class S5cmdShim:
    def __init__(self, log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._log = log
        self._mp = monkeypatch

    def set_rcs(self, *rcs: int) -> None:
        self._mp.setenv("SWM_SHIM_RCS", ",".join(str(rc) for rc in rcs))

    def observe_lock(self, path: str) -> None:
        self._mp.setenv("SWM_SHIM_LOCK", path)

    @property
    def calls(self) -> list[str]:
        return self._log.read_text().splitlines() if self._log.exists() else []

    @property
    def lock_seen(self) -> list[str]:
        p = Path(str(self._log) + ".lock")
        return p.read_text().splitlines() if p.exists() else []


@pytest.fixture
def session() -> LocalSession:
    return LocalSession()


@pytest.fixture
def s5cmd_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> S5cmdShim:
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "s5cmd"
    shim.write_text(_SHIM)
    shim.chmod(0o755)
    log = tmp_path / "s5cmd.calls"
    monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("SWM_SHIM_LOG", str(log))
    monkeypatch.setenv("SWM_SHIM_RCS", "0")
    monkeypatch.delenv("SWM_SHIM_LOCK", raising=False)
    return S5cmdShim(log, monkeypatch)


FAKE_LN = """#!/bin/bash
# Fake ln: refuse (with $SWM_FAKE_LN_ERROR, as coreutils words it) any link
# whose source path contains $SWM_FAKE_LN_MATCH; pass everything else on.
src="${@: -2:1}"
if [ -n "${SWM_FAKE_LN_MATCH:-}" ] && [[ "$src" == *"$SWM_FAKE_LN_MATCH"* ]]; then
  echo "ln: failed to create hard link '${@: -1}': $SWM_FAKE_LN_ERROR" >&2
  exit 1
fi
exec /bin/ln "$@"
"""


@pytest.fixture
def fake_ln(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Returns refuse(name, error) that makes ``ln`` fail for sources matching name."""
    bin_dir = tmp_path / "lnbin"
    bin_dir.mkdir()
    shim = bin_dir / "ln"
    shim.write_text(FAKE_LN)
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    def refuse(name: str, error: str = "Disk quota exceeded") -> None:
        monkeypatch.setenv("SWM_FAKE_LN_MATCH", name)
        monkeypatch.setenv("SWM_FAKE_LN_ERROR", error)

    return refuse


@pytest.fixture
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop Rich from wrapping console lines so tests can match whole messages."""
    monkeypatch.setenv("COLUMNS", "500")


@pytest.fixture
def pod_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Point every on-pod bookkeeping path the push code uses into tmp_path."""
    import swm.sync.paths as paths
    import swm.sync.push as push

    state = tmp_path / "state"
    state.mkdir()
    mapping = {
        "PUSH_STAMP": str(state / "last_push"),
        "WATCH_LOG": str(state / "changes.log"),
        "DELETED_LIST": str(state / "push_deleted"),
        "TAR_PATH": str(state / "workspace.tar.gz"),
        "_FILELIST": str(state / "push_files"),
        "_FINDLIST": str(state / "push_find_files"),
        "_WATCH_SNAP": str(state / "push_watch_snap"),
        "_CYCLE_MARK": str(state / "push_cycle_mark"),
        "_IN_PLACE": str(state / "push_in_place"),
    }
    for name, value in mapping.items():
        monkeypatch.setattr(push, name, value)
    lock = str(state / "transfer.lock")
    monkeypatch.setattr(paths, "TRANSFER_LOCK", lock)
    mapping["TRANSFER_LOCK"] = lock
    monkeypatch.setattr(push, "_s3_env", lambda slug: "SWM_TEST_ENV=1")
    return mapping
