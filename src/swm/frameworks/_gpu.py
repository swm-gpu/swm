"""PyTorch builds matched to the pod's GPU, shared by every GPU framework.

A workspace moves between pods: a venv built on a CUDA 13 driver and a
Blackwell card has to run on a CUDA 12.4 driver and an A100 next week. The
check below is the only one that catches every mismatch class at once
(kernels missing for this GPU's architecture, a torch runtime newer than the
driver, a wedged install): it runs a real CUDA op. When it fails, the torch
stack is reinstalled from the PyTorch index this pod can actually use.
"""

from __future__ import annotations

PYTORCH_INDEX = "https://download.pytorch.org/whl"

# GPU-aware wheel-index selection.  Detection queries NVML via ctypes
# (the approach used by WheelNext's nvidia-variant-provider) and falls
# back to the kernel-provided /proc file — never the nvidia-smi CLI,
# which marketplace hosts sometimes replace with broken wrapper scripts.
# Selection honours both constraints that decide whether a wheel runs:
#   * the driver's max supported CUDA bounds how new the index may be;
#   * the GPU architecture bounds it below — PyTorch dropped pre-Turing
#     (< sm_75) from cu128+ wheels, so those cards stay on the cu126
#     legacy tier (kept through torch 2.14).
# Runs under python -c '...', so it must never contain a single quote.
CUDA_INDEX_SNIPPET = """\
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

# Installed torch-stack pins with the local tag stripped ("torch==2.14.0"),
# so the same versions can be fetched as a different CUDA build.
_PINS_SNIPPET = """\
import importlib.metadata as m
pins = []
for name in ("torch", "torchvision", "torchaudio"):
    try:
        pins.append(name + "==" + m.version(name).split("+")[0])
    except m.PackageNotFoundError:
        pass
print(" ".join(pins))
"""


def cuda_index(python: str) -> str:
    """Shell expression: the PyTorch index tag (cu118 … cu130) for this pod."""
    return f'$({python} -c \'{CUDA_INDEX_SNIPPET}\' 2>/dev/null || echo cu128)'


def torch_check(python: str) -> str:
    """Shell that exits 0 only when torch can run a CUDA op on this GPU."""
    return (
        f"{python} -c 'import torch; "
        "torch.zeros(1,device=\"cuda\").add(1); torch.cuda.synchronize()' "
        "2>/dev/null"
    )


def torch_install(python: str, uv_pip: str, *, keep_version: bool,
                  force: bool = False) -> str:
    """Shell that makes the torch stack usable on this pod's GPU.

    Unless *force*, does nothing when the CUDA op already works. Otherwise
    reinstalls the installed torch/torchvision/torchaudio versions as the
    build for this pod's driver and GPU. When those versions have no such
    build, *keep_version* frameworks (vLLM, Axolotl, LLM Studio pin torch
    exactly and break on a different one) fail so the environment is rebuilt;
    the rest take the newest build on that index. With nothing installed yet
    it installs the newest build. Exits non-zero, saying why, when torch still
    cannot use the GPU afterwards (a driver too old for any supported build).
    """
    check = torch_check(python)
    fallback = (
        'echo "  no $PINS build on $IDX; this framework needs that exact '
        'version, so its environment has to be rebuilt"; exit 1'
        if keep_version else
        f"{uv_pip} --reinstall --index-url {PYTORCH_INDEX}/$IDX "
        "torch torchvision torchaudio"
    )
    guard = "false" if force else check
    return (
        f"if {guard}; then echo '  PyTorch can use this GPU'; else "
        f"IDX={cuda_index(python)}; "
        f"PINS=$({python} -c '{_PINS_SNIPPET}' 2>/dev/null); "
        'if [ -z "$PINS" ]; then '
        '  echo "  installing the newest PyTorch build for this GPU ($IDX)"; '
        f"  {uv_pip} --index-url {PYTORCH_INDEX}/$IDX torch torchvision torchaudio || exit 1; "
        "else "
        '  echo "  reinstalling $PINS as the $IDX build for this GPU"; '
        f"  {uv_pip} --reinstall --index-url {PYTORCH_INDEX}/$IDX $PINS "
        f"    || {{ {fallback} || exit 1; }}; "
        "fi; "
        f"{check} || {{ echo \"  PyTorch still cannot use this GPU with the $IDX build; "
        "the GPU driver may be too old for any supported PyTorch\"; exit 1; }; "
        "fi"
    )
