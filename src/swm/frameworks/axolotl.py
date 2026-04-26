"""Axolotl — LLM fine-tuning framework."""

from swm.frameworks import Framework, Step, nvidia_ld_exports

_VENV = "/workspace/axolotl/venv"
_PIP = f"{_VENV}/bin/pip"
_PYTHON = f"{_VENV}/bin/python"
_PIP_CACHE = "/workspace/.cache/pip"

FRAMEWORK = Framework(
    name="axolotl",
    label="Axolotl",
    repo="https://github.com/axolotl-ai-cloud/axolotl.git",
    install_dir="/workspace/axolotl",
    launch_cmd=f"{_PYTHON} -m axolotl.cli.train",
    ports={},
    process_pattern="axolotl\\.cli\\.train",
    category="training",
    env_setup=(
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
            command=f"python3 -m venv {_VENV}",
            check=f"[ -x {_PYTHON} ]",
        ),
        Step(
            label="Installing Axolotl",
            command=f"{_PIP} install -e '.[flash-attn,deepspeed]'",
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
            command=f"python3 -m venv {_VENV} && {_PIP} install -e '.[flash-attn,deepspeed]'",
            check=f"[ -x {_PYTHON} ] && {_PYTHON} -c 'import axolotl' 2>/dev/null",
        ),
    ],
)
