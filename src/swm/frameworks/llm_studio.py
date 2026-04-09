"""H2O LLM Studio — no-code LLM fine-tuning UI."""

from swm.frameworks import Framework, Step

_VENV = "/workspace/h2o-llmstudio/venv"
_PIP = f"{_VENV}/bin/pip"
_PYTHON = f"{_VENV}/bin/python"
_PIP_CACHE = "/workspace/.cache/pip"
_TORCH_INDEX = "https://download.pytorch.org/whl/cu126"

FRAMEWORK = Framework(
    name="llm-studio",
    label="H2O LLM Studio",
    repo="https://github.com/h2oai/h2o-llmstudio.git",
    install_dir="/workspace/h2o-llmstudio",
    launch_cmd="make llmstudio",
    ports={10101: "http"},
    category="training",
    stop_cmd="pkill -f 'wave.*llmstudio'",
    process_pattern="wave.*llmstudio",
    env_setup=f"export PIP_CACHE_DIR={_PIP_CACHE} && [ -f {_VENV}/bin/activate ] && source {_VENV}/bin/activate || true",
    steps=[
        Step(
            label="Cloning H2O LLM Studio",
            command="git clone --depth 1 https://github.com/h2oai/h2o-llmstudio.git",
            check="[ -d /workspace/h2o-llmstudio ]",
            workdir="/workspace",
        ),
        Step(
            label="Creating virtual environment",
            command=f"python -m venv {_VENV}",
            check=f"[ -x {_PYTHON} ]",
        ),
        Step(
            label="Installing LLM Studio",
            command=f"source {_VENV}/bin/activate && {_PIP} install --extra-index-url {_TORCH_INDEX} -r requirements.txt && {_PIP} install uv",
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
            label="Ensuring Python venv and uv",
            command=f"python -m venv {_VENV} && {_PIP} install --extra-index-url {_TORCH_INDEX} -r requirements.txt && {_PIP} install uv",
            check=f"[ -x {_VENV}/bin/uv ]",
        ),
    ],
)
