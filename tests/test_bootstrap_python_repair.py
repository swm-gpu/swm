"""Regression tests for the workspace-Python repair scripts (bootstrap):
_python_link_repair_script and _python_incomplete_install_script.

The script is a bash one-liner that runs on pods over SSH; these tests run
it locally against synthetic /workspace/.python layouts. `sort -V` is GNU
coreutils; on macOS a small shim provides a real version sort so the
newest-full-version selection is exercised faithfully.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

from swm.bootstrap import _python_incomplete_install_script, _python_link_repair_script

PLAT = "linux-x86_64-gnu"


def _make_full(python_dir: Path, patch: str, *, with_py3_link: bool) -> Path:
    full = python_dir / f"cpython-3.11.{patch}-{PLAT}"
    (full / "bin").mkdir(parents=True)
    binary = full / "bin" / "python3.11"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if with_py3_link:
        (full / "bin" / "python3").symlink_to("python3.11")
    return full


def _sort_shim(tmp_path: Path) -> dict[str, str]:
    """PATH with a `sort` that implements -V correctly on any platform."""
    if sys.platform.startswith("linux"):
        return os.environ.copy()
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir(exist_ok=True)
    shim = shim_dir / "sort"
    shim.write_text(
        "#!/bin/sh\n"
        'exec python3 -c "\n'
        "import sys, re\n"
        "args = [a for a in sys.argv[1:] if a != '-V']\n"
        "src = open(args[0]).read() if args else sys.stdin.read()\n"
        "def key(s):\n"
        "    return [int(t) if t.isdigit() else t for t in re.split(r'(\\d+)', s)]\n"
        "sys.stdout.write(''.join(sorted(src.splitlines(True), key=key)))\n"
        '" "$@"\n'
    )
    shim.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{shim_dir}:{env['PATH']}"
    return env


def _run_repair(tmp_path: Path, python_dir: Path) -> subprocess.CompletedProcess:
    script = _python_link_repair_script().replace(
        "/workspace/.python", str(python_dir))
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True,
        env=_sort_shim(tmp_path), timeout=30, check=False,
    )


def test_repair_materialized_minor_link_and_missing_python3(tmp_path):
    """The production failure: minor slot is a real directory, and the full
    install lost its bin/python3 file symlink during sync."""
    python_dir = tmp_path / ".python"
    python_dir.mkdir()
    full = _make_full(python_dir, "15", with_py3_link=False)
    materialized = python_dir / f"cpython-3.11-{PLAT}"
    materialized.mkdir()  # a real directory where a symlink belongs

    result = _run_repair(tmp_path, python_dir)

    assert result.returncode == 0, result.stderr
    assert materialized.is_symlink()
    assert materialized.resolve() == full.resolve()
    py3 = full / "bin" / "python3"
    assert py3.is_symlink() and py3.resolve() == (full / "bin" / "python3.11")
    assert "Repaired materialized python link" in result.stdout
    assert "Restored missing interpreter link" in result.stdout


def test_healthy_layout_is_untouched(tmp_path):
    python_dir = tmp_path / ".python"
    python_dir.mkdir()
    full = _make_full(python_dir, "15", with_py3_link=True)
    minor = python_dir / f"cpython-3.11-{PLAT}"
    minor.symlink_to(full.name)

    result = _run_repair(tmp_path, python_dir)

    assert result.returncode == 0, result.stderr
    assert minor.is_symlink() and minor.resolve() == full.resolve()
    assert "Repaired" not in result.stdout


def test_picks_newest_full_version(tmp_path):
    python_dir = tmp_path / ".python"
    python_dir.mkdir()
    _make_full(python_dir, "9", with_py3_link=True)
    newest = _make_full(python_dir, "15", with_py3_link=True)
    materialized = python_dir / f"cpython-3.11-{PLAT}"
    materialized.mkdir()

    result = _run_repair(tmp_path, python_dir)

    assert result.returncode == 0, result.stderr
    assert materialized.resolve() == newest.resolve()


def test_materialized_minor_without_full_install_is_left_alone(tmp_path):
    """No full install to point at: leave the copy for uv to replace."""
    python_dir = tmp_path / ".python"
    python_dir.mkdir()
    materialized = python_dir / f"cpython-3.11-{PLAT}"
    materialized.mkdir()

    result = _run_repair(tmp_path, python_dir)

    assert result.returncode == 0, result.stderr
    assert materialized.is_dir() and not materialized.is_symlink()


def test_no_python_dir_is_a_noop(tmp_path):
    result = _run_repair(tmp_path, tmp_path / "absent")
    assert result.returncode == 0, result.stderr


# ── _python_incomplete_install_script ────────────────────────────────


def _run_incomplete(python_dir: Path) -> subprocess.CompletedProcess:
    script = _python_incomplete_install_script().replace(
        "/workspace/.python", str(python_dir))
    return subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, timeout=30, check=False)


def test_install_restored_without_its_interpreter_is_removed(tmp_path):
    """The production failure: the restored install kept its tree but not
    bin/python3.11, and uv refused with "Missing expected Python
    executable" instead of reinstalling."""
    python_dir = tmp_path / ".python"
    python_dir.mkdir()
    full = python_dir / f"cpython-3.11.15-{PLAT}"
    (full / "bin").mkdir(parents=True)
    (full / "lib").mkdir()
    slot = python_dir / f"cpython-3.11-{PLAT}"
    slot.symlink_to(full)

    result = _run_incomplete(python_dir)

    assert result.returncode == 0, result.stderr
    assert not full.exists()
    assert not slot.is_symlink() and not slot.exists()
    assert "Removing incomplete Python install" in result.stdout


