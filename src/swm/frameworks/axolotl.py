"""Axolotl — LLM fine-tuning framework."""

from swm.bootstrap import (
    PYTHON_DEFAULT_MINOR,
    UV_ENV_EXPORTS,
    WORKSPACE_UV,
)
from swm.frameworks import Framework, Step, nvidia_ld_exports

_VENV = "/workspace/axolotl/venv"
_PYTHON = f"{_VENV}/bin/python"
_PIP_CACHE = "/workspace/.cache/pip"
_UV_PIP = f"{WORKSPACE_UV} pip install --python {_PYTHON}"

FRAMEWORK = Framework(
    name="axolotl",
    label="Axolotl",
    repo="https://github.com/axolotl-ai-cloud/axolotl.git",
    install_dir="/workspace/axolotl",
    venv=_VENV,
    launch_cmd=f"{_PYTHON} -m axolotl.cli.train",
    ports={},
    process_pattern="axolotl\\.cli\\.train",
    category="training",
    env_setup=(
        f"{UV_ENV_EXPORTS} && "
        f"export PIP_CACHE_DIR={_PIP_CACHE} && "
        f"source {_VENV}/bin/activate && "
        f"{nvidia_ld_exports(_VENV)}"
    ),
    steps=[
        Step(
            label="Cloning Axolotl",
            command="git clone --depth 1 https://github.com/axolotl-ai-cloud/axolotl.git",
            check="[ -d /workspace/axolotl ]",
            workdir="/workspace",
        ),
        Step(
            label="Creating virtual environment",
            command=f"{WORKSPACE_UV} venv --python {PYTHON_DEFAULT_MINOR} --seed {_VENV}",
            check=f"[ -x {_PYTHON} ]",
        ),
        Step(
            label="Installing Axolotl",
            command=f"{_UV_PIP} -e '.[flash-attn,deepspeed]'",
        ),
    ],
    post_install=[
        Step(
            label="Verifying installation",
            command=f"{_PYTHON} -c 'import axolotl; print(f\"axolotl {{axolotl.__version__}}\")'",
        ),
    ],
    pre_start=[
        Step(
            label="Ensuring Python venv exists",
            command=(
                f"{WORKSPACE_UV} venv --python {PYTHON_DEFAULT_MINOR} --seed {_VENV} "
                f"&& {_UV_PIP} -e '.[flash-attn,deepspeed]'"
            ),
            check=f"[ -x {_PYTHON} ] && {_PYTHON} -c 'import axolotl' 2>/dev/null",
        ),
    ],
)
