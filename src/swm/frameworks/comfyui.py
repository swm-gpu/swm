"""ComfyUI framework definition."""

from swm.bootstrap import (
    PYTHON_DEFAULT_MINOR,
    UV_ENV_EXPORTS,
    WORKSPACE_UV,
)
from swm.frameworks import Framework, Step, nvidia_ld_exports
from swm.frameworks._gpu import torch_install, torch_matches
from swm.frameworks._model_store import (
    DIFFUSION_BUCKETS,
    DIFFUSION_CONSUMES,
    link_store_script,
)

_LINK_COMFYUI = link_store_script("/workspace/ComfyUI/models", DIFFUSION_BUCKETS)

_VENV = "/workspace/ComfyUI/venv"
_PYTHON = f"{_VENV}/bin/python"
_PIP_CACHE = "/workspace/.cache/pip"

# All package operations go through uv against the venv's Python.  We
# never use the venv's bundled pip directly — uv resolves + installs
# 10-100x faster and avoids any get-pip bootstrap dance.
_UV_PIP = f"{WORKSPACE_UV} pip install --python {_PYTHON}"

_TORCH_CHECK = torch_matches(_PYTHON)
_TORCH_INSTALL = torch_install(_PYTHON, _UV_PIP, keep_version=False, best_build=True)

FRAMEWORK = Framework(
    name="comfyui",
    label="ComfyUI",
    repo="https://github.com/comfyanonymous/ComfyUI.git",
    install_dir="/workspace/ComfyUI",
    venv=_VENV,
    # Custom nodes import at startup; a large set takes minutes.
    ready_timeout=600,
    gpu_torch="flexible",
    launch_cmd=f"{_PYTHON} main.py --listen 0.0.0.0 --port 8188",
    ports={8188: "http"},
    category="inference",
    consumes=DIFFUSION_CONSUMES,
    stop_cmd="pkill -f 'python main.py.*--port 8188'",
    process_pattern="python main.py.*--listen",
    env_setup=(
        f"{UV_ENV_EXPORTS} && "
        f"export PIP_CACHE_DIR={_PIP_CACHE} && "
        f"{{ [ -f {_VENV}/bin/activate ] && source {_VENV}/bin/activate || true; }} && "
        f"{nvidia_ld_exports(_VENV)}"
    ),
    steps=[
        Step(
            label="Cloning ComfyUI",
            command="git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git",
            check="[ -d /workspace/ComfyUI ]",
            workdir="/workspace",
        ),
        Step(
            label="Creating virtual environment",
            command=f"{WORKSPACE_UV} venv --python {PYTHON_DEFAULT_MINOR} --seed {_VENV}",
            check=f"[ -x {_PYTHON} ]",
        ),
        Step(
            label="Installing PyTorch matching GPU driver",
            command=_TORCH_INSTALL,
            check=_TORCH_CHECK,
        ),
        Step(
            label="Installing Python requirements",
            command=f"{_UV_PIP} -r requirements.txt",
        ),
    ],
    post_install=[
        Step(
            label="Installing ComfyUI Manager",
            command="git clone --depth 1 https://github.com/ltdrdata/ComfyUI-Manager.git",
            check="[ -d /workspace/ComfyUI/custom_nodes/ComfyUI-Manager ]",
            workdir="/workspace/ComfyUI/custom_nodes",
        ),
        Step(
            label="Linking model directories to unified store",
            command=_LINK_COMFYUI,
            check="[ -L /workspace/ComfyUI/models/checkpoints ]",
        ),
    ],
    pre_start=[
        Step(
            label="Redirecting pip cache to /workspace",
            command=(
                f"mkdir -p {_PIP_CACHE} /root/.cache "
                "&& if [ -d /root/.cache/pip ] && [ ! -L /root/.cache/pip ]; then "
                "rm -rf /root/.cache/pip; fi "
                f"&& ln -sfn {_PIP_CACHE} /root/.cache/pip"
            ),
            check="[ -L /root/.cache/pip ]",
        ),
        Step(
            label="Ensuring Python venv exists",
            # uv-managed venvs don't need a get-pip dance — uv handles
            # everything externally.  If the venv is missing the user
            # should re-run `swm setup install comfyui` for a clean
            # rebuild against workspace-owned Python.
            command=(
                f"if [ ! -x {_PYTHON} ]; then "
                f"  echo 'venv missing - re-run: swm setup install comfyui' "
                f"  && exit 1; "
                f"fi"
            ),
            check=f"[ -x {_PYTHON} ]",
        ),
        Step(
            label="Ensuring PyTorch matches GPU driver",
            command=_TORCH_INSTALL,
            check=_TORCH_CHECK,
        ),
        Step(
            label="Updating dependencies",
            command=f"{_UV_PIP} -r requirements.txt",
        ),
        Step(
            label="Ensuring model directory symlinks",
            command=_LINK_COMFYUI,
            check="[ -L /workspace/ComfyUI/models/checkpoints ]",
        ),
    ],
    repair=[
        Step(
            label="Installing custom-node requirements",
            # Best effort per node: one node's unsatisfiable pin must not
            # block the rest of the repair.
            command=(
                "for r in /workspace/ComfyUI/custom_nodes/*/requirements.txt; do "
                '[ -f "$r" ] || continue; echo "  $r"; '
                f'{_UV_PIP} -r "$r" || echo "  warning: could not install $r"; '
                "done"
            ),
        ),
    ],
)
