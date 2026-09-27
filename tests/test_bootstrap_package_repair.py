"""Regression tests for the venv package repair in repair_venv (bootstrap).

A restore from storage written before 0.3.5 can hold a package's
``*.dist-info`` while the package's code is gone: uv installs by renaming a
temporary directory into place, and that rename never reached storage. These
tests build a real venv with the test interpreter, plant distributions in each
failure shape, and run the scanner and the full repair script locally with a
fake ``uv`` that records what it was asked to reinstall.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from swm.bootstrap import WORKSPACE_UV, _package_scan_py, _venv_package_repair_script


@pytest.fixture
def venv(tmp_path: Path) -> Path:
    path = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(path)],
                   check=True, timeout=120)
    return path


def _site_packages(venv: Path) -> Path:
    out = subprocess.run(
        [str(venv / "bin" / "python"), "-c",
         "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        capture_output=True, text=True, check=True, timeout=30)
    return Path(out.stdout.strip())


def _dist(site: Path, name: str, version: str, files: dict[str, bool], *,
          metadata: bool = True, direct_url: dict | None = None) -> None:
    """A dist-info whose RECORD lists *files*; True ones exist on disk."""
    info = site / f"{name}-{version}.dist-info"
    info.mkdir()
    if metadata:
        (info / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n")
    if direct_url is not None:
        (info / "direct_url.json").write_text(json.dumps(direct_url))
    rows = []
    for rel, present in files.items():
        if present:
            target = site / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x")
        rows.append(f"{rel},sha256=abc,1")
    rows.append(f"{info.name}/RECORD,,")
    (info / "RECORD").write_text("\n".join(rows) + "\n")


def _scan(venv: Path) -> set[str]:
    out = subprocess.run([str(venv / "bin" / "python"), "-I", "-"],
                         input=_package_scan_py(), capture_output=True,
                         text=True, check=True, timeout=60)
    return set(filter(None, out.stdout.splitlines()))


def test_complete_packages_and_sync_excluded_files_are_not_broken(venv):
    site = _site_packages(venv)
    _dist(site, "ok", "1.0", {
        "ok/__init__.py": True,
        # Never stored by the workspace sync, so a restore always lacks them.
        "ok/__pycache__/__init__.cpython-311.pyc": False,
        "ok/build.log": False,
        "ok/.cache/blob": False,
    })
    assert _scan(venv) == set()


def test_each_failure_shape_is_reported_with_its_reinstall_route(venv):
    site = _site_packages(venv)
    _dist(site, "gone", "2.0", {"gone/__init__.py": False})
    _dist(site, "pydantic_settings", "2.15.0",
          {"pydantic_settings/__init__.py": True}, metadata=False)
    _dist(site, "torch", "2.14.0+cu130", {"torch/__init__.py": False})
    _dist(site, "gitpkg", "0.1", {"gitpkg/__init__.py": False},
          direct_url={"url": "https://github.com/o/r.git",
                      "vcs_info": {"vcs": "git", "commit_id": "abc123"}})
    _dist(site, "editable", "0.1", {"editable/__init__.py": False},
          direct_url={"url": "file:///src/editable", "dir_info": {"editable": True}})
    _dist(site, "localwheel", "1.0", {"localwheel/__init__.py": False},
          direct_url={"url": "file:///tmp/localwheel-1.0-py3-none-any.whl",
                      "archive_info": {}})

    assert _scan(venv) == {
        "PLAIN gone==2.0",
        "PLAIN pydantic_settings==2.15.0",
        "LOCAL torch==2.14.0+cu130",
        "PLAIN gitpkg @ git+https://github.com/o/r.git@abc123",
        "SKIP editable==0.1",
        "SKIP localwheel==1.0",
    }


def _fake_uv(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "uv.log"
    fake = tmp_path / "fake-uv"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "ARGS $*" >> "{log}"\n'
        'prev=""\n'
        'for a in "$@"; do\n'
        f'  if [ "$prev" = "-r" ]; then cat "$a" >> "{log}"; fi\n'
        '  prev="$a"\n'
        "done\n")
    fake.chmod(0o755)
    return fake, log


def _run_repair(venv: Path, fake_uv: Path) -> subprocess.CompletedProcess:
    script = _venv_package_repair_script(str(venv)).replace(WORKSPACE_UV, str(fake_uv))
    return subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, timeout=60, check=False)


def test_repair_reinstalls_exactly_the_broken_packages_without_deps(tmp_path, venv):
    site = _site_packages(venv)
    _dist(site, "ok", "1.0", {"ok/__init__.py": True})
    _dist(site, "aiohttp", "3.14.3", {"aiohttp/__init__.py": False})
    _dist(site, "torch", "2.14.0+cu130", {"torch/__init__.py": False})
    _dist(site, "editable", "0.1", {"editable/__init__.py": False},
          direct_url={"url": "file:///src/editable", "dir_info": {"editable": True}})
    fake, log = _fake_uv(tmp_path)

    result = _run_repair(venv, fake)

    assert result.returncode == 0, result.stderr
    calls = log.read_text()
    python = f"{venv}/bin/python"
    assert f"ARGS pip install --python {python} --reinstall --no-deps -r " in calls
    assert "aiohttp==3.14.3" in calls
    assert "ok==" not in calls
    assert (f"ARGS pip install --python {python} --reinstall --no-deps "
            "--index-url https://download.pytorch.org/whl/cu130 torch==2.14.0+cu130") in calls
    assert "editable" not in calls
    assert "reinstall it by hand: editable==0.1" in result.stdout


def test_repair_is_a_noop_on_a_complete_venv(tmp_path, venv):
    _dist(_site_packages(venv), "ok", "1.0", {"ok/__init__.py": True})
    fake, log = _fake_uv(tmp_path)

    result = _run_repair(venv, fake)

    assert result.returncode == 0, result.stderr
    assert "are complete" in result.stdout
    assert not log.exists()


def test_repair_skips_a_missing_venv(tmp_path):
    fake, log = _fake_uv(tmp_path)
    result = _run_repair(tmp_path / "absent", fake)
    assert result.returncode == 0, result.stderr
    assert not log.exists()


def test_a_failed_reinstall_fails_the_step(tmp_path, venv):
    """A broken package that cannot be reinstalled must stop the framework
    start loudly, not let it launch into an import error."""
    _dist(_site_packages(venv), "aiohttp", "3.14.3", {"aiohttp/__init__.py": False})
    failing = tmp_path / "failing-uv"
    failing.write_text("#!/bin/sh\nexit 2\n")
    failing.chmod(0o755)

    result = _run_repair(venv, failing)

    assert result.returncode != 0
