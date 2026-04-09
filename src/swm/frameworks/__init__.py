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

    @property
    def launch_workdir(self) -> str:
        return self.install_dir


_FRAMEWORK_MODULES = [
    "comfyui",
    "swarmui",
    "axolotl",
    "llm_studio",
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
