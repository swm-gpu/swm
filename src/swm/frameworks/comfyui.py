"""ComfyUI framework definition."""

from swm.frameworks import Framework, Step, nvidia_ld_exports

_VENV = "/workspace/ComfyUI/venv"
_PIP = f"{_VENV}/bin/pip"
_PYTHON = f"{_VENV}/bin/python"
_PIP_CACHE = "/workspace/.cache/pip"

_TORCH_CHECK = (
    f"{_PYTHON} -c 'import torch,sys,subprocess,re; "
    "out=subprocess.check_output([\"nvidia-smi\"]).decode(); "
    "m=re.search(r\"CUDA Version: ([0-9]+)\\.([0-9]+)\",out); "
    "dm,dn=(int(m.group(1)),int(m.group(2))) if m else (0,0); "
    "tv=torch.version.cuda or \"\"; "
    "tm,tn=(int(tv.split(\".\")[0]),int(tv.split(\".\")[1])) if tv else (0,0); "
    "sys.exit(0 if tm==dm and tn<=dn else 1)' 2>/dev/null"
)
_PIP_REPAIR = (
    'echo "Repairing pip..."; '
    "curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py && "
    f"{_PYTHON} /tmp/get-pip.py --force-reinstall --no-warn-script-location "
    "pip setuptools wheel >/dev/null"
)
_TORCH_INSTALL = (
    f"if ! {_TORCH_CHECK}; then "
    f"{_PIP_REPAIR} && "
    'CUDA_VER=$(nvidia-smi 2>/dev/null | grep -oE "CUDA Version: [0-9]+\\.[0-9]+" '
    '| head -1 | grep -oE "[0-9]+\\.[0-9]+"); '
    'case "$CUDA_VER" in '
    "13.*|12.9*|12.8*) IDX=cu128 ;; "
    "12.7*|12.6*) IDX=cu126 ;; "
    "12.5*|12.4*) IDX=cu124 ;; "
    "12.3*|12.2*|12.1*) IDX=cu121 ;; "
    "11.*) IDX=cu118 ;; "
    "*) IDX=cu124 ;; "
    "esac; "
    'echo "Installing PyTorch for CUDA $CUDA_VER ($IDX) (force-reinstall)"; '
    f"{_PIP} install --force-reinstall "
    "--index-url https://download.pytorch.org/whl/$IDX "
    "torch torchvision torchaudio; "
    "fi"
)

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
    env_setup=(
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
            command=f"python3 -m venv {_VENV}",
            check=f"[ -x {_PYTHON} ]",
        ),
        Step(
            label="Installing PyTorch matching GPU driver",
            command=_TORCH_INSTALL,
            check=_TORCH_CHECK,
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
            command=(
                f"if ! {_PIP} --version > /dev/null 2>&1; then "
                f"rm -rf {_VENV} && python3 -m venv {_VENV}; "
                f"fi"
            ),
            check=f"{_PIP} --version > /dev/null 2>&1",
        ),
        Step(
            label="Ensuring PyTorch matches GPU driver",
            command=_TORCH_INSTALL,
            check=_TORCH_CHECK,
        ),
        Step(
            label="Updating dependencies",
            command=f"{_PIP} install -r requirements.txt",
        ),
    ],
)
