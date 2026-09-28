"""ComfyUI and SwarmUI move to the PyTorch build each pod calls for.

A workspace carried a cu126 torch from a V100 (the only build that runs
there) onto newer GPUs. It still ran a CUDA op on an RTX A5000 with a 580
driver, so it was kept, and ComfyUI 0.37 then ran with its comfy-kitchen
kernels off: they need the CUDA 13 build on any GPU that can run one. These
tests run the generated shell against a fake python and uv.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from swm.frameworks import get_framework
from swm.frameworks._gpu import torch_install, torch_matches

PINS = "torch==2.14.0 torchvision==0.29.0 torchaudio==2.11.0"

# Answers the three snippets the shell runs: driver/GPU detection (prints the
# index), the installed build (from the state file; empty = CUDA op fails),
# and the installed pins.
_FAKE_PY = f"""#!/bin/bash
code="$2"
build=$(cat "$FAKE_STATE" 2>/dev/null)
case "$code" in
  *nvmlInit*) echo "$FAKE_IDX" ;;
  *importlib.metadata*) echo "{PINS}" ;;
  *torch.version*) [ -n "$build" ] || exit 1; echo "$build" ;;
  *torch.zeros*) [ -n "$build" ] && [ "$build" != broken ] ;;
  *) exit 2 ;;
esac
"""

# Logs each call; an install from a PyTorch index becomes the installed build.
_FAKE_UV = """#!/bin/bash
echo "$*" >> "$FAKE_UV_LOG"
for a in "$@"; do
  case "$a" in https://download.pytorch.org/whl/*) echo "${a##*/}" > "$FAKE_STATE" ;; esac
done
"""


@pytest.fixture
def pod(tmp_path, monkeypatch):
    for name, body in (("python", _FAKE_PY), ("uv", _FAKE_UV)):
        path = tmp_path / name
        path.write_text(body)
        path.chmod(0o755)
    monkeypatch.setenv("FAKE_STATE", str(tmp_path / "build"))
    monkeypatch.setenv("FAKE_UV_LOG", str(tmp_path / "uv.log"))

    def run(script: str, *, installed: str, index: str) -> tuple[int, str, list[str]]:
        (tmp_path / "build").write_text(installed)
        (tmp_path / "uv.log").write_text("")
        done = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              env={**os.environ, "FAKE_IDX": index}, check=False)
        calls = (tmp_path / "uv.log").read_text().splitlines()
        return done.returncode, done.stdout + done.stderr, calls

    run.python = str(tmp_path / "python")
    run.uv_pip = f"{tmp_path / 'uv'} pip install --python {tmp_path / 'python'}"
    run.state = tmp_path / "build"
    return run


def _best(pod) -> str:
    return torch_install(pod.python, pod.uv_pip, keep_version=False, best_build=True)


def test_a_working_but_older_build_moves_to_the_pods_index(pod):
    code, out, calls = pod(_best(pod), installed="cu126", index="cu130")
    assert code == 0, out
    assert calls == [f"pip install --python {pod.python} --reinstall "
                     f"--index-url https://download.pytorch.org/whl/cu130 {PINS}"]
    assert pod.state.read_text().strip() == "cu130"


@pytest.mark.parametrize(("installed", "index"), [
    ("cu130", "cu130"),   # already the right build
    ("cu126", "cu126"),   # a V100: cu126 is the best it can run
    ("rocm", "cu128"),    # an AMD build is never swapped for a CUDA one
])
def test_the_right_build_is_left_alone(pod, installed, index):
    code, out, calls = pod(_best(pod), installed=installed, index=index)
    assert code == 0, out
    assert calls == []
    assert "PyTorch can use this GPU" in out


def test_a_build_that_cannot_use_the_gpu_is_reinstalled(pod):
    code, out, calls = pod(_best(pod), installed="", index="cu128")
    assert code == 0, out
    assert len(calls) == 1 and "/whl/cu128 " in calls[0]


def test_exact_version_frameworks_still_keep_a_working_build(pod):
    script = torch_install(pod.python, pod.uv_pip, keep_version=True)
    code, out, calls = pod(script, installed="cu126", index="cu130")
    assert code == 0, out
    assert calls == []


@pytest.mark.parametrize(("installed", "index", "expected"), [
    ("cu130", "cu130", 0), ("cu126", "cu130", 1), ("cu128", "cu130", 1),
    ("rocm", "cu130", 0), ("", "cu130", 1),
])
def test_torch_matches(pod, installed, index, expected):
    assert pod(torch_matches(pod.python), installed=installed, index=index)[0] == expected


@pytest.mark.parametrize("name", ["comfyui", "swarmui"])
def test_comfyui_and_swarmui_check_for_the_best_build(name):
    fw = get_framework(name)
    torch_steps = [s for s in (*fw.steps, *fw.pre_start) if "PyTorch" in s.label]
    assert torch_steps
    for step in torch_steps:
        assert "torch.version" in step.check and "nvmlInit" in step.check
        assert "torch.version" in step.command
