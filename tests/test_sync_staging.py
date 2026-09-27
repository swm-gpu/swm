"""Hardlink staging must never put a symlink inode into the staging tree.

s5cmd follows symlinks by default and, on one it cannot resolve, halts the
walk of that whole top-level entry and exits 1 — the failure mode behind the
wedged-autosync incident. Symlinks are therefore materialised as their
target file under the link name, or skipped and counted.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from swm.sync._common import clear_staged_files, stage_hardlinks


def _files_under(root: Path) -> list[Path]:
    return sorted(
        p.relative_to(root) for p in root.rglob("*")
        if p.is_symlink() or not p.is_dir()
    )


def test_stage_hardlinks_materialises_or_skips_symlinks(
    session, tmp_path, capsys, wide_console,
):
    ws = tmp_path / "ws"
    (ws / "d").mkdir(parents=True)
    (ws / "a.txt").write_text("a")
    (ws / "d" / "b.txt").write_text("b")
    (ws / "d" / "hidden.txt").write_text("h")
    (ws / "link_sibling").symlink_to("a.txt")
    (ws / "link_hidden").symlink_to("d/hidden.txt")
    (ws / "link_abs").symlink_to(ws / "d" / "b.txt")
    (ws / "dangling").symlink_to("nope.txt")
    (ws / "dirlink").symlink_to("d")

    filelist = tmp_path / "files"
    filelist.write_text("".join(
        f"{ws / name}\n" for name in (
            "a.txt", "d/b.txt", "link_sibling", "link_hidden", "link_abs",
            "dangling", "dirlink",
        )
    ))

    staging = Path(stage_hardlinks(session, str(filelist), str(ws)))

    assert staging == ws / ".swm_staging" / "push"
    assert _files_under(staging) == [
        Path("a.txt"), Path("d/b.txt"), Path("link_abs"),
        Path("link_hidden"), Path("link_sibling"),
    ]
    for p in staging.rglob("*"):
        assert not p.is_symlink(), p
    assert (staging / "a.txt").stat().st_ino == (ws / "a.txt").stat().st_ino
    assert (staging / "link_sibling").stat().st_ino == (ws / "a.txt").stat().st_ino
    assert (staging / "link_hidden").stat().st_ino == (ws / "d" / "hidden.txt").stat().st_ino
    assert (staging / "link_abs").stat().st_ino == (ws / "d" / "b.txt").stat().st_ino

    out = capsys.readouterr().out
    assert re.search(r"skipped 2 symlink\(s\) that cannot be materialised", out)
    assert re.search(r"3 symlink\(s\)", out)


def test_stage_hardlinks_regular_files_only_prints_no_symlink_summary(
    session, tmp_path, capsys, wide_console,
):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("a")
    filelist = tmp_path / "files"
    filelist.write_text(f"{ws / 'a.txt'}\n{ws / 'vanished.txt'}\n")

    staging = Path(stage_hardlinks(session, str(filelist), str(ws)))

    assert _files_under(staging) == [Path("a.txt")]
    assert "symlink" not in capsys.readouterr().out


def test_clear_staged_files_removes_symlinks_and_keeps_skeleton(session, tmp_path):
    staging = tmp_path / "ws" / ".swm_staging" / "push"
    (staging / "sub").mkdir(parents=True)
    (staging / "f").write_text("x")
    (staging / "sub" / "lnk").symlink_to("../f")
    (staging / "dangling").symlink_to("missing")

    clear_staged_files(session, str(staging))

    assert staging.is_dir() and (staging / "sub").is_dir()
    assert _files_under(staging) == []
    assert not os.path.lexists(staging / "dangling")


# A quota that charges every hardlink its full size (MooseFS network volumes)
# refuses to stage a large new file even though it fits on the volume once.


def test_stage_hardlinks_lists_quota_refused_files_for_in_place_upload(
    session, tmp_path, fake_ln,
):
    ws = tmp_path / "ws"
    (ws / "models").mkdir(parents=True)
    (ws / "a.txt").write_text("a")
    (ws / "models" / "big.bin").write_text("big")
    (ws / "big_link").symlink_to("models/big.bin")
    filelist = tmp_path / "files"
    filelist.write_text(f"{ws}/a.txt\n{ws}/models/big.bin\n{ws}/big_link\n")
    in_place = tmp_path / "in_place"
    in_place.write_text("stale entry from an earlier push\n")
    fake_ln("big.bin")

    staging = Path(stage_hardlinks(session, str(filelist), str(ws), str(in_place)))

    assert _files_under(staging) == [Path("a.txt")]
    assert in_place.read_text() == (
        f"{ws}/models/big.bin\tmodels/big.bin\n"
        f"{ws}/models/big.bin\tbig_link\n"
    )


def test_stage_hardlinks_other_link_failures_abort_with_the_real_error(
    session, tmp_path, fake_ln,
):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("a")
    filelist = tmp_path / "files"
    filelist.write_text(f"{ws}/a.txt\n")
    fake_ln("a.txt", "Operation not permitted")

    with pytest.raises(RuntimeError, match=r"a\.txt.*Operation not permitted"):
        stage_hardlinks(session, str(filelist), str(ws), str(tmp_path / "in_place"))

    assert _files_under(ws / ".swm_staging" / "push") == []
