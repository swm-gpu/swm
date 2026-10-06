"""tar_pull's extract writes 1 MiB records. RunPod's /workspace is MooseFS over
FUSE, where every write() is a round-trip to the mount daemon: with tar's
default 10 KiB record a 61 GB restore wrote at 93 MB/s on a volume that took
371 MB/s with 1 MiB records (and 500 MB/s from dd)."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile

import pytest

from swm.sync import pull

GIB = 1024 ** 3


def test_extract_uses_1mib_records_and_keeps_one_checkpoint_per_gib():
    pipeline = pull.tar_extract_pipeline("cat", "/w/.swm_workspace.tar.zst", "/w")

    assert "-b 2048" in pull._TAR_GNU_FLAGS
    assert "--checkpoint=1024 " in pull._TAR_GNU_FLAGS
    assert 2048 * 512 * 1024 == GIB
    assert "tar --version 2>/dev/null | grep -q GNU && TARF=" in pipeline
    assert "| tar $TARF -xf - -C '/w'" in pipeline
    assert subprocess.run(["bash", "-n"], input=pipeline, text=True,
                          check=False).returncode == 0


@pytest.mark.parametrize("version_line, expects_flags", [
    ("tar (GNU tar) 1.34", True),
    ("bsdtar 3.7.4 - libarchive 3.7.4", False),
    ("tar: unrecognized option: version", False),  # busybox
])
def test_record_size_and_checkpoint_only_reach_gnu_tar(tmp_path, version_line,
                                                        expects_flags):
    """busybox tar knows neither -b nor --checkpoint and would fail the restore
    on them, so both flags sit behind the GNU version probe. The shim answers
    --version like each implementation and echoes the argv it would run."""
    shim = tmp_path / "bin"
    shim.mkdir()
    tar = shim / "tar"
    tar.write_text("#!/bin/sh\n"
                   f"if [ \"$1\" = --version ]; then echo '{version_line}'; exit 0; fi\n"
                   "cat >/dev/null; echo \"tar $*\"\n")
    tar.chmod(0o755)
    staged = tmp_path / "staged.tar"
    staged.write_bytes(b"x" * 512)

    run = subprocess.run(
        ["bash", "-c", pull.tar_extract_pipeline("cat", str(staged), str(tmp_path))],
        capture_output=True, text=True, check=False,
        env={**os.environ, "PATH": f"{shim}:{os.environ['PATH']}"})

    assert run.returncode == 0, run.stderr
    argv = run.stdout.splitlines()[0]
    assert argv.startswith("tar ") and argv.endswith(f"-C {tmp_path}")
    assert (pull._TAR_GNU_FLAGS in argv) is expects_flags
    assert "extract stages: decompress=0 extract=0" in run.stdout


@pytest.mark.skipif(shutil.which("tar") is None, reason="tar not installed")
def test_pipeline_extracts_a_real_archive_through_the_system_tar(tmp_path):
    """A 1 MiB record over a 64 KiB pipe only works because GNU tar refills
    short reads from stdin; this runs the rendered command against the real
    tar on the box (GNU on Linux CI, where the flags are live)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as archive:
        for name, size in (("a.bin", 3 * 1024 * 1024 + 7), ("dir/b.txt", 13)):
            info = tarfile.TarInfo(name)
            info.size = size
            archive.addfile(info, io.BytesIO(b"\xab" * size))
    staged = tmp_path / "staged.tar"
    staged.write_bytes(buf.getvalue())
    dest = tmp_path / "dest"
    dest.mkdir()

    run = subprocess.run(
        ["bash", "-c", pull.tar_extract_pipeline("cat", str(staged), str(dest))],
        capture_output=True, text=True, check=False)

    assert run.returncode == 0, run.stdout + run.stderr
    assert "extract stages: decompress=0 extract=0" in run.stdout
    assert (dest / "a.bin").stat().st_size == 3 * 1024 * 1024 + 7
    assert (dest / "dir" / "b.txt").read_bytes() == b"\xab" * 13
