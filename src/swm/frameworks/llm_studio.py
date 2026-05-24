"""H2O LLM Studio — no-code LLM fine-tuning UI."""

from swm.bootstrap import (
    PYTHON_DEFAULT_MINOR,
    UV_ENV_EXPORTS,
    WORKSPACE_UV,
)
from swm.frameworks import Framework, Step, nvidia_ld_exports

_VENV = "/workspace/h2o-llmstudio/venv"
_PYTHON = f"{_VENV}/bin/python"
_PIP_CACHE = "/workspace/.cache/pip"
_TORCH_INDEX = "https://download.pytorch.org/whl/cu126"
_UV_PIP = f"{WORKSPACE_UV} pip install --python {_PYTHON}"

FRAMEWORK = Framework(
    name="llm-studio",
    label="H2O LLM Studio",
    repo="https://github.com/h2oai/h2o-llmstudio.git",
    install_dir="/workspace/h2o-llmstudio",
    venv=_VENV,
    launch_cmd="make llmstudio",
    ports={10101: "http"},
    category="training",
    stop_cmd="pkill -f 'wave.*llmstudio'",
    process_pattern="wave.*llmstudio",
    env_setup=(
        f"{UV_ENV_EXPORTS} && "
        f"export PIP_CACHE_DIR={_PIP_CACHE} && "
        f"{{ [ -f {_VENV}/bin/activate ] && source {_VENV}/bin/activate || true; }} && "
        f"{nvidia_ld_exports(_VENV)}"
    ),
    steps=[
        Step(
            label="Cloning H2O LLM Studio",
            command="git clone --depth 1 https://github.com/h2oai/h2o-llmstudio.git",
            check="[ -d /workspace/h2o-llmstudio ]",
            workdir="/workspace",
        ),
        Step(
            label="Creating virtual environment",
            command=f"{WORKSPACE_UV} venv --python {PYTHON_DEFAULT_MINOR} --seed {_VENV}",
            check=f"[ -x {_PYTHON} ]",
        ),
        Step(
            label="Installing LLM Studio",
            command=f"{_UV_PIP} --extra-index-url {_TORCH_INDEX} -r requirements.txt",
        ),
    ],
    post_install=[
        Step(
            label="Verifying installation",
            command=f"{_PYTHON} -c 'import llm_studio; print(\"LLM Studio OK\")'",
        ),
    ],
    pre_start=[
        Step(
            label="Ensuring Python venv",
            command=(
                f"{WORKSPACE_UV} venv --python {PYTHON_DEFAULT_MINOR} --seed {_VENV} "
                f"&& {_UV_PIP} --extra-index-url {_TORCH_INDEX} -r requirements.txt"
            ),
            check=f"[ -x {_PYTHON} ] && {_PYTHON} -c 'import llm_studio' 2>/dev/null",
        ),
    ],
)
