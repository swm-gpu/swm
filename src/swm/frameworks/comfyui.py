"""ComfyUI framework definition."""

from swm.bootstrap import (
    PYTHON_DEFAULT_MINOR,
    UV_ENV_EXPORTS,
    WORKSPACE_UV,
)
from swm.frameworks import Framework, Step, nvidia_ld_exports

_COMFY_MODEL_DIRS = [
    "checkpoints", "loras", "vae", "controlnet", "embeddings",
    "clip", "clip_vision", "upscale_models", "unet",
    "diffusion_models", "text_encoders",
]


def _link_script() -> str:
    """Build the bash that points ComfyUI's per-type dirs at the unified store.

    Preserves anything already sitting under ``/workspace/ComfyUI/models/<type>``
    by moving it into ``/workspace/models/<type>`` before replacing with a
    symlink.  Idempotent: re-running is a no-op once the symlinks exist.
    """
    parts = [
        "mkdir -p /workspace/models/{" + ",".join(_COMFY_MODEL_DIRS) + "}",
        "mkdir -p /workspace/ComfyUI/models",
    ]
    for d in _COMFY_MODEL_DIRS:
        target = f"/workspace/ComfyUI/models/{d}"
        store = f"/workspace/models/{d}"
        parts.append(
            f"if [ -L {target} ]; then :; "
            f"elif [ -d {target} ]; then "
            f"  ( shopt -s dotglob nullglob; mv {target}/* {store}/ 2>/dev/null || true ); "
            f"  rmdir {target} 2>/dev/null || rm -rf {target}; "
            f"  ln -s {store} {target}; "
            f"else "
            f"  ln -s {store} {target}; "
            f"fi"
        )
    return " && ".join(parts)


_LINK_COMFYUI = _link_script()

_VENV = "/workspace/ComfyUI/venv"
_PYTHON = f"{_VENV}/bin/python"
_PIP_CACHE = "/workspace/.cache/pip"

# All package operations go through uv against the venv's Python.  We
# never use the venv's bundled pip directly — uv resolves + installs
# 10-100x faster and avoids any get-pip bootstrap dance.
_UV_PIP = f"{WORKSPACE_UV} pip install --python {_PYTHON}"

# A CUDA op is the only check that catches every mismatch class at once:
# missing kernels for this GPU's architecture (e.g. a cu124 build on
# sm_100 Blackwell), a torch runtime newer than the driver, or a wedged
# install.  Any failure exits non-zero and triggers _TORCH_INSTALL.
_TORCH_CHECK = (
    f"{_PYTHON} -c 'import torch; "
    "torch.zeros(1,device=\"cuda\").add(1); torch.cuda.synchronize()' "
    "2>/dev/null"
)

# GPU-aware wheel-index selection.  Detection queries NVML via ctypes
# (the approach used by WheelNext's nvidia-variant-provider) and falls
# back to the kernel-provided /proc file — never the nvidia-smi CLI,
# which marketplace hosts sometimes replace with broken wrapper scripts.
# Selection honours both constraints that decide whether a wheel runs:
#   * the driver's max supported CUDA bounds how new the index may be;
#   * the GPU architecture bounds it below — PyTorch dropped pre-Turing
#     (< sm_75) from cu128+ wheels, so those cards stay on the cu126
#     legacy tier (kept through torch 2.14).
_CUDA_IDX_SNIPPET = """\
import ctypes, re
drv = cc = None
try:
    l = ctypes.CDLL("libnvidia-ml.so.1")
    if getattr(l, "nvmlInit_v2", l.nvmlInit)() == 0:
        try:
            v = ctypes.c_int(0)
            get_ver = getattr(l, "nvmlSystemGetCudaDriverVersion_v2", l.nvmlSystemGetCudaDriverVersion)
            if get_ver(ctypes.byref(v)) == 0 and v.value > 0:
                drv = (v.value // 1000, v.value % 1000 // 10)
            h = ctypes.c_void_p()
            get_h = getattr(l, "nvmlDeviceGetHandleByIndex_v2", l.nvmlDeviceGetHandleByIndex)
            ma, mi = ctypes.c_int(0), ctypes.c_int(0)
            if get_h(0, ctypes.byref(h)) == 0 and l.nvmlDeviceGetCudaComputeCapability(h, ctypes.byref(ma), ctypes.byref(mi)) == 0:
                cc = (ma.value, mi.value)
        finally:
            l.nvmlShutdown()
except Exception:
    pass
if drv is None:
    try:
        m = re.search(r"Module\\s+(\\d+)\\.", open("/proc/driver/nvidia/version").read())
        if m:
            d = int(m.group(1))
            drv = (13, 0) if d >= 580 else (12, 8) if d >= 570 else (12, 6) if d >= 560 else (12, 4) if d >= 550 else (12, 1) if d >= 530 else (11, 8)
    except Exception:
        pass
pre_turing = cc is not None and cc < (7, 5)
if drv is None:
    idx = "cu128"
elif not pre_turing and drv >= (13, 0):
    idx = "cu130"
elif not pre_turing and drv >= (12, 8):
    idx = "cu128"
elif drv >= (12, 6):
    idx = "cu126"
elif drv >= (12, 4):
    idx = "cu124"
elif drv >= (12, 1):
    idx = "cu121"
else:
    idx = "cu118"
print(idx)
"""

_TORCH_INSTALL = (
    f"if ! {_TORCH_CHECK}; then "
    f"IDX=$({_PYTHON} -c '{_CUDA_IDX_SNIPPET}' 2>/dev/null); "
    'IDX="${IDX:-cu128}"; '
    'echo "Installing PyTorch wheels ($IDX) (force-reinstall)"; '
    f"{_UV_PIP} --force-reinstall "
    "--index-url https://download.pytorch.org/whl/$IDX "
    "torch torchvision torchaudio; "
    "fi"
)

FRAMEWORK = Framework(
    name="comfyui",
    label="ComfyUI",
    repo="https://github.com/comfyanonymous/ComfyUI.git",
    install_dir="/workspace/ComfyUI",
    venv=_VENV,
    launch_cmd=f"{_PYTHON} main.py --listen 0.0.0.0 --port 8188",
    ports={8188: "http"},
    category="inference",
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
)