def test_an_interpreter_that_will_not_start_is_removed(tmp_path):
    python_dir = tmp_path / ".python"
    python_dir.mkdir()
    full = _make_full(python_dir, "15", with_py3_link=True)
    (full / "bin" / "python3.11").write_text("#!/bin/sh\nexit 1\n")

    result = _run_incomplete(python_dir)

    assert result.returncode == 0, result.stderr
    assert not full.exists()


def test_a_healthy_install_is_untouched(tmp_path):
    python_dir = tmp_path / ".python"
    python_dir.mkdir()
    full = _make_full(python_dir, "15", with_py3_link=True)
    slot = python_dir / f"cpython-3.11-{PLAT}"
    slot.symlink_to(full.name)

    result = _run_incomplete(python_dir)

    assert result.returncode == 0, result.stderr
    assert full.is_dir() and slot.is_symlink()
    assert result.stdout == ""


def test_a_slot_linked_to_another_healthy_patch_is_kept(tmp_path):
    python_dir = tmp_path / ".python"
    python_dir.mkdir()
    broken = python_dir / f"cpython-3.11.9-{PLAT}"
    (broken / "bin").mkdir(parents=True)
    healthy = _make_full(python_dir, "15", with_py3_link=True)
    slot = python_dir / f"cpython-3.11-{PLAT}"
    slot.symlink_to(healthy.name)

    result = _run_incomplete(python_dir)

    assert result.returncode == 0, result.stderr
    assert not broken.exists()
    assert slot.is_symlink() and slot.resolve() == healthy.resolve()


def test_a_materialized_slot_goes_only_when_no_install_of_its_minor_remains(tmp_path):
    python_dir = tmp_path / ".python"
    python_dir.mkdir()
    broken = python_dir / f"cpython-3.11.15-{PLAT}"
    (broken / "bin").mkdir(parents=True)
    materialized = python_dir / f"cpython-3.11-{PLAT}"
    materialized.mkdir()

    result = _run_incomplete(python_dir)

    assert result.returncode == 0, result.stderr
    assert not broken.exists() and not materialized.exists()


def test_non_plain_versions_and_minor_slots_are_ignored(tmp_path):
    python_dir = tmp_path / ".python"
    python_dir.mkdir()
    freethreaded = python_dir / f"cpython-3.13.1+freethreaded-{PLAT}"
    (freethreaded / "bin").mkdir(parents=True)
    slot_dir = python_dir / f"cpython-3.12-{PLAT}"
    slot_dir.mkdir()

    result = _run_incomplete(python_dir)

    assert result.returncode == 0, result.stderr
    assert freethreaded.is_dir() and slot_dir.is_dir()
