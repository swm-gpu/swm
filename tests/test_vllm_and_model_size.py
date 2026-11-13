"""Tests for the vLLM launcher script and HF repo disk-usage calculation."""
from __future__ import annotations

import subprocess
from pathlib import Path

from swm.commands.models import _disk_usage
from swm.frameworks import vllm_server


def test_vllm_launcher_forwards_extra_args_and_disables_flashinfer_sampler_by_default(tmp_path: Path):
    launcher = tmp_path / "start.sh"
    fake_venv = tmp_path / "venv" / "bin"
    fake_venv.mkdir(parents=True)
    fake_vllm = fake_venv / "vllm"
    fake_vllm.write_text(
        "#!/bin/bash\n"
        "printf 'VLLM_USE_FLASHINFER_SAMPLER=%s\\n' \"$VLLM_USE_FLASHINFER_SAMPLER\"\n"
        "printf 'ARGS:%s\\n' \"$*\"\n"
    )
    fake_vllm.chmod(0o755)
    model_file = tmp_path / "model.txt"
    model_file.write_text("Qwen/Qwen3.8-27B-FP8\n")

    write_cmd = (
        vllm_server._WRITE_LAUNCHER
        .replace(vllm_server._LAUNCHER, str(launcher))
        .replace(vllm_server._VENV, str(tmp_path / "venv"))
        .replace(vllm_server._MODEL_FILE, str(model_file))
    )
    check_cmd = vllm_server._LAUNCHER_CHECK.replace(vllm_server._LAUNCHER, str(launcher))

    # A legacy start.sh without "$@" fails the check so pre_start rewrites it.
    launcher.write_text("#!/bin/bash\nexec vllm serve m --max-model-len 8192\n")
    launcher.chmod(0o755)
    assert subprocess.run(["bash", "-c", check_cmd], check=False).returncode != 0

    assert subprocess.run(["bash", "-c", write_cmd], check=False).returncode == 0
    assert subprocess.run(["bash", "-c", check_cmd], check=False).returncode == 0

    proc = subprocess.run(
        ["bash", str(launcher), "--max-model-len", "32768", "--max-num-seqs", "64"],
        capture_output=True, text=True, check=True,
    )
    assert "VLLM_USE_FLASHINFER_SAMPLER=0" in proc.stdout
    assert "--max-model-len 8192 --max-model-len 32768 --max-num-seqs 64" in proc.stdout


def test_disk_usage_dereferences_symlinks_for_hf_shared_blob_cache():
    cmds: list[str] = []

    class FakeSession:
        def exec(self, cmd: str, stream: bool = False):
            cmds.append(cmd)
            return 0, "30890081053\n", ""

    size = _disk_usage(FakeSession(), "/workspace/models/hf/hub/models--Qwen--Qwen3.8-27B-FP8")
    assert size == 30_890_081_053
    assert "du -sbL" in cmds[0]
