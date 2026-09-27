"""swm download: one stat round trip, compression only where it helps."""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from swm.remote import ssh as ssh_mod
from swm.remote.ssh import RemoteSession, worth_compressing


def _session_with(output: str) -> tuple[RemoteSession, list[str]]:
    calls: list[str] = []

    def fake_exec(command, stream=True, **_):
        calls.append(command)
        return 0, output, ""

    sess = RemoteSession("h")
    sess.exec = fake_exec  # type: ignore[method-assign]
    return sess, calls


def test_worth_compressing_by_extension():
    assert not worth_compressing("/workspace/ComfyUI/output/x.mp4")
    assert not worth_compressing("/workspace/models/unet.SAFETENSORS")
    assert worth_compressing("/workspace/notes.txt")
    assert worth_compressing("/workspace/script.py")


def test_stat_path_single_file_uses_one_round_trip():
    sess, calls = _session_with("FILE\n")
    assert sess.stat_path("/workspace/out.mp4") == (False, 0, False)
    assert sess.stat_path("/workspace/out.json") == (False, 0, True)
    assert len(calls) == 2


def test_stat_path_directory_reports_count_and_skips_gzip_for_media():
    sess, _ = _session_with("DIR 130 128\n")
    assert sess.stat_path("/workspace/ComfyUI/output") == (True, 130, False)
    sess, _ = _session_with("DIR 40 3\n")
    assert sess.stat_path("/workspace/src") == (True, 40, True)
    sess, _ = _session_with("DIR 0 0\n")
    assert sess.stat_path("/workspace/empty") == (True, 0, True)


def test_stat_path_tolerates_garbled_output():
    sess, _ = _session_with("DIR x\n")
    assert sess.stat_path("/workspace/d") == (True, 0, True)


def test_ssh_options_multiplex_connections():
    opts = " ".join(ssh_mod._SSH_OPTS)
    assert "ControlMaster=auto" in opts and "ControlPersist=" in opts
    assert len("/tmp/swm-ssh-" + "0" * 40) < 104


# ── download_dir: tar-over-SSH against a fake ssh on PATH ──────────
# The watchdog outlives the assertion timeout below, so a blocked remote
# fails the test with its own message instead of hanging the suite.

_SSH_SHIM = """#!/bin/bash
( sleep 60; kill -9 $$ 2>/dev/null ) >/dev/null 2>&1 </dev/null &
printf '%s\\n' "$*" >> "$SWM_SSH_LOG"
if [ -n "${SWM_SSH_STDERR_TEXT:-}" ]; then
  printf '%s\\n' "$SWM_SSH_STDERR_TEXT" >&2
fi
if [ "${SWM_SSH_STDERR_BYTES:-0}" -gt 0 ]; then
  head -c "${SWM_SSH_STDERR_BYTES}" /dev/zero | tr '\\0' 'w' >&2
fi
if [ -n "${SWM_SSH_TREE:-}" ]; then
  tar czf - -C "$(dirname "${SWM_SSH_TREE}")" "$(basename "${SWM_SSH_TREE}")"
fi
exit "${SWM_SSH_RC:-0}"
"""

_STDERR_FLOOD_BYTES = 262144


@pytest.fixture
def ssh_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "ssh"
    shim.write_text(_SSH_SHIM)
    shim.chmod(0o755)
    log = tmp_path / "argv.log"
    monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("SWM_SSH_LOG", str(log))
    for var in ("SWM_SSH_STDERR_BYTES", "SWM_SSH_STDERR_TEXT",
                "SWM_SSH_TREE", "SWM_SSH_RC"):
        monkeypatch.delenv(var, raising=False)
    return log


def _tree(tmp_path: Path) -> Path:
    tree = tmp_path / "video"
    tree.mkdir()
    (tree / "a.mp4").write_bytes(b"a" * 4096)
    (tree / "b.mp4").write_bytes(b"b" * 4096)
    return tree


def _download_dir_within(remote: str, dest: str, timeout: float = 15.0) -> None:
    done = threading.Event()
    raised: list[BaseException] = []

    def run() -> None:
        try:
            RemoteSession("pod").download_dir(remote, dest)
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            raised.append(exc)
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    assert done.wait(timeout), (
        "download_dir never finished: the remote blocked writing stderr"
    )
    if raised:
        raise raised[0]


def test_download_dir_survives_a_stderr_flood_from_remote_tar(
    ssh_shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SWM_SSH_TREE", str(_tree(tmp_path)))
    monkeypatch.setenv("SWM_SSH_STDERR_BYTES", str(_STDERR_FLOOD_BYTES))
    dest = tmp_path / "local"

    _download_dir_within("/workspace/ComfyUI/output/video", str(dest))

    assert (dest / "video" / "a.mp4").read_bytes() == b"a" * 4096
    assert (dest / "video" / "b.mp4").read_bytes() == b"b" * 4096


def test_download_dir_surfaces_remote_stderr_when_ssh_fails(
    ssh_shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SWM_SSH_TREE", str(_tree(tmp_path)))
    monkeypatch.setenv("SWM_SSH_STDERR_TEXT",
                       "tar: /workspace/x.sock: socket ignored")
    monkeypatch.setenv("SWM_SSH_RC", "2")

    with pytest.raises(RuntimeError, match="socket ignored"):
        _download_dir_within("/workspace/ComfyUI/output/video",
                             str(tmp_path / "local"))


def test_download_dir_quotes_a_remote_path_containing_a_quote(
    ssh_shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SWM_SSH_TREE", str(_tree(tmp_path)))

    _download_dir_within("/workspace/bob's outputs/video",
                         str(tmp_path / "local"))

    assert "-C '/workspace/bob'\\''s outputs' 'video'" in ssh_shim.read_text()
