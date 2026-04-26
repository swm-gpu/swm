"""Framework registry — data-driven installer for GPU workloads."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module


@dataclass
class Step:
    """A single install / setup step executed on the remote pod."""

    label: str
    command: str
    check: str | None = None
    workdir: str | None = None


@dataclass
class Framework:
    """Declarative description of an installable framework."""

    name: str
    label: str
    repo: str
    install_dir: str
    launch_cmd: str
    ports: dict[int, str] = field(default_factory=dict)
    steps: list[Step] = field(default_factory=list)
    post_install: list[Step] = field(default_factory=list)
    pre_start: list[Step] = field(default_factory=list)
    env_setup: str = ""
    stop_cmd: str = ""
    process_pattern: str = ""
    category: str = "inference"
    description: str = ""

    @property
    def launch_workdir(self) -> str:
        return self.install_dir


def nvidia_ld_exports(venv: str) -> str:
    """Return a bash snippet that prepends a venv's bundled NVIDIA libs to LD_LIBRARY_PATH.

    Recent torch wheels pip-install companion packages (``nvidia-cuda-cupti-cu12``,
    ``nvidia-cudnn-cu12``, ``nvidia-cublas-cu12``, …) under
    ``<venv>/lib/python*/site-packages/nvidia/*/lib/`` and expect the dynamic
    linker to pick them up. On many pod images an older system libcupti /
    libcudnn ships in ``/usr/local/cuda/lib64``, gets resolved first, and
    crashes torch with errors like::

        ImportError: ... undefined symbol: cuptiActivityEnableDriverApi,
        version libcupti.so.12

    Prepending the venv-local NVIDIA lib dirs fixes that without touching the
    image. No-op when the venv has no bundled libs.
    """
    return (
        f'NVLIBS=$(ls -d {venv}/lib/python*/site-packages/nvidia/*/lib 2>/dev/null '
        f'| tr "\\n" ":" | sed "s/:$//"); '
        f'if [ -n "$NVLIBS" ]; then export LD_LIBRARY_PATH="$NVLIBS:${{LD_LIBRARY_PATH:-}}"; fi'
    )


_FRAMEWORK_MODULES = [
    "comfyui",
    "swarmui",
    "axolotl",
    "llm_studio",
    "ollama",
    "open_webui",
    "vllm_server",
]

_registry: dict[str, Framework] | None = None


def _load_registry() -> dict[str, Framework]:
    global _registry
    if _registry is not None:
        return _registry
    _registry = {}
    for mod_name in _FRAMEWORK_MODULES:
        mod = import_module(f"swm.frameworks.{mod_name}")
        fw: Framework = mod.FRAMEWORK
        _registry[fw.name] = fw
    return _registry


def get_framework(name: str) -> Framework:
    reg = _load_registry()
    if name not in reg:
        avail = ", ".join(sorted(reg))
        raise KeyError(f"Unknown framework '{name}'. Available: {avail}")
    return reg[name]


def list_frameworks() -> list[Framework]:
    return list(_load_registry().values())
