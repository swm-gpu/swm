"""swm download: one stat round trip, compression only where it helps."""
from __future__ import annotations

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
