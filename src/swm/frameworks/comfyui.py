"""ComfyUI framework definition."""

from swm.frameworks import Framework, Step

_VENV = "/workspace/ComfyUI/venv"
_PIP = f"{_VENV}/bin/pip"
_PYTHON = f"{_VENV}/bin/python"
_PIP_CACHE = "/workspace/.cache/pip"

FRAMEWORK = Framework(
    name="comfyui",
    label="ComfyUI",
    repo="https://github.com/comfyanonymous/ComfyUI.git",
    install_dir="/workspace/ComfyUI",
    launch_cmd=f"{_PYTHON} main.py --listen 0.0.0.0 --port 8188",
    ports={8188: "http"},
    category="inference",
    stop_cmd="pkill -f 'python main.py.*--port 8188'",
    process_pattern="python main.py.*--listen",
    env_setup=f"export PIP_CACHE_DIR={_PIP_CACHE} && source {_VENV}/bin/activate",
    steps=[
        Step(
            label="Cloning ComfyUI",
            command="git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git",
            check="[ -d /workspace/ComfyUI ]",
            workdir="/workspace",
        ),
        Step(
            label="Creating virtual environment",
            command=f"python -m venv {_VENV}",
            check=f"[ -x {_PYTHON} ]",
        ),
        Step(
            label="Installing Python requirements",
            command=f"{_PIP} install -r requirements.txt",
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
            label="Creating model directories",
            command=(
                "mkdir -p /workspace/ComfyUI/models/"
                "{checkpoints,loras,vae,controlnet,clip,upscale_models,unet,"
                "diffusion_models,text_encoders,clip_vision}"
            ),
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
            command=f"python -m venv {_VENV}",
            check=f"[ -x {_PYTHON} ]",
        ),
        Step(
            label="Updating dependencies",
            command=f"{_PIP} install -r requirements.txt",
        ),
    ],
)
